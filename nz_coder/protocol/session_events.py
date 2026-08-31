"""Instance-local events for clients, adapters, and SSE transports."""
from __future__ import annotations

import copy
import json
import math
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from nz_coder.foundation.json_safety import json_safe_value

_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CLOSED = object()
_JOURNAL_READ_BYTES = 16 * 1024 * 1024


def _safe_event_properties(properties: dict[str, Any]) -> dict[str, Any]:
    """Detach metadata and guarantee strict-JSON live/replay transport values."""
    payload = json_safe_value(properties)
    return payload if isinstance(payload, dict) else {}


class EventCursorExpiredError(LookupError):
    """Raised when a requested event ID is outside bounded replay history."""


class EventSubscriptionGapError(RuntimeError):
    """Raised when a bounded subscriber can no longer prove continuity."""

    def __init__(self, dropped_events: int, latest_sequence: int):
        super().__init__("subscriber queue overflow requires snapshot resync")
        self.dropped_events = dropped_events
        self.latest_sequence = latest_sequence

    def to_dict(self) -> dict[str, Any]:
        """Return the transport-safe resynchronization notice."""
        return {
            "reason": "subscriber_queue_overflow",
            "dropped_events": self.dropped_events,
            "latest_sequence": self.latest_sequence,
            "resume_required": True,
        }


@dataclass
class _GapNotice:
    dropped_events: int
    latest_sequence: int


