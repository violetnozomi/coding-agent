"""Ordered host-neutral middleware around Agent runtime boundaries."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, TypeVar, runtime_checkable


T = TypeVar("T")


@runtime_checkable
class RuntimeMiddleware(Protocol):
    """Optional async hooks around run, model, and tool-batch boundaries."""

    async def before_run(self, context: object) -> None: ...
    async def after_run(self, context: object, result: object) -> None: ...
    async def on_run_error(self, context: object, error: BaseException) -> None: ...


@dataclass(frozen=True)
class MiddlewarePipeline:
    """Compose middleware with forward entry and reverse exit semantics."""

    middleware: tuple[object, ...] = ()

    def __init__(self, middleware=()) -> None:
        object.__setattr__(self, "middleware", tuple(middleware))

    async def run(
        self,
        boundary: str,
        context: object,
        execute: Callable[[], Awaitable[T]],
    ) -> T:
        """Execute one boundary and preserve the original operation failure."""
        before = f"before_{boundary}"
        after = f"after_{boundary}"
        on_error = f"on_{boundary}_error"
        entered: list[object] = []
        try:
            for item in self.middleware:
                hook = getattr(item, before, None)
                if callable(hook):
                    await _await_if_needed(hook(context))
                entered.append(item)
            result = await execute()
            for item in reversed(entered):
                hook = getattr(item, after, None)
                if callable(hook):
                    await _await_if_needed(hook(context, result))
            return result
        except BaseException as error:
            for item in reversed(entered):
                hook = getattr(item, on_error, None)
                if callable(hook):
                    try:
                        await _await_if_needed(hook(context, error))
                    except BaseException:
                        # The operation's original failure is authoritative.
                        continue
            raise


async def _await_if_needed(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


__all__ = ["MiddlewarePipeline", "RuntimeMiddleware"]
