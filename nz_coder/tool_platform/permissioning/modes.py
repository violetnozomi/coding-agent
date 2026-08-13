"""Permission mode definitions."""
from __future__ import annotations


MODES = ("default", "auto", "plan", "acceptEdits")


def normalize_mode(mode: str | None, default: str = "default") -> str:
    """Return a supported permission mode."""
    if mode in MODES:
        return str(mode)
    return default
