"""Legacy Agent host adapter for focused Tool Runtime contexts."""
from __future__ import annotations

import json

from nz_coder.runtime.core.tool_context import (
    ToolExecutionContext,
    ToolLifecycleContext,
    ToolPolicyContext,
    ToolProjectionContext,
)
from nz_coder.runtime.tool_runtime.observers import LegacyCodingToolObserver


def tool_context_from_legacy_host(
    host,
    run_context=None,
    services=None,
) -> ToolExecutionContext:
    """Snapshot policy identity and bind Session-owned lifecycle operations."""
    active_marker = getattr(host, "active_run_context", None)
    policy_context = policy_context_from_legacy_host(host, fresh=True)

    async def checkpoint(messages: list[dict], status: str) -> None:
        if run_context is not None and services is not None:
            await services.session_runtime.checkpoint(run_context, status)
            return
        if active_marker is not None:
            raise RuntimeError(
                "Active Tool Runtime requires a SessionRuntime checkpoint callback"
            )
        legacy = getattr(host, "_checkpoint_messages", None)
        if callable(legacy):
            legacy(messages, status)

    services = services or getattr(host, "runtime_services", None)
    transitions = getattr(services, "transitions", None)

    def signal_from_metadata(metadata: dict | None):
        if transitions is None:
            return None
        return transitions.signal_from_metadata(host, metadata)

    def after_result(messages: list[dict], result, output: str) -> None:
        host.hooks.after_tool_result(host, messages, result, output)
        host.hooks.on_post_tool_use(
            host,
            messages,
            result.name,
            result.tool_input,
            file_path=host._infer_hook_file_path(result.tool_input),
            output=output,
            status="error" if result.dispatch_failed else "ok",
            is_write=result.is_write,
        )

    async def apply_transition(signal, messages: list[dict], processor):
        if transitions is None:
            return None
        transition = transitions.apply(host, signal, messages, processor)
        policy_context.agent_name = str(
            getattr(host, "current_agent_name", "") or policy_context.agent_name
        )
        notify = getattr(host, "_notify_agent_switched_async", None)
        if callable(notify):
            await notify(transition)
        return transition

    async def describe_read_results(dispatched: list, messages: list) -> bool:
        inputs = getattr(services, "inputs", None)
        if inputs is None:
            return False
        return await inputs.describe_read_results(host, dispatched, messages)

    guardrails = getattr(services, "guardrails", None)

    async def before_tool(tool_call: dict, messages: list[dict]):
        if guardrails is None:
            return tool_call, None
        return await guardrails.before_tool(host, tool_call, messages)

    async def after_tool(tool_call: dict, result, messages: list[dict]):
        if guardrails is None:
            return result
        return await guardrails.after_tool(host, tool_call, result, messages)

    def metadata_reporter(processor, messages):
        factory = getattr(host, "_tool_metadata_callback", None)
        return factory(processor, messages) if callable(factory) else _discard_trace

    def question_reporter(processor, messages):
        factory = getattr(host, "_question_lifecycle_callback", None)
        return factory(processor, messages) if callable(factory) else _discard_trace

    txn = getattr(host, "txn", None)
    hooks = getattr(host, "hooks", None)
    dispatch_override = _legacy_override(host, "_dispatch_tool_calls_async")
    write_override = _legacy_override(host, "_tool_batch_has_write")

    return ToolExecutionContext(
        run=run_context,
        policy=policy_context,
        lifecycle=ToolLifecycleContext(
            checkpoint=checkpoint,
            processor_for_messages=getattr(
                host,
                "_processor_for_latest_assistant",
                lambda _messages: None,
            ),
            write_override=write_override,
            begin_transaction=(getattr(txn, "begin", _discard_trace)),
            transaction_active=lambda: bool(getattr(txn, "active", False)),
            finish_transaction=getattr(host, "_finish_tool_transaction", _discard_trace),
            metadata_reporter=metadata_reporter,
            question_reporter=question_reporter,
            dispatch_override_async=dispatch_override,
            consume_override=_legacy_override(host, "_consume_dispatched_tools"),
            model_capabilities=getattr(host, "model_capabilities", None),
            describe_read_results=describe_read_results,
            strict_completed=getattr(
                host,
                "_strict_verification_completed",
                lambda _dispatched: False,
            ),
            apply_transition=apply_transition,
            observer=LegacyCodingToolObserver(host),
            has_pre_tool_hooks=(
                getattr(hooks, "has_pre_tool_use_hooks", lambda: False)
            ),
            executor=getattr(host, "executor", None),
            execute_one=getattr(host, "_execute_tool_call_with_hooks", _missing_execute),
            before_tool=before_tool,
            after_tool=after_tool,
            trace=getattr(getattr(host, "tracer", None), "log", _discard_trace),
        ),
        projection=projection_context_from_legacy_host(
            host,
            signal_from_metadata=signal_from_metadata,
            after_result=after_result,
        ),
    )


