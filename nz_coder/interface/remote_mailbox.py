"""Bounded semantic mailbox between remote SSE transport and the renderer."""
from __future__ import annotations

import asyncio
import copy
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from nz_coder.protocol.public_error import PublicError, to_public_error


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
    """Preserve semantic order while reserving capacity for critical events."""

    def __init__(self, capacity: int = 512, critical_reserve: int = 16) -> None:
        self.capacity = max(1, int(capacity))
        self.critical_reserve = max(1, int(critical_reserve))
        self._queue: deque[dict] = deque()
        self._gap_pending = False
        self._gap_interaction = ""
        self._last_applied_sequence = 0
        self._condition = threading.Condition()

    @property
    def buffered_count(self) -> int:
        with self._condition:
            return self._count_locked() + int(self._gap_pending)

    @property
    def rebase_required(self) -> bool:
        """Return whether ordinary event loss has invalidated this epoch."""
        with self._condition:
            return self._gap_pending

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
        """Pop local recovery control first, otherwise preserve arrival order."""
        with self._condition:
            payload: dict | None
            if self._gap_pending:
                payload = self._gap_payload_locked()
                self._gap_pending = False
            elif self._queue:
                payload = self._queue.popleft()
            else:
                return None
            self._condition.notify_all()
            return payload

    def snapshot(self) -> list[dict]:
        """Return the current delivery order for tests and diagnostics."""
        with self._condition:
            result = []
            if self._gap_pending:
                result.append(self._gap_payload_locked())
            result.extend(copy.deepcopy(item) for item in self._queue)
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
        self._queue.append(payload)
        self._condition.notify_all()
        return True

    def _offer_delta_locked(self, payload: dict) -> bool:
        existing = self._queue[-1] if self._queue else None
        if (
            isinstance(existing, dict)
            and self._delta_key(existing) == self._delta_key(payload)
            and self._sequence(payload) == self._sequence_to(existing) + 1
            and self._sequence(payload) > 0
        ):
            self._queue[-1] = self._merge_delta(existing, payload)
            return True
        return self._offer_ordinary_locked(payload)

    def _offer_status_locked(self, payload: dict) -> bool:
        return self._offer_ordinary_locked(payload)

    def _offer_ordinary_locked(self, payload: dict) -> bool:
        total_limit = self.capacity + self.critical_reserve
        while (
            self._ordinary_count_locked() >= self.capacity
            or self._count_locked() >= total_limit
        ):
            if not self._discard_oldest_ordinary_locked():
                # Critical work owns every available slot. Dropping this
                # reconstructible event is safe only with an explicit rebase.
                self._mark_gap_locked(payload)
                self._condition.notify_all()
                return True
            self._mark_gap_locked(payload)
        self._queue.append(payload)
        self._condition.notify_all()
        return True

    def _discard_oldest_ordinary_locked(self) -> bool:
        for index, item in enumerate(self._queue):
            if not is_critical_remote_payload(item):
                del self._queue[index]
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
        return len(self._queue)

    def _ordinary_count_locked(self) -> int:
        return sum(
            1 for item in self._queue
            if not is_critical_remote_payload(item)
        )

    @staticmethod
    def _sequence(payload: dict) -> int:
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        value = meta.get("sequence")
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @classmethod
    def _sequence_to(cls, payload: dict) -> int:
        properties = (
            payload.get("properties")
            if isinstance(payload.get("properties"), dict)
            else {}
        )
        value = properties.get("to_sequence")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        return cls._sequence(payload)

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
        existing_from = old_properties.get("from_sequence")
        if not isinstance(existing_from, int) or isinstance(existing_from, bool):
            old_meta = (
                existing.get("meta")
                if isinstance(existing.get("meta"), dict)
                else {}
            )
            existing_from = old_meta.get("sequence")
        incoming_meta = (
            incoming.get("meta")
            if isinstance(incoming.get("meta"), dict)
            else {}
        )
        incoming_sequence = incoming_meta.get("sequence")
        if isinstance(existing_from, int) and not isinstance(existing_from, bool):
            properties["from_sequence"] = existing_from
        if isinstance(incoming_sequence, int) and not isinstance(incoming_sequence, bool):
            properties["to_sequence"] = incoming_sequence
        merged["properties"] = properties
        return merged


@dataclass
class RemoteTransportState:
    """Out-of-band control state that cannot consume mailbox capacity."""

    closed: bool = False
    reader_done: bool = False
    reconnect_required: bool = False
    fatal_error: PublicError | None = None
    failure_reason: str = ""
    exit_reason: str = ""


