"""Attempt-scoped private buffering and publication for Provider streams."""
from __future__ import annotations

import time
from typing import Any


class StreamAttemptBuffer:
    """Own one Provider attempt and fence all callbacks by its generation.

    Guarded output uses the same buffer with ``publish=False``.  In that mode
    raw provider data never reaches Message/Part state, events, checkpoints, or
    UI callbacks; the Runner later materializes only the guardrail-approved
    completed result.
    """

    def __init__(
        self,
        host,
        message_part: dict | None,
        *,
        processor=None,
        publish: bool = True,
        delta_interval_seconds: float = 0.05,
        delta_min_chars: int = 256,
    ) -> None:
        self.host = host
        self.message_part = message_part
        self.processor = processor
        self.publish = bool(publish)
        self.text: list[str] = []
        self._pending_text: list[str] = []
        self._pending_chars = 0
        self._delta_interval_seconds = min(
            0.08,
            max(0.03, float(delta_interval_seconds)),
        )
        self._delta_min_chars = max(1, int(delta_min_chars))
        self._last_delta_flush = time.monotonic()
        self._has_flushed_delta = False
        self.reasoning: list[str] = []
        self._pending_reasoning_chars = 0
        self._last_reasoning_flush = time.monotonic()
        self._has_flushed_reasoning = False
        self.tools: dict[int, dict] = {}
        self._identity = self._capture_identity()

    @property
    def content(self) -> str:
        return "".join(self.text)

    @property
    def reasoning_content(self) -> str:
        return "".join(self.reasoning)

    def is_active(self) -> bool:
        if self.message_part is None:
            return True
        checker = getattr(self.host, "_message_part_matches", None)
        return bool(callable(checker) and checker(self.message_part, self._identity))

    def append_text(self, delta: str) -> bool:
        value = str(delta or "")
        if not value:
            return False
        lock = self.message_part.get("lock") if isinstance(self.message_part, dict) else None
        if lock is None:
            if not self.is_active():
                return False
            self.text.append(value)
            self._pending_text.append(value)
            self._pending_chars += len(value)
            return True
        with lock:
            if not self.is_active():
                return False
            self.text.append(value)
            self._pending_text.append(value)
            self._pending_chars += len(value)
            return True

    def flush_text(self, *, force: bool = False) -> int:
        """Publish one coalesced text delta and its matching Part snapshot."""
        if (
            not self.publish
            or self.message_part is None
            or not self._pending_text
        ):
            return 0
        now = time.monotonic()
        if (
            not force
            and self._has_flushed_delta
            and self._pending_chars < self._delta_min_chars
            and now - self._last_delta_flush < self._delta_interval_seconds
        ):
            return 0
        lock = self.message_part.get("lock")
        with lock:
            if not self.is_active() or not self._pending_text:
                return 0
            delta = "".join(self._pending_text)
            self._pending_text.clear()
            self._pending_chars = 0
            self.host._emit_message_delta(self.message_part, delta)
            if self.processor is not None:
                self.processor.stream_text(
                    self.content,
                    part_id=self.message_part["part_id"],
                    run_id=self.message_part["run_id"],
                    attempt_id=self.message_part["attempt_id"],
                    generation_id=self.message_part["generation_id"],
                    generation=self.message_part["generation"],
                    version=self.message_part["version"],
                )
            self._last_delta_flush = now
            self._has_flushed_delta = True
            return len(delta)

    def append_reasoning(self, delta: str) -> bool:
        value = str(delta or "")
        if not value:
            return False
        lock = self.message_part.get("lock") if isinstance(self.message_part, dict) else None
        if lock is None:
            if not self.is_active():
                return False
            self.reasoning.append(value)
            self._pending_reasoning_chars += len(value)
            return True
        with lock:
            if not self.is_active():
                return False
            self.reasoning.append(value)
            self._pending_reasoning_chars += len(value)
            return True

    def flush_reasoning(self, *, force: bool = False) -> int:
        """Publish one coalesced reasoning snapshot at a bounded cadence."""
        if (
            not self.publish
            or self.processor is None
            or not self._pending_reasoning_chars
        ):
            return 0
        now = time.monotonic()
        if (
            not force
            and self._has_flushed_reasoning
            and self._pending_reasoning_chars < self._delta_min_chars
            and now - self._last_reasoning_flush < self._delta_interval_seconds
        ):
            return 0
        lock = self.message_part.get("lock") if self.message_part is not None else None
        if lock is None:
            if not self.is_active():
                return 0
            return self._publish_reasoning(now)
        with lock:
            if not self.is_active() or not self._pending_reasoning_chars:
                return 0
            return self._publish_reasoning(now)

    def _publish_reasoning(self, now: float) -> int:
        pending = self._pending_reasoning_chars
        self.processor.add_reasoning(self.reasoning_content)
        self._pending_reasoning_chars = 0
        self._last_reasoning_flush = now
        self._has_flushed_reasoning = True
        return pending

    def update_tool(self, index: int, value: dict) -> bool:
        selected = max(0, int(index))
        lock = self.message_part.get("lock") if isinstance(self.message_part, dict) else None
        if lock is None:
            if not self.is_active():
                return False
            self.tools[selected] = dict(value)
            return True
        with lock:
            if not self.is_active():
                return False
            self.tools[selected] = dict(value)
            # Tool envelopes are private Provider state. The first public
            # ToolPart is created only after repair, guardrail, and admission.
            return True

    def reset_after_retry(self, reason: str) -> None:
        """Remove the failed attempt, rotate identity, and clear private data."""
        if self.message_part is not None:
            self.host._discard_message_part(self.message_part, reason)
        self.text.clear()
        self._pending_text.clear()
        self._pending_chars = 0
        self._last_delta_flush = time.monotonic()
        self._has_flushed_delta = False
        self.reasoning.clear()
        self._pending_reasoning_chars = 0
        self._last_reasoning_flush = time.monotonic()
        self._has_flushed_reasoning = False
        self.tools.clear()
        self._identity = self._capture_identity()

    def _capture_identity(self) -> dict[str, Any] | None:
        if self.message_part is None:
            return None
        capture = getattr(self.host, "_message_part_identity", None)
        return capture(self.message_part) if callable(capture) else None


class StreamCheckpointScheduler:
    """Coalesce transient stream mutations into bounded durable checkpoints."""

    def __init__(
        self,
        host,
        messages: list | None,
        *,
        enabled: bool,
        interval_seconds: float,
        min_chars: int,
        active_check=None,
    ) -> None:
        self.host = host
        self.messages = messages
        self.enabled = bool(enabled and isinstance(messages, list))
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.min_chars = max(1, int(min_chars))
        self._pending = False
        self._chars = 0
        self._last_flush = time.monotonic()
        self._active_check = active_check

    def note(self, chars: int = 1) -> bool:
        """Record one mutation and flush only at a time/size boundary."""
        if not self.enabled:
            return False
        self._pending = True
        self._chars += max(1, int(chars))
        now = time.monotonic()
        if (
            self._chars < self.min_chars
            and now - self._last_flush < self.interval_seconds
        ):
            return False
        return self.flush()

    def flush(self, *, force: bool = False) -> bool:
        """Persist the latest state once; ``force`` also commits removals."""
        if callable(self._active_check) and not self._active_check():
            self._pending = False
            self._chars = 0
            return False
        if not self.enabled or (not self._pending and not force):
            return False
        self.host._checkpoint_messages(self.messages, "running")
        self._pending = False
        self._chars = 0
        self._last_flush = time.monotonic()
        return True
