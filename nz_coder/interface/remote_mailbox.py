"""Bounded semantic mailbox between remote SSE transport and the renderer."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections import OrderedDict, deque
from typing import Any


_CRITICAL_EVENTS = frozenset({
    "permission.asked",
    "question.asked",
    "session.run.completed",
    "session.run.failed",
    "session.run.cancelled",
    "session.run.settled",
    "server.snapshot",
    "server.event_gap",
})
_DELTA_EVENTS = frozenset({
    "message.part.delta",
    "message.reasoning.delta",
    "session.tool.progress",
})


def is_critical_remote_payload(payload: dict) -> bool:
    """Return whether backpressure must never discard this payload."""
    return bool(
        payload.get("_error")
        or payload.get("_transport_done")
        or str(payload.get("type") or "") in _CRITICAL_EVENTS
    )


class RemoteEventMailbox:
    """Store critical events separately and coalesce reconstructible deltas."""

    def __init__(self, capacity: int = 512, critical_reserve: int = 16) -> None:
        self.capacity = max(1, int(capacity))
        self.critical_reserve = max(1, int(critical_reserve))
        self._critical: deque[dict] = deque()
        self._deltas: OrderedDict[tuple[str, ...], dict] = OrderedDict()
        self._status: deque[dict] = deque()
        self._gap_pending = False
        self._gap_interaction = ""
        self._last_applied_sequence = 0
        self._condition = threading.Condition()

    @property
    def buffered_count(self) -> int:
        with self._condition:
            return self._count_locked() + int(self._gap_pending)

    def offer(
        self,
        payload: dict,
        *,
        block_critical: bool = False,
        timeout: float | None = None,
    ) -> bool:
        """Offer one payload without allowing ordinary data to evict critical state."""
        if not isinstance(payload, dict):
            return False
        selected = copy.deepcopy(payload)
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._condition:
            if is_critical_remote_payload(selected):
                while not self._offer_critical_locked(selected):
                    if not block_critical:
                        return False
                    remaining = (
                        None if deadline is None else max(0.0, deadline - time.monotonic())
                    )
                    if remaining == 0.0:
                        return False
                    self._condition.wait(remaining)
                return True
            event_type = str(selected.get("type") or "")
            if event_type in _DELTA_EVENTS:
                return self._offer_delta_locked(selected)
            return self._offer_status_locked(selected)

    def pop(self) -> dict | None:
        """Pop critical work first, then the gap boundary, then ordinary state."""
        with self._condition:
            payload: dict | None
            if self._critical:
                payload = self._critical.popleft()
            elif self._gap_pending:
                payload = self._gap_payload_locked()
                self._gap_pending = False
            elif self._status:
                payload = self._status.popleft()
            elif self._deltas:
                _key, payload = self._deltas.popitem(last=False)
            else:
                return None
            self._condition.notify_all()
            return payload

    def snapshot(self) -> list[dict]:
        """Return the current delivery order for tests and diagnostics."""
        with self._condition:
            result = [copy.deepcopy(item) for item in self._critical]
            if self._gap_pending:
                result.append(self._gap_payload_locked())
            result.extend(copy.deepcopy(item) for item in self._status)
            result.extend(copy.deepcopy(item) for item in self._deltas.values())
            return result

    def clear_gap(self) -> None:
        """A successful authoritative snapshot ends the current gap epoch."""
        with self._condition:
            self._gap_pending = False
            self._gap_interaction = ""

    def mark_applied(self, sequence: int) -> None:
        with self._condition:
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                self._last_applied_sequence = max(self._last_applied_sequence, sequence)

    def _offer_critical_locked(self, payload: dict) -> bool:
        total_limit = self.capacity + self.critical_reserve
        while self._count_locked() >= total_limit:
            if not self._discard_oldest_ordinary_locked():
                return False
            self._mark_gap_locked(payload)
        self._critical.append(payload)
        self._condition.notify_all()
        return True

    def _offer_delta_locked(self, payload: dict) -> bool:
        key = self._delta_key(payload)
        existing = self._deltas.get(key)
        if existing is not None:
            self._deltas[key] = self._merge_delta(existing, payload)
            return True
        if self._ordinary_count_locked() >= self.capacity:
            self._discard_oldest_ordinary_locked()
            self._mark_gap_locked(payload)
        self._deltas[key] = payload
        return True

    def _offer_status_locked(self, payload: dict) -> bool:
        if self._ordinary_count_locked() >= self.capacity:
            self._discard_oldest_ordinary_locked()
            self._mark_gap_locked(payload)
        self._status.append(payload)
        return True

    def _discard_oldest_ordinary_locked(self) -> bool:
        if self._status:
            self._status.popleft()
            return True
        if self._deltas:
            self._deltas.popitem(last=False)
            return True
        return False

    def _mark_gap_locked(self, payload: dict) -> None:
        self._gap_pending = True
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        properties = (
            payload.get("properties")
            if isinstance(payload.get("properties"), dict)
            else {}
        )
        self._gap_interaction = str(
            meta.get("interaction_run_id")
            or meta.get("run_id")
            or properties.get("interaction_run_id")
            or self._gap_interaction
            or ""
        )

    def _gap_payload_locked(self) -> dict:
        return {
            "type": "server.event_gap",
            "properties": {
                "interaction_run_id": self._gap_interaction,
                "last_applied_sequence": self._last_applied_sequence,
                "overflow_reason": "remote_mailbox_overflow",
                "resume_required": True,
            },
        }

    def _count_locked(self) -> int:
        return len(self._critical) + self._ordinary_count_locked()

    def _ordinary_count_locked(self) -> int:
        return len(self._status) + len(self._deltas)

    @staticmethod
    def _delta_key(payload: dict) -> tuple[str, ...]:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        properties = (
            payload.get("properties")
            if isinstance(payload.get("properties"), dict)
            else {}
        )
        return (
            str(payload.get("type") or ""),
            str(meta.get("interaction_run_id") or meta.get("run_id") or ""),
            str(properties.get("message_id") or ""),
            str(properties.get("part_id") or ""),
            str(properties.get("attempt_id") or ""),
            str(properties.get("generation_id") or ""),
            str(properties.get("field") or ""),
        )

    @staticmethod
    def _merge_delta(existing: dict, incoming: dict) -> dict:
        merged = copy.deepcopy(incoming)
        old_properties = (
            existing.get("properties")
            if isinstance(existing.get("properties"), dict)
            else {}
        )
        properties = (
            merged.get("properties")
            if isinstance(merged.get("properties"), dict)
            else {}
        )
        old_delta = old_properties.get("delta")
        new_delta = properties.get("delta")
        if isinstance(old_delta, str) and isinstance(new_delta, str):
            properties["delta"] = old_delta + new_delta
        merged["properties"] = properties
        return merged


class RemoteTransportBridge:
    """Thread-safe bounded bridge with one coalesced event-loop wakeup."""

    def __init__(
        self,
        loop: Any,
        *,
        capacity: int = 512,
        critical_reserve: int = 16,
    ) -> None:
        self.loop = loop
        self.mailbox = RemoteEventMailbox(capacity, critical_reserve)
        self._ready = asyncio.Event()
        self._lock = threading.Lock()
        self._wake_scheduled = False

    @property
    def buffered_count(self) -> int:
        return self.mailbox.buffered_count

    def offer(self, payload: dict) -> bool:
        """Receive from the SSE thread and schedule at most one pending wakeup."""
        accepted = self.mailbox.offer(
            payload,
            block_critical=is_critical_remote_payload(payload),
            timeout=5.0 if is_critical_remote_payload(payload) else None,
        )
        with self._lock:
            if accepted and not self._wake_scheduled:
                self._wake_scheduled = True
                self.loop.call_soon_threadsafe(self._ready.set)
            return accepted

    async def get(self, timeout: float | None = None) -> dict:
        """Return the next semantic payload without callback-per-delta fanout."""
        while True:
            payload = self.mailbox.pop()
            if payload is not None:
                with self._lock:
                    if self.mailbox.buffered_count == 0:
                        self._wake_scheduled = False
                        self._ready.clear()
                return payload
            with self._lock:
                if self.mailbox.buffered_count:
                    continue
                self._wake_scheduled = False
                self._ready.clear()
            waiter = self._ready.wait()
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=timeout)

    def snapshot(self) -> list[dict]:
        return self.mailbox.snapshot()

    def clear_gap(self) -> None:
        self.mailbox.clear_gap()

    def mark_applied(self, sequence: int) -> None:
        self.mailbox.mark_applied(sequence)
