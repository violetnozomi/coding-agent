"""Durable background-workflow events and revisioned process snapshots."""
from __future__ import annotations

import copy
import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


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
        self._events = self._load_events()
        self._items: dict[str, dict] = {}
        self._terminal_task_ids: set[str] = set()
        self._peak_active_agents = 0
        self._started_at = time.time()
        self._latest_message = ""
        self._replay()
        if not self._events:
            self._append("workflow_started", data={"message": "workflow started"})
        else:
            self._persist_snapshot()

    def record_task(self, event_type: str, state: dict, *, message: str = "") -> dict:
        """Append one task transition and return the new immutable snapshot."""
        if event_type not in _EVENT_TYPES - {"workflow_started"}:
            raise ValueError(f"Unsupported workflow event type: {event_type}")
        item = _item_from_state(state)
        if (
            event_type == "task_terminal"
            and item["task_id"] in self._terminal_task_ids
        ):
            return self.snapshot()
        return self._append(
            event_type,
            task_id=item["task_id"],
            data={"item": item, **({"message": message[:1000]} if message else {})},
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
        for state in states:
            item = _item_from_state(state)
            previous = self._items.get(item["task_id"])
            if previous != item:
                self._append(
                    "task_reconciled",
                    task_id=item["task_id"],
                    data={"item": item, "message": "task state reconciled"},
                )
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._snapshot())

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
    ) -> dict:
        with self._lock:
            previous = self._events[-1]["id"] if self._events else ""
            event = {
                "id": f"workflow-event-{uuid.uuid4().hex}",
                "run_id": self.run_id,
                "sequence": len(self._events) + 1,
                "timestamp": time.time(),
                "type": event_type,
                "parent_id": previous,
                "task_id": str(task_id)[:200],
                "data": copy.deepcopy(data),
            }
            encoded = (
                json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
                + "\n"
            ).encode("utf-8")
            if len(encoded) > 64 * 1024:
                raise ValueError("Workflow event exceeds 64 KiB")
            self.root.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                self.events_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                0o600,
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._events.append(event)
            self._apply(event)
            self._persist_snapshot()
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

    def _load_events(self) -> list[dict]:
        try:
            raw = self.events_path.read_bytes()
        except FileNotFoundError:
            return []
        if len(raw) > _MAX_JOURNAL_BYTES:
            raise ValueError("Workflow event journal exceeds 16 MiB")
        events: list[dict] = []
        lines = raw.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if not line.endswith(b"\n") and index == len(lines) - 1:
                break
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"Invalid workflow event at line {index + 1}"
                ) from exc
            parent = events[-1]["id"] if events else ""
            if not _valid_event(
                event,
                run_id=self.run_id,
                sequence=index + 1,
                parent_id=parent,
            ):
                raise ValueError(
                    f"Invalid workflow event chain at line {index + 1}"
                )
            events.append(event)
            if len(events) > 100_000:
                raise ValueError("Workflow event journal exceeds 100000 entries")
        return events

    def _replay(self) -> None:
        for event in self._events:
            self._apply(event)

    def _apply(self, event: dict) -> None:
        if event["type"] == "workflow_started":
            self._started_at = float(event["timestamp"])
        item = event.get("data", {}).get("item")
        if isinstance(item, dict) and item.get("task_id"):
            self._items[str(item["task_id"])] = copy.deepcopy(item)
            self._peak_active_agents = max(
                self._peak_active_agents,
                sum(
                    1
                    for current in self._items.values()
                    if current.get("status") == "running"
                ),
            )
        if event["type"] == "task_terminal" and event.get("task_id"):
            self._terminal_task_ids.add(str(event["task_id"]))
        message = event.get("data", {}).get("message")
        if isinstance(message, str) and message:
            self._latest_message = message[:1000]

    def _snapshot(self) -> dict:
        items = sorted(
            self._items.values(),
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
            float(self._events[-1]["timestamp"])
            if self._events else self._started_at
        )
        token_spent = sum(
            int((item.get("usage") or {}).get("total", 0) or 0)
            for item in items
        )
        return {
            "schema_version": 1,
            "revision": len(self._events),
            "run_id": self.run_id,
            "workflow_name": "background-agents",
            "status": status,
            "started_at": _iso(self._started_at),
            "updated_at": _iso(updated_at),
            "elapsed_ms": max(0, round((updated_at - self._started_at) * 1000, 3)),
            "items": copy.deepcopy(items),
            "counts": counts,
            "progress": {
                "spawned_agents": len(items),
                "finished_agents": counts["completed"] + counts["failed"] + counts["cancelled"],
                "active_agents": counts["running"],
                "peak_active_agents": self._peak_active_agents,
                "failed_agents": counts["failed"],
                "stopped_agents": counts["cancelled"],
                "agent_cap": self.agent_cap,
                "concurrency_cap": self.concurrency_cap,
            },
            "tokens": {"spent": max(0, token_spent)},
            **({"latest_message": self._latest_message} if self._latest_message else {}),
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
            os.write(descriptor, encoded)
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
    return bool(
        isinstance(event, dict)
        and event.get("run_id") == run_id
        and event.get("sequence") == sequence
        and event.get("parent_id") == parent_id
        and isinstance(event.get("id"), str)
        and event.get("type") in _EVENT_TYPES
        and isinstance(event.get("timestamp"), (int, float))
        and not isinstance(event.get("timestamp"), bool)
        and math.isfinite(float(event["timestamp"]))
        and isinstance(event.get("task_id"), str)
        and isinstance(event.get("data"), dict)
    )
