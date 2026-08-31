"""Provider-neutral token and cost normalization for all model calls."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from nz_coder.providers.pricing import calculate_usage_cost
from nz_coder.providers.registry import ModelPricing


@dataclass(frozen=True)
class NormalizedUsage:
    """Mutually exclusive token buckets persisted by Agent runtime consumers."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"NormalizedUsage {name} must be a non-negative integer")

    def as_legacy_dict(self) -> dict[str, int]:
        """Project into the stable keys used by Session and trace records."""
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "total": self.total_tokens,
            "reasoning": self.reasoning_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
        }


def _field(owner: Any, name: str) -> Any:
    if isinstance(owner, dict):
        return owner.get(name)
    return getattr(owner, name, None)


def _token(owner: Any, *names: str) -> int:
    for name in names:
        value = _field(owner, name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                continue
            return max(0, int(value))
    return 0


def _optional_token(owner: Any, *names: str) -> int | None:
    for name in names:
        value = _field(owner, name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, float) and not math.isfinite(value):
                continue
            return max(0, int(value))
    return None


def normalize_usage(value: Any) -> NormalizedUsage:
    """Normalize OpenAI, Anthropic, Gemini, and compatible usage shapes."""
    if value is None:
        return NormalizedUsage()
    raw_input = _token(value, "prompt_tokens", "input_tokens")
    raw_output = _token(value, "completion_tokens", "output_tokens")
    provider_total = _token(value, "total_tokens")
    prompt_details = _field(value, "prompt_tokens_details") or {}
    input_details = _field(value, "input_tokens_details") or {}
    completion_details = _field(value, "completion_tokens_details") or {}
    output_details = _field(value, "output_tokens_details") or {}
    reasoning = (
        _token(value, "reasoning_tokens")
        or _token(completion_details, "reasoning_tokens")
        or _token(output_details, "reasoning_tokens")
    )
    cache_read = (
        _token(value, "cache_read_input_tokens", "cached_input_tokens")
        or _token(prompt_details, "cached_tokens")
        or _token(input_details, "cached_tokens")
    )
    cache_write = _token(
        value,
        "cache_creation_input_tokens",
        "cache_write_input_tokens",
    )
    explicit_uncached = _optional_token(value, "uncached_input_tokens")
    input_tokens = (
        explicit_uncached
        if explicit_uncached is not None
        else max(0, raw_input - cache_read - cache_write)
    )
    output_tokens = max(0, raw_output - reasoning)
    # ``total_tokens`` is not consistent across native APIs.  OpenAI-style
    # totals include cached input and reasoning, while Anthropic reports those
    # in separate buckets.  Never let the aggregate undercount the mutually
    # exclusive buckets used by context pressure and cost accounting.
    bucket_total = (
        input_tokens
        + output_tokens
        + reasoning
        + cache_read
        + cache_write
    )
    total = max(provider_total, bucket_total)
    return NormalizedUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total,
        reasoning_tokens=reasoning,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )


def _finite_cost(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0 or result > 1_000_000_000:
        return None
    return result


def extract_provider_reported_cost(value: Any) -> float | None:
    """Extract an authoritative Provider or gateway USD charge when present."""
    if value is None:
        return None
    direct = _finite_cost(_field(value, "cost"))
    if direct is not None:
        return direct

    def nested(*names: str) -> Any:
        current = value
        for name in names:
            current = _field(current, name)
            if current is None:
                return None
        return current

    for path in (
        ("cost_details", "upstream_inference_cost"),
        ("costDetails", "upstreamInferenceCost"),
        ("raw", "cost_details", "upstream_inference_cost"),
        ("provider_metadata", "openrouter", "usage", "cost"),
        ("providerMetadata", "openrouter", "usage", "cost"),
        ("provider_metadata", "gateway", "marketCost"),
        ("providerMetadata", "gateway", "marketCost"),
    ):
        cost = _finite_cost(nested(*path))
        if cost is not None:
            return cost
    return None


def resolve_usage_cost(
    usage: NormalizedUsage,
    pricing: ModelPricing | None,
    provider_reported_cost: float | None = None,
) -> tuple[float | None, str | None]:
    """Resolve authoritative Provider cost before deterministic registry cost."""
    reported = _finite_cost(provider_reported_cost)
    if reported is not None:
        return reported, "provider"
    calculated = calculate_usage_cost(
        pricing,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )
    return (calculated, "registry") if calculated is not None else (None, None)
