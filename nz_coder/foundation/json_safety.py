"""Strict-JSON normalization shared by durable and wire protocols."""
from __future__ import annotations

import math
from typing import Any


def json_safe_value(
    value: Any,
    *,
    max_depth: int = 32,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> Any:
    """Detach arbitrary values into a finite, acyclic JSON-compatible tree."""
    if _depth >= max(1, max_depth):
        return "[maximum JSON nesting depth reached]"
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (dict, list, tuple, set, frozenset)):
        seen = _seen if _seen is not None else set()
        identity = id(value)
        if identity in seen:
            return "[circular reference]"
        seen.add(identity)
        try:
            if isinstance(value, dict):
                return {
                    str(key): json_safe_value(
                        item,
                        max_depth=max_depth,
                        _seen=seen,
                        _depth=_depth + 1,
                    )
                    for key, item in value.items()
                }
            return [
                json_safe_value(
                    item,
                    max_depth=max_depth,
                    _seen=seen,
                    _depth=_depth + 1,
                )
                for item in value
            ]
        finally:
            seen.remove(identity)
    try:
        return str(value)
    except Exception:
        return f"[{type(value).__name__} is not serializable]"


def reject_nonstandard_json_constant(value: str) -> None:
    """JSON parser hook that rejects Python's NaN/Infinity extensions."""
    raise ValueError(f"non-standard JSON numeric constant: {value}")


__all__ = ["json_safe_value", "reject_nonstandard_json_constant"]
