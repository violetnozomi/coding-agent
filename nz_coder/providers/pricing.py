"""Deterministic models.dev token-cost calculation for persisted usage."""
from __future__ import annotations

import math

from nz_coder.providers.registry import ModelPricing


def calculate_usage_cost(
    pricing: ModelPricing | None,
    *,
    input_tokens: int,
    output_tokens: int,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Return USD cost, or ``None`` when no authoritative price is known."""
    if pricing is None:
        return None
    input_count = _tokens(input_tokens)
    output_count = _tokens(output_tokens)
    reasoning_count = _tokens(reasoning_tokens)
    cache_read_count = _tokens(cache_read_tokens)
    cache_write_count = _tokens(cache_write_tokens)
    selected = (
        pricing.context_over_200k
        if pricing.context_over_200k is not None
        and input_count + cache_read_count > 200_000
        else pricing
    )
    # NZ stores output excluding reasoning after A096, matching InfCode's
    # normalized token contract. Reasoning is billed at the output rate.
    cost = (
        input_count * selected.input
        + output_count * selected.output
        + reasoning_count * selected.output
        + cache_read_count * selected.cache_read
        + cache_write_count * selected.cache_write
    ) / 1_000_000
    return cost if math.isfinite(cost) and cost >= 0 else None


def _tokens(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
