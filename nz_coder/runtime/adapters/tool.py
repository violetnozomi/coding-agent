"""Legacy Agent host adapter for focused Tool Runtime contexts."""
from __future__ import annotations

import json
from dataclasses import replace

from nz_coder.runtime.core.tool_context import (
    ToolExecutionContext,
    ToolLifecycleContext,
    ToolPolicyContext,
    ToolProjectionContext,
)
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime


def tool_context_from_legacy_host(
    host,
    run_context=None,
    services=None,
    *, sync: bool = False,
) -> ToolExecutionContext:
    """Snapshot policy identity and bind Session-owned lifecycle operations."""
    factory = getattr(host, "tool_execution_context", None)
    if callable(factory):
        return factory(run_context, services, sync=sync)
    active_marker = getattr(host, "active_run_context", None)
    if active_marker is not None and (run_context is None or services is None):
        raise RuntimeError("Active Tool Runtime requires a SessionRuntime checkpoint callback")
    if run_context is None and not callable(getattr(host, "_checkpoint_messages", None)):
        raise TypeError("Legacy ToolRuntime requires checkpoint")
    for name in ("_finish_tool_transaction", "_record_tool_result", "_trace_tool_result"):
        if not callable(getattr(host, name, None)):
            raise TypeError(f"Legacy ToolRuntime requires {name}")
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
        if not callable(legacy):
            raise TypeError("Legacy ToolRuntime requires checkpoint")
        legacy(messages, status)

    async def drain_progress() -> None:
        """Legacy progress persistence is synchronous; no pending work exists."""

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
        callback = guardrails.before_tool_sync if sync else guardrails.before_tool
        return await callback(host, tool_call, messages)

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
            drain_progress=drain_progress,
            checkpoint=checkpoint,
            processor_for_messages=getattr(
                host,
                "_processor_for_latest_assistant",
                lambda _messages: None,
            ),
            write_override=write_override,
            transaction=LegacyToolTransaction(txn, host._finish_tool_transaction),
            metadata_reporter=metadata_reporter,
            question_reporter=question_reporter,
            dispatch_override_async=dispatch_override,
            dispatch_override_sync=_legacy_override(host, "_dispatch_tool_calls"),
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
        record_result=host._record_tool_result,
        trace_result=host._trace_tool_result,
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


class LegacyCodingToolObserver:
    """Own index, diagnostics, patch, hook, and plan effects after tool work."""

    def __init__(self, host) -> None:
        self._host = host

    def post_write(self, dispatched: list, messages: list[dict]) -> None:
        admission = getattr(self._host, "_admission_session", None)
        if admission is not None:
            for _index, _tool_call, result in dispatched:
                admission.record_committed_mutation(result)
        self._host.recovery.reset_tool_call_history(reason="workspace_changed")
        self._required("_refresh_patch_risk")(messages)
        self._required("_refresh_code_index")(dispatched)
        self._required("_attach_lsp_write_diagnostics")(dispatched, messages)

    def after_batch(self, messages: list[dict], batch_state: dict, on_text) -> None:
        self._host.hooks.after_tool_batch(
            self._host,
            messages,
            manual_compact=batch_state["manual_compact"],
            used_todo=batch_state["used_todo"],
            on_text=on_text,
            write_total=batch_state["write_total"],
            write_denied=batch_state["write_denied"],
        )

    def apply_plan_mode(self) -> None:
        self._required("_apply_pending_plan_mode")()

    async def capture_snapshot(self, processor):
        if not processor.step_snapshot:
            return None
        return await self._required("_capture_step_snapshot_async")(
            "step-finish", processor.message_id,
        )

    def record_patch(self, messages, processor, finish_snapshot) -> None:
        self._required("_record_step_patch")(
            messages, processor, finish_snapshot,
        )

    def _required(self, name: str):
        value = getattr(self._host, name, None)
        if not callable(value):
            raise RuntimeError(f"Tool observer is missing required capability {name}")
        return value


class LegacyToolTransaction:
    """Named outer exception for existing non-product host characterization."""
    def __init__(self, txn, finish):
        if txn is None or not callable(getattr(txn, "begin", None)) or not callable(finish):
            raise TypeError("Legacy ToolRuntime requires transaction begin/finish")
        self.txn = txn
        self._finish = finish

    @property
    def active(self):
        return self.txn.active

    def begin(self):
        self.txn.begin()

    def finish(self, has_write, all_succeeded, messages):
        return self._finish(has_write, all_succeeded, messages)


class LegacyToolRuntime(ProductionToolRuntime):
    """Compatibility entry only; native service graph uses the strict core."""

    def __init__(self, runtime=None):
        super().__init__()
        self.runtime = runtime

    def execute_batch_sync(self, host, calls, messages, on_tool=None, on_text=None, **kwargs):
        context = self._context(host, sync=True)
        if self.runtime is not None:
            return self.runtime.execute_batch_sync(context, calls, messages, on_tool=on_tool, on_text=on_text, **kwargs)
        return super().execute_batch_sync(context, calls, messages, on_tool, on_text, **kwargs)

    async def execute_batch_async(self, host, calls, messages, on_tool=None, on_text=None, **kwargs):
        context = self._context(host)
        if self.runtime is not None:
            return await self.runtime.execute_batch_async(context, calls, messages, on_tool=on_tool, on_text=on_text, **kwargs)
        return await super().execute_batch_async(context, calls, messages, on_tool, on_text, **kwargs)

    def approve_tool_calls_sync(self, host, calls, messages):
        context = host if isinstance(host, ToolExecutionContext) else tool_context_from_legacy_host(host, sync=True)
        return super().approve_tool_calls_sync(context, calls, messages)

    def dispatch_sync(self, host, calls, has_write, messages, **kwargs):
        context = host if isinstance(host, ToolExecutionContext) else tool_context_from_legacy_host(host, sync=True)
        return super().dispatch_sync(context, calls, has_write, messages, **kwargs)

    @staticmethod
    def _context(host, *, sync=False):
        if isinstance(host, ToolExecutionContext):
            return host
        context = tool_context_from_legacy_host(host, sync=sync)
        # Only this explicitly legacy entry honors old caller overrides.
        # Native assembly never selects or introspects these methods.
        return replace(context, lifecycle=replace(
            context.lifecycle,
            write_override=_legacy_override(host, "_tool_batch_has_write"),
            dispatch_override_async=_legacy_override(host, "_dispatch_tool_calls_async"),
            dispatch_override_sync=_legacy_override(host, "_dispatch_tool_calls"),
            consume_override=_legacy_override(host, "_consume_dispatched_tools"),
        ))
