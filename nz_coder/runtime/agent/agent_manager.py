"""Session-owned background orchestration for isolated write-capable subagents."""
from __future__ import annotations

import hashlib
import multiprocessing
import threading
import time
import weakref
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from pathlib import Path
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.runtime.agent.child_result import (
    CHILD_RESULT_KEY,
    ChildAgentResult,
    child_result_from_state,
)
from nz_coder.runtime.agent.child_contracts import presentation_excerpt
from nz_coder.protocol.public_error import (
    PublicError,
    public_error_from_wire,
    to_public_error,
)
from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.tools import ToolOutput, dispatch, register


_TERMINAL_STATUSES = frozenset({
    "completed", "cancelled", "timeout", "error", "max_turns",
    "tool_error_rolled_back", "verification_failed_rolled_back", "interrupted",
    "verification_failed", "completed_unverified", "applied",
})
_LIVE_STATUSES = frozenset({"queued", "running", "cancel_requested"})
_STOP_SETTLE_SECONDS = 2.0
_MANAGER: ContextVar[BackgroundAgentManager | None] = ContextVar(
    "nz_coder_background_agent_manager",
    default=None,
)
_MESSAGE_ROUTE: ContextVar[tuple[object, str] | None] = ContextVar(
    "nz_coder_agent_message_route",
    default=None,
)
_INSTANCE_LOCK = threading.Lock()
_INSTANCES: dict[tuple[Path, str], BackgroundAgentManager] = {}
_CHILD_PROCESS_RESULT_SCHEMA = "nz.child_result.v1"


@dataclass
class _LiveJob:
    cancel_event: threading.Event
    done_event: threading.Event
    thread: threading.Thread
    process: object | None = None
    capability_lease_id: str = ""


@dataclass
class _ManagedWorkflowRun:
    run_id: str
    name: str
    status: str
    started_at: float
    condition: threading.Condition
    ended_at: float | None = None
    error: str = ""


def _run_subagent_process(connection, payload: dict) -> None:
    """Run one child behind a spawn boundary and return a bounded result."""
    try:
        from nz_coder.runtime.agent.subagent import run_subagent, scoped_parent_context
        from nz_coder.runtime.process.workdir import scoped_workdir
        from nz_coder.foundation.workspace_trust import scoped_config_snapshot

        snapshot = payload.pop("config_snapshot", None)
        with (
            scoped_workdir(payload["workspace"]),
            scoped_config_snapshot(snapshot) if snapshot is not None else nullcontext(),
            scoped_parent_context(
                session_id=payload["parent_session_id"],
                config_snapshot=snapshot,
            ),
        ):
            result = run_subagent(
                payload["prompt"],
                agent_type=payload["agent_type"],
                session_id=payload["session_id"],
                allowed_tools=payload.get("allowed_tools"),
                target_paths=payload.get("target_paths"),
                output_schema=payload.get("output_schema"),
                model_hint=payload.get("model_hint"),
                evidence_refs=payload.get("evidence_refs"),
                verification=payload.get("verification"),
                cancel_event=threading.Event(),
            )
        connection.send({
            "schema": _CHILD_PROCESS_RESULT_SCHEMA,
            "ok": True,
            "result": str(result)[:2_000_000],
        })
    except BaseException as exc:
        connection.send({
            "schema": _CHILD_PROCESS_RESULT_SCHEMA,
            "ok": False,
            "public_error": to_public_error(exc).to_dict(),
        })
    finally:
        connection.close()


def _decode_child_process_envelope(
    envelope: object,
) -> tuple[str, PublicError | None]:
    """Validate the spawn wire contract and fail closed on malformed data."""
    invalid = PublicError(
        "child_process_protocol_error",
        "The child process returned an invalid result.",
    )
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != _CHILD_PROCESS_RESULT_SCHEMA
        or not isinstance(envelope.get("ok"), bool)
    ):
        return invalid.message, invalid
    if envelope["ok"] is True:
        result = envelope.get("result")
        if not isinstance(result, str):
            return invalid.message, invalid
        return result[:2_000_000], None
    public = public_error_from_wire(envelope.get("public_error"))
    if public is None:
        return invalid.message, invalid
    return public.message, public


