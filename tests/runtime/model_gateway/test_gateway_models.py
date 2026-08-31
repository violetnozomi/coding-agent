"""Behavior contracts for Provider-neutral model call envelopes."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import math

import pytest

from nz_coder.runtime.model_gateway.models import (
    ModelCall,
    ModelCallOutcome,
    ModelCallPurpose,
    ModelCallStatus,
    ModelStreamEvent,
)


def test_auto_mode_has_distinct_model_call_purpose() -> None:
    """Classifier usage must not be attributed to the coding model turn."""
    assert ModelCallPurpose.AUTO_MODE.value == "auto_mode"


def test_model_call_snapshots_messages_tools_and_tool_choice() -> None:
    """Caller mutation cannot change a request after Gateway admission."""
    messages = [{"role": "user", "content": "inspect"}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    tool_choice = {"type": "function", "function": {"name": "read_file"}}
    call = ModelCall(
        purpose=ModelCallPurpose.CODING,
        messages=messages,
        tools=tools,
        tool_choice=tool_choice,
        max_output_tokens=1000,
    )

    messages[0]["content"] = "changed"
    tools[0]["function"]["name"] = "bash"
    tool_choice["function"]["name"] = "bash"

    assert call.messages[0]["content"] == "inspect"
    assert call.tools[0]["function"]["name"] == "read_file"
    assert call.tool_choice["function"]["name"] == "read_file"
    assert call.streaming is False


@pytest.mark.parametrize("max_tokens", [0, -1])
def test_model_call_rejects_non_positive_output_budget(max_tokens: int) -> None:
    """Invalid output budgets must not reach Provider wire adapters."""
    with pytest.raises(ValueError, match="max_output_tokens"):
        ModelCall(
            purpose=ModelCallPurpose.CODING,
            messages=[],
            max_output_tokens=max_tokens,
        )


@pytest.mark.parametrize(
    "timeout",
    [True, 0, -1, float("nan"), float("inf"), -float("inf")],
)
def test_model_call_rejects_non_finite_or_boolean_timeout(timeout) -> None:
    """Invalid deadlines cannot disable the Gateway's hard timeout."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        ModelCall(
            purpose=ModelCallPurpose.CODING,
            messages=[],
            max_output_tokens=100,
            timeout_seconds=timeout,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempts", True),
        ("attempts", 0),
        ("duration_ms", float("nan")),
        ("duration_ms", float("inf")),
        ("duration_ms", -1),
        ("first_token_ms", float("nan")),
        ("first_token_ms", -1),
        ("cost", float("inf")),
        ("cost", -0.01),
    ],
)
def test_model_call_outcome_rejects_corrupt_accounting_fields(
    field: str,
    value,
) -> None:
    """Terminal evidence must remain finite before observers persist it."""
    values = {field: value}
    with pytest.raises(ValueError, match=field):
        ModelCallOutcome.completed(**values)


def test_model_call_outcome_accepts_finite_accounting_fields() -> None:
    outcome = ModelCallOutcome.completed(
        attempts=2,
        duration_ms=12.5,
        first_token_ms=3.5,
        cost=0.002,
    )

    assert outcome.attempts == 2
    assert math.isfinite(outcome.duration_ms)


def test_context_overflow_outcome_is_not_a_client_error() -> None:
    """Prompt-capacity recovery must remain distinct from malformed requests."""
    outcome = ModelCallOutcome.context_overflow("maximum context length")

    assert outcome.status is ModelCallStatus.CONTEXT_OVERFLOW
    assert outcome.retryable is False
    assert outcome.error == "maximum context length"


def test_completed_outcome_snapshots_tool_calls_and_metadata() -> None:
    """Late stream mutation cannot rewrite terminal Provider evidence."""
    calls = [{"id": "call-1", "function": {"name": "read_file"}}]
    metadata = {"cache": {"hit": True}}
    outcome = ModelCallOutcome.completed(
        content="done",
        tool_calls=calls,
        provider_metadata=metadata,
    )
    calls[0]["id"] = "changed"
    metadata["cache"]["hit"] = False

    assert outcome.tool_calls[0]["id"] == "call-1"
    assert outcome.provider_metadata["cache"]["hit"] is True


@pytest.mark.parametrize(
    "kind",
    ["text", "reasoning", "tool_delta", "usage", "provider_metadata", "finish"],
)
def test_stream_event_accepts_only_documented_kinds(kind: str) -> None:
    """Consumers can exhaustively process every admitted stream event."""
    event = ModelStreamEvent(kind=kind, data={"value": 1})
    assert event.kind == kind


def test_stream_event_rejects_unknown_kind_and_is_immutable() -> None:
    """A new event kind cannot silently bypass Session projection."""
    with pytest.raises(ValueError, match="kind"):
        ModelStreamEvent(kind="raw_chunk")
    event = ModelStreamEvent(kind="text", data={"text": "hello"})
    with pytest.raises(FrozenInstanceError):
        event.kind = "finish"
