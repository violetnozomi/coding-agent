"""Bounded raw-response bridge for legacy callers during Gateway migration."""
from __future__ import annotations

import queue
import threading
import time
from typing import Callable


def raw_completion_with_timeout(
    client,
    *,
    provider,
    timeout_seconds: float,
    cancel_event: threading.Event | None,
    cancelled_error: Callable[[str], BaseException],
    timeout_error: Callable[[str], BaseException],
    kwargs: dict,
):
    """Run one legacy adapter call while keeping SDK access in this boundary."""
    if cancel_event is not None and cancel_event.is_set():
        raise cancelled_error("subagent cancelled by parent")

    events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
    settled = threading.Event()

    def invoke() -> None:
        try:
            if provider is None:
                value = client.chat.completions.create(**kwargs)
            else:
                value = provider.create_completion(client, **kwargs)
        except BaseException as exc:
            item = ("error", exc)
        else:
            item = ("result", value)
        if settled.is_set():
            return
        try:
            events.put_nowait(item)
        except queue.Full:
            pass

    threading.Thread(target=invoke, name="nz-legacy-model-call", daemon=True).start()
    deadline = (
        float("inf")
        if timeout_seconds <= 0
        else time.monotonic() + timeout_seconds
    )
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
                raise cancelled_error("subagent cancelled by parent")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise timeout_error(
                    f"subagent API call timed out after {timeout_seconds}s"
                )
            try:
                kind, value = events.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if kind == "error":
                raise value
            return value
    finally:
        settled.set()
