"""Session-owned background orchestration for isolated write-capable subagents."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import uuid
import threading
import time
import weakref
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import CancelledError
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from nz_coder.runtime.core.run_settings import RunSettings, current_run_settings
from nz_coder.runtime.agent.child_result import (
    CHILD_RESULT_KEY,
    ChildAgentResult,
    child_result_from_state,
)
from nz_coder.protocol.public_error import (
    format_public_error,
    PublicError,
    PublicRuntimeError,
    to_public_error,
)
from nz_coder.runtime.agent.child_process_result import allocate_child_process_result
from nz_coder.runtime.agent.process_supervisor import SubagentProcessSupervisor
from nz_coder.runtime.agent.settlement import FrozenSettlement, SettlementCoordinator, freeze_settlement, project_checkpoint
from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.runtime.process.command_broker import ParentCommandScope
from nz_coder.runtime.process.command_channel import CommandChannelLimits
from nz_coder.runtime.process.preparation import PreparationInterrupted, PreparationScope
from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.tools import ToolOutput, dispatch, register


_TERMINAL_STATUSES = frozenset({
    "completed", "cancelled", "timeout", "error", "max_turns",
    "tool_error_rolled_back", "verification_failed_rolled_back", "interrupted",
    "verification_failed", "completed_unverified", "applied",
    "needs_parent", "completed_conflicted",
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


@dataclass
class _LiveJob:
    cancel_event: threading.Event
    done_event: threading.Event
    thread: threading.Thread
    supervisor: SubagentProcessSupervisor | None = None
    command_scope: ParentCommandScope | None = None
    preparation_scope: PreparationScope | None = None
    environment: object | None = None
    capability_lease_id: str = ""
    settlement_id: str = ""
    frozen: bool = False
    acquired: bool = False
    stop_reason: str = ""
    cancel_requested_at: float = 0.0
    cancel_diagnostic_attempted: bool = False

    @property
    def process(self):
        return self.supervisor.process if self.supervisor is not None else None


@dataclass
class _ManagedWorkflowRun:
    run_id: str
    name: str
    status: str
    started_at: float
    condition: threading.Condition
    ended_at: float | None = None
    error: str = ""


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
    verification = state.get("postconditions")
    if not isinstance(verification, dict):
        verification = str(state.get("verification") or "")
    return child_result_from_state(
        state,
        final_text=str(state.get("background_result") or ""),
        status=str(state.get("status") or "unknown"),

    )


class BackgroundAgentManager:
    """Own background child threads and persistent lifecycle state for one Session."""

    def __init__(self, workspace: Path, parent_session_id: str):
        self.workspace = workspace.resolve()
        self.parent_session_id = str(parent_session_id or "main-session")
        self._lock = threading.RLock()
        self._close_condition = threading.Condition(self._lock)
        self._closing = False
        self._retiring = False
        self._closed = False
        self._jobs: dict[str, _LiveJob] = {}
        self._message_sequence = 0
        self._mailboxes: dict[str, list[dict]] = {}
        self._worker_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
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
        self._settlements = SettlementCoordinator(self._workflow, lambda state: self._save(state), self._lock)
        self._restore_workflow_run_identities()
        self._reconcile_interrupted()
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
            for state in self._visible_states()
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
        if any(
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

        message_id = self._deliver_message(
            recipients, sender=sender_id, content=body, seen_by=chain,
            kind="coordinator_instruction" if sender_id == "worker" else "peer_message",
        )
        return f"Message {message_id} delivered to {', '.join(recipients)}."

    def _deliver_message(
        self,
        recipients: list[str],
        *,
        sender: str,
        content: str,
        seen_by: list[str],
        kind: str,
        status: str | None = None,
    ) -> str:
        """Enqueue peer/completion mail through one bounded notification path."""
        with self._lock:
            self._message_sequence += 1
            message_id = f"peer-{self._message_sequence:06d}"
            message = {
                "id": message_id,
                "sender": sender,
                "content": content[:4000],
                "seen_by": seen_by,
                "kind": kind,
                "created_at": time.time(),
            }
            if status is not None:
                message["status"] = status[:120]
            for recipient in recipients:
                mailbox = self._mailboxes.setdefault(recipient, [])
                mailbox.append(dict(message, recipient=recipient))
                if len(mailbox) > 200:
                    del mailbox[:-200]
            if "worker" in recipients:
                # 消息先入箱，再跨线程通知；邮箱才是消息事实来源。
                for loop, event in self._worker_waiters:
                    loop.call_soon_threadsafe(event.set)
                self._worker_waiters.clear()
        return message_id

    def drain_messages(self, recipient: str) -> list[dict]:
        """Atomically consume pending in-flight messages for one Agent."""
        target = str(recipient or "").strip()
        if not target:
            return []
        with self._lock:
            messages = self._mailboxes.pop(target, [])
        return [dict(item) for item in messages]

    async def wait_for_worker_wake(self) -> None:
        """Await parent mail without polling or binding the manager to one loop."""
        event = asyncio.Event()
        waiter = (asyncio.get_running_loop(), event)
        with self._lock:
            # 检查与登记共用投递锁，覆盖消息先到、登记后到这两种时序。
            if self._mailboxes.get("worker") or not self.has_worker_wake_source():
                return
            self._worker_waiters.add(waiter)
        try:
            await event.wait()
        finally:
            with self._lock:
                self._worker_waiters.discard(waiter)

    def has_worker_wake_source(self) -> bool:
        """Return whether parent idle-yield has mail or a live child to await."""
        with self._lock:
            return bool(self._mailboxes.get("worker")) or any(
                not live.done_event.is_set() for live in self._jobs.values()
            )

    def _states(self) -> list[dict]:
        from nz_coder.runtime.agent.subagent import _iter_subagent_states

        return _iter_subagent_states(self.parent_session_id, self.workspace)

    def _load_raw(self, session_id: str) -> dict:
        from nz_coder.runtime.agent.subagent import _load_subagent_state

        return _load_subagent_state(self.parent_session_id, session_id, self.workspace)

    def _project_state(self, state: dict) -> dict:
        """Expose a terminal child only after its parent-owned handoff settles."""
        with self._lock:
            live = self._jobs.get(str(state.get("session_id") or ""))
            if live is None or live.done_event.is_set():
                return state
            state = dict(state)
            if state.get("status") in _TERMINAL_STATUSES:
                state["status"] = "cancel_requested" if live.cancel_event.is_set() else "running"
            state.pop(CHILD_RESULT_KEY, None)
            state.pop("background_result", None)
            return state

    def _visible_states(self) -> list[dict]:
        with self._lock:
            raw = {str(state.get("session_id") or ""): state for state in self._states()}
            items = {str(item["task_id"]): item for item in self._workflow.snapshot()["items"]}
            committed = {str(item["task_id"]): item for item in self._workflow.committed_items()}
            result = []
            for task in dict.fromkeys([*raw, *items]):
                state = raw.get(task, {})
                live = self._jobs.get(task)
                if live is not None and not live.done_event.is_set():
                    if not state:
                        state = {"session_id": task, "background": True, "status": "running", "settlement_id": live.settlement_id}
                    result.append(self._project_state(state))
                else:
                    result.append(project_checkpoint(
                        state, committed.get(task) or items.get(task), self._workflow.current_invocation(task),
                        committed=task in committed, parent_session_id=self.parent_session_id,
                    ))
            return result

    def _load(self, session_id: str) -> dict:
        with self._lock:
            return next((state for state in self._visible_states() if state.get("session_id") == session_id), {})

    def _workflow_snapshot(self) -> dict:
        with self._lock:
            return self._workflow.project_snapshot([
                state for state in self._visible_states() if state.get("background")
            ])

    def _save(self, state: dict) -> None:
        from nz_coder.runtime.agent.subagent import _save_subagent_state

        _save_subagent_state(self.parent_session_id, state, self.workspace)

    def _reconcile_interrupted(self) -> None:
        for state in self._visible_states():
            if state.get("_uncommitted_invocation"):
                identity = "checkpoint-repair-" + str(state["session_id"]) + "-" + str(state.get("settlement_id") or "history")
                self._settlements.submit(FrozenSettlement(identity, identity, state, None, lambda: None, reconcile_only=True))

    def _admit(self, state: dict) -> None:
        """Assign one identity before checkpoint/queued publication or execution."""
        task = str(state["session_id"])
        if self._settlements.closing:
            raise ValueError("Settlement recovery is closing; no new invocation can start")
        live = self._jobs.get(task)
        if (live is not None and not live.done_event.is_set()) or self._settlements.has_pending_task(task):
            raise ValueError("Previous invocation must settle before another launch")
        previous = state.get(CHILD_RESULT_KEY)
        if isinstance(previous, dict):
            cost = ChildAgentResult.from_dict(previous).cost
            if cost is not None:
                state["child_cost_baseline"] = cost
        # 基线独立保留；上一轮展示/终态与计数不能冒充本轮执行事实。
        for key in ("background_result", "digest", "summary_kind", "finished_at", "cancel_requested_at",
                    "stop_reason", "run_started_at", "public_error", "structured_output", "postconditions", "execution_verification",
                    "tokens", "cost", "cost_known", "iterations"):
            state.pop(key, None)
        state.update(settlement_id=uuid.uuid4().hex, status="queued", queued_at=time.time())
        self._save(state)
        self._workflow.record_task("task_queued", state, message=f"queued {state.get('display_name') or task}")

    def _result_error_metadata(self, states: list[dict]) -> dict:
        for state in states:
            if state.get("_checkpoint_unavailable"):
                return {"public_error": PublicError("child_result_unavailable", "The task is recorded, but its complete result is unavailable.", metadata={"task_id": state["session_id"]}).to_dict()}
            live = self._jobs.get(str(state.get("session_id") or ""))
            error = self._settlements.error_for(live.settlement_id) if live is not None else None
            if error is not None:
                return {"public_error": error.to_dict()}
        return {}

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
            _parent_memory_scope,
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
            if self._retiring or self._closing or self._closed:
                return "Error: background Agent manager is closed"
        if not worktree_enabled and any(isinstance(t, dict) and not t.get("read_only") for t in (tasks or [])):
            return "Error: background write agents require SUBAGENT_WORKTREE_ENABLED=1"
        if not isinstance(tasks, list) or not tasks:
            return "Error: tasks must be a non-empty list"
        if len(tasks) > agent_cap:
            return f"Error: fan-out exceeds maxAgents lifetime cap ({agent_cap})"

        # Admission, state publication, and job registration share one lock.
        # Concurrent callers therefore cannot both observe the same remaining
        # lifetime capacity or claim overlapping paths.
        with self._lock:
            if self._retiring or self._closing or self._closed:
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
                    return format_public_error(exc)
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
                environment_mode = item.get('environment_mode', 'reuse')
                if environment_mode not in {'reuse', 'private'}:
                    return 'Error: environment_mode must be reuse or private'
                agent_type = "explore" if read_only else "general-purpose"
                state = _new_subagent_state(
                    self.parent_session_id, agent_type, item.get("allowed_tools"),
                    memory_scope=_parent_memory_scope(self.workspace),
                )
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
                    "environment_mode": environment_mode,
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
                self._admit(state)
            self._workflow.agent_cap = agent_cap
            self._workflow.concurrency_cap = concurrency_cap
            for state, item in prepared:
                self._launch(
                    state,
                    item,
                    run_snapshot=run_snapshot,
                    concurrency_cap=concurrency_cap,
                    process_stop_grace=process_stop_grace,
                    admitted=True,
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
        admitted: bool = False,
    ) -> None:
        with self._lock:
            self._launch_registered(state, item, run_snapshot=run_snapshot, concurrency_cap=concurrency_cap,
                                    process_stop_grace=process_stop_grace, admitted=admitted)

    def _launch_registered(
        self,
        state: dict,
        item: dict,
        *,
        run_snapshot=None,
        concurrency_cap: int | None = None,
        process_stop_grace: float | None = None,
        admitted: bool = False,
    ) -> None:
        from nz_coder.runtime.agent.subagent import (
            _bind_subagent_memory_scope, _subagent_model, prepare_subagent_command_context,
            run_subagent, scoped_parent_context, current_parent_command_authorizer,
        )
        from nz_coder.state.memory_scope import scoped_memory_scope

        state = copy.deepcopy(state)
        memory_scope = _bind_subagent_memory_scope(state, self.workspace)
        if not admitted:
            self._admit(state)

        cancel_event = threading.Event()
        done_event = threading.Event()
        session_id = state["session_id"]
        # spawn 不继承 ContextVar；在登记时冻结父请求模型，不能退回环境默认。
        parent_model_id = _subagent_model("general-purpose", run_snapshot)
        parent_command_authorizer = current_parent_command_authorizer()
        if parent_command_authorizer is not None:
            parent_command_authorizer = parent_command_authorizer.fork(self.workspace)
        context = copy_context()
        if concurrency_cap is None:
            _agent_cap, concurrency_cap = self._run_limits(run_snapshot)
        if process_stop_grace is None:
            process_stop_grace = current_run_settings().subagent_process_stop_grace
        settings = RunSettings.from_snapshot(run_snapshot) if run_snapshot is not None else current_run_settings()
        live_job: _LiveJob | None = None

        def worker() -> None:
            acquired = False
            latest = state
            result = "The child did not produce a result."
            result_path = None
            process_outcome = None
            execution_error = None
            deadline_elapsed_before_start = False
            preparation_interrupted = ""
            try:
                with self._capacity_condition:
                    while self._active_jobs >= concurrency_cap:
                        if cancel_event.is_set():
                            latest = self._load_raw(session_id) or state
                            latest["status"] = "cancelled"
                            result = "Cancelled before execution started."
                            return
                        self._capacity_condition.wait(0.1)
                    if cancel_event.is_set():
                        latest = self._load_raw(session_id) or state
                        latest["status"] = "cancelled"
                        result = "Cancelled before execution started."
                        return
                    self._active_jobs += 1
                    acquired = True
                    assert live_job is not None
                    live_job.acquired = True
                deadline = time.monotonic() + settings.subagent_timeout_seconds
                latest = self._load_raw(session_id) or state
                latest["status"] = "running"
                latest["run_started_at"] = time.time()
                self._save(latest)
                self._workflow.record_task(
                    "task_started", latest,
                    message=f"started {latest.get('display_name') or session_id}",
                )
                from nz_coder.runtime.agent.child_contracts import resolve_effective_evidence_refs
                from nz_coder.runtime.agent import prepared_evidence
                normalized_refs = resolve_effective_evidence_refs(item.get("evidence_refs"), latest.get("evidence_refs"))
                def record_preparation_target(worktree) -> None:
                    with self._lock:
                        current = self._load_raw(session_id)
                        if current.get("settlement_id") != live_job.settlement_id:
                            raise PublicRuntimeError(PublicError(
                                "preparation_identity_changed", "Preparation invocation identity changed.",
                            ))
                        current.update(worktree=asdict(worktree), worktree_preparing=True)
                        self._save(current)

                def publish_preparation_state(update):
                    with self._lock:
                        current = self._load_raw(session_id)
                        if current.get('settlement_id') != live_job.settlement_id:
                            raise PublicRuntimeError(PublicError('preparation_identity_changed', 'Preparation invocation identity changed.'))
                        for key in ('worktree', 'worktree_rel', 'scratch_rel', 'environment_preparation', 'source_copy_manifest', 'delivery_baseline', 'baseline_hashes'):
                            if key in update:
                                current[key] = copy.deepcopy(update[key])
                        if not update.get('worktree_preparing'):
                            current.pop('worktree_preparing', None)
                        if 'source_copy_manifest' not in update:
                            current.pop('source_copy_manifest', None)
                        self._save(current)

                preparation = PreparationScope(
                    self.workspace, parent_session_id=self.parent_session_id,
                    child_session_id=session_id, settlement_id=live_job.settlement_id,
                    config_snapshot=run_snapshot, run_settings=settings,
                    cancel_event=cancel_event, deadline=deadline,
                    on_target=record_preparation_target, on_state=publish_preparation_state,
                )
                with self._lock:
                    live_job.preparation_scope = preparation
                prepared, authority = prepare_subagent_command_context(
                    str(item.get("prompt") or ""), latest,
                    workspace=self.workspace, config_snapshot=run_snapshot,
                    settings=settings, settlement_id=state["settlement_id"],
                    preparation=preparation,
                )
                command_authorizer = parent_command_authorizer.fork(prepared.execution_root) if parent_command_authorizer is not None else None
                # 准备工作树可能耗时；保留已经接纳的取消，不能用旧快照覆盖。
                with self._lock:
                    current = self._load_raw(session_id) or latest
                    current.update({key: latest[key] for key in ("worktree", "worktree_rel", "scratch_rel") if key in latest})
                    if current.get("settlement_id") != live_job.settlement_id:
                        raise PublicRuntimeError(PublicError("preparation_identity_changed", "Preparation invocation identity changed."))
                    current["evidence_refs"] = normalized_refs
                    current.pop("worktree_preparing", None)
                    self._save(current)
                    publisher = self._task_publishers.get(session_id)
                evidence = prepared_evidence.capture_diff_evidence(
                    normalized_refs, workspace=self.workspace,
                    parent_session_id=self.parent_session_id, child_session_id=session_id,
                    settlement_id=live_job.settlement_id, preparation=preparation,
                )
                prepared = replace(prepared, diff_evidence=evidence)
                preparation.seal()
                if cancel_event.is_set():
                    return
                if time.monotonic() >= deadline:
                    deadline_elapsed_before_start = True
                    result = "Timed out before child execution started."
                    return
                if state.get("isolation") == "process":
                    channel_errors: list[PublicError] = []

                    def channel_error(error: PublicError) -> None:
                        with self._lock:
                            if live_job.supervisor is None:
                                channel_errors.append(error)
                            else:
                                live_job.supervisor.fail(error)

                    scope = ParentCommandScope(authority, deadline=deadline, child_publisher=publisher,
                        on_channel_error=channel_error, command_authorizer=command_authorizer)
                    with self._lock:
                        live_job.command_scope = scope
                    if cancel_event.is_set():
                        return
                    if time.monotonic() >= deadline:
                        deadline_elapsed_before_start = True
                        result = "Timed out before child execution started."
                        return
                    endpoint = scope.start()
                    payload = {
                        "workspace": str(self.workspace),
                        "parent_session_id": self.parent_session_id,
                        "parent_model_id": parent_model_id,
                        "memory_scope": memory_scope.to_dict(),
                        "prompt": str(item.get("prompt") or ""),
                        "agent_type": str(state.get("agent_type") or "general-purpose"),
                        "session_id": session_id,
                        "allowed_tools": item.get("allowed_tools"),
                        "target_paths": list(state.get("claimed_paths") or []),
                        "output_schema": item.get("output_schema"),
                        "model_hint": item.get("model_hint"),
                        "evidence_refs": normalized_refs,
                        "verification": item.get("verification"),
                        "config_snapshot": run_snapshot,
                        "settlement_id": state["settlement_id"],
                        "prepared_workspace": prepared,
                        "command_settings": authority.run_settings,
                        "command_limits": CommandChannelLimits.from_settings(authority.run_settings),
                        "command_deadline": deadline,
                    }
                    result_path = allocate_child_process_result(self.workspace)
                    if cancel_event.is_set():
                        return
                    if time.monotonic() >= deadline:
                        deadline_elapsed_before_start = True
                        result = "Timed out before child execution started."
                        return
                    assert live_job is not None
                    with self._lock:
                        live_job.supervisor = SubagentProcessSupervisor(
                            payload, result_path, command_endpoint=endpoint, on_stop=scope.request_stop,
                        )
                        for error in channel_errors:
                            live_job.supervisor.fail(error)
                    live_job.supervisor.start()
                    process_outcome = live_job.supervisor.wait(
                        cancel_event, deadline=deadline, stop_grace=process_stop_grace,
                    )
                    result = process_outcome.result
                else:
                    from nz_coder.runtime.agent.subagent import scoped_prepared_subagent_workspace, scoped_subagent_cleanup_owner
                    def retain_environment(environment):
                        with self._lock:
                            if self._jobs.get(session_id) is not live_job:
                                raise RuntimeError("Child cleanup owner identity changed")
                            if live_job.settlement_id != state["settlement_id"]:
                                raise RuntimeError("Child cleanup settlement identity changed")
                            live_job.environment = environment
                            retiring = self._retiring or cancel_event.is_set()
                        if retiring:
                            environment.request_retirement()

                    def run_prepared(*args, **kwargs):
                        with (
                            scoped_prepared_subagent_workspace(prepared),
                            scoped_subagent_cleanup_owner(session_id, retain_environment),
                            scoped_memory_scope(memory_scope),
                            scoped_parent_context(memory_scope=memory_scope.to_dict(), command_authorizer=command_authorizer),
                        ):
                            return run_subagent(*args, **kwargs)
                    result = context.run(
                        run_prepared,
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
            except PreparationInterrupted as exc:
                preparation_interrupted = exc.reason
                result = "Preparation cancelled." if exc.reason == "cancelled" else "Timed out during child preparation."
            except Exception as exc:
                execution_error = to_public_error(exc)
                result = execution_error.message
            finally:
                if live_job is not None and live_job.supervisor is not None and not live_job.supervisor.exited:
                    cleanup_cancel = threading.Event()
                    cleanup_cancel.set()
                    live_job.supervisor.wait(cleanup_cancel, deadline=time.monotonic(), stop_grace=process_stop_grace)
                # 查询投影与父交接共用锁，防止快照把刚发布的终态写回 running。
                with self._lock:
                    latest = self._load_raw(session_id) or state
                    # stop 也在这把锁内接纳请求；已接纳的取消不能在交接窗口变成 error。
                    if (cancel_event.is_set() or preparation_interrupted == "cancelled"
                            or (process_outcome is not None and process_outcome.reason == "cancelled")):
                        latest["status"] = "cancelled"
                        if live_job.cancel_requested_at:
                            latest.update(stop_reason=live_job.stop_reason, cancel_requested_at=live_job.cancel_requested_at)
                        latest.pop("public_error", None)
                        if acquired and state.get("isolation") == "process":
                            result = "Cancelled; child execution stopped."
                    elif execution_error is not None:
                        latest["status"] = "error"
                        latest["public_error"] = execution_error.to_dict()
                    elif (deadline_elapsed_before_start or preparation_interrupted == "timeout"
                          or (process_outcome is not None and process_outcome.reason == "timeout")):
                        latest["status"] = "timeout"
                        latest.pop("public_error", None)
                    elif process_outcome is not None and process_outcome.public_error is not None:
                        latest["status"] = "error"
                        latest["public_error"] = process_outcome.public_error.to_dict()
                    assert live_job is not None
                    try:
                        candidate = freeze_settlement(
                            latest, result, settlement_id=live_job.settlement_id,
                            canonical=process_outcome.canonical if process_outcome is not None else None,
                            canonical_settlement_id=process_outcome.canonical_settlement_id if process_outcome is not None else "",
                        )
                    except ValueError:
                        error = PublicError("child_result_protocol_error", "The child returned an invalid result for this invocation.")
                        latest.update(status="error", public_error=error.to_dict())
                        latest.pop(CHILD_RESULT_KEY, None)
                        latest.pop("child_result_settlement_id", None)
                        result = error.message
                        candidate = freeze_settlement(latest, result, settlement_id=live_job.settlement_id)
                    live_job.frozen = True

                # 原worker保留命令域和租约，直到所有native owner实际退出。
                # 先冻结再等待，晚到stop只推进清理，不能重写业务结果。
                scopes = [scope for scope in (live_job.preparation_scope, live_job.command_scope) if scope is not None]
                environment = live_job.environment
                while scopes or environment is not None:
                    for scope in scopes:
                        scope.request_stop()
                    round_deadline = time.monotonic() + .05
                    closed = True
                    for scope in scopes:
                        closed = scope.close(deadline=round_deadline) and closed
                    if environment is not None:
                        try:
                            environment.close()
                        except BaseException:
                            closed = False
                        else:
                            closed = environment.environment_cleanup_complete and closed
                    if closed:
                        break
                    time.sleep(0.01)
                self._submit_frozen_settlement(live_job, candidate, result_path)

        thread = threading.Thread(
            target=worker,
            name=f"nz-subagent-{session_id}",
            daemon=True,
        )
        live_job = _LiveJob(cancel_event, done_event, thread, settlement_id=state["settlement_id"])
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
        try:
            thread.start()
        except Exception as exc:
            if thread.ident is not None:
                # 已启动的 worker 是唯一 owner，继续由它完成交接。
                return
            latest = copy.deepcopy(state)
            error = to_public_error(exc)
            latest.update(status="error", public_error=error.to_dict())
            candidate = freeze_settlement(latest, error.message, settlement_id=live_job.settlement_id)
            live_job.frozen = True
            self._submit_frozen_settlement(live_job, candidate, None)

    def _submit_frozen_settlement(self, live: _LiveJob, candidate: dict, result_path) -> None:
        session_id = candidate["session_id"]

        def committed() -> None:
            if live.done_event.is_set():
                return
            try:
                self._deliver_message(
                    ["worker"], sender=session_id, content=str(candidate["background_result"]),
                    seen_by=[session_id], kind="task_completed", status=str(candidate["status"]),
                )
            finally:
                if live.acquired:
                    self._active_jobs -= 1
                    live.acquired = False
                    self._capacity_condition.notify_all()
                if live.capability_lease_id:
                    capability_leases().release(live.capability_lease_id)
                    live.capability_lease_id = ""
                live.done_event.set()

        self._settlements.submit(FrozenSettlement(
            live.settlement_id, "workflow-terminal-" + live.settlement_id,
            candidate, result_path, committed,
        ))

    def status(self, session_ids: list[str] | None = None, wait_ms: int = 0) -> str:
        """Return persistent status and optionally wait briefly for one task."""
        ids = [str(item) for item in (session_ids or []) if str(item).strip()]
        if wait_ms and len(ids) == 1:
            with self._lock:
                live = self._jobs.get(ids[0])
            if live is not None:
                live.done_event.wait(max(0, min(int(wait_ms), 10000)) / 1000)
        with self._lock:
            states = self._visible_states()
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
                if state.get("_checkpoint_unavailable"):
                    rows.append("  Complete result unavailable; committed workflow facts are preserved.")
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
                if state.get("_checkpoint_unavailable"):
                    continue
                canonical = state.get(CHILD_RESULT_KEY)
                if isinstance(canonical, dict):
                    outcomes.append(ChildAgentResult.from_dict(canonical).to_dict())
                elif state.get("status") in _TERMINAL_STATUSES:
                    outcomes.append(child_result_from_state(
                        state,
                        final_text=str(state.get("background_result") or ""),
                        status=str(state.get("status") or "unknown"),

                    ).to_dict())
            workflow_snapshot = self._workflow_snapshot()
            return ToolOutput(
                "\n".join(rows),
                title="Background Agent status",
                metadata={
                    "child_results": outcomes,
                    "workflow_snapshot": workflow_snapshot,
                    **self._result_error_metadata(states),
                },
            )

    def events(self, after_sequence: int = 0) -> str:
        """Return the durable workflow event suffix for replay consumers."""
        with self._lock:
            events = self._workflow.events(after_sequence)
            snapshot = self._workflow_snapshot()
            return ToolOutput(
                f"Workflow events after revision {max(0, int(after_sequence))}: "
                f"{len(events)}; current revision: {snapshot['revision']}.",
                title="Background workflow events",
                metadata={
                    "workflow_events": events,
                    "workflow_snapshot": snapshot,
                    **self._result_error_metadata(self._visible_states()),
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
            if not (self._load(session_id) or {}).get("background")
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

        with self._lock:
            states = [self._load(session_id) for session_id in ids]
            outcomes = [
                _outcome_from_background_state(state).to_dict()
                for state in states
                if state.get("status") in _TERMINAL_STATUSES and not state.get("_checkpoint_unavailable")
            ]
            unsettled = [
                str(state.get("session_id") or "")
                for state in states
                if state.get("status") not in _TERMINAL_STATUSES
            ]
            snapshot = self._workflow_snapshot()
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
                    **self._result_error_metadata(states),
                },
            )

    def wait_for_result(
        self,
        session_id: str,
        cancel_event: threading.Event | None = None,
    ) -> ChildAgentResult | None:
        """Wait for parent settlement and read its result without consuming mail."""
        with self._lock:
            state = self._load(session_id)
            if not state or not state.get("background"):
                raise ValueError(f"unknown workflow task: {session_id}")
            live = self._jobs.get(session_id)
            pending = live is not None and not live.done_event.is_set()
        if pending:
            assert live is not None
            # 完成直接由原结清事件唤醒；短期限只用于检查调用方取消，期间不读盘。
            while not live.done_event.is_set():
                if cancel_event is not None and cancel_event.is_set():
                    raise CancelledError()
                live.done_event.wait(0.05 if cancel_event is not None else None)
            with self._lock:
                state = self._load(session_id)
            # 同一任务重新启动后，前一次等待不能接纳新一轮的结果。
            if state.get("settlement_id") != live.settlement_id:
                return None
        if state.get("status") not in _TERMINAL_STATUSES or state.get("_checkpoint_unavailable"):
            return None
        return _outcome_from_background_state(state)

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
            if not (self._load(session_id) or {}).get("background")
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
        with self._lock:
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
                        if state.get("status") in _TERMINAL_STATUSES and not state.get("_checkpoint_unavailable")
                    ],
                    "workflow_snapshot": self._workflow_snapshot(),
                    **self._result_error_metadata(states),
                },
            )

    def request_retirement(self) -> None:
        """同锁关准入、通知全部未冻结孩子；诊断和等待留给 close。"""
        with self._lock:
            self._retiring = True
            resources = []
            for live in self._jobs.values():
                if not live.frozen and not live.done_event.is_set() and not live.cancel_event.is_set():
                    live.stop_reason = "parent Session closed"
                    live.cancel_requested_at = time.time()
                    live.cancel_event.set()
                resources.extend(scope for scope in (live.preparation_scope, live.command_scope) if scope is not None)
                environment = getattr(live, "environment", None)
                if environment is not None:
                    resources.append(environment)
        # 先保证所有业务 Event 已通知，再进入各资源的非阻塞退休入口。
        for resource in resources:
            notify = getattr(resource, "request_retirement", None) or resource.request_stop
            notify()

    def _request_stop(self, session_ids: list[str], *, reason: str) -> list[str]:
        requested: list[str] = []
        with self._lock:
            scopes = []
            for task in session_ids:
                live = self._jobs.get(task)
                if live is None:
                    continue
                scopes.extend(scope for scope in (live.preparation_scope, live.command_scope) if scope is not None)
                if live.environment is not None:
                    scopes.append(live.environment)
                # 取消与冻结共用交接锁，先接纳业务事实，再通知资源停止。
                if not live.frozen and not live.done_event.is_set() and not live.cancel_event.is_set():
                    live.stop_reason = str(reason)[:1000]
                    live.cancel_requested_at = time.time()
                    live.cancel_event.set()
                    requested.append(task)
        # 所有资源先收到停止请求；即使结果已冻结或随后写盘失败，也继续回收。
        for scope in scopes:
            notify = getattr(scope, "request_retirement", None) or scope.request_stop
            notify()
        for session_id in session_ids:
            with self._lock:
                live = self._jobs.get(session_id)
                # 锁外通知期间可能已保存新目标或终态，不能回写旧state。
                state = self._load_raw(session_id)
                pending = live is not None and not live.done_event.is_set()
                if not state:
                    continue
                if live is not None:
                    if not live.cancel_requested_at or live.cancel_diagnostic_attempted:
                        continue
                    accepted_reason = live.stop_reason
                    accepted_at = live.cancel_requested_at
                    # 诊断可晚于冻结，但只尝试一次；写盘结果不明时不能盲目重复事件。
                    live.cancel_diagnostic_attempted = True
                    save_requested = pending and not live.frozen
                else:
                    if state.get("status") == "cancel_requested" or self._load(session_id).get("status") in _TERMINAL_STATUSES:
                        continue
                    accepted_reason = str(reason)[:1000]
                    accepted_at = time.time()
                    save_requested = True
                    requested.append(session_id)
                try:
                    if save_requested:
                        state.update(status="cancel_requested", cancel_requested_at=accepted_at, stop_reason=accepted_reason)
                        self._save(state)
                    diagnostic = self._project_state(state)
                    if pending:
                        diagnostic = {**diagnostic, "status": "cancel_requested"}
                    self._workflow.record_task(
                        "task_cancel_requested", diagnostic,
                        message=f"stop requested for {session_id}: {accepted_reason}"[:1000],
                    )
                except (OSError, ValueError):
                    # 停止信号先于诊断落盘；存储故障不能让实际执行继续失控。
                    self._settlements.wake()
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
            requested = bool(result.metadata.get("requested_task_ids"))
            return ToolOutput(
                ("cancellation requested. " if requested else "No new cancellation accepted. ") + str(result),
                title=result.title,
                metadata=result.metadata,
            )
        return result

    def close(self, timeout: float = 5.0) -> None:
        """Cancel and settle every process-local child owned by this Session."""
        self.request_retirement()
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
            self._settlements.wake(recheck=True)
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
            if not self._settlements.close(deadline=deadline):
                raise RuntimeError("background settlement recovery did not stop before Session deletion")
        except BaseException:
            with self._lock:
                self._closing = False
                self._close_condition.notify_all()
            raise
        for _session_id, live in jobs:
            if live.supervisor is not None:
                live.supervisor.close(deadline=deadline)
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
        if state.get("_checkpoint_unavailable"):
            return [], [], "Error: complete child result is unavailable; changes cannot be applied"
        if state.get("status") != "completed":
            return [], [], f"Error: subagent must be completed before apply; status={state.get('status')}"
        if not isinstance(state.get('delivery_baseline'), dict):
            return [], [], 'Error: child delivery baseline is unavailable; review and port changes explicitly'
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
            return [], [], format_public_error(
                exc, context="invalid child worktree ownership: ",
            )
        if worktree is None or worktree.mode not in {"git", "copy"}:
            return [], [], "Error: direct-mode child changes cannot be safely applied"
        child_root = Path(worktree.path).resolve()
        from nz_coder.runtime.worktree import WorktreeManager, WorktreeError
        repository = WorktreeManager(self.workspace)
        receipt = state.get('environment_preparation') or {}
        modules = set(receipt.get('module_paths', [])) | {item['path'] for item in receipt.get('submodules', [])}
        try:
            if repository.is_git_repo():
                modules.update(repository.gitlinks())
            if worktree.mode == 'git':
                modules.update(repository.gitlinks(child_root, ref=worktree.head_commit or 'HEAD'))
                modules.update(repository.gitlinks(child_root))
        except WorktreeError as exc:
            return [], [], format_public_error(exc)
        if any(path == root or path.startswith(root + '/') for path in changed for root in modules):
            return [], [], 'Error: submodule changes require a separate review and application protocol'
        baseline = dict(state.get("baseline_hashes") or {})
        writes: list[dict] = []
        deletes: list[dict] = []
        conflicts: list[str] = []
        for relative in changed:
            for root in (self.workspace, child_root):
                cursor = root
                for part in Path(relative).parts:
                    cursor /= part
                    if cursor.is_symlink():
                        return [], [], f"Error: symbolic-link changes cannot be applied: {relative}"
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
        return format_public_error(exc)


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
        return format_public_error(exc)


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

            )
        return ToolOutput(
            output,
            title="Apply child changes",
            metadata=outcome.to_metadata(),
        )
    except Exception as exc:
        return format_public_error(exc)


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
                        "environment_mode": {"type": "string", "enum": ["reuse", "private"]},
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
