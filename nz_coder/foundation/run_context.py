"""Dependency-neutral binding for the immutable settings of the active Run."""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_ACTIVE_RUN_SETTINGS: ContextVar[Any | None] = ContextVar(
    "nz_coder_active_run_settings", default=None,
)


def active_run_settings_object() -> Any | None:
    """Return the bound settings object without importing the runtime layer."""
    return _ACTIVE_RUN_SETTINGS.get()


def bind_run_settings_object(settings: Any) -> Token:
    """Bind a runtime-owned immutable settings object to this context."""
    return _ACTIVE_RUN_SETTINGS.set(settings)


def reset_run_settings_object(token: Token) -> None:
    """Restore the previous settings binding."""
    _ACTIVE_RUN_SETTINGS.reset(token)


__all__ = [
    "active_run_settings_object",
    "bind_run_settings_object",
    "reset_run_settings_object",
]
