"""Bounded consumption of blocking Provider streams."""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable, Iterator


class ProviderStreamTimeout(TimeoutError):
    """Local guard expiry, distinct from a transport/SDK timeout."""

    def __init__(self, kind: str, seconds: float):
        self.timeout_kind = kind
        super().__init__(f"Provider {kind} timeout after {seconds:g}s")


def close_stream(stream) -> None:
    """Best-effort close at the stream ownership boundary."""
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def iter_stream_with_timeouts(
    stream,
    *,
    idle_timeout_seconds: float,
    hard_timeout_seconds: float,
    cancelled: Callable[[], bool] | None = None,
) -> Iterator[object]:
    """Guard active Provider waits; hard includes all elapsed consumer time."""
    events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=256)
    stopped = threading.Event()
    consumed = threading.Event()

    def publish(kind: str, value: object) -> bool:
        while not stopped.is_set():
            try:
                events.put((kind, value), timeout=0.05)
                return True
            except queue.Full:
                continue
        return False

    def consume() -> None:
        try:
            for item in stream:
                consumed.clear()
                if not publish("item", item):
                    return
                while not stopped.is_set() and not consumed.wait(0.05):
                    continue
        except BaseException as exc:
            publish("error", exc)
        finally:
            publish("done", None)

    threading.Thread(target=consume, name="nz-provider-stream", daemon=True).start()
    started = time.monotonic()
    last_activity = started
    idle = max(0.0, float(idle_timeout_seconds))
    hard = max(0.001, float(hard_timeout_seconds))
    try:
        while True:
            now = time.monotonic()
            if cancelled is not None and cancelled():
                return
            if now - started >= hard:
                raise ProviderStreamTimeout("hard", hard)
            if idle and now - last_activity >= idle:
                raise ProviderStreamTimeout("idle", idle)
            wait_for = min(0.05, max(0.001, hard - (now - started)))
            if idle:
                wait_for = min(
                    wait_for,
                    max(0.001, idle - (now - last_activity)),
                )
            try:
                kind, value = events.get(timeout=wait_for)
            except queue.Empty:
                continue
            if kind == "item":
                last_activity = time.monotonic()
                try:
                    yield value
                finally:
                    # The reader is waiting for consumed, not for the Provider.
                    # Start the next idle window only when the consumer returns.
                    # Do not move started: hard remains an absolute deadline.
                    last_activity = time.monotonic()
                    consumed.set()
            elif kind == "error":
                raise value
            else:
                return
    finally:
        stopped.set()
        consumed.set()
        close_stream(stream)
