"""Tests for normalized streaming Gateway behavior."""
from __future__ import annotations

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.normalized import chunk, completion
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


def test_pre_boundary_stream_failure_uses_one_buffered_fallback() -> None:
    gateway, provider = _gateway([
        ConnectionError("network unavailable"),
        completion(content="fallback", finish_reason="stop"),
    ], wait=lambda _seconds: None)

    outcome = gateway.complete_stream_sync(_call())

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "fallback"
    assert [request.get("stream") for request in provider.requests] == [True, None]
    assert outcome.attempts == 2


def test_partial_text_failure_retries_stream_without_buffered_fallback() -> None:
    def broken():
        yield chunk(content="visible")
        raise ConnectionError("network unavailable")

    gateway, provider = _gateway([
        broken(),
        iter([chunk(content="recovered", finish_reason="stop")]),
    ], max_retries=3, wait=lambda _seconds: None)
    outcome = gateway.complete_stream_sync(_call())

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "recovered"
    assert [request.get("stream") for request in provider.requests] == [True, True]
