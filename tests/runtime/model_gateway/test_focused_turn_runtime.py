"""Model turn behavior through the focused production context."""
from __future__ import annotations

from dataclasses import replace

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
