"""Tests for normalized streaming Gateway behavior."""
from __future__ import annotations

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.normalized import chunk, completion
from nz_coder.providers.registry import ModelPricing
from nz_coder.runtime.model_gateway import (
    ModelCall,
    ModelCallPurpose,
    ModelCallStatus,
    ProductionModelGateway,
    ResolvedModelRuntime,
)


class _Provider:
    name = "fake"

    def __init__(self, values):
        self.values = list(values)
        self.requests = []

    def create_completion(self, _client, **kwargs):
        self.requests.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def _gateway(values, **kwargs):
    provider = _Provider(values)
    runtime = ResolvedModelRuntime(
        provider_id="fake",
        model_id="logical",
        request_model_id="wire",
        variant=None,
        provider=provider,
        client=object(),
        capabilities=ModelCapabilities(provider="fake", model_id="logical"),
    )
    return ProductionModelGateway(runtime, **kwargs), provider


def _call():
    return ModelCall(
        purpose=ModelCallPurpose.CODING,
        messages=[{"role": "user", "content": "fix"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        max_output_tokens=100,
        streaming=True,
        timeout_seconds=1,
    )


def test_stream_emits_ordered_normalized_events_and_outcome() -> None:
    gateway, _provider = _gateway([iter([
        chunk(content="<think>check"),
        chunk(content="</think>done"),
        chunk(tool_calls=[{
            "index": 0,
            "id": "call-1",
            "name": "read_file",
            "arguments": '{"path":',
        }]),
        chunk(tool_calls=[{"index": 0, "arguments": '"a.py"}'}]),
        chunk(finish_reason="tool_calls", usage={
            "input_tokens": 8,
            "output_tokens": 4,
            "total_tokens": 12,
        }),
    ])])
    events = []

    outcome = gateway.complete_stream_sync(_call(), on_event=events.append)

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "done"
    assert outcome.reasoning == "check"
    assert outcome.tool_calls[0]["function"] == {
        "name": "read_file",
        "arguments": '{"path":"a.py"}',
    }
    assert outcome.usage.total_tokens == 12
    assert [event.kind for event in events] == [
        "reasoning", "text", "tool_delta", "tool_delta", "usage", "finish"
    ]


def test_stream_finish_observation_reports_logical_call_usage() -> None:
    observed = []
    gateway, _provider = _gateway(
        [iter([chunk(content="done", finish_reason="stop", usage={
            "input_tokens": 8,
            "output_tokens": 4,
            "total_tokens": 12,
        })])],
        observer=lambda name, payload: observed.append((name, payload)),
    )

    gateway.complete_stream_sync(_call())

    finish = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finish) == 1
    assert finish[0]["purpose"] == "coding"
    assert finish[0]["usage"]["total"] == 12


def test_pre_boundary_stream_failure_uses_one_buffered_fallback() -> None:
    observed = []
    gateway, provider = _gateway([
        ConnectionError("network unavailable"),
        completion(content="fallback", finish_reason="stop"),
    ], wait=lambda _seconds: None,
        observer=lambda name, payload: observed.append((name, payload)))

    outcome = gateway.complete_stream_sync(_call())

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "fallback"
    assert [request.get("stream") for request in provider.requests] == [True, None]
    assert outcome.attempts == 2
    finishes = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finishes) == 1
    assert finishes[0]["attempts"] == 2
    assert finishes[0]["duration_ms"] == outcome.duration_ms


def test_stream_fallback_accounts_usage_from_every_dispatched_attempt() -> None:
    """A billed failed stream must not disappear from the logical ledger."""
    observed = []

    def billed_then_broken():
        yield chunk(usage={
            "input_tokens": 5,
            "output_tokens": 1,
            "total_tokens": 6,
        })
        raise ConnectionError("stream disconnected")

    gateway, _provider = _gateway([
        billed_then_broken(),
        completion(content="recovered", finish_reason="stop", usage={
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
        }),
    ], wait=lambda _seconds: None,
        observer=lambda name, payload: observed.append((name, payload)))
    gateway.runtime.pricing = ModelPricing(input=1.0, output=2.0)

    outcome = gateway.complete_stream_sync(_call())

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.attempts == 2
    assert outcome.usage.as_legacy_dict() == {
        "input": 12,
        "output": 3,
        "total": 15,
        "reasoning": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    assert outcome.cost == 0.000018
    assert outcome.cost_source == "registry"
    finish = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(finish) == 1
    assert finish[0]["usage"]["total"] == 15


def test_stream_retries_without_forced_tool_choice_when_gateway_rejects_it() -> None:
    error = RuntimeError("unsupported parameter: tool_choice")
    error.status_code = 400
    gateway, provider = _gateway([
        error,
        iter([chunk(content="accepted", finish_reason="stop")]),
    ], max_retries=0)
    base = _call()
    call = ModelCall(
        purpose=ModelCallPurpose.STALL_SIDECAR,
        messages=base.messages,
        tools=base.tools,
        tool_choice={"type": "function", "function": {"name": "read_file"}},
        max_output_tokens=base.max_output_tokens,
        streaming=True,
        timeout_seconds=base.timeout_seconds,
    )

    outcome = gateway.complete_stream_sync(call)

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "accepted"
    assert outcome.attempts == 2
    assert provider.requests[0]["tool_choice"]["function"]["name"] == "read_file"
    assert "tool_choice" not in provider.requests[1]
    assert provider.requests[1]["tools"] == list(call.tools)


def test_partial_text_failure_retries_stream_without_buffered_fallback() -> None:
    def broken():
        yield chunk(content="visible")
        yield chunk(usage={
            "input_tokens": 5,
            "output_tokens": 1,
            "total_tokens": 6,
        })
        raise ConnectionError("network unavailable")

    gateway, provider = _gateway([
        broken(),
        iter([chunk(content="recovered", finish_reason="stop", usage={
            "input_tokens": 7,
            "output_tokens": 2,
            "total_tokens": 9,
        })]),
    ], max_retries=3, wait=lambda _seconds: None)
    outcome = gateway.complete_stream_sync(_call())

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "recovered"
    assert outcome.usage.total_tokens == 15
    assert [request.get("stream") for request in provider.requests] == [True, True]
