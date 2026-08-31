"""Safe token accounting reconstructed from durable assistant history."""
from __future__ import annotations

import math


def last_assistant_usage_total(messages: list[dict]) -> int:
    """Return finite usage after the latest valid compaction boundary."""
    compacted_at = max(
        (
            created_at
            for message in messages
            if isinstance(message, dict)
            and isinstance((marker := message.get("_nz_compaction")), dict)
            and (
                created_at := _finite_nonnegative_float(marker.get("created_at"))
            ) is not None
        ),
        default=0.0,
    )
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if compacted_at:
            timestamp = _finite_nonnegative_float(message.get("_timestamp"))
            if timestamp is None or timestamp <= compacted_at:
                continue
        usage = message.get("_nz_usage")
        if not isinstance(usage, dict):
            return 0
        total = _finite_nonnegative_int(usage.get("total"))
        if total is not None:
            return total
        input_tokens = _finite_nonnegative_int(usage.get("input"))
        output_tokens = _finite_nonnegative_int(usage.get("output"))
        if input_tokens is None or output_tokens is None:
            return 0
        return input_tokens + output_tokens
    return 0


def _finite_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _finite_nonnegative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return None
    return numeric


__all__ = ["last_assistant_usage_total"]