class RemoteTransportBridge:
    """Thread-safe bounded bridge with one coalesced event-loop wakeup."""

    def __init__(
        self,
        loop: Any,
        *,
        capacity: int = 512,
        critical_reserve: int = 16,
        critical_offer_timeout: float = 5.0,
    ) -> None:
        self.loop = loop
        self.mailbox = RemoteEventMailbox(capacity, critical_reserve)
        self._ready = asyncio.Event()
        self._lock = threading.Lock()
        self._wake_scheduled = False
        self._state = RemoteTransportState()
        self._error_delivered = False
        self._done_delivered = False
        self._critical_offer_timeout = max(0.0, float(critical_offer_timeout))

    @property
    def buffered_count(self) -> int:
        return self.mailbox.buffered_count

    @property
    def rebase_required(self) -> bool:
        return self.mailbox.rebase_required

    @property
    def state(self) -> RemoteTransportState:
        """Return a detached snapshot of the transport control plane."""
        with self._lock:
            return copy.deepcopy(self._state)

    def offer(self, payload: dict) -> bool:
        """Receive from the SSE thread and schedule at most one pending wakeup."""
        with self._lock:
            if self._state.closed:
                return False
        accepted = self.mailbox.offer(
            payload,
            block_critical=is_critical_remote_payload(payload),
            timeout=(
                self._critical_offer_timeout
                if is_critical_remote_payload(payload)
                else None
            ),
        )
        with self._lock:
            if accepted and not self._wake_scheduled:
                self._wake_scheduled = True
                self.loop.call_soon_threadsafe(self._ready.set)
            return accepted

    def close_reader(self, reason: str = "clean_eof") -> None:
        """Publish reader completion without requiring a mailbox slot."""
        with self._lock:
            self._state.reader_done = True
            self._state.exit_reason = str(reason or "clean_eof")
            self._schedule_wakeup_locked()

    def deactivate(self) -> None:
        """Fence a superseded reader without publishing a transport failure."""
        with self._lock:
            self._state.closed = True
            self._schedule_wakeup_locked()

    def fail_closed(
        self,
        error: PublicError,
        *,
        reconnect_required: bool,
    ) -> None:
        """Stop the current stream and publish one safe fatal control error."""
        public = to_public_error(error)
        with self._lock:
            if self._state.fatal_error is None:
                self._state.fatal_error = public
                self._state.failure_reason = public.code
            self._state.closed = True
            self._state.reconnect_required = bool(
                self._state.reconnect_required or reconnect_required
            )
            self._schedule_wakeup_locked()

    async def get(self, timeout: float | None = None) -> dict:
        """Return the next semantic payload without callback-per-delta fanout."""
        while True:
            control = self._pop_control()
            if control is not None:
                return control
            payload = self.mailbox.pop()
            if payload is not None:
                with self._lock:
                    if self.mailbox.buffered_count == 0:
                        self._wake_scheduled = False
                        self._ready.clear()
                return payload
            with self._lock:
                control = self._pop_control_locked(mailbox_empty=True)
                if control is not None:
                    return control
                if self.mailbox.buffered_count:
                    continue
                self._wake_scheduled = False
                self._ready.clear()
            waiter = self._ready.wait()
            if timeout is None:
                await waiter
            else:
                await asyncio.wait_for(waiter, timeout=timeout)

    def _pop_control(self) -> dict | None:
        with self._lock:
            return self._pop_control_locked(
                mailbox_empty=self.mailbox.buffered_count == 0,
            )

    def _pop_control_locked(self, *, mailbox_empty: bool) -> dict | None:
        if self._state.fatal_error is not None and not self._error_delivered:
            self._error_delivered = True
            return {
                "_error": self._state.fatal_error.to_dict(),
                "_reconnect_required": self._state.reconnect_required,
            }
        if mailbox_empty and self._state.reader_done and not self._done_delivered:
            self._done_delivered = True
            return {
                "_transport_done": True,
                "_exit_reason": self._state.exit_reason or "clean_eof",
            }
        return None

    def _schedule_wakeup_locked(self) -> None:
        if self._wake_scheduled:
            return
        self._wake_scheduled = True
        self.loop.call_soon_threadsafe(self._ready.set)

    def snapshot(self) -> list[dict]:
        return self.mailbox.snapshot()

    def clear_gap(self) -> None:
        self.mailbox.clear_gap()

    def mark_applied(self, sequence: int) -> None:
        self.mailbox.mark_applied(sequence)
