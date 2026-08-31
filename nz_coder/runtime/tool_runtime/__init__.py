"""Tool scheduling, policy, observation, and execution pipeline services."""
from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "ProductionToolRuntime": (
        "nz_coder.runtime.tool_runtime.pipeline",
        "ProductionToolRuntime",
    ),
    "execute_concurrent": (
        "nz_coder.runtime.tool_runtime.scheduler",
        "_execute_concurrent",
    ),
    "execute_concurrent_async": (
        "nz_coder.runtime.tool_runtime.scheduler",
        "_execute_concurrent_async",
    ),
    "execute_scheduled": (
        "nz_coder.runtime.tool_runtime.scheduler",
        "_execute_scheduled",
    ),
    "execute_scheduled_async": (
        "nz_coder.runtime.tool_runtime.scheduler",
        "_execute_scheduled_async",
    ),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str):  # noqa: ANN202
    """Resolve compatibility exports without importing the pipeline eagerly."""
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazily exported names in interactive discovery."""
    return sorted(set(globals()).union(__all__))