def _digest(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _within_scope(path: str, scopes: list[str]) -> bool:
    candidate = Path(path).parts
    return any(candidate[: len(Path(scope).parts)] == Path(scope).parts for scope in scopes)


def _outcome_from_background_state(state: dict) -> ChildAgentResult:
    canonical = state.get(CHILD_RESULT_KEY)
    if isinstance(canonical, dict):
        return ChildAgentResult.from_dict(canonical)
    verification = state.get("verification_result")
    if not isinstance(verification, dict):
        verification = str(state.get("verification") or "")
    return child_result_from_state(
        state,
        final_text=str(state.get("background_result") or ""),
        status=str(state.get("status") or "unknown"),
        verification=verification,
    )


class BackgroundAgentManager:
    """Own background child threads and persistent lifecycle state for one Session."""

    def __init__(self, workspace: Path, parent_session_id: str):
        self.workspace = workspace.resolve()
        self.parent_session_id = str(parent_session_id or "main-session")
        self._lock = threading.RLock()
        self._close_condition = threading.Condition(self._lock)
        self._closing = False
        self._closed = False
        self._jobs: dict[str, _LiveJob] = {}
        self._message_sequence = 0
        self._mailboxes: dict[str, list[dict]] = {}
        self._event_bus = None
        self._event_publisher = None
        self._task_publishers: dict[str, object] = {}
        self._lineage = None
        self._managed_runs: dict[str, _ManagedWorkflowRun] = {}
        settings = current_run_settings()
        self._agent_cap = max(
            1,
            min(settings.subagent_background_max_tasks, 20),
        )
        self._concurrency_cap = max(
            1,
            min(
                settings.subagent_background_max_concurrent,
                self._agent_cap,
            ),
        )
        self._active_jobs = 0
        self._capacity_condition = threading.Condition(self._lock)
        from nz_coder.runtime.agent.subagent import _subagent_root

        process_root = (
            _subagent_root(self.parent_session_id, self.workspace).parent
            / "workflow"
        )
        self._workflow = WorkflowProcessStore(
            process_root,
            self.parent_session_id,
            agent_cap=self._agent_cap,
            concurrency_cap=self._concurrency_cap,
            on_event=self._bridge_workflow_event,
        )
        self._restore_workflow_run_identities()
        self._reconcile_interrupted()
        self._workflow.reconcile([
            state for state in self._states() if state.get("background")
        ])
        from nz_coder.runtime.workflows.workflow_sweep import sweep_workflow_worktrees

        stale_sweep = sweep_workflow_worktrees(
            self._states(),
            self.workspace,
            older_than_seconds=6 * 60 * 60,
        )
        if stale_sweep["removed"] or stale_sweep["warnings"]:
            self.record_workflow_event(
                "worktree_sweep_completed",
                data={"scope": "stale-startup", **stale_sweep},
            )

    def _restore_workflow_run_identities(self) -> None:
        """Rehydrate terminal runs and fail closed on process-orphaned active runs."""
        restored = self._workflow.workflow_run_lifecycles()
        now = time.time()
        orphaned: list[str] = []
        for item in restored:
            status = str(item.get("status") or "failed")
            if status in {"running", "paused"}:
                status = "failed"
                orphaned.append(str(item["run_id"]))
            run = _ManagedWorkflowRun(
                run_id=str(item["run_id"]),
                name=str(item.get("name") or "workflow")[:200],
                status=status,
                started_at=float(item.get("started_at") or now),
                condition=threading.Condition(threading.RLock()),
                ended_at=(
                    float(item.get("ended_at") or now)
                    if status in {"completed", "failed", "stopped"}
                    else None
                ),
                error=(
                    "workflow interrupted by process restart"
                    if str(item.get("run_id")) in orphaned
                    else str(item.get("error") or "")[:4000]
                ),
            )
            self._managed_runs[run.run_id] = run
        for run_id in orphaned:
            self._workflow.record_event(
                "workflow_run_failed",
                data={
                    "run_id": run_id,
                    "error": "workflow interrupted by process restart",
                    "recovered": True,
                },
            )
        self._prune_terminal_runs_locked()

    def bind_event_bus(self, event_bus) -> None:
        """Attach the owning Session's already-existing live event bus."""
        self._event_bus = event_bus

    def bind_event_publisher(self, publisher) -> None:
        """Attach the immutable publisher for newly-created background work."""
        self._event_publisher = publisher

    def _remember_task_publisher(self, task_id: str, publisher) -> None:
        """Freeze one child task's event identity for its entire lifetime."""
        task = str(task_id or "").strip()
        if not task or publisher is None:
            return
        with self._lock:
            self._task_publishers.setdefault(task, publisher)

    def bind_lineage(self, lineage) -> None:
        """Attach the owning Session's append-only outcome journal."""
        self._lineage = lineage

    def record_workflow_outcome(self, outcome: dict) -> dict | None:
        """Persist one bounded, idempotent workflow digest for later review."""
        lineage = self._lineage
        if lineage is None:
            return None
        run_id = str(outcome.get("run_id") or "").strip()
        if not run_id:
            raise ValueError("workflow outcome requires run_id")
        result = outcome.get("result")
        result_digest = {}
        if isinstance(result, dict):
            result_digest = {
                key: result.get(key)
                for key in ("task_id", "status", "digest", "verification")
                if result.get(key) not in (None, "")
            }
            sidecar = result.get("sidecar_verification")
            if isinstance(sidecar, dict):
                result_digest["verifier_verdict"] = sidecar.get("verdict")
        snapshot = outcome.get("workflow_snapshot")
        task_ids = []
        if isinstance(snapshot, dict):
            task_ids = [
                str(item.get("task_id") or "")
                for item in snapshot.get("items", [])
                if isinstance(item, dict) and item.get("task_id")
            ][:20]
        payload = {
            "run_id": run_id,
            "status": str(outcome.get("status") or "unknown")[:100],
            "phase_names": list((outcome.get("outputs") or {}).keys())[:32],
            "task_ids": task_ids,
            "replayed_agents": int(outcome.get("replayed_agents") or 0),
            "budget": dict(outcome.get("budget") or {}),
            "result": result_digest,
            **(
                {"capsule_ref": {
                    key: outcome["capsule_ref"].get(key)
                    for key in ("name", "source", "execution")
                    if outcome["capsule_ref"].get(key) is not None
                }}
                if isinstance(outcome.get("capsule_ref"), dict) else {}
            ),
        }
        entry = lineage.append_unique(
            "memory_outcome_digest",
            f"workflow:{run_id}",
            payload,
        )
        if entry is not None:
            self.record_workflow_event(
                "memory_outcome_recorded",
                data={"run_id": run_id, "lineage_id": entry["id"]},
            )
        return entry

    def _bridge_workflow_event(self, event: dict, snapshot: dict) -> None:
        task_id = str(event.get("task_id") or "")
        with self._lock:
            bus = self._task_publishers.get(task_id) or self._event_publisher
        if bus is None:
            from nz_coder.protocol.session_events import current_session_event_bus

            bus = current_session_event_bus()
        if bus is None:
            bus = self._event_bus
        if bus is None:
            return
        bus.publish(
            f"workflow.{str(event.get('type') or 'updated').replace('_', '.')}",
            {
                "workflow_event": event,
                "workflow_snapshot": snapshot,
            },
        )
        event_type = str(event.get("type") or "")
        child_event = {
            "task_started": "session.child.started",
            "task_terminal": "session.child.finished",
        }.get(event_type)
        if child_event:
            bus.publish(child_event, {
                "session_id": str(event.get("task_id") or ""),
                "workflow_event": event,
                "workflow_snapshot": snapshot,
            })

    @property
    def agent_cap(self) -> int:
        return self._run_limits()[0]

    @property
    def concurrency_cap(self) -> int:
        return self._run_limits()[1]

    def _run_limits(self, snapshot=None) -> tuple[int, int]:
        """Resolve child budgets from the active parent epoch when present."""
        if snapshot is None:
            from nz_coder.foundation.workspace_trust import active_config_snapshot

            snapshot = active_config_snapshot(self.workspace)
        agent_cap = (
            snapshot.get_int(
                "SUBAGENT_BACKGROUND_MAX_TASKS", self._agent_cap,
                minimum=1, maximum=20,
            )
            if snapshot is not None else self._agent_cap
        )
        configured_concurrency = (
            snapshot.get_int(
                "SUBAGENT_BACKGROUND_MAX_CONCURRENT", self._concurrency_cap,
                minimum=1, maximum=20,
            )
            if snapshot is not None else self._concurrency_cap
        )
        concurrency_cap = min(configured_concurrency, agent_cap)
        return agent_cap, concurrency_cap

    def spawned_count(self) -> int:
        return sum(1 for state in self._states() if state.get("background"))

    def record_workflow_event(
        self,
        event_type: str,
        *,
        data: dict | None = None,
        task_id: str = "",
    ) -> dict:
        return self._workflow.record_event(
            event_type,
            data=data,
            task_id=task_id,
        )

    def begin_workflow_run(self, run_id: str, name: str) -> None:
        """Register one eagerly-started managed workflow lifecycle."""
        with self._lock:
            if run_id in self._managed_runs:
                raise ValueError(f"duplicate workflow run: {run_id}")
            self._managed_runs[run_id] = _ManagedWorkflowRun(
                run_id=run_id,
                name=str(name or "workflow")[:200],
                status="running",
                started_at=time.time(),
                condition=threading.Condition(threading.RLock()),
            )
            self._prune_terminal_runs_locked()

    def _prune_terminal_runs_locked(self) -> None:
        """Retain every active run and only the newest 500 terminal runs."""
        terminal = sorted(
            (
                item for item in self._managed_runs.values()
                if item.status in {"completed", "failed", "stopped"}
            ),
            key=lambda item: item.ended_at or 0,
        )
        for stale in terminal[:-500]:
            self._managed_runs.pop(stale.run_id, None)

    def workflow_run_snapshots(self) -> list[dict]:
        """Return newest-first defensive managed-run snapshots."""
        with self._lock:
            runs = sorted(
                self._managed_runs.values(),
                key=lambda item: item.started_at,
                reverse=True,
            )
            return [{
                "run_id": item.run_id,
                "name": item.name,
                "status": item.status,
                "started_at": item.started_at,
                **({"ended_at": item.ended_at} if item.ended_at is not None else {}),
                **({"error": item.error} if item.error else {}),
            } for item in runs]

    def wait_workflow_spawn_gate(
        self,
        run_id: str,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Wait while paused and reject new spawns after stop."""
        with self._lock:
            run = self._managed_runs.get(run_id)
        if run is None:
            raise ValueError(f"unknown workflow run: {run_id}")
        with run.condition:
            while run.status == "paused":
                if cancel_event is not None and cancel_event.is_set():
                    return False
                run.condition.wait(0.05)
            return run.status == "running"

    def pause_workflow_run(self, run_id: str) -> bool:
        with self._lock:
            run = self._managed_runs.get(run_id)
        if run is None:
            return False
        with run.condition:
            if run.status != "running":
                return False
            run.status = "paused"
        self.record_workflow_event("workflow_run_paused", data={"run_id": run_id})
        return True

    def resume_workflow_run(self, run_id: str) -> bool:
        with self._lock:
            run = self._managed_runs.get(run_id)
        if run is None:
            return False
        with run.condition:
            if run.status != "paused":
                return False
            run.status = "running"
            run.condition.notify_all()
        self.record_workflow_event("workflow_run_resumed", data={"run_id": run_id})
        return True

    def stop_workflow_run(self, run_id: str, reason: str = "workflow stopped") -> bool:
        with self._lock:
            run = self._managed_runs.get(run_id)
        if run is None:
            return False
        with run.condition:
            if run.status in {"completed", "failed", "stopped"}:
                return False
            run.status = "stopped"
            run.error = str(reason)[:1000]
            run.ended_at = time.time()
            run.condition.notify_all()
        active = [
            str(state.get("session_id") or "")
            for state in self._states()
            if state.get("workflow_run_id") == run_id
            and state.get("status") in _LIVE_STATUSES
        ]
        if active:
            self._request_stop(active, reason=reason)
        with self._lock:
            self._prune_terminal_runs_locked()
        return True

    def workflow_run_stopped(self, run_id: str) -> bool:
        with self._lock:
            run = self._managed_runs.get(run_id)
            return run is not None and run.status == "stopped"

    def finish_workflow_run(self, run_id: str, status: str, error: str = "") -> None:
        """Settle unless an explicit stop already owns the terminal status."""
        with self._lock:
            run = self._managed_runs.get(run_id)
        if run is None:
            return
        with run.condition:
            if run.status != "stopped":
                run.status = (
                    status
                    if status in {"completed", "failed", "stopped"}
                    else "failed"
                )
                run.error = str(error)[:4000]
                run.ended_at = time.time()
            run.condition.notify_all()
        with self._lock:
            self._prune_terminal_runs_locked()

    def send_message(
        self,
        *,
        sender: str,
        recipient: str,
        content: str,
        seen_by: list[str] | None = None,
    ) -> str:
        """Route one bounded in-flight message between owned Agent tasks."""
        sender_id = str(sender or "").strip()
        target = str(recipient or "").strip()
        body = str(content or "").strip()
        sender_state = self._load_raw(sender_id) if sender_id != "worker" else None
        if sender_id != "worker" and not sender_state:
            return "Error: sender must be an owned child session"
        if sender_state and sender_state.get("status") not in _LIVE_STATUSES:
            return "Error: sender child session is not live"
        if not target:
            return "Error: recipient is required"
        if not body:
            return "Error: content is required"
        if len(body) > 4000:
            return "Error: content exceeds 4000 characters"

        states = self._states()
        states_by_id: dict[str, dict] = {}
        aliases: dict[str, str] = {}
        for state in states:
            session_id = str(state.get("session_id") or "")
            if not session_id:
                continue
            states_by_id[session_id] = state
            aliases[session_id] = session_id
            display_name = str(state.get("display_name") or "").strip()
            if display_name and display_name not in aliases:
                aliases[display_name] = session_id

        if target == "*":
            recipients = [
                str(state.get("session_id") or "")
                for state in states
                if str(state.get("session_id") or "") != sender_id
                and state.get("status") in _LIVE_STATUSES
            ]
            if sender_id != "worker":
                recipients.append("worker")
        elif target in {"parent", "worker"}:
            if sender_id == "worker":
                return "Error: the Worker cannot message itself"
            recipients = ["worker"]
        else:
            resolved = aliases.get(target)
            if not resolved:
                return f"Error: unknown peer recipient '{target}'"
            if resolved == sender_id:
                return "Error: an Agent cannot message itself"
            if states_by_id[resolved].get("status") not in _LIVE_STATUSES:
                return "Error: recipient child session is not live"
            recipients = [resolved]

        recipients = list(dict.fromkeys(item for item in recipients if item))
        if sender_id == "worker" and any(
            str(states_by_id.get(item, {}).get("isolation") or "thread") == "process"
            for item in recipients
        ):
            return "Error: online steering is unavailable for process-isolated children"
        if len(recipients) > 20:
            return "Error: broadcast exceeds 20 recipients"
        chain = [str(item).strip() for item in (seen_by or []) if str(item).strip()]
        chain = list(dict.fromkeys(chain))
        if sender_id in chain:
            return "Error: forwarding cycle detected for sender"
        if len(chain) >= 8:
            return "Error: forwarding chain exceeds 8 Agents"
        chain.append(sender_id)
        cycle_targets = [item for item in recipients if item in chain]
        if cycle_targets:
            return "Error: forwarding cycle detected for recipient"

        with self._lock:
            self._message_sequence += 1
            message_id = f"peer-{self._message_sequence:06d}"
            message = {
                "id": message_id,
                "sender": sender_id,
                "content": body,
                "seen_by": chain,
                "kind": (
                    "coordinator_instruction"
                    if sender_id == "worker"
                    else "peer_message"
                ),
                "created_at": time.time(),
            }
            for item in recipients:
                mailbox = self._mailboxes.setdefault(item, [])
                mailbox.append(dict(message, recipient=item))
                if len(mailbox) > 200:
                    del mailbox[:-200]
        return f"Message {message_id} delivered to {', '.join(recipients)}."

    def drain_messages(self, recipient: str) -> list[dict]:
        """Atomically consume pending in-flight messages for one Agent."""
        target = str(recipient or "").strip()
        if not target:
            return []
        with self._lock:
            messages = self._mailboxes.pop(target, [])
        return [dict(item) for item in messages]

    def _enqueue_worker_wake(
        self,
        *,
        sender: str,
        content: str,
        kind: str,
        status: str = "",
    ) -> None:
        """Persist one bounded parent wake without depending on child timing."""
        with self._lock:
            self._message_sequence += 1
            message_id = f"peer-{self._message_sequence:06d}"
            mailbox = self._mailboxes.setdefault("worker", [])
            mailbox.append({
                "id": message_id,
                "sender": str(sender or "unknown"),
                "recipient": "worker",
                "content": str(content or "")[:4000],
                "kind": str(kind or "peer_message"),
                "status": str(status or "")[:120],
                "seen_by": [str(sender or "unknown")],
                "created_at": time.time(),
            })
            if len(mailbox) > 200:
                del mailbox[:-200]

    def has_worker_wake_source(self) -> bool:
        """Return whether parent idle-yield has mail or a live child to await."""
        with self._lock:
            if self._mailboxes.get("worker"):
                return True
            jobs = list(self._jobs.items())
        for session_id, live in jobs:
            if live.done_event.is_set():
                continue
            state = self._load_raw(session_id)
            if state.get("status") in _LIVE_STATUSES:
                return True
        return False

    def _states(self) -> list[dict]:
        from nz_coder.runtime.agent.subagent import _iter_subagent_states

        return _iter_subagent_states(self.parent_session_id, self.workspace)

    def _load_raw(self, session_id: str) -> dict:
        from nz_coder.runtime.agent.subagent import _load_subagent_state

        return _load_subagent_state(self.parent_session_id, session_id, self.workspace)

    def _load(self, session_id: str) -> dict:
        """Hide a child terminal state until the manager publishes its result."""
        state = self._load_raw(session_id)
        with self._lock:
            live = self._jobs.get(session_id)
        if (
            live is not None
            and not live.done_event.is_set()
            and state.get("status") in _TERMINAL_STATUSES
            and "background_result" not in state
        ):
            state = dict(state)
            state["status"] = (
                "cancel_requested" if live.cancel_event.is_set() else "running"
            )
        return state

    def _save(self, state: dict) -> None:
        from nz_coder.runtime.agent.subagent import _save_subagent_state

        _save_subagent_state(self.parent_session_id, state, self.workspace)

    def _reconcile_interrupted(self) -> None:
        for state in self._states():
            if not state.get("background") or state.get("status") not in _LIVE_STATUSES:
                continue
            state["status"] = "interrupted"
            state["interrupted_at"] = time.time()
            self._save(state)

    def _baseline(self, scopes: list[str]) -> dict[str, str | None]:
        manifest: dict[str, str | None] = {}
        for scope in scopes:
            target = (self.workspace / scope).resolve()
            try:
                target.relative_to(self.workspace)
            except ValueError as exc:
                raise ValueError(f"Target path escapes workspace: {scope}") from exc
            if target.is_file():
                manifest[scope] = _digest(target)
                continue
            if not target.exists():
                manifest[scope] = None
                continue
            for candidate in sorted(target.rglob("*")):
                if not candidate.is_file() or candidate.is_symlink():
                    continue
                relative = candidate.relative_to(self.workspace)
                if any(part in {".git", ".nz-coder", "node_modules", ".venv", "venv"} for part in relative.parts):
                    continue
                manifest[relative.as_posix()] = _digest(candidate)
                if len(manifest) > 20000:
                    raise ValueError("Target scopes contain more than 20000 files")
        return manifest

    def start(self, tasks: list[dict]) -> str:
        """Atomically admit one ordered fan-out under lifetime/live caps."""
        from nz_coder.runtime.agent.subagent import (
            _active_scope_conflicts,
            _new_subagent_state,
            _normalize_scope_paths,
            _overlapping_paths,
        )

        from nz_coder.protocol.session_events import current_session_event_publisher
        from nz_coder.foundation.workspace_trust import active_config_snapshot

        origin_publisher = current_session_event_publisher() or self._event_publisher
        run_snapshot = active_config_snapshot(self.workspace)
        agent_cap, concurrency_cap = self._run_limits(run_snapshot)
        worktree_enabled = (
            run_snapshot.get_bool("SUBAGENT_WORKTREE_ENABLED", True)
            if run_snapshot is not None else current_run_settings().subagent_worktree_enabled
        )
        process_isolation_enabled = (
            run_snapshot.get_bool("NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED", True)
            if run_snapshot is not None
            else current_run_settings().subagent_process_isolation_enabled
        )
        process_stop_grace = (
            run_snapshot.get_float(
                "NZ_SUBAGENT_PROCESS_STOP_GRACE_SECONDS", 0.5,
                minimum=0.0, maximum=30.0,
            )
            if run_snapshot is not None
            else current_run_settings().subagent_process_stop_grace
        )
        with self._lock:
            if self._closing or self._closed:
                return "Error: background Agent manager is closed"
        if not worktree_enabled:
            return "Error: background write agents require SUBAGENT_WORKTREE_ENABLED=1"
        if not isinstance(tasks, list) or not tasks:
            return "Error: tasks must be a non-empty list"
        if len(tasks) > agent_cap:
            return f"Error: fan-out exceeds maxAgents lifetime cap ({agent_cap})"

        # Admission, state publication, and job registration share one lock.
        # Concurrent callers therefore cannot both observe the same remaining
        # lifetime capacity or claim overlapping paths.
        with self._lock:
            if self._closing or self._closed:
                return "Error: background Agent manager is closed"
            spawned = sum(
                1 for state in self._states() if state.get("background")
            )
            remaining = agent_cap - spawned
            if len(tasks) > remaining:
                return (
                    "Error: maxAgents lifetime cap "
                    f"({agent_cap}) would be exceeded; "
                    f"spawned={spawned}, requested={len(tasks)}, remaining={max(0, remaining)}"
                )

            fanout_id = f"fanout-{time.time_ns()}"
            prepared: list[tuple[dict, dict]] = []
            for index, item in enumerate(tasks):
                if not isinstance(item, dict):
                    return f"Error: task {index} must be an object"
                prompt = str(item.get("prompt") or "").strip()
                if not prompt:
                    return f"Error: task {index} requires prompt"
                read_only = item.get("read_only", False)
                if not isinstance(read_only, bool):
                    return f"Error: task {index} read_only must be a boolean"
                isolation = str(item.get("isolation") or "thread")
                if isolation not in {"thread", "process"}:
                    return f"Error: task {index} isolation must be thread or process"
                if isolation == "process" and not process_isolation_enabled:
                    return "Error: process isolation is disabled by configuration"
                try:
                    scopes = _normalize_scope_paths(item.get("target_paths"), self.workspace)
                except ValueError as exc:
                    return f"Error: {exc}"
                if not read_only and not scopes:
                    return f"Error: task {index} requires non-empty target_paths for write isolation"
                for sibling, _ in prepared:
                    overlap = _overlapping_paths(scopes, list(sibling.get("claimed_paths") or []))
                    if overlap:
                        return f"Error: task {index} overlaps another requested task: {', '.join(overlap)}"
                conflicts = _active_scope_conflicts(
                    self.parent_session_id,
                    "",
                    scopes,
                    self.workspace,
                )
                if conflicts:
                    ids = ", ".join(str(item.get("session_id") or "-") for item in conflicts)
                    return f"Error: task {index} target_paths conflict with active child sessions: {ids}"
                agent_type = "explore" if read_only else "general-purpose"
                state = _new_subagent_state(self.parent_session_id, agent_type, item.get("allowed_tools"))
                state.update({
                    "background": True,
                    "display_name": str(item.get("name") or f"task-{index + 1}").strip(),
                    "claimed_paths": scopes,
                    "status": "queued",
                    "baseline_hashes": self._baseline(scopes),
                    "queued_at": time.time(),
                    "fanout_id": fanout_id,
                    "fanout_index": index,
                    "read_only": read_only,
                    "phase": str(item.get("phase") or "")[:120],
                    "isolation": isolation,
                    "workflow_run_id": str(item.get("workflow_run_id") or "")[:200],
                })
                prepared.append((state, item))

            for state, _ in prepared:
                if origin_publisher is not None:
                    child_publisher = origin_publisher.for_child(
                        str(state["session_id"]),
                    )
                    self._remember_task_publisher(
                        str(state["session_id"]),
                        child_publisher,
                    )
                    state.update({
                        "origin_interaction_run_id": (
                            child_publisher.interaction_run_id
                        ),
                        "agent_invocation_id": child_publisher.agent_invocation_id,
                        "parent_agent_invocation_id": (
                            child_publisher.parent_agent_invocation_id
                        ),
                    })
                self._save(state)
                self._workflow.record_task(
                    "task_queued",
                    state,
                    message=f"queued {state['display_name']}",
                )
            self._workflow.agent_cap = agent_cap
            self._workflow.concurrency_cap = concurrency_cap
            for state, item in prepared:
                self._launch(
                    state,
                    item,
                    run_snapshot=run_snapshot,
                    concurrency_cap=concurrency_cap,
                    process_stop_grace=process_stop_grace,
                )

        lines = [f"Started {len(prepared)} background write subagent(s)."]
        lines.extend(
            f"- {state['session_id']} [{state['display_name']}] scope: {', '.join(state['claimed_paths'])}"
            for state, _ in prepared
        )
        lines.append("Use agent_manager action=status to inspect progress or action=stop to stop tasks.")
        return ToolOutput(
            "\n".join(lines),
            title="Background Agent fan-out",
            metadata={
                "fanout_id": fanout_id,
                "task_ids": [state["session_id"] for state, _ in prepared],
                "max_agents": agent_cap,
                "max_concurrency": concurrency_cap,
                "workflow_snapshot": self._workflow.snapshot(),
            },
        )

    def _launch(
        self,
        state: dict,
        item: dict,
        *,
        run_snapshot=None,
        concurrency_cap: int | None = None,
        process_stop_grace: float | None = None,
    ) -> None:
        from nz_coder.runtime.agent.subagent import run_subagent

        cancel_event = threading.Event()
        done_event = threading.Event()
        session_id = state["session_id"]
        context = copy_context()
        if concurrency_cap is None:
            _agent_cap, concurrency_cap = self._run_limits(run_snapshot)
        if process_stop_grace is None:
            process_stop_grace = current_run_settings().subagent_process_stop_grace
        live_job: _LiveJob | None = None

        def persist_terminal(latest: dict, result: str) -> None:
            """Persist one canonical result even for pre-loop failures/cancel."""
            latest["background_result"] = result
            metadata = getattr(result, "metadata", {})
            canonical = (
                metadata.get(CHILD_RESULT_KEY)
                if isinstance(metadata, dict) else None
            )
            if isinstance(canonical, dict):
                latest[CHILD_RESULT_KEY] = ChildAgentResult.from_dict(
                    canonical
                ).to_dict()
            else:
                latest["digest"], latest["summary_kind"] = presentation_excerpt(
                    str(result)
                )
                latest[CHILD_RESULT_KEY] = child_result_from_state(
                    latest,
                    final_text=str(result),
                    status=str(latest.get("status") or "error"),
                    verification=str(latest.get("verification") or ""),
                ).to_dict()
            latest["finished_at"] = time.time()
            self._save(latest)
            self._workflow.record_task(
                "task_terminal",
                latest,
                message=f"{session_id} settled as {latest.get('status')}",
            )
            self._enqueue_worker_wake(
                sender=session_id,
                content=str(result),
                kind="task_completed",
                status=str(latest.get("status") or "error"),
            )

        def worker() -> None:
            acquired = False
            process_public_error: PublicError | None = None
            try:
                with self._capacity_condition:
                    while self._active_jobs >= concurrency_cap:
                        if cancel_event.is_set():
                            latest = self._load_raw(session_id) or state
                            latest["status"] = "cancelled"
                            persist_terminal(
                                latest,
                                "Cancelled before execution started.",
                            )
                            return
                        self._capacity_condition.wait(0.1)
                    self._active_jobs += 1
                    acquired = True
                latest = self._load_raw(session_id) or state
                latest["status"] = "running"
                latest["run_started_at"] = time.time()
                self._save(latest)
                self._workflow.record_task(
                    "task_started",
                    latest,
                    message=f"started {latest.get('display_name') or session_id}",
                )
                if state.get("isolation") == "process":
                    spawn = multiprocessing.get_context("spawn")
                    parent_connection, child_connection = spawn.Pipe(duplex=False)
                    payload = {
                        "workspace": str(self.workspace),
                        "parent_session_id": self.parent_session_id,
                        "prompt": str(item.get("prompt") or ""),
                        "agent_type": str(state.get("agent_type") or "general-purpose"),
                        "session_id": session_id,
                        "allowed_tools": item.get("allowed_tools"),
                        "target_paths": list(state.get("claimed_paths") or []),
                        "output_schema": item.get("output_schema"),
                        "model_hint": item.get("model_hint"),
                        "evidence_refs": item.get("evidence_refs"),
                        "verification": item.get("verification"),
                        "config_snapshot": run_snapshot,
                    }
                    process = spawn.Process(
                        target=_run_subagent_process,
                        args=(child_connection, payload),
                        name=f"nz-subagent-process-{session_id}",
                        daemon=True,
                    )
                    assert live_job is not None
                    live_job.process = process
                    process.start()
                    child_connection.close()
                    while process.is_alive() and not cancel_event.wait(0.05):
                        pass
                    if cancel_event.is_set() and process.is_alive():
                        process.terminate()
                        process.join(max(0.0, process_stop_grace))
                        if process.is_alive() and hasattr(process, "kill"):
                            process.kill()
                    process.join()
                    if cancel_event.is_set():
                        latest = self._load_raw(session_id) or state
                        latest["status"] = "cancelled"
                        result = "Cancelled; isolated child process terminated."
                    elif parent_connection.poll():
                        envelope = parent_connection.recv()
                        result, process_public_error = (
                            _decode_child_process_envelope(envelope)
                        )
                    else:
                        process_public_error = PublicError(
                            "child_process_exit",
                            "The isolated child process exited unexpectedly.",
                            metadata={"exit_code": process.exitcode},
                        )
                        result = process_public_error.message
                    parent_connection.close()
                else:
                    result = context.run(
                        run_subagent,
                        str(item.get("prompt") or ""),
                        agent_type=str(state.get("agent_type") or "general-purpose"),
                        session_id=session_id,
                        allowed_tools=item.get("allowed_tools"),
                        target_paths=list(state.get("claimed_paths") or []),
                        output_schema=item.get("output_schema"),
                        model_hint=item.get("model_hint"),
                        evidence_refs=item.get("evidence_refs"),
                        verification=item.get("verification"),
                        cancel_event=cancel_event,
                    )
                latest = self._load_raw(session_id) or state
                if state.get("isolation") == "process" and cancel_event.is_set():
                    latest["status"] = "cancelled"
                elif process_public_error is not None:
                    latest["status"] = "error"
                elif latest.get("status") in _LIVE_STATUSES:
                    latest["status"] = "error"
                persist_terminal(latest, result)
            except Exception as exc:
                latest = self._load_raw(session_id) or state
                latest["status"] = "error"
                persist_terminal(latest, to_public_error(exc).message)
            finally:
                # Publish the terminal transition before making execution
                # capacity available.  The event projection can therefore
                # never report more running tasks than the parent epoch admits.
                if acquired:
                    with self._capacity_condition:
                        self._active_jobs -= 1
                        self._capacity_condition.notify_all()
                if live_job is not None and live_job.capability_lease_id:
                    capability_leases().release(live_job.capability_lease_id)
                    live_job.capability_lease_id = ""
                done_event.set()

        thread = threading.Thread(
            target=worker,
            name=f"nz-subagent-{session_id}",
            daemon=True,
        )
        live_job = _LiveJob(cancel_event, done_event, thread)
        manager_ref = weakref.ref(self)

        def revoke_child() -> None:
            manager = manager_ref()
            if manager is not None:
                result = manager.stop(
                    [str(session_id)],
                    reason="workspace trust revoked",
                    timeout_ms=2000,
                )
                metadata = getattr(result, "metadata", {})
                if metadata.get("unsettled_task_ids"):
                    raise RuntimeError("background child did not settle after revocation")

        lease = capability_leases().create(
            kind="workflow-child" if state.get("workflow_run_id") else "background-child",
            resource_id=str(session_id),
            workspace=self.workspace,
            control_fingerprint=(
                run_snapshot.control_fingerprint
                if run_snapshot is not None else "legacy"
            ),
            run_id=str(state.get("workflow_run_id") or session_id),
            interaction_id=str(
                state.get("origin_interaction_run_id") or session_id
            ),
            owner_session=self.parent_session_id,
            revoke=revoke_child,
        )
        live_job.capability_lease_id = lease.lease_id
        with self._lock:
            self._jobs[session_id] = live_job
        thread.start()

    def status(self, session_ids: list[str] | None = None, wait_ms: int = 0) -> str:
        """Return persistent status and optionally wait briefly for one task."""
        ids = [str(item) for item in (session_ids or []) if str(item).strip()]
        if wait_ms and len(ids) == 1:
            with self._lock:
                live = self._jobs.get(ids[0])
            if live is not None:
                live.done_event.wait(max(0, min(int(wait_ms), 10000)) / 1000)
        states = self._states()
        if ids:
            wanted = set(ids)
            states = [state for state in states if state.get("session_id") in wanted]
        else:
            states = [state for state in states if state.get("background")]
        if not states:
            return "No matching background subagent tasks."
        rows = [f"Background subagents: {len(states)}"]
        for state in sorted(states, key=lambda item: float(item.get("created_at") or 0)):
            changed = ", ".join(state.get("changed_files") or []) or "(none)"
            rows.append(
                f"- {state.get('session_id')} [{state.get('status')}] "
                f"{state.get('display_name') or ''}; changed: {changed}"
            )
            result = str(state.get("background_result") or "").strip()
            if result and state.get("status") not in {"queued", "running"}:
                canonical = state.get(CHILD_RESULT_KEY)
                summary = (
                    str(canonical.get("digest") or "").strip()
                    if isinstance(canonical, dict)
                    else ""
                )
                rows.append(
                    "  summary: "
                    + (summary or " ".join(result.split())[:800])
                )
        outcomes: list[dict] = []
        for state in states:
            canonical = state.get(CHILD_RESULT_KEY)
            if isinstance(canonical, dict):
                outcomes.append(ChildAgentResult.from_dict(canonical).to_dict())
            elif state.get("status") in _TERMINAL_STATUSES:
                outcomes.append(child_result_from_state(
                    state,
                    final_text=str(state.get("background_result") or ""),
                    status=str(state.get("status") or "unknown"),
                    verification=str(state.get("verification") or ""),
                ).to_dict())
        workflow_snapshot = self._workflow.reconcile([
            state for state in self._states() if state.get("background")
        ])
        return ToolOutput(
            "\n".join(rows),
            title="Background Agent status",
            metadata={
                "child_results": outcomes,
                "workflow_snapshot": workflow_snapshot,
            },
        )

    def events(self, after_sequence: int = 0) -> str:
        """Return the durable workflow event suffix for replay consumers."""
        events = self._workflow.events(after_sequence)
        snapshot = self._workflow.snapshot()
        return ToolOutput(
            f"Workflow events after revision {max(0, int(after_sequence))}: "
            f"{len(events)}; current revision: {snapshot['revision']}.",
            title="Background workflow events",
            metadata={
                "workflow_events": events,
                "workflow_snapshot": snapshot,
            },
        )

    def wait(self, session_ids: list[str], timeout_ms: int = 0) -> str:
        """Wait for owned tasks with one shared deadline and ordered results."""
        ids = list(dict.fromkeys(
            str(item).strip() for item in session_ids if str(item).strip()
        ))
        if not ids:
            return "Error: session_ids is required for wait"
        timeout = max(0, min(int(timeout_ms), 600_000))
        deadline = (
            time.monotonic() + timeout / 1000
            if timeout > 0 else None
        )
        unknown = [
            session_id for session_id in ids
            if not (self._load_raw(session_id) or {}).get("background")
        ]
        if unknown:
            return f"Error: unknown workflow task(s): {', '.join(unknown)}"

        timed_out: list[str] = []
        for session_id in ids:
            state = self._load(session_id)
            if state.get("status") in _TERMINAL_STATUSES:
                continue
            with self._lock:
                live = self._jobs.get(session_id)
            if live is None:
                continue
            remaining = (
                None if deadline is None else max(0.0, deadline - time.monotonic())
            )
            if remaining == 0 or not live.done_event.wait(remaining):
                timed_out.extend([
                    item for item in ids[ids.index(session_id):]
                    if self._load(item).get("status") not in _TERMINAL_STATUSES
                ])
                break

        if timed_out:
            self._request_stop(timed_out, reason="workflow wait timed out")
            settle_deadline = time.monotonic() + _STOP_SETTLE_SECONDS
            for session_id in timed_out:
                with self._lock:
                    live = self._jobs.get(session_id)
                if live is not None:
                    live.done_event.wait(max(0.0, settle_deadline - time.monotonic()))

        states = [self._load(session_id) for session_id in ids]
        outcomes = [
            _outcome_from_background_state(state).to_dict()
            for state in states
            if state.get("status") in _TERMINAL_STATUSES
        ]
        unsettled = [
            str(state.get("session_id") or "")
            for state in states
            if state.get("status") not in _TERMINAL_STATUSES
        ]
        snapshot = self._workflow.reconcile([
            state for state in self._states() if state.get("background")
        ])
        return ToolOutput(
            (
                f"Waited for {len(ids)} workflow task(s); "
                f"settled={len(outcomes)}, timed_out={len(timed_out)}, "
                f"unsettled={len(unsettled)}."
            ),
            title="Background workflow wait",
            metadata={
                "child_results": outcomes,
                "timed_out_task_ids": timed_out,
                "unsettled_task_ids": unsettled,
                "workflow_snapshot": snapshot,
            },
        )

    def wait_until_settled(
        self,
        session_id: str,
        cancel_event: threading.Event | None = None,
        *,
        poll_seconds: float = 0.05,
    ) -> bool:
        """Wait without inventing timeout semantics; return false on caller abort."""
        state = self._load_raw(session_id)
        if not state or not state.get("background"):
            raise ValueError(f"unknown workflow task: {session_id}")
        interval = max(0.01, min(float(poll_seconds), 0.5))
        while self._load(session_id).get("status") not in _TERMINAL_STATUSES:
            if cancel_event is not None and cancel_event.is_set():
                return False
            with self._lock:
                live = self._jobs.get(session_id)
            if live is None:
                return self._load(session_id).get("status") in _TERMINAL_STATUSES
            live.done_event.wait(interval)
        return True

    def stop(
        self,
        session_ids: list[str],
        *,
        reason: str = "stopped by parent",
        timeout_ms: int = 2000,
    ) -> str:
        """Idempotently stop tasks and wait through one shared settle deadline."""
        ids = list(dict.fromkeys(
            str(item).strip() for item in session_ids if str(item).strip()
        ))
        if not ids:
            return "Error: session_ids is required for stop"
        unknown = [
            session_id for session_id in ids
            if not (self._load_raw(session_id) or {}).get("background")
        ]
        if unknown:
            return f"Error: unknown workflow task(s): {', '.join(unknown)}"
        requested = self._request_stop(ids, reason=reason)
        deadline = time.monotonic() + max(0, min(int(timeout_ms), 30_000)) / 1000
        for session_id in ids:
            with self._lock:
                live = self._jobs.get(session_id)
            if live is not None and not live.done_event.is_set():
                live.done_event.wait(max(0.0, deadline - time.monotonic()))
        states = [self._load(session_id) for session_id in ids]
        unsettled = [
            str(state.get("session_id") or "")
            for state in states
            if state.get("status") not in _TERMINAL_STATUSES
        ]
        return ToolOutput(
            f"Stop results: requested={len(requested)}, unsettled={len(unsettled)}.",
            title="Background workflow stop",
            metadata={
                "requested_task_ids": requested,
                "unsettled_task_ids": unsettled,
                "child_results": [
                    _outcome_from_background_state(state).to_dict()
                    for state in states
                    if state.get("status") in _TERMINAL_STATUSES
                ],
                "workflow_snapshot": self._workflow.reconcile([
                    state for state in self._states() if state.get("background")
                ]),
            },
        )

    def _request_stop(self, session_ids: list[str], *, reason: str) -> list[str]:
        requested: list[str] = []
        for session_id in session_ids:
            with self._lock:
                state = self._load(session_id)
                if not state or state.get("status") in _TERMINAL_STATUSES:
                    continue
                if state.get("status") != "cancel_requested":
                    state["status"] = "cancel_requested"
                    state["cancel_requested_at"] = time.time()
                    state["stop_reason"] = str(reason)[:1000]
                    self._save(state)
                    self._workflow.record_task(
                        "task_cancel_requested",
                        state,
                        message=f"stop requested for {session_id}: {reason}"[:1000],
                    )
                    requested.append(session_id)
                live = self._jobs.get(session_id)
                if live is not None:
                    live.cancel_event.set()
        return requested

    def cancel(self, session_ids: list[str]) -> str:
        """Request cooperative cancellation for live tasks."""
        if not session_ids:
            return "Error: session_ids is required for cancel"
        result = self.stop(
            session_ids,
            reason="cancelled by parent",
            timeout_ms=0,
        )
        if isinstance(result, ToolOutput):
            return ToolOutput(
                "cancellation requested. " + str(result),
                title=result.title,
                metadata=result.metadata,
            )
        return result

    def close(self, timeout: float = 5.0) -> None:
        """Cancel and settle every process-local child owned by this Session."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            while self._closing and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError(
                        "background Agent manager close did not settle before deadline"
                    )
                self._close_condition.wait(remaining)
            if self._closed:
                return
            self._closing = True
            jobs = list(self._jobs.items())
        try:
            self._request_stop(
                [
                    session_id
                    for session_id, live in jobs
                    if not live.done_event.is_set()
                ],
                reason="parent Session closed",
            )
            for session_id, live in jobs:
                live.done_event.wait(max(0.0, deadline - time.monotonic()))
                if not live.done_event.is_set():
                    raise RuntimeError(
                        f"background subagent '{session_id}' did not settle "
                        "before Session deletion"
                    )
        except BaseException:
            with self._lock:
                self._closing = False
                self._close_condition.notify_all()
            raise
        with self._lock:
            self._jobs.clear()
            self._closed = True
            self._closing = False
            self._close_condition.notify_all()

    def application_changes(self, session_id: str, reviewed_files: list[str]) -> tuple[list[dict], list[dict], str]:
        """Build guarded writes/deletes after parent review and baseline validation."""
        state = self._load(session_id)
        if not state or not state.get("background"):
            return [], [], f"Error: Unknown background subagent session '{session_id}'"
        if state.get("status") != "completed":
            return [], [], f"Error: subagent must be completed before apply; status={state.get('status')}"
        changed = sorted(dict.fromkeys(str(item) for item in state.get("changed_files") or []))
        reviewed = sorted(dict.fromkeys(str(item) for item in reviewed_files or []))
        if changed != reviewed:
            return [], [], "Error: reviewed_files must exactly match the child changed_files"
        if not changed:
            return [], [], "Error: child produced no changed files"
        if len(changed) > 50:
            return [], [], "Error: child changed more than 50 files; apply manually"
        scopes = list(state.get("claimed_paths") or [])
        outside = [path for path in changed if not _within_scope(path, scopes)]
        if outside:
            return [], [], f"Error: child changed files outside claimed scope: {', '.join(outside)}"

        from nz_coder.runtime.agent.subagent import _validated_persisted_worktree

        try:
            worktree = _validated_persisted_worktree(self.workspace, state)
        except ValueError as exc:
            return [], [], f"Error: invalid child worktree ownership: {exc}"
        if worktree is None or worktree.mode not in {"git", "copy"}:
            return [], [], "Error: direct-mode child changes cannot be safely applied"
        child_root = Path(worktree.path).resolve()
        baseline = dict(state.get("baseline_hashes") or {})
        writes: list[dict] = []
        deletes: list[dict] = []
        conflicts: list[str] = []
        for relative in changed:
            parent_path = (self.workspace / relative).resolve()
            child_path = (child_root / relative).resolve()
            try:
                parent_path.relative_to(self.workspace)
                child_path.relative_to(child_root)
            except ValueError:
                conflicts.append(relative)
                continue
            if parent_path.is_symlink() or child_path.is_symlink():
                return [], [], f"Error: symbolic-link changes cannot be applied: {relative}"
            child_hash = _digest(child_path)
            parent_hash = _digest(parent_path)
            if child_hash == parent_hash:
                continue
            if parent_hash != baseline.get(relative):
                conflicts.append(relative)
                continue
            if child_path.is_file() and not child_path.is_symlink():
                try:
                    content = child_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return [], [], f"Error: binary or unreadable child file cannot be applied: {relative}"
                writes.append({"path": relative, "content": content})
            elif parent_path.is_file():
                deletes.append({"op": "delete", "path": relative})
            else:
                conflicts.append(relative)
        if conflicts:
            return [], [], "Error: parent changed since child snapshot or path is unsafe: " + ", ".join(conflicts)
        if not writes and not deletes:
            return [], [], "Already applied: parent files match the child result"
        return writes, deletes, ""


def background_agent_manager(workspace: Path, parent_session_id: str) -> BackgroundAgentManager:
    """Return the process-local owner for one workspace/Session identity."""
    key = (workspace.resolve(), str(parent_session_id or "main-session"))
    with _INSTANCE_LOCK:
        manager = _INSTANCES.get(key)
        if manager is None:
            manager = BackgroundAgentManager(*key)
            _INSTANCES[key] = manager
        return manager


def dispose_background_agent_manager(
    workspace: Path,
    parent_session_id: str,
    *,
    timeout: float = 5.0,
    manager: BackgroundAgentManager | None = None,
) -> None:
    """Dispose one cached Session manager without constructing a new owner."""
    key = (workspace.resolve(), str(parent_session_id or "main-session"))
    with _INSTANCE_LOCK:
        registered = _INSTANCES.get(key)
        selected = manager or registered
        if selected is None:
            return
    # Keep the owner discoverable until all children settle. If close fails, a
    # later deletion retry must reach the same live jobs instead of constructing
    # a fresh manager and orphaning them. Closing outside the registry lock also
    # prevents one slow Session from freezing unrelated workspaces.
    selected.close(timeout=timeout)
    with _INSTANCE_LOCK:
        if _INSTANCES.get(key) is selected:
            _INSTANCES.pop(key, None)


@contextmanager
def scoped_background_agent_manager(manager: BackgroundAgentManager):
    """Bind a Session-owned manager to tool dispatch."""
    token = _MANAGER.set(manager)
    try:
        yield manager
    finally:
        _MANAGER.reset(token)


@contextmanager
def scoped_agent_message_sender(
    manager: BackgroundAgentManager,
    sender: str,
):
    """Bind the authenticated sender used by the shared send_message tool."""
    token = _MESSAGE_ROUTE.set((manager, str(sender or "worker")))
    try:
        yield
    finally:
        _MESSAGE_ROUTE.reset(token)


def _current_manager() -> BackgroundAgentManager:
    manager = _MANAGER.get()
    if manager is None:
        raise RuntimeError("background agent manager is not bound to this Session")
    if manager.workspace != current_workdir().resolve():
        raise RuntimeError("background agent manager workspace mismatch")
    return manager


def bound_background_agent_manager(
    parent_session_id: str | None = None,
) -> BackgroundAgentManager | None:
    """Return the Session-bound manager to a child running in its worktree."""
    manager = _MANAGER.get()
    if manager is None:
        return None
    if parent_session_id and manager.parent_session_id != str(parent_session_id):
        return None
    return manager


def agent_manager(
    action: str,
    tasks: list[dict] | None = None,
    session_ids: list[str] | None = None,
    wait_ms: int = 0,
    after_sequence: int = 0,
    timeout_ms: int = 0,
    reason: str = "",
    run_id: str = "",
) -> str:
    """Start, inspect, replay, or cancel background write subagents."""
    try:
        manager = _current_manager()
        normalized = str(action or "").strip().lower()
        if normalized == "start":
            return manager.start(tasks or [])
        if normalized == "status":
            return manager.status(session_ids, wait_ms)
        if normalized == "events":
            return manager.events(after_sequence)
        if normalized == "wait":
            return manager.wait(session_ids or [], timeout_ms)
        if normalized == "stop":
            return manager.stop(
                session_ids or [],
                reason=reason or "stopped by parent",
                timeout_ms=timeout_ms or 2000,
            )
        if normalized == "cancel":
            return manager.cancel(session_ids or [])
        if normalized == "run_list":
            snapshots = manager.workflow_run_snapshots()
            return ToolOutput(
                f"Managed workflow runs: {len(snapshots)}.",
                title="Workflow runs",
                metadata={"workflow_runs": snapshots},
            )
        if normalized == "run_pause":
            return (
                "Workflow paused."
                if manager.pause_workflow_run(run_id)
                else "Error: workflow is not running"
            )
        if normalized == "run_resume":
            return (
                "Workflow resumed."
                if manager.resume_workflow_run(run_id)
                else "Error: workflow is not paused"
            )
        if normalized == "run_stop":
            return (
                "Workflow stop requested."
                if manager.stop_workflow_run(run_id, reason or "stopped by parent")
                else "Error: workflow is not active"
            )
        return "Error: unsupported agent_manager action"
    except Exception as exc:
        return f"Error: {exc}"


def send_message(to: str, content: str, seen_by: list[str] | None = None) -> str:
    """Send one bounded message using the current Agent's authenticated identity."""
    try:
        route = _MESSAGE_ROUTE.get()
        manager, sender = route if route is not None else (_current_manager(), "worker")
        return manager.send_message(
            sender=sender,
            recipient=to,
            content=content,
            seen_by=seen_by,
        )
    except Exception as exc:
        return f"Error: {exc}"


def apply_agent_changes(session_id: str, reviewed_files: list[str], confirm: bool = False) -> str:
    """Apply a reviewed child result through the parent's active transaction."""
    try:
        if not confirm:
            return "Error: confirm=true is required after reviewing every changed file"
        manager = _current_manager()
        child_state = manager._load(session_id)
        writes, deletes, error = manager.application_changes(session_id, reviewed_files)
        if error:
            return error
        outputs: list[str] = []
        if writes:
            result = dispatch("write_files_batch", {"files": writes, "overwrite": True})
            if result.startswith(("Error:", "Denied")):
                return result
            outputs.append(result)
        if deletes:
            result = dispatch("apply_patch", {"changes": deletes})
            if result.startswith(("Error:", "Denied")):
                return result
            outputs.append(result)
        output = (
            f"Applied reviewed child changes from {session_id} "
            f"({len(writes)} writes, {len(deletes)} deletes).\n\n"
            + "\n\n".join(outputs)
        )
        canonical = child_state.get(CHILD_RESULT_KEY)
        if isinstance(canonical, dict):
            outcome = ChildAgentResult.from_dict(canonical).with_status(
                "applied",
                final_text=output,
            )
        else:
            outcome = child_result_from_state(
                child_state,
                final_text=output,
                status="applied",
                verification=str(child_state.get("verification") or ""),
            )
        return ToolOutput(
            output,
            title="Apply child changes",
            metadata=outcome.to_metadata(),
        )
    except Exception as exc:
        return f"Error: {exc}"


register(
    name="agent_manager",
    description=(
        "Start bounded isolated write-capable child agents in the background, "
        "inspect their persistent status/events, or request cooperative cancellation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "status", "events", "wait", "stop", "cancel", "run_list", "run_pause", "run_resume", "run_stop"]},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "prompt": {"type": "string"},
                        "target_paths": {"type": "array", "items": {"type": "string"}},
                        "allowed_tools": {"type": "array", "items": {"type": "string"}},
                        "output_schema": {
                            "type": "object",
                            "description": "Optional supported JSON-Schema subset for the child result.",
                        },
                        "model_hint": {
                            "type": "string",
                            "enum": ["fast", "balanced", "deep"],
                        },
                        "evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "verification": {"type": "object"},
                        "read_only": {
                            "type": "boolean",
                            "description": "Run as an isolated read-only child; target_paths may be omitted.",
                        },
                        "phase": {"type": "string"},
                        "isolation": {
                            "type": "string",
                            "enum": ["thread", "process"],
                            "description": "Optional spawn-process boundary for hard termination.",
                        },
                    },
                    "required": ["prompt", "target_paths"],
                },
            },
            "session_ids": {"type": "array", "items": {"type": "string"}},
            "wait_ms": {"type": "integer", "description": "Status may wait up to 10000 ms for one task."},
            "after_sequence": {"type": "integer", "description": "Events strictly after this workflow revision."},
            "timeout_ms": {"type": "integer", "description": "Shared wait/stop deadline in milliseconds."},
            "reason": {"type": "string", "description": "Bounded stop reason recorded in workflow events."},
            "run_id": {"type": "string", "description": "Managed workflow run identity."},
        },
        "required": ["action"],
    },
    handler=agent_manager,
    execution="serial",
)

register(
    name="send_message",
    description=(
        "Send a non-blocking coordinator instruction to a live child Agent by "
        "session id or display name, or use '*' for a bounded broadcast."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Live child session id, display name, or '*'.",
            },
            "content": {
                "type": "string",
                "description": "Actionable instruction that changes the child's plan.",
            },
            "seen_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Forwarding chain copied from a received message.",
            },
        },
        "required": ["to", "content"],
    },
    handler=send_message,
    execution="serial",
)

register(
    name="apply_agent_changes",
    description=(
        "Apply one completed background child's changes after reviewing the exact "
        "changed_files list. Rejects scope violations and parent/child baseline conflicts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "reviewed_files": {"type": "array", "items": {"type": "string"}},
            "confirm": {"type": "boolean"},
        },
        "required": ["session_id", "reviewed_files", "confirm"],
    },
    handler=apply_agent_changes,
    execution="write",
    side_effect="mutates-fs",
)
