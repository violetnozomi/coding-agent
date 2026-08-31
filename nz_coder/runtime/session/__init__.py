"""Session-owned transcript, storage, and runtime coordination."""
from __future__ import annotations

from importlib import import_module


_EXPORTS = {
    "Session": "model",
    "SessionIdentity": "model",
    "SessionSnapshot": "model",
    "SessionStatus": "model",
}

__all__ = [
    "Session",
    "SessionIdentity",
    "SessionSnapshot",
    "SessionStatus",
]


def __getattr__(name: str):  # noqa: ANN202
    """Resolve Session model exports only when callers request them."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy Session exports in interactive discovery."""
    return sorted(set(globals()).union(__all__))
