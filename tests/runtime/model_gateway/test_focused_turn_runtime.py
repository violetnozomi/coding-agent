"""Model turn behavior through the focused production context."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.providers.capabilities import resolve_model_capabilities
from nz_coder.runtime.core.model_context import ModelExecutionContext
from nz_coder.runtime.conversation.model_result import (
    LLMResult,
    ModelCompletionError,
)
from nz_coder.runtime.execution.services import ProductionTurnModelRuntime
from nz_coder.runtime.execution.loop import ProductRunEnvironment
from nz_coder.runtime.model_gateway.models import ModelCallOutcome


def _context(*, streaming: bool) -> tuple[ModelExecutionContext, list]:
    events = []
    capability = replace(
        resolve_model_capabilities("test", "focused-model"),
        supports_streaming=streaming,
    )
    context = ModelExecutionContext(
        capabilities=lambda: capability,
        active_model_id=lambda: "focused-model",
        provider_instance_id=lambda: "provider-instance-focused",
        active_tool_specs=lambda: [],
        prompt_budget=lambda: None,
        call_streaming=lambda *_args, **_kwargs: LLMResult(content="stream"),
        call_non_streaming=lambda *_args, **_kwargs: LLMResult(content="buffered"),
        gateway=lambda **_kwargs: None,
        project_outcome=lambda outcome: outcome,
        record_success=lambda: None,
        trace=lambda event, **payload: events.append((event, payload)),
        retire_message_part=lambda *_args: None,
        complete_override=None,
    )
    return context, events


def test_focused_turn_runtime_falls_back_when_streaming_is_unsupported() -> None:
    context, events = _context(streaming=False)

    result = ProductionTurnModelRuntime().complete_turn_sync(
        context,
        [],
        stream=True,
        on_token=None,
        message_part=None,
        stream_tool_handler=None,
    )

    assert result.content == "buffered"
    assert events[0][0] == "provider_capability_fallback"
    assert events[0][1]["model"] == "focused-model"


def test_focused_turn_runtime_uses_streaming_capability() -> None:
    context, events = _context(streaming=True)

    result = ProductionTurnModelRuntime().complete_turn_sync(
        context,
        [],
        stream=True,
        on_token=None,
        message_part=None,
        stream_tool_handler=None,
    )

    assert result.content == "stream"
    assert events == []


def test_provider_context_overflow_projection_is_typed_and_distinct() -> None:
    environment = ProductRunEnvironment.__new__(ProductRunEnvironment)
    result = environment._gateway_outcome_result(
        ModelCallOutcome.context_overflow("provider rejected received request")
    )

    assert result.needs_compaction is True
    assert result.failure_source == "provider_context_overflow"
    assert result.input_tokens == 0
    assert result.cost == 0.0


def test_focused_turn_runtime_compacts_before_over_budget_provider_call() -> None:
    calls = []
    events = []
    capability = replace(
        resolve_model_capabilities("test", "focused-model"),
        supports_streaming=True,
    )
    context = ModelExecutionContext(
        capabilities=lambda: capability,
        active_model_id=lambda: "focused-model",
        active_tool_specs=lambda: [{
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "x" * 2_000,
                "parameters": {"type": "object", "properties": {}},
            },
        }],
        prompt_budget=lambda: SimpleNamespace(usable_input_tokens=100),
        call_streaming=lambda *_args, **_kwargs: calls.append("stream"),
        call_non_streaming=lambda *_args, **_kwargs: calls.append("buffered"),
        gateway=lambda **_kwargs: None,
        project_outcome=lambda outcome: outcome,
        record_success=lambda: None,
        trace=lambda event, **payload: events.append((event, payload)),
        retire_message_part=lambda *_args: None,
    )

    result = ProductionTurnModelRuntime().complete_turn_sync(
        context,
        [{"role": "user", "content": "x" * 500}],
        stream=True,
        on_token=None,
        message_part=None,
        stream_tool_handler=None,
    )

    assert result.needs_compaction is True
    assert calls == []
    event, payload = events[-1]
    assert event == "request_budget_overflow"
    assert payload["estimated_tokens"] > payload["usable_input_tokens"]


def test_focused_turn_runtime_checks_overflow_for_non_streaming_calls() -> None:
    context, events = _context(streaming=False)
    calls = []
    context = replace(
        context,
        prompt_budget=lambda: SimpleNamespace(usable_input_tokens=100),
        call_non_streaming=lambda *_args, **_kwargs: calls.append("buffered"),
        trace=lambda event, **payload: events.append((event, payload)),
    )

    result = ProductionTurnModelRuntime().complete_turn_sync(
        context,
        [{"role": "user", "content": "x" * 500}],
        stream=False,
        on_token=None,
        message_part=None,
        stream_tool_handler=None,
    )

    assert result.needs_compaction is True
    assert calls == []
    assert events[-1][0] == "request_budget_overflow"


def test_focused_turn_runtime_checks_legacy_override_before_provider_call() -> None:
    context, events = _context(streaming=True)
    calls = []
    context = replace(
        context,
        prompt_budget=lambda: SimpleNamespace(usable_input_tokens=100),
        complete_override=lambda *_args, **_kwargs: calls.append("override"),
        trace=lambda event, **payload: events.append((event, payload)),
    )

    result = asyncio.run(
        ProductionTurnModelRuntime().complete_turn(
            context,
            [{"role": "user", "content": "x" * 500}],
            stream=True,
            on_token=None,
            message_part=None,
            stream_tool_handler=None,
        )
    )

    assert result.needs_compaction is True
    assert calls == []
    assert events[-1][0] == "request_budget_overflow"


def test_text_completion_preflights_before_provider_call() -> None:
    calls = []
    context, events = _context(streaming=False)
    context = replace(
        context,
        prompt_budget=lambda: SimpleNamespace(
            usable_input_tokens=100,
            output_reserve_tokens=200,
        ),
        gateway=lambda **_kwargs: SimpleNamespace(
            complete_sync=lambda _call: calls.append("provider")
        ),
        trace=lambda event, **payload: events.append((event, payload)),
    )

    with pytest.raises(ModelCompletionError) as raised:
        ProductionTurnModelRuntime().complete_text(
            context,
            "system " + "x" * 500,
            "prompt",
        )

    assert raised.value.result.failure_source == "local_request_budget"
    assert raised.value.result.needs_compaction is True
    assert calls == []
    assert events[-1][0] == "request_budget_overflow"


def test_text_completion_projects_provider_overflow_with_typed_source() -> None:
    calls = []
    context, _events = _context(streaming=False)
    environment = ProductRunEnvironment.__new__(ProductRunEnvironment)
    context = replace(
        context,
        prompt_budget=lambda: SimpleNamespace(
            usable_input_tokens=10_000,
            output_reserve_tokens=200,
        ),
        gateway=lambda **_kwargs: SimpleNamespace(
            complete_sync=lambda _call: calls.append("provider")
            or ModelCallOutcome.context_overflow("provider rejected request")
        ),
        project_outcome=environment._gateway_outcome_result,
    )

    with pytest.raises(ModelCompletionError) as raised:
        ProductionTurnModelRuntime().complete_text(context, "system", "prompt")

    assert raised.value.result.failure_source == "provider_context_overflow"
    assert raised.value.result.needs_compaction is True
    assert calls == ["provider"]


def test_text_completion_returns_content_for_normal_provider_result() -> None:
    context, _events = _context(streaming=False)
    environment = ProductRunEnvironment.__new__(ProductRunEnvironment)
    context = replace(
        context,
        prompt_budget=lambda: SimpleNamespace(
            usable_input_tokens=10_000,
            output_reserve_tokens=200,
        ),
        gateway=lambda **_kwargs: SimpleNamespace(
            complete_sync=lambda _call: ModelCallOutcome.completed(
                content='{"action":"decline","reason":"simple"}',
            )
        ),
        project_outcome=environment._gateway_outcome_result,
    )

    result = ProductionTurnModelRuntime().complete_text(context, "system", "prompt")

    assert result == '{"action":"decline","reason":"simple"}'
