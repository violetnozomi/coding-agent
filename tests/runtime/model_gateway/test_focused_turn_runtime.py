"""Model turn behavior through the focused production context."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

from nz_coder.providers.capabilities import resolve_model_capabilities
from nz_coder.runtime.core.model_context import ModelExecutionContext
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.execution.services import ProductionTurnModelRuntime


def _context(*, streaming: bool) -> tuple[ModelExecutionContext, list]:
    events = []
    capability = replace(
        resolve_model_capabilities("test", "focused-model"),
        supports_streaming=streaming,
    )
    context = ModelExecutionContext(
        capabilities=lambda: capability,
        active_model_id=lambda: "focused-model",
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
