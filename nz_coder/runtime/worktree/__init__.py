"""Worktree helpers for isolated child-agent execution."""
from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "Worktree": "models",
    "WorktreeError": "manager",
    "WorktreeManager": "manager",
}

__all__ = ["Worktree", "WorktreeError", "WorktreeManager"]


def __getattr__(name: str):  # noqa: ANN202
    """Resolve worktree exports without importing manager services eagerly."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy worktree exports in interactive discovery."""
    return sorted(set(globals()).union(__all__))