def policy_context_from_legacy_host(host, *, fresh: bool = False) -> ToolPolicyContext:
    """Return one compatibility policy context, cached only for direct callers."""
    if not fresh:
        cached = getattr(host, "_compat_tool_policy_context", None)
        if isinstance(cached, ToolPolicyContext):
            return cached
    allowlist = getattr(host, "tool_allowlist", None)
    parser = getattr(host, "_best_effort_tool_input", _parse_tool_input)
    tracer = getattr(host, "tracer", None)
    trace = getattr(tracer, "log", _discard_trace)
    observability = getattr(host, "_tool_observability", None)
    if not isinstance(observability, dict):
        observability = {}
    context = ToolPolicyContext(
        agent_name=str(getattr(host, "current_agent_name", "") or "worker"),
        agent_graph=getattr(host, "agent_graph", None),
        tool_allowlist=frozenset(allowlist) if allowlist is not None else None,
        admission_handle=getattr(host, "admission_handle", None),
        runtime_state=getattr(host, "runtime_state", None),
        recovery=getattr(host, "recovery", None),
        permissions=getattr(host, "permissions", None),
        stall_orchestrator=getattr(host, "stall_orchestrator", None),
        parse_input=parser,
        trace=trace,
        observability=observability,
    )
    if not context.observability:
        from nz_coder.runtime.core.tool_context import _empty_observability

        context.observability.update(_empty_observability())
    if not fresh:
        host._compat_tool_policy_context = context
    return context


def projection_context_from_legacy_host(
    host,
    *,
    signal_from_metadata=None,
    after_result=None,
) -> ToolProjectionContext:
    """Bind stable result operations for direct compatibility callers."""
    if signal_from_metadata is None:
        services = getattr(host, "runtime_services", None)
        signal_from_metadata = (
            (lambda metadata: services.transitions.signal_from_metadata(host, metadata))
            if services is not None
            else (lambda _metadata: None)
        )
    if after_result is None:
        def after_result(messages: list[dict], result, output: str) -> None:
            hooks = getattr(host, "hooks", None)
            if hooks is None:
                return
            host.hooks.after_tool_result(host, messages, result, output)
            host.hooks.on_post_tool_use(
                host,
                messages,
                result.name,
                result.tool_input,
                file_path=host._infer_hook_file_path(result.tool_input),
                output=output,
                status="error" if result.dispatch_failed else "ok",
                is_write=result.is_write,
            )

    def available_result_tokens(messages: list[dict]) -> int:
        budget_factory = getattr(host, "_prompt_budget", None)
        projected = getattr(host, "_projected_request_tokens", None)
        if not callable(budget_factory) or not callable(projected):
            return 16_000
        budget = budget_factory()
        usable = int(getattr(budget, "usable_input_tokens", 0) or 0)
        if usable <= 0:
            return 16_000
        return max(1, usable - int(projected(messages)))

    return ToolProjectionContext(
        signal_from_metadata=signal_from_metadata,
        record_result=getattr(host, "_record_tool_result", lambda _result: False),
        trace_result=getattr(host, "_trace_tool_result", _discard_trace),
        stall_orchestrator=getattr(host, "stall_orchestrator", None),
        after_result=after_result,
        available_result_tokens=available_result_tokens,
        runtime_state=getattr(host, "runtime_state", None),
    )


def _parse_tool_input(raw_arguments) -> dict:
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    if not isinstance(raw_arguments, str):
        return {}
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _discard_trace(*_args, **_kwargs) -> None:
    return None


def _missing_execute(*_args, **_kwargs):
    raise RuntimeError("ToolExecutionContext has no tool executor")


def _legacy_override(host, name: str):
    candidate = getattr(host, name, None)
    if not callable(candidate):
        return None
    function = getattr(candidate, "__func__", candidate)
    if getattr(function, "__module__", "") == "nz_coder.runtime.execution.loop":
        return None
    return candidate
