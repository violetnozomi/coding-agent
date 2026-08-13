"""Tests for deterministic models.dev usage-cost calculation."""
from __future__ import annotations

import pytest

from nz_coder.providers.pricing import calculate_usage_cost
from nz_coder.providers.registry import ModelPricing


def _pricing() -> ModelPricing:
    return ModelPricing(
        input=1.0,
        output=4.0,
        cache_read=0.1,
        cache_write=1.25,
        context_over_200k=ModelPricing(
            input=2.0,
            output=6.0,
            cache_read=0.2,
            cache_write=2.0,
        ),
    )


def test_unknown_pricing_omits_cost_instead_of_claiming_zero():
    assert calculate_usage_cost(
        None,
        input_tokens=100,
        output_tokens=20,
    ) is None


def test_pricing_charges_normalized_input_output_reasoning_and_cache():
    cost = calculate_usage_cost(
        _pricing(),
        input_tokens=100_000,
        output_tokens=10_000,
        reasoning_tokens=5_000,
        cache_read_tokens=20_000,
        cache_write_tokens=1_000,
    )

    assert cost == pytest.approx(0.16325)


def test_pricing_switches_to_over_200k_tier_using_input_plus_cache_read():
    cost = calculate_usage_cost(
        _pricing(),
        input_tokens=190_000,
        output_tokens=10_000,
        reasoning_tokens=5_000,
        cache_read_tokens=20_000,
        cache_write_tokens=1_000,
    )

    assert cost == pytest.approx(0.476)


def test_authoritative_zero_rates_remain_known_zero():
    cost = calculate_usage_cost(
        ModelPricing(input=0.0, output=0.0),
        input_tokens=1_000,
        output_tokens=1_000,
    )

    assert cost == 0.0
