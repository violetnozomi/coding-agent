"""Durable background-workflow events and revisioned process snapshots."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant


_EVENT_TYPES = frozenset({
    "workflow_started",
    "task_queued",
    "task_started",
    "task_cancel_requested",
    "task_terminal",
    "task_reconciled",
    "task_replayed",
    "phase_started",
    "phase_finished",
    "synthesis_completed",
    "verifier_started",
    "verifier_verdict",
    "budget_updated",
    "workflow_run_started",
    "workflow_run_completed",
    "workflow_run_failed",
    "workflow_run_stopped",
    "memory_outcome_recorded",
    "workflow_run_paused",
    "workflow_run_resumed",
    "artifact_written",
    "workflow_log",
    "worktree_sweep_completed",
    "workflow_run_renamed",
})
_LIVE = frozenset({"pending", "running"})
_MAX_JOURNAL_BYTES = 16 * 1024 * 1024
_MAX_JOURNAL_ENTRIES = 100_000


class WorkflowJournalBlocked(ValueError):
    """A complete bad journal or hard capacity limit needs explicit repair."""


@dataclass(frozen=True)
class TerminalCommit:
    event_id: str
    sequence: int
    snapshot_dirty: bool


def _write_all(descriptor: int, encoded: bytes) -> None:
    remaining = memoryview(encoded)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Workflow storage write made no progress")
        remaining = remaining[written:]


@dataclass
class _JournalProjection:
    items: dict[str, dict] = field(default_factory=dict)
    terminal_events: dict[tuple[str, str], dict] = field(default_factory=dict)
    current_invocations: dict[str, str] = field(default_factory=dict)
    peak_active_agents: int = 0
    started_at: float = field(default_factory=time.time)
    latest_message: str = ""

    def apply(self, event: dict) -> None:
        if event["type"] == "workflow_started":
            self.started_at = float(event["timestamp"])
        item = event.get("data", {}).get("item")
        if isinstance(item, dict) and item.get("task_id"):
            task = str(item["task_id"])
            identity = str(event.get("data", {}).get("settlement_id") or "")
            if event["type"] == "task_queued" or task not in self.current_invocations:
                self.current_invocations[task] = identity
            committed = (task, identity) in self.terminal_events
            if event["type"] == "task_terminal":
                self.terminal_events[(task, identity)] = event
            if self.current_invocations[task] == identity and (not committed or event["type"] == "task_terminal"):
                self.items[task] = copy.deepcopy(item)
            self.peak_active_agents = max(
                self.peak_active_agents,
                sum(
                    1
                    for current in self.items.values()
                    if current.get("status") == "running"
                ),
            )
        message = event.get("data", {}).get("message")
        if isinstance(message, str) and message:
            self.latest_message = message[:1000]


class WorkflowProcessStore:
    """Own one append-only task event log and its atomically cached snapshot."""

    def __init__(
        self,
        root: Path,
        run_id: str,
        *,
        agent_cap: int,
        concurrency_cap: int | None = None,
        on_event=None,
    ):
        self.root = Path(root)
        self.run_id = str(run_id)
        self.agent_cap = max(1, min(int(agent_cap), 20))
        self.concurrency_cap = max(
            1,
            min(
                int(concurrency_cap if concurrency_cap is not None else agent_cap),
                self.agent_cap,
            ),
        )
        self._on_event = on_event
        self.events_path = self.root / "events.jsonl"
        self.snapshot_path = self.root / "snapshot.json"
        self._lock = threading.RLock()
        self._recovery_wakeup = None
        self._snapshot_dirty = False
        self._journal_uncertain = False
        self._events, self._journal_bytes, tail = self._read_journal()
        self._journal_uncertain = tail
        self._projection = self._replay(self._events)
        if not self._events:
            self._append("workflow_started", data={"message": "workflow started"})
        else:
            self._try_snapshot()

    @property
    def needs_recovery(self) -> bool:
        with self._lock:
            return self._journal_uncertain or self._snapshot_dirty

    def set_recovery_wakeup(self, callback) -> None:
        with self._lock:
            self._recovery_wakeup = callback
            if self.needs_recovery:
                callback()

    def _wake_recovery(self) -> None:
        if self._recovery_wakeup is not None:
            self._recovery_wakeup()

    def _try_snapshot(self) -> None:
        self._snapshot_dirty = True
        try:
            self._persist_snapshot()
        except OSError:
            self._wake_recovery()
        else:
            self._snapshot_dirty = False

    def current_invocation(self, task_id: str) -> str | None:
        with self._lock:
            return self._projection.current_invocations.get(task_id)

    def committed_items(self) -> list[dict]:
        with self._lock:
            return [
                {**copy.deepcopy(event["data"]["item"]), "settlement_id": identity}
                for (task, identity), event in self._projection.terminal_events.items()
                if self._projection.current_invocations.get(task) == identity
            ]

    def commit_terminal(self, state: dict, *, event_id: str) -> TerminalCommit:
        if not isinstance(state.get("settlement_id"), str) or not state["settlement_id"]:
            raise ValueError("Terminal settlement requires invocation identity")
        self._append(
            "task_terminal", task_id=str(state.get("session_id") or ""),
            data={"item": _item_from_state(state), "settlement_id": state["settlement_id"]},
            event_id=event_id,
        )
        with self._lock:
            event = self._projection.terminal_events[(str(state["session_id"]), state["settlement_id"])]
            return TerminalCommit(event["id"], event["sequence"], self._snapshot_dirty)

    def record_task(self, event_type: str, state: dict, *, message: str = "") -> dict:
        """Append one task transition and return the new immutable snapshot."""
        if event_type not in _EVENT_TYPES - {"workflow_started"}:
            raise ValueError(f"Unsupported workflow event type: {event_type}")
        item = _item_from_state(state)
        return self._append(
            event_type,
            task_id=item["task_id"],
            data={"item": item, **({"message": message[:1000]} if message else {}),
                  **({"settlement_id": state["settlement_id"]} if state.get("settlement_id") else {})},
        )

    def record_event(
        self,
        event_type: str,
        *,
        data: dict | None = None,
        task_id: str = "",
    ) -> dict:
        """Append one bounded non-task workflow transition."""
        if event_type not in _EVENT_TYPES - {
            "workflow_started",
            "task_queued",
            "task_started",
            "task_cancel_requested",
            "task_terminal",
            "task_reconciled",
        }:
            raise ValueError(f"Unsupported workflow event type: {event_type}")
        payload = copy.deepcopy(data or {})
        if len(json.dumps(payload, default=str)) > 32 * 1024:
            raise ValueError("Workflow event data exceeds 32 KiB")
        return self._append(event_type, task_id=task_id, data=payload)

    def reconcile(self, states: list[dict]) -> dict:
        """Repair the materialized snapshot from authoritative child states."""
        with self._lock:
            for state in states:
                item = _item_from_state(state)
                identity = str(state.get("settlement_id") or "")
                task = item["task_id"]
                current = self._projection.current_invocations.get(task)
                if (task, identity) in self._projection.terminal_events or (current is not None and current != identity):
                    continue
                # 有本轮身份的成功只能通过正式提交进入投影，不能由查询补成成功。
                if identity and item["status"] == "completed":
                    continue
                previous = self._projection.items.get(task)
                if previous != item:
                    self._append(
                        "task_reconciled", task_id=task,
                        data={"item": item, "message": "task state reconciled",
                              **({"settlement_id": identity} if identity else {})},
                    )
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._snapshot())

    def project_snapshot(self, states: list[dict]) -> dict:
        """Render handoff/orphan visibility without appending a query repair."""
        with self._lock:
            items = copy.deepcopy(self._projection.items)
            for state in states:
                task = str(state.get("session_id") or "")
                identity = str(state.get("settlement_id") or "")
                if (task, identity) in self._projection.terminal_events:
                    continue
                if task in self._projection.current_invocations and self._projection.current_invocations[task] != identity:
                    continue
                items[task] = _item_from_state(state)
            return copy.deepcopy(self._snapshot(items))

    def events(self, after_sequence: int = 0) -> list[dict]:
        """Return a bounded immutable replay suffix after one revision."""
        cursor = max(0, int(after_sequence))
        with self._lock:
            return copy.deepcopy([
                event for event in self._events if event["sequence"] > cursor
            ])

    def workflow_run_lifecycles(self) -> list[dict]:
        """Replay durable run identities independently of the owning process."""
        states: dict[str, dict] = {}
        status_by_type = {
            "workflow_run_started": "running",
            "workflow_run_paused": "paused",
            "workflow_run_resumed": "running",
            "workflow_run_completed": "completed",
            "workflow_run_failed": "failed",
            "workflow_run_stopped": "stopped",
        }
        with self._lock:
            events = copy.deepcopy(self._events)
        for event in events:
            status = status_by_type.get(str(event.get("type") or ""))
            data = event.get("data") if isinstance(event.get("data"), dict) else {}
            run_id = str(data.get("run_id") or "")
            if status is None or not run_id:
                continue
            current = states.setdefault(run_id, {
                "run_id": run_id,
                "name": str(data.get("display_name") or data.get("name") or "workflow")[:200],
                "status": status,
                "started_at": float(event.get("timestamp") or time.time()),
            })
            current["status"] = status
            if data.get("display_name") or data.get("name"):
                current["name"] = str(
                    data.get("display_name") or data.get("name")
                )[:200]
            if status in {"completed", "failed", "stopped"}:
                current["ended_at"] = float(event.get("timestamp") or time.time())
                if data.get("error"):
                    current["error"] = str(data["error"])[:4000]
        return sorted(
            states.values(), key=lambda item: item["started_at"], reverse=True
        )

    def _append(
        self,
        event_type: str,
        *,
        task_id: str = "",
        data: dict,
        event_id: str | None = None,
    ) -> dict:
        with self._lock:
            if self._journal_uncertain:
                self._verify_journal()
            identity = str(data.get("settlement_id") or "")
            if event_type == "task_terminal":
                if (task_id, identity) in self._projection.terminal_events:
                    if self._snapshot_dirty:
                        self._try_snapshot()
                    return copy.deepcopy(self._snapshot())
                if identity and self._projection.current_invocations.get(task_id) != identity:
                    raise WorkflowJournalBlocked("Terminal settlement has no confirmed admission")
            previous = self._events[-1]["id"] if self._events else ""
            event = {
                "id": event_id or f"workflow-event-{uuid.uuid4().hex}",
                "run_id": self.run_id,
                "sequence": len(self._events) + 1,
                "timestamp": time.time(),
                "type": event_type,
                "parent_id": previous,
                "task_id": str(task_id)[:200],
                "data": copy.deepcopy(data),
            }
            if not _valid_event(event, run_id=self.run_id, sequence=len(self._events) + 1, parent_id=previous):
                raise WorkflowJournalBlocked("Invalid workflow event structure")
            encoded = (
                json.dumps(event, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)
                + "\n"
            ).encode("utf-8")
            if len(encoded) > 64 * 1024:
                raise WorkflowJournalBlocked("Workflow event exceeds 64 KiB")
            if self._journal_bytes + len(encoded) > _MAX_JOURNAL_BYTES or len(self._events) >= _MAX_JOURNAL_ENTRIES:
                raise WorkflowJournalBlocked("Workflow event journal capacity reached")
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.events_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                _write_all(descriptor, encoded)
                os.fsync(descriptor)
            except OSError:
                # 完整记录可能已写入；先核实磁盘，不能按旧内存序号直接重写。
                self._journal_uncertain = True
                self._wake_recovery()
                raise
            finally:
                os.close(descriptor)
            self._events.append(event)
            self._journal_bytes += len(encoded)
            self._projection.apply(event)
            self._try_snapshot()
            snapshot = copy.deepcopy(self._snapshot())
            event_copy = copy.deepcopy(event)
        if self._on_event is not None:
            try:
                self._on_event(event_copy, snapshot)
            except Exception:
                # The durable journal is authoritative; a live presentation
                # sink must never roll back an already-fsynced transition.
                pass
        return snapshot

    def _read_journal(self) -> tuple[list[dict], int, bool]:
        try:
            with self.events_path.open("rb") as stream:
                raw = stream.read(_MAX_JOURNAL_BYTES + 1)
        except FileNotFoundError:
            return [], 0, False
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise WorkflowJournalBlocked("Workflow event journal exceeds 16 MiB")
        events: list[dict] = []
        lines = raw.splitlines(keepends=True)
        valid_bytes = 0
        for index, line in enumerate(lines):
            if not line.endswith(b"\n") and index == len(lines) - 1:
                break
            if len(line) > 64 * 1024:
                raise WorkflowJournalBlocked("Workflow event exceeds 64 KiB")
            try:
                event = json.loads(line, parse_constant=reject_nonstandard_json_constant)
            except (UnicodeDecodeError, ValueError) as exc:
                raise WorkflowJournalBlocked(
                    f"Invalid workflow event at line {index + 1}"
                ) from exc
            parent = events[-1]["id"] if events else ""
            if not _valid_event(
                event,
                run_id=self.run_id,
                sequence=index + 1,
                parent_id=parent,
            ):
                raise WorkflowJournalBlocked(
                    f"Invalid workflow event chain at line {index + 1}"
                )
            events.append(event)
            valid_bytes += len(line)
            if len(events) > _MAX_JOURNAL_ENTRIES:
                raise WorkflowJournalBlocked("Workflow event journal exceeds 100000 entries")
        return events, valid_bytes, valid_bytes != len(raw)

    def _verify_journal(self) -> None:
        events, length, tail = self._read_journal()
        # 新日志先在临时投影中完整重放；失败不能污染已确认的查询状态。
        projection = self._replay(events)
        self._snapshot(projection=projection, events=events)
        descriptor = os.open(self.events_path, os.O_RDWR)
        try:
            if tail:
                os.ftruncate(descriptor, length)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._events = events
        self._journal_bytes = length
        self._projection = projection
        self._journal_uncertain = False
        self._snapshot_dirty = True

    def recover(self) -> None:
        with self._lock:
            if self._journal_uncertain:
                self._verify_journal()
            if self._snapshot_dirty:
                self._try_snapshot()

    @staticmethod
    def _replay(events: list[dict]) -> _JournalProjection:
        projection = _JournalProjection()
        for event in events:
            projection.apply(event)
        return projection

    def _snapshot(
        self, visible_items: dict | None = None, *,
        projection: _JournalProjection | None = None, events: list[dict] | None = None,
    ) -> dict:
        projection = self._projection if projection is None else projection
        events = self._events if events is None else events
        items = sorted(
            (projection.items if visible_items is None else visible_items).values(),
            key=lambda item: (float(item.get("created_at") or 0), item["task_id"]),
        )
        counts = {
            name: sum(1 for item in items if item["status"] == name)
            for name in ("pending", "running", "completed", "failed", "cancelled")
        }
        if any(item["status"] in _LIVE for item in items) or not items:
            status = "running"
        elif counts["failed"]:
            status = "failed"
        elif counts["cancelled"] == len(items):
            status = "cancelled"
        else:
            status = "completed"
        updated_at = (
            float(events[-1]["timestamp"])
            if events else projection.started_at
        )
        token_spent = sum(
            int((item.get("usage") or {}).get("total", 0) or 0)
            for item in items
        )
        return {
            "schema_version": 1,
            "revision": len(events),
            "run_id": self.run_id,
            "workflow_name": "background-agents",
            "status": status,
            "started_at": _iso(projection.started_at),
            "updated_at": _iso(updated_at),
            "elapsed_ms": max(0, round((updated_at - projection.started_at) * 1000, 3)),
            "items": copy.deepcopy(items),
            "counts": counts,
            "progress": {
                "spawned_agents": len(items),
                "finished_agents": counts["completed"] + counts["failed"] + counts["cancelled"],
                "active_agents": counts["running"],
                "peak_active_agents": projection.peak_active_agents,
                "failed_agents": counts["failed"],
                "stopped_agents": counts["cancelled"],
                "agent_cap": self.agent_cap,
                "concurrency_cap": self.concurrency_cap,
            },
            "tokens": {"spent": max(0, token_spent)},
            **({"latest_message": projection.latest_message} if projection.latest_message else {}),
        }

    def _persist_snapshot(self) -> None:
        payload = self._snapshot()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.snapshot_path.with_name(
            f".{self.snapshot_path.name}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        try:
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self.snapshot_path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _item_from_state(state: dict) -> dict:
    raw_status = str(state.get("status") or "queued")
    if raw_status == "queued":
        status = "pending"
    elif raw_status in {"running", "cancel_requested"}:
        status = "running"
    elif raw_status in {"completed", "completed_unverified", "applied"}:
        status = "completed"
    elif raw_status == "cancelled":
        status = "cancelled"
    else:
        status = "failed"
    canonical = state.get("child_result")
    canonical = canonical if isinstance(canonical, dict) else {}
    current_identity = str(state.get("settlement_id") or "")
    if current_identity and state.get("child_result_settlement_id") != current_identity:
        canonical = {}
        state = {key: value for key, value in state.items() if key not in {"tokens", "digest", "summary_kind"}}
    created_at = _finite_time(
        state.get("queued_at"),
        state.get("created_at"),
        default=time.time(),
    )
    started_at = _finite_time(state.get("run_started_at"), default=0.0)
    ended_at = _finite_time(
        state.get("finished_at"),
        state.get("updated_at") if status not in _LIVE else None,
        default=0.0,
    )
    summary = str(canonical.get("digest") or state.get("digest") or "")[:800]
    summary_kind = str(
        canonical.get("summary_kind") or state.get("summary_kind") or ""
    )
    return {
        "task_id": str(state.get("session_id") or "")[:200],
        "title": str(state.get("display_name") or state.get("agent_type") or "agent")[:200],
        "kind": "agent",
        "status": status,
        "raw_status": raw_status[:80],
        "agent_id": str(state.get("agent_id") or "")[:200],
        "provider": str(canonical.get("provider") or state.get("provider_id") or "")[:120],
        "model": str(canonical.get("model") or state.get("model_id") or "")[:240],
        "created_at": created_at,
        **({"started_at": _iso(started_at)} if started_at else {}),
        **({"ended_at": _iso(ended_at)} if ended_at else {}),
        **({"summary": summary} if summary else {}),
        **({"summary_status": _summary_status(summary_kind)} if summary_kind else {}),
        "usage": copy.deepcopy(canonical.get("usage") or state.get("tokens") or {}),
        "changed_files": [str(item)[:1000] for item in state.get("changed_files") or []][:50],
        **({"phase": str(state.get("phase"))[:120]} if state.get("phase") else {}),
        **({"fanout_id": str(state.get("fanout_id"))[:200]} if state.get("fanout_id") else {}),
        **({"fanout_index": int(state.get("fanout_index"))} if isinstance(state.get("fanout_index"), int) else {}),
    }


def _summary_status(kind: str) -> str:
    return {
        "digest": "result",
        "excerpt": "notice",
        "pending": "pending",
        "digest-failed": "unavailable",
    }.get(kind, "notice")


def _finite_time(*values: object, default: float) -> float:
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            number = float(value)
            if math.isfinite(number) and number >= 0:
                return number
    return default


def _iso(value: float) -> str:
    return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()


def _valid_event(
    event: object,
    *,
    run_id: str,
    sequence: int,
    parent_id: str,
) -> bool:
    valid = bool(
        isinstance(event, dict)
        and event.get("run_id") == run_id
        and event.get("sequence") == sequence
        and event.get("parent_id") == parent_id
        and isinstance(event.get("id"), str)
        and bool(event["id"])
        and isinstance(event.get("type"), str)
        and event.get("type") in _EVENT_TYPES
        and _valid_timestamp(event.get("timestamp"))
        and isinstance(event.get("task_id"), str)
        and isinstance(event.get("data"), dict)
    )
    if not valid:
        return False
    item = event["data"].get("item")
    if event["type"] in {"task_queued", "task_started", "task_cancel_requested", "task_terminal", "task_reconciled"} or item is not None:
        if not _valid_item(item) or item["task_id"] != event["task_id"]:
            return False
    identity = event["data"].get("settlement_id")
    return identity is None or (isinstance(identity, str) and bool(identity))


def _valid_timestamp(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        datetime.fromtimestamp(value, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return False
    return True


def _valid_item(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if not isinstance(item.get("task_id"), str) or not item["task_id"]:
        return False
    if not isinstance(item.get("status"), str) or item["status"] not in {"pending", "running", "completed", "failed", "cancelled"}:
        return False
    if not isinstance(item.get("raw_status"), str) or not item["raw_status"]:
        return False
    created = item.get("created_at")
    if not isinstance(created, (int, float)) or isinstance(created, bool):
        return False
    try:
        if not math.isfinite(float(created)) or created < 0:
            return False
    except OverflowError:
        return False
    usage = item.get("usage")
    return isinstance(usage, dict) and all(
        isinstance(count, int) and not isinstance(count, bool) and count >= 0
        for count in usage.values()
    )
