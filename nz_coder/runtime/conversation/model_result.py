"""Stable provider-neutral result envelope for one model turn."""
from __future__ import annotations

from dataclasses import dataclass, field
import math


@dataclass
class LLMResult:
    """Normalized buffered or streaming model outcome consumed by AgentRunner."""

    content: str | None = None
    tool_calls: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    diagnostic: str | None = None
    needs_compaction: bool = False
    compaction_error: str = ""
    aborted: bool = False
    duration_ms: float = 0.0
    first_token_ms: float | None = None
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    provider_reported_cost: float | None = None
    cost: float = 0.0
    cost_known: bool = False
    finish_reason: str = ""
    tools_executed_in_stream: bool = False
    tool_outcome: str = ""
    post_tool_stream_error: str = ""
    assistant_error: dict | None = None
    stream_tool_wait_ms: float = 0.0


def _normalize_llm_result_metrics(result: LLMResult) -> tuple[str, ...]:
    """Repair untrusted ModelPort metrics without discarding valid content.

    Production gateways already normalize Provider usage, but RuntimeServices
    deliberately accepts custom ModelPort implementations.  This final typed
    boundary prevents NaN, infinity, booleans, strings, or negative values
    from crashing TokenUsage construction or poisoning persisted telemetry.
    The result is mutated in place because downstream message/session code
    already treats the model envelope as the one mutable turn record.
    """
    if not isinstance(result, LLMResult):
        raise TypeError("ModelPort must return LLMResult")

    repaired: list[str] = []
    token_fields = (
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    for name in token_fields:
        value = getattr(result, name)
        normalized = _finite_nonnegative_int(value, fallback=0)
        if (
            normalized != value
            or not isinstance(value, int)
            or isinstance(value, bool)
        ):
            setattr(result, name, normalized)
            repaired.append(name)

    total = _finite_nonnegative_int(result.total_tokens, fallback=0)
    minimum_total = sum(getattr(result, name) for name in token_fields)
    total = max(total, minimum_total)
    if (
        total != result.total_tokens
        or not isinstance(result.total_tokens, int)
        or isinstance(result.total_tokens, bool)
    ):
        result.total_tokens = total
        repaired.append("total_tokens")

    duration = _finite_nonnegative_float(result.duration_ms, fallback=0.0)
    if duration != result.duration_ms or isinstance(result.duration_ms, bool):
        result.duration_ms = duration
        repaired.append("duration_ms")

    if result.first_token_ms is not None:
        first_token = _finite_nonnegative_float(
            result.first_token_ms,
            fallback=None,
        )
        if first_token != result.first_token_ms or isinstance(
            result.first_token_ms,
            bool,
        ):
            result.first_token_ms = first_token
            repaired.append("first_token_ms")

    attempts = _finite_positive_int(result.attempts, fallback=1)
    if (
        attempts != result.attempts
        or not isinstance(result.attempts, int)
        or isinstance(result.attempts, bool)
    ):
        result.attempts = attempts
        repaired.append("attempts")

    if result.provider_reported_cost is not None:
        provider_cost = _finite_nonnegative_float(
            result.provider_reported_cost,
            fallback=None,
        )
        if provider_cost != result.provider_reported_cost or isinstance(
            result.provider_reported_cost,
            bool,
        ):
            result.provider_reported_cost = provider_cost
            repaired.append("provider_reported_cost")

    cost = _finite_nonnegative_float(result.cost, fallback=0.0)
    cost_known = result.cost_known is True
    if cost != result.cost or isinstance(result.cost, bool):
        cost_known = False
        repaired.append("cost")
    if cost_known != result.cost_known:
        repaired.append("cost_known")
    result.cost = cost
    result.cost_known = cost_known
    return tuple(dict.fromkeys(repaired))


def _finite_nonnegative_int(value, *, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if not math.isfinite(value) or value < 0 or not float(value).is_integer():
        return fallback
    return int(value)


def _finite_positive_int(value, *, fallback: int) -> int:
    normalized = _finite_nonnegative_int(value, fallback=fallback)
    return normalized if normalized > 0 else fallback


def _finite_nonnegative_float(
    value,
    *,
    fallback: float | None,
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return fallback
    return numeric
