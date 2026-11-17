"""Legacy Agent host adapter for the focused model runtime context."""
from __future__ import annotations

from nz_coder.runtime.core.model_context import ModelExecutionContext
from nz_coder.runtime.core.run_context import current_run_context, scoped_run_context


def model_context_from_legacy_host(host, run_context=None) -> ModelExecutionContext:
    """Bind dynamic model operations without retaining the broad host object."""
    tracer = getattr(host, "tracer", None)
    recovery = getattr(host, "recovery", None)
    complete_override = vars(host).get("_call_llm")
    from nz_coder.tool_platform.exposure import expose_specs

    if run_context is None:
        run_context = current_run_context() or getattr(host, "active_run_context", None)

    def bound(operation):
        def invoke(*args, **kwargs):
            with scoped_run_context(run_context):
                return operation(*args, **kwargs)
        return invoke

    return ModelExecutionContext(
        run_context=run_context,
        capabilities=lambda: getattr(host, "model_capabilities", None),
        active_model_id=getattr(host, "_active_model_id"),
        provider_instance_id=lambda: str(
            getattr(getattr(host, "model_runtime", None), "provider_instance_id", "")
        ),
        active_tool_specs=lambda: expose_specs(host._active_tool_specs()),
        prompt_budget=getattr(host, "_prompt_budget"),
        call_streaming=bound(getattr(host, "_call_streaming")),
        call_non_streaming=bound(getattr(host, "_call_non_streaming")),
        gateway=bound(getattr(host, "_gateway")),
        working_memory_allowed=lambda: (
            getattr(host, "runtime_profile", "") not in {"read_child", "write_child"}
            and not getattr(host, "_structured_output_active_repair", "")
            and getattr(getattr(host, "model_capabilities", None), "supports_tools", True)
        ),
        project_outcome=getattr(host, "_gateway_outcome_result"),
        record_success=getattr(recovery, "record_success", _discard),
        trace=getattr(tracer, "log", _discard),
        retire_message_part=getattr(host, "_retire_message_part", _discard),
        complete_override=(
            bound(complete_override) if callable(complete_override) else None
        ),
    )


def _discard(*_args, **_kwargs) -> None:
    return None
