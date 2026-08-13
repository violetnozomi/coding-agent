"""Behavioral tests for the buffered production model Gateway."""
from __future__ import annotations

import threading
from types import SimpleNamespace

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.registry import ModelPricing
from nz_coder.runtime.model_gateway import (
    ModelCall,
    ModelCallPurpose,
    ModelCallStatus,
    ProductionModelGateway,
    ResolvedModelRuntime,
)


class _ToolCall:
    def model_dump(self):
        return {
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
        }


def _response():
    message = SimpleNamespace(
        content="<think>check</think>done",
        reasoning_content="native ",
        provider_extra={"request_id": "req-1"},
        tool_calls=[_ToolCall()],
    )
    usage = SimpleNamespace(
        prompt_tokens=110,
        completion_tokens=25,
        total_tokens=135,
        prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        usage=usage,
    )


class _Provider:
    name = "fake"
    uses_capability_snapshot = True

    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def create_completion(self, client, **kwargs):
        self.calls.append((client, kwargs))
        value = self.values.pop(0)
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value()
        return value


def _runtime(provider) -> ResolvedModelRuntime:
    return ResolvedModelRuntime(
        provider_id="fake",
        model_id="logical",
        request_model_id="wire",
        variant=None,
        provider=provider,
        client=object(),
        capabilities=ModelCapabilities(
            provider="fake",
            model_id="logical",
            context_tokens=1000,
            output_tokens=100,
        ),
        pricing=ModelPricing(input=1.0, output=2.0),
    )


def _call(timeout=1.0) -> ModelCall:
    return ModelCall(
        purpose=ModelCallPurpose.CODING,
        messages=[{"role": "user", "content": "fix"}],
        tools=[{"type": "function", "function": {"name": "read_file"}}],
        max_output_tokens=100,
        timeout_seconds=timeout,
    )


def test_buffered_success_normalizes_every_model_fact() -> None:
    provider = _Provider([_response()])
    outcome = ProductionModelGateway(_runtime(provider)).complete_sync(_call())

    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.content == "done"
    assert outcome.reasoning == "native check"
    assert outcome.tool_calls[0]["function"]["name"] == "read_file"
    assert outcome.provider_metadata == {"request_id": "req-1"}
    assert outcome.finish_reason == "tool_calls"
    assert outcome.usage.input_tokens == 100
    assert outcome.usage.output_tokens == 20
    assert outcome.usage.reasoning_tokens == 5
    assert outcome.usage.cache_read_tokens == 10
    assert outcome.cost == 0.00015
    assert outcome.cost_source == "registry"
    assert outcome.attempts == 1
    assert outcome.duration_ms >= 0
    request = provider.calls[0][1]
    assert request["model"] == "wire"
    assert request["max_tokens"] == 100
    assert request["_capabilities"].model_id == "logical"


class _HTTPError(RuntimeError):
    def __init__(self, status_code: int, message: str, headers=None):
        super().__init__(message)
        self.status_code = status_code
        self.headers = headers or {}


def test_context_overflow_precedes_generic_400_classification() -> None:
    provider = _Provider([_HTTPError(400, "maximum context length exceeded")])
    outcome = ProductionModelGateway(_runtime(provider)).complete_sync(_call())
    assert outcome.status is ModelCallStatus.CONTEXT_OVERFLOW
    assert len(provider.calls) == 1


def test_ordinary_400_is_non_retryable_client_error() -> None:
    provider = _Provider([_HTTPError(400, "invalid request")])
    outcome = ProductionModelGateway(_runtime(provider)).complete_sync(_call())
    assert outcome.status is ModelCallStatus.CLIENT_ERROR
    assert outcome.retryable is False
    assert len(provider.calls) == 1


def test_retry_after_429_then_success_uses_injected_wait() -> None:
    waits = []
    provider = _Provider([
        _HTTPError(429, "rate limited", {"retry-after": "1.25"}),
        _response(),
    ])
    outcome = ProductionModelGateway(
        _runtime(provider),
        wait=lambda seconds: waits.append(seconds),
    ).complete_sync(_call())
    assert outcome.status is ModelCallStatus.COMPLETED
    assert outcome.attempts == 2
    assert waits == [1.25]


def test_retry_exhaustion_is_aborted_and_retryable() -> None:
    provider = _Provider([TimeoutError("temporary timeout")] * 3)
    outcome = ProductionModelGateway(
        _runtime(provider),
        max_retries=2,
        wait=lambda _seconds: None,
    ).complete_sync(_call())
    assert outcome.status is ModelCallStatus.ABORTED
    assert outcome.retryable is True
    assert outcome.attempts == 3


def test_hard_timeout_returns_without_publishing_late_success() -> None:
    release = threading.Event()
    provider = _Provider([lambda: (release.wait(1), _response())[1]])
    outcome = ProductionModelGateway(
        _runtime(provider),
        max_retries=0,
    ).complete_sync(_call(timeout=0.03))
    release.set()
    assert outcome.status is ModelCallStatus.ABORTED
    assert "timeout" in outcome.error.lower()


def test_cancellation_before_dispatch_skips_provider() -> None:
    cancelled = threading.Event()
    cancelled.set()
    provider = _Provider([_response()])
    outcome = ProductionModelGateway(_runtime(provider)).complete_sync(
        _call(),
        cancel_event=cancelled,
    )
    assert outcome.status is ModelCallStatus.CANCELLED
    assert provider.calls == []


def test_cancellation_during_worker_returns_cancelled() -> None:
    started = threading.Event()
    release = threading.Event()
    cancelled = threading.Event()

    def slow():
        started.set()
        release.wait(1)
        return _response()

    provider = _Provider([slow])
    results = []
    thread = threading.Thread(
        target=lambda: results.append(
            ProductionModelGateway(_runtime(provider)).complete_sync(
                _call(), cancel_event=cancelled
            )
        )
    )
    thread.start()
    assert started.wait(0.5)
    cancelled.set()
    thread.join(0.5)
    release.set()
    assert not thread.is_alive()
    assert results[0].status is ModelCallStatus.CANCELLED
