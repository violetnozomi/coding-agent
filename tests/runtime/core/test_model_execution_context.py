"""Focused model capability ownership tests."""
from __future__ import annotations

from nz_coder.runtime.adapters.model import model_context_from_legacy_host
from nz_coder.runtime.core.model_context import ModelExecutionContext


class _Tracer:
    def log(self, *_args, **_kwargs) -> None:
        return None


class _Host:
    def __init__(self) -> None:
        self.model_capabilities = object()
        self.tracer = _Tracer()
        self.recovery = object()
        self._call_llm = None

    def _active_model_id(self) -> str:
        return "model-a"

    def _active_tool_specs(self) -> list[dict]:
        return [{"type": "function"}]

    def _prompt_budget(self):
        return object()

    def _call_streaming(self, *_args, **_kwargs):
        return "stream"

    def _call_non_streaming(self, *_args, **_kwargs):
        return "buffered"

    def _gateway(self, **_kwargs):
        return object()

    def _gateway_outcome_result(self, outcome):
        return outcome

    def _retire_message_part(self, *_args) -> None:
        return None


def test_model_context_projects_dynamic_model_capabilities() -> None:
    """Agent handoff may replace the active model after context construction."""
    host = _Host()
    context = model_context_from_legacy_host(host)

    assert isinstance(context, ModelExecutionContext)
    assert context.capabilities() is host.model_capabilities
    assert context.active_model_id() == "model-a"
    assert context.active_tool_specs() == [{"type": "function"}]
    assert "host" not in vars(context)

    replacement = object()
    host.model_capabilities = replacement
    assert context.capabilities() is replacement


def test_model_context_rejects_non_callable_capability() -> None:
    """Focused contexts fail at composition rather than during a model call."""
    values = {
        "capabilities": lambda: None,
        "active_model_id": lambda: "model",
        "active_tool_specs": lambda: [],
        "prompt_budget": lambda: object(),
        "call_streaming": lambda *_args, **_kwargs: None,
        "call_non_streaming": lambda *_args, **_kwargs: None,
        "gateway": lambda **_kwargs: None,
        "project_outcome": lambda outcome: outcome,
        "record_success": lambda: None,
        "trace": lambda *_args, **_kwargs: None,
        "retire_message_part": lambda *_args: None,
        "complete_override": None,
    }
    values["gateway"] = None

    try:
        ModelExecutionContext(**values)
    except TypeError as error:
        assert "gateway" in str(error)
    else:
        raise AssertionError("invalid ModelExecutionContext was accepted")
