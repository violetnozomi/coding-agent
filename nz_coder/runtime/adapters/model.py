"""Legacy Agent host adapter for the focused model runtime context."""
from __future__ import annotations

from nz_coder.runtime.core.model_context import ModelExecutionContext


def model_context_from_legacy_host(host) -> ModelExecutionContext:
    """Bind dynamic model operations without retaining the broad host object."""
    tracer = getattr(host, "tracer", None)
    recovery = getattr(host, "recovery", None)
    complete_override = vars(host).get("_call_llm")
    from nz_coder.tool_platform.exposure import expose_specs

    return ModelExecutionContext(
        capabilities=lambda: getattr(host, "model_capabilities", None),
        active_model_id=getattr(host, "_active_model_id"),
        active_tool_specs=lambda: expose_specs(host._active_tool_specs()),
        prompt_budget=getattr(host, "_prompt_budget"),
        call_streaming=getattr(host, "_call_streaming"),
        call_non_streaming=getattr(host, "_call_non_streaming"),
        gateway=getattr(host, "_gateway"),
        project_outcome=getattr(host, "_gateway_outcome_result"),
        record_success=getattr(recovery, "record_success", _discard),
        trace=getattr(tracer, "log", _discard),
        retire_message_part=getattr(host, "_retire_message_part", _discard),
        complete_override=(
            complete_override if callable(complete_override) else None
        ),
    )


def _discard(*_args, **_kwargs) -> None:
    return None