@dataclass(frozen=True)
class SessionEvent:
    """One immutable, ordered event in the public session protocol."""

    type: str
    properties: dict[str, Any]
    sequence: int
    timestamp: float
    session_id: str
    run_id: str
    agent_id: str
    event_id: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        """Return the stable wire envelope used by adapters and SSE."""
        return {
            "type": self.type,
            "properties": copy.deepcopy(self.properties),
            "meta": {
                "schema_version": self.schema_version,
                "event_id": self.event_id,
                "sequence": self.sequence,
                "timestamp": self.timestamp,
                "session_id": self.session_id,
                "run_id": self.run_id,
                "agent_id": self.agent_id,
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SessionEvent" | None:
        """Validate and restore one event journal record."""
        if not isinstance(payload, dict):
            return None
        event_type = payload.get("type")
        properties = payload.get("properties")
        meta = payload.get("meta")
        if (
            not isinstance(event_type, str)
            or not _EVENT_TYPE_RE.fullmatch(event_type)
            or not isinstance(properties, dict)
            or not isinstance(meta, dict)
        ):
            return None
        sequence = meta.get("sequence")
        timestamp = meta.get("timestamp")
        schema_version = meta.get("schema_version")
        event_id = meta.get("event_id")
        identity = [meta.get(name) for name in ("session_id", "run_id", "agent_id")]
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or not math.isfinite(float(timestamp))
            or not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version < 1
            or not isinstance(event_id, str)
            or not _EVENT_ID_RE.fullmatch(event_id)
            or not all(isinstance(value, str) for value in identity)
        ):
            return None
        return cls(
            type=event_type,
            properties=_safe_event_properties(properties),
            sequence=sequence,
            timestamp=float(timestamp),
            session_id=identity[0],
            run_id=identity[1],
            agent_id=identity[2],
            event_id=event_id,
            schema_version=schema_version,
        )


class _EventJournal:
    """Best-effort compacting JSONL persistence for HTTP reconnect replay."""

    def __init__(self, path: Path, capacity: int, session_id: str):
        self.path = path
        self.capacity = max(1, capacity)
        self.session_id = session_id
        self.max_entries = max(256, self.capacity * 4)
        self.last_sequence = 0
        self._handle = None
        self._entry_count = 0

    def load(self) -> list[SessionEvent]:
        # Only expose the final contiguous, valid suffix for cursor replay. If
        # one record is corrupt or missing, retaining events on both sides of
        # the gap would make a cursor before it look losslessly resumable.
        recent: deque[SessionEvent] = deque(maxlen=self.capacity)
        last_sequence = 0
        saw_valid = False
        try:
            size = self.path.stat().st_size
            with self.path.open("rb") as handle:
                truncated = size > _JOURNAL_READ_BYTES
                if truncated:
                    handle.seek(size - _JOURNAL_READ_BYTES)
                    handle.readline()
                for raw_line in handle:
                    self._entry_count += 1
                    try:
                        payload = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        if saw_valid:
                            recent.clear()
                        continue
                    event = SessionEvent.from_dict(payload)
                    if event is None or event.session_id != self.session_id:
                        if saw_valid:
                            recent.clear()
                        continue
                    if saw_valid and event.sequence != last_sequence + 1:
                        recent.clear()
                    if event.sequence <= last_sequence:
                        continue
                    recent.append(event)
                    last_sequence = event.sequence
                    self.last_sequence = last_sequence
                    saw_valid = True
                if truncated:
                    self._entry_count = self.max_entries
        except OSError:
            return []
        return list(recent)

    def append(self, event: SessionEvent, recent: list[SessionEvent]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self._handle is None:
                self._handle = self.path.open("a", encoding="utf-8", buffering=1)
            self._handle.write(
                json.dumps(
                    event.to_dict(),
                    ensure_ascii=False,
                    default=str,
                    allow_nan=False,
                ) + "\n"
            )
            self._entry_count += 1
            if self._entry_count >= self.max_entries:
                self._compact(recent)
        except Exception:
            # Journaling is a replay optimization. Extension metadata with an
            # unsupported or cyclic value must not abort live Agent delivery.
            self.close()

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def _compact(self, recent: list[SessionEvent]) -> None:
        self.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for event in recent:
                    handle.write(
                        json.dumps(
                            event.to_dict(),
                            ensure_ascii=False,
                            default=str,
                            allow_nan=False,
                        )
                        + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(self.path)
            self._entry_count = len(recent)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            temp_path.unlink(missing_ok=True)
            raise


class SessionSubscription:
    """Bounded subscription owned by one client or transport."""

    def __init__(
        self,
        bus: "SessionEventBus",
        *,
        event_types: set[str] | None,
        max_queue: int,
    ):
        if (
            not isinstance(max_queue, int)
            or isinstance(max_queue, bool)
            or max_queue <= 0
        ):
            raise ValueError("max_queue must be a positive integer")
        self._bus = bus
        self._event_types = event_types
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._closed = False
        self._gapped = False
        self._gap_notice: _GapNotice | None = None
        self.dropped_events = 0

    def accepts(self, event: SessionEvent) -> bool:
        return self._event_types is None or event.type in self._event_types

    def get(self, timeout: float | None = None) -> SessionEvent:
        """Wait for the next event; raise StopIteration after unsubscribe."""
        if timeout is not None:
            if isinstance(timeout, bool):
                raise ValueError("subscription timeout must be a non-negative finite number")
            try:
                wait_timeout = float(timeout)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(
                    "subscription timeout must be a non-negative finite number"
                ) from exc
            if (
                not math.isfinite(wait_timeout)
                or wait_timeout < 0
                or wait_timeout > 86_400
            ):
                raise ValueError(
                    "subscription timeout must be a non-negative finite number"
                )
            timeout = wait_timeout
        if self._closed and self._queue.empty():
            raise StopIteration
        item = self._queue.get(timeout=timeout)
        if item is _CLOSED:
            raise StopIteration
        if isinstance(item, _GapNotice):
            raise EventSubscriptionGapError(
                item.dropped_events,
                item.latest_sequence,
            )
        return item

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._bus._unsubscribe(self)
        self._offer_closed()

    def _offer(self, event: SessionEvent) -> None:
        if self._closed or not self.accepts(event):
            return
        if self._gapped:
            self.dropped_events += 1
            if self._gap_notice is not None:
                self._gap_notice.dropped_events = self.dropped_events
                self._gap_notice.latest_sequence = event.sequence
            return
        try:
            self._queue.put_nowait(event)
            return
        except queue.Full:
            self._gapped = True
        discarded = 1
        while True:
            try:
                self._queue.get_nowait()
                discarded += 1
            except queue.Empty:
                break
        self.dropped_events += discarded
        notice = _GapNotice(self.dropped_events, event.sequence)
        self._gap_notice = notice
        self._queue.put_nowait(notice)

    def _offer_closed(self) -> None:
        # A pending gap is the terminal continuity result. Replacing it with a
        # close sentinel would make an overflow silent when bus shutdown races
        # the transport reader.
        if self._gapped:
            return
        try:
            self._queue.put_nowait(_CLOSED)
            return
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(_CLOSED)
        except queue.Full:
            pass

    def __enter__(self) -> "SessionSubscription":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


class SessionEventBus:
    """Thread-safe event fan-out scoped to one Agent/session instance."""

    def __init__(
        self,
        *,
        session_id: str = "",
        run_id: str = "",
        agent_id: str = "",
        replay_capacity: int = 256,
        journal_path: str | Path | None = None,
    ):
        self.session_id = str(session_id or "")
        self.run_id = str(run_id or "")
        self.agent_id = str(agent_id or "")
        if (
            not isinstance(replay_capacity, int)
            or isinstance(replay_capacity, bool)
            or replay_capacity < 0
        ):
            raise ValueError("replay_capacity must be a non-negative integer")
        capacity = replay_capacity
        self._journal = (
            _EventJournal(Path(journal_path), capacity, self.session_id)
            if journal_path is not None and capacity > 0
            else None
        )
        restored = self._journal.load() if self._journal is not None else []
        self._sequence = (
            self._journal.last_sequence
            if self._journal is not None
            else max((event.sequence for event in restored), default=0)
        )
        self._closed = False
        self._lock = threading.Lock()
        self._subscriptions: set[SessionSubscription] = set()
        self._recent: deque[SessionEvent] = deque(restored, maxlen=capacity)

    def bind_identity(self, *, run_id: str = "", agent_id: str = "") -> None:
        """Attach run/agent identity before the first public event."""
        with self._lock:
            if run_id:
                self.run_id = str(run_id)
            if agent_id:
                self.agent_id = str(agent_id)

    def publish(self, event_type: str, properties: dict[str, Any] | None = None) -> SessionEvent:
        """Publish without allowing a slow subscriber to block the Agent."""
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValueError(f"Invalid session event type: {event_type!r}")
        if properties is not None and not isinstance(properties, dict):
            raise ValueError("Session event properties must be an object")
        with self._lock:
            if self._closed:
                raise RuntimeError("Session event bus is closed")
            return self._publish_locked(event_type, properties or {})

    def checkpoint(
        self,
        snapshot_factory: Callable[[], Any],
        *,
        event_type: str,
        properties: dict[str, Any] | None = None,
    ) -> tuple[Any, SessionEvent]:
        """Capture state and append its cursor event in one publish boundary.

        The callback runs under the EventBus lock and must not call back into
        this bus. It should only copy state protected by an outer owner lock.
        """
        if not callable(snapshot_factory):
            raise ValueError("snapshot_factory must be callable")
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValueError(f"Invalid session event type: {event_type!r}")
        if properties is not None and not isinstance(properties, dict):
            raise ValueError("Session event properties must be an object")
        with self._lock:
            if self._closed:
                raise RuntimeError("Session event bus is closed")
            snapshot = copy.deepcopy(snapshot_factory())
            event = self._publish_locked(event_type, properties or {})
        return snapshot, event

    def checkpoint_with_replay(
        self,
        snapshot_factory: Callable[[], Any],
        *,
        event_type: str,
        properties: dict[str, Any] | None = None,
        replay: int = 256,
    ) -> tuple[Any, SessionEvent, list[SessionEvent]]:
        """Capture state, bounded prior events, and a cursor atomically.

        Remote attach needs events already emitted by an active run as well as
        all events after its new cursor. Keeping this operation on the event
        bus prevents a publish from falling between those two views.
        """
        if not callable(snapshot_factory):
            raise ValueError("snapshot_factory must be callable")
        if not isinstance(event_type, str) or not _EVENT_TYPE_RE.fullmatch(event_type):
            raise ValueError(f"Invalid session event type: {event_type!r}")
        if properties is not None and not isinstance(properties, dict):
            raise ValueError("Session event properties must be an object")
        if not isinstance(replay, int) or isinstance(replay, bool) or replay < 0:
            raise ValueError("replay must be a non-negative integer")
        with self._lock:
            if self._closed:
                raise RuntimeError("Session event bus is closed")
            snapshot = copy.deepcopy(snapshot_factory())
            recent = list(self._recent)[-replay:] if replay else []
            event = self._publish_locked(event_type, properties or {})
        return snapshot, event, recent

    def subscribe(
        self,
        event_types: set[str] | list[str] | tuple[str, ...] | None = None,
        *,
        max_queue: int = 256,
        replay: int = 0,
        after_event_id: str | None = None,
    ) -> SessionSubscription:
        """Create a bounded subscription with optional recent-event replay."""
        if not isinstance(replay, int) or isinstance(replay, bool) or replay < 0:
            raise ValueError("replay must be a non-negative integer")
        if replay and after_event_id is not None:
            raise ValueError("replay and after_event_id cannot be combined")
        if after_event_id is not None and (
            not isinstance(after_event_id, str)
            or not _EVENT_ID_RE.fullmatch(after_event_id)
        ):
            raise ValueError("after_event_id must be a valid event ID")
        normalized = None if event_types is None else {str(item) for item in event_types}
        if normalized is not None:
            invalid = [item for item in normalized if not _EVENT_TYPE_RE.fullmatch(item)]
            if invalid:
                raise ValueError(f"Invalid session event type: {invalid[0]!r}")
        subscription = SessionSubscription(
            self,
            event_types=normalized,
            max_queue=max_queue,
        )
        with self._lock:
            if self._closed:
                raise RuntimeError("Session event bus is closed")
            recent = list(self._recent)
            if after_event_id is not None:
                cursor_index = next(
                    (
                        index
                        for index, event in enumerate(recent)
                        if event.event_id == after_event_id
                    ),
                    None,
                )
                if cursor_index is None:
                    raise EventCursorExpiredError(after_event_id)
                recent = recent[cursor_index + 1:]
            elif replay:
                recent = recent[-max(0, replay):]
            else:
                recent = []
            # Replay must enter the queue before live fan-out can observe this
            # subscription; otherwise a concurrent publish could overtake it.
            for event in recent:
                subscription._offer(event)
            self._subscriptions.add(subscription)
        return subscription

    def close(self) -> None:
        """Dispose subscribers; the Agent owns when this lifecycle ends."""
        with self._lock:
            if self._closed:
                return
            disposed = self._publish_locked("session.disposed", {})
            self._closed = True
            subscriptions = list(self._subscriptions)
            self._subscriptions.clear()
            if self._journal is not None:
                self._journal.close()
        for subscription in subscriptions:
            subscription._closed = True
            if not subscription.accepts(disposed) or subscription._queue.empty():
                subscription._offer_closed()

    def _publish_locked(
        self,
        event_type: str,
        properties: dict[str, Any],
    ) -> SessionEvent:
        self._sequence += 1
        event = SessionEvent(
            type=event_type,
            properties=_safe_event_properties(properties),
            sequence=self._sequence,
            timestamp=time.time(),
            session_id=self.session_id,
            run_id=self.run_id,
            agent_id=self.agent_id,
            event_id=uuid.uuid4().hex,
        )
        self._recent.append(event)
        if self._journal is not None:
            self._journal.append(event, list(self._recent))
        # Queue fan-out stays in the sequence critical section so two
        # publisher threads cannot deliver event N+1 before event N.
        # _offer() is strictly non-blocking and each queue is bounded.
        for subscription in self._subscriptions:
            subscription._offer(event)
        return event

    def recent(self, limit: int = 100) -> list[SessionEvent]:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("limit must be an integer")
        bounded = max(0, limit)
        if bounded == 0:
            return []
        with self._lock:
            return list(self._recent)[-bounded:]

    def _unsubscribe(self, subscription: SessionSubscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)


_CURRENT_EVENT_BUS: ContextVar[SessionEventBus | None] = ContextVar(
    "nz_coder_session_event_bus",
    default=None,
)


@contextmanager
def scoped_session_event_bus(bus: SessionEventBus):
    """Bind the active session bus for tools and optional adapters."""
    token = _CURRENT_EVENT_BUS.set(bus)
    try:
        yield bus
    finally:
        _CURRENT_EVENT_BUS.reset(token)


def current_session_event_bus() -> SessionEventBus | None:
    return _CURRENT_EVENT_BUS.get()


def publish_session_event(
    event_type: str,
    properties: dict[str, Any] | None = None,
) -> SessionEvent | None:
    bus = current_session_event_bus()
    return bus.publish(event_type, properties) if bus is not None else None


def encode_sse(event: SessionEvent | dict[str, Any]) -> str:
    """Encode one event as a Server-Sent Events data frame."""
    payload = event.to_dict() if isinstance(event, SessionEvent) else dict(event)
    event_id = event.event_id if isinstance(event, SessionEvent) else ""
    prefix = f"id: {event_id}\n" if event_id else ""
    return prefix + "data: " + json.dumps(
        payload,
        ensure_ascii=False,
        default=str,
        allow_nan=False,
    ) + "\n\n"


def iter_sse(
    subscription: SessionSubscription,
    *,
    heartbeat_seconds: float = 10.0,
) -> Iterator[str]:
    """Yield SSE frames with InfCode-style connected/heartbeat control events."""
    yield encode_sse({"type": "server.connected", "properties": {}})
    try:
        timeout = float(heartbeat_seconds)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("heartbeat_seconds must be a positive finite number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("heartbeat_seconds must be a positive finite number")
    timeout = max(0.05, timeout)
    while True:
        try:
            event = subscription.get(timeout=timeout)
        except queue.Empty:
            yield encode_sse({"type": "server.heartbeat", "properties": {}})
        except EventSubscriptionGapError as exc:
            yield encode_sse({
                "type": "server.event_gap",
                "properties": exc.to_dict(),
            })
            return
        except StopIteration:
            return
        else:
            yield encode_sse(event)
