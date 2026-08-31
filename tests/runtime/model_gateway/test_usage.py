"""Literal usage and cost normalization contracts for every Provider path."""
from __future__ import annotations

from nz_coder.providers.registry import ModelPricing
from nz_coder.runtime.model_gateway.usage import (
    NormalizedUsage,
    extract_provider_reported_cost,
    normalize_usage,
    resolve_usage_cost,
)


def test_openai_usage_separates_cache_and_reasoning_from_billable_buckets() -> None:
    """Cached input and reasoning must not be double-counted."""
    usage = normalize_usage({
        "prompt_tokens": 120,
        "completion_tokens": 50,
        "total_tokens": 170,
        "prompt_tokens_details": {"cached_tokens": 20},
        "completion_tokens_details": {"reasoning_tokens": 10},
    })

    assert usage == NormalizedUsage(
        input_tokens=100,
        output_tokens=40,
        total_tokens=170,
        reasoning_tokens=10,
        cache_read_tokens=20,
    )


def test_anthropic_usage_preserves_explicit_uncached_and_cache_write() -> None:
    """Explicit uncached input wins over subtraction-derived input."""
    usage = normalize_usage({
        "input_tokens": 70,
        "output_tokens": 30,
        "uncached_input_tokens": 50,
        "cache_read_input_tokens": 10,
        "cache_creation_input_tokens": 10,
    })

    assert usage.input_tokens == 50
    assert usage.output_tokens == 30
    assert usage.total_tokens == 100
    assert usage.cache_read_tokens == 10
    assert usage.cache_write_tokens == 10


def test_usage_total_covers_all_mutually_exclusive_buckets() -> None:
    """Anthropic cache buckets must count toward context pressure and totals."""
    usage = normalize_usage({
        "input_tokens": 31,
        "uncached_input_tokens": 31,
        "output_tokens": 12,
        "total_tokens": 43,
        "cache_read_input_tokens": 11,
        "cache_creation_input_tokens": 4,
    })

    assert usage.total_tokens == 58


def test_usage_rejects_negative_boolean_and_malformed_values() -> None:
    """Untrusted Provider accounting cannot create negative or boolean tokens."""
    usage = normalize_usage({
        "input_tokens": -10,
        "output_tokens": True,
        "total_tokens": "100",
    })

    assert usage == NormalizedUsage()


def test_usage_rejects_non_finite_provider_token_values() -> None:
    """NaN/Inf in an OpenAI-compatible usage body must not break completion."""
    usage = normalize_usage({
        "input_tokens": float("nan"),
        "output_tokens": float("inf"),
        "total_tokens": float("-inf"),
        "cache_read_input_tokens": float("nan"),
    })

    assert usage == NormalizedUsage()


def test_provider_reported_cost_precedes_registry_price() -> None:
    """An authoritative gateway charge must override estimated registry cost."""
    usage = NormalizedUsage(input_tokens=1000, output_tokens=500)
    pricing = ModelPricing(input=1.0, output=2.0)

    cost, source = resolve_usage_cost(
        usage,
        pricing,
        provider_reported_cost=0.123,
    )

    assert cost == 0.123
    assert source == "provider"


def test_registry_cost_is_used_when_provider_does_not_report_one() -> None:
    """Known model pricing must produce the same deterministic fallback."""
    usage = NormalizedUsage(input_tokens=1000, output_tokens=500)
    pricing = ModelPricing(input=1.0, output=2.0)

    cost, source = resolve_usage_cost(usage, pricing)

    assert cost == 0.002
    assert source == "registry"


def test_nested_provider_cost_is_bounded_and_parsed() -> None:
    """OpenRouter-style nested cost is recognized without accepting NaN."""
    assert extract_provider_reported_cost({
        "provider_metadata": {"openrouter": {"usage": {"cost": "0.045"}}},
    }) == 0.045
    assert extract_provider_reported_cost({"cost": "nan"}) is None
