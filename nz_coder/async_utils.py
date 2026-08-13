"""Async helpers used by the NZ-Coder runtime."""
from __future__ import annotations

import asyncio
import threading
from contextvars import copy_context
from typing import Awaitable, Callable, TypeVar


_T = TypeVar("_T")


async def to_thread_settled(
    function: Callable[..., _T],
    /,
    *args,
    cancel_callback: Callable[[], object] | None = None,
    **kwargs,
) -> _T:
    """Run a thread call without letting task cancellation orphan its effects."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancel_error:
        if cancel_callback is not None:
            try:
                cancel_callback()
            except Exception:
                pass
        # A running Python thread cannot be force-cancelled. Keep awaiting the
        # worker so callers may roll back or retire its effects before they
        # report cancellation. Repeated task.cancel() calls must not bypass
        # this cleanup boundary.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
        raise cancel_error


def start_background_coro(coro: Awaitable[object]) -> threading.Thread:
    """Run a coroutine in a detached helper thread."""

    context = copy_context()

    def runner() -> None:
        asyncio.run(coro)

    thread = threading.Thread(target=lambda: context.run(runner), daemon=True)
    thread.start()
    return thread
