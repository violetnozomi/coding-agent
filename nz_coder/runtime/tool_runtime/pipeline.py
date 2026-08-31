"""Complete tool-batch lifecycle shared by Agent hosts.

The pipeline owns transaction ordering, dispatch settlement, result projection,
post-processing, and cancellation cleanup. Hosts provide policy callbacks but
do not reimplement this lifecycle.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Awaitable, Callable

from nz_coder.foundation import config
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.adapters.tool import (
    policy_context_from_legacy_host,
    projection_context_from_legacy_host,
    tool_context_from_legacy_host,
)
from nz_coder.runtime.core.tool_context import ToolExecutionContext, ToolPolicyContext
from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
from nz_coder.runtime.conversation.input_preflight import ProductionInputPreflight
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.tool_runtime.scheduler import (
    _execute_scheduled,
    _execute_scheduled_async,
    _execute_with_tool_cancellation,
)
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.tools import (
    current_tool_cancel_event,
    scoped_dynamic_tool_snapshot,
    scoped_tool_metadata_reporter,
)
from nz_coder.tools.question import scoped_question_lifecycle_reporter


class ProductionToolRuntime:
    """Settle one tool batch through the canonical lifecycle."""

    def __init__(
        self,
        policy: ProductionToolPolicy | None = None,
        results: ProductionToolResultProjector | None = None,
    ) -> None:
        self.policy = policy or ProductionToolPolicy()
        self.results = results or ProductionToolResultProjector()

    def execute_batch_sync(
        self,
        host,
        tool_calls_raw: list,
        messages: list,
        on_tool=None,
        on_text=None,
        *,
        processor: Any | None = None,
        usage: Any | None = None,
    ) -> str:
        """Execute one batch against one immutable dynamic-tool generation."""
        with scoped_dynamic_tool_snapshot():
            return self._execute_batch_sync_snapshot(
                host,
                tool_calls_raw,
                messages,
                on_tool=on_tool,
                on_text=on_text,
                processor=processor,
                usage=usage,
            )

    def _execute_batch_sync_snapshot(
        self,
        host,
        tool_calls_raw: list,
        messages: list,
        on_tool=None,
        on_text=None,
        *,
        processor: Any | None = None,
        usage: Any | None = None,
    ) -> str:
        """执行一批工具调用，并分发执行后的状态更新。"""
        policy_context = policy_context_from_legacy_host(host)
        projection_context = projection_context_from_legacy_host(host)
        resolver = getattr(host, "_processor_for_latest_assistant", None)
        if processor is None and callable(resolver):
            processor = resolver(messages)
        if processor is not None:
            processor.start_tools(tool_calls_raw)
            host._checkpoint_messages(messages, "running")
        write_override = _legacy_dispatch_override(host, "_tool_batch_has_write")
        has_write = (
            write_override(tool_calls_raw)
            if write_override is not None
            else self.policy.tool_batch_has_write(policy_context, tool_calls_raw)
        )
        if has_write:
            host.txn.begin()

        transaction_finished = False
        callback_factory = getattr(host, "_tool_metadata_callback", None)
        reporter = (
            callback_factory(processor, messages)
            if callable(callback_factory)
            else (lambda _title, _metadata: None)
        )
        question_factory = getattr(host, "_question_lifecycle_callback", None)
        question_reporter = (
            question_factory(processor, messages)
            if callable(question_factory)
            else (lambda _action, _payload: None)
        )
        try:
            with (
                scoped_tool_metadata_reporter(reporter),
                scoped_question_lifecycle_reporter(question_reporter),
            ):
                legacy_dispatch = _legacy_dispatch_override(
                    host, "_dispatch_tool_calls",
                )
                dispatched = (
                    legacy_dispatch(tool_calls_raw, has_write, messages)
                    if legacy_dispatch is not None
                    else self.dispatch_sync(
                        host,
                        tool_calls_raw,
                        has_write,
                        messages,
                        policy_context=policy_context,
                    )
                )
            describe_interrupted = False
            if _has_read_image_result(dispatched, getattr(host, "model_capabilities", None)):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    describe_interrupted = asyncio.run(
                        _input_preflight(host).describe_read_results(
                            host, dispatched, messages,
                        )
                    )
                else:
                    host.tracer.log(
                        "read_image_describe_skipped",
                        reason="sync_tool_pipeline_inside_event_loop",
                    )
            consume_kwargs = {"on_tool": on_tool}
            if processor is not None:
                consume_kwargs["processor"] = processor
            consume_override = _legacy_dispatch_override(
                host, "_consume_dispatched_tools",
            )
            batch_state = (
                consume_override(dispatched, messages, **consume_kwargs)
                if consume_override is not None
                else self.results.consume(
                    projection_context, dispatched, messages, **consume_kwargs,
                )
            )
            if host._strict_verification_completed(dispatched):
                batch_state["terminal"] = True
            host._finish_tool_transaction(
                has_write,
                batch_state["all_succeeded"],
                messages,
            )
            transaction_finished = True
            signal = batch_state.get("handoff_signal")
            if signal is not None:
                transition = host.runtime_services.transitions.apply(
                    host, signal, messages, processor,
                )
                batch_state["agent_transition"] = transition
                batch_state["terminal"] = bool(
                    transition and transition.get("terminal")
                )
                host._notify_agent_switched(transition)
            if describe_interrupted:
                raise asyncio.CancelledError
        except BaseException:
            if processor is not None:
                processor.interrupt_unsettled()
                host._checkpoint_messages(messages, "interrupted")
            if has_write and not transaction_finished and host.txn.active:
                host._finish_tool_transaction(has_write, False, messages)
            raise

        if has_write and batch_state["all_succeeded"]:
            if host._admission_session is not None:
                for _index, _tool_call, result in dispatched:
                    host._admission_session.record_committed_mutation(result)
            host.recovery.reset_tool_call_history(reason="workspace_changed")
            self.policy.trace_tool_streak_reset(policy_context)
            host._refresh_patch_risk(messages)
            host._refresh_code_index(dispatched)
            host._attach_lsp_write_diagnostics(dispatched, messages)
        host.hooks.after_tool_batch(
            host,
            messages,
            manual_compact=batch_state["manual_compact"],
            used_todo=batch_state["used_todo"],
            on_text=on_text,
            write_total=batch_state["write_total"],
            write_denied=batch_state["write_denied"],
        )
        host._apply_pending_plan_mode()
        if processor is not None:
            finish_snapshot = (
                host._capture_step_snapshot("step-finish", processor.message_id)
                if processor.step_snapshot else None
            )
            processor.finish_step(
                (usage.finish_reason if usage is not None else "") or "tool-calls",
                input_tokens=(usage.input_tokens if usage is not None else 0),
                output_tokens=(usage.output_tokens if usage is not None else 0),
                total_tokens=(usage.total_tokens if usage is not None else 0),
                reasoning_tokens=(usage.reasoning_tokens if usage is not None else 0),
                cache_read_tokens=(usage.cache_read_tokens if usage is not None else 0),
                cache_write_tokens=(usage.cache_write_tokens if usage is not None else 0),
                cost=(usage.cost if usage is not None and usage.cost_known else None),
                snapshot=finish_snapshot,
            )
            host._record_step_patch(messages, processor, finish_snapshot)
            host._checkpoint_messages(messages, "running")
        action = (
            processor.process_result()
            if processor is not None
            else ("stop" if batch_state["blocked"] else "continue")
        )
        if batch_state.get("terminal"):
            action = "terminal"
        host.tracer.log(
            "step_processor_result",
            result=action,
            blocked=bool(batch_state["blocked"]),
            tool_calls=len(tool_calls_raw),
        )
        return action

    async def execute_batch_async(
        self,
        owner,
        tool_calls_raw: list,
        messages: list,
        on_tool=None,
        on_text=None,
        *,
        processor: Any | None = None,
        usage: Any | None = None,
        finish_step: bool = True,
        checkpoint: Callable[[str], Awaitable[None]] | None = None,
        tool_context: ToolExecutionContext | None = None,
    ) -> str:
        """Execute one async batch against one dynamic-tool generation."""
        with scoped_dynamic_tool_snapshot():
            return await self._execute_batch_async_snapshot(
                owner,
                tool_calls_raw,
                messages,
                on_tool=on_tool,
                on_text=on_text,
                processor=processor,
                usage=usage,
                finish_step=finish_step,
                checkpoint=checkpoint,
                tool_context=tool_context,
            )

    async def _execute_batch_async_snapshot(
        self,
        owner,
        tool_calls_raw: list,
        messages: list,
        on_tool=None,
        on_text=None,
        *,
        processor: Any | None = None,
        usage: Any | None = None,
        finish_step: bool = True,
        checkpoint: Callable[[str], Awaitable[None]] | None = None,
        tool_context: ToolExecutionContext | None = None,
    ) -> str:
        """Async variant of one tool batch execution."""
        context = (
            owner
            if isinstance(owner, ToolExecutionContext)
            else tool_context or tool_context_from_legacy_host(owner)
        )
        policy_context = context.policy
        lifecycle = context.lifecycle
        if processor is None:
            processor = lifecycle.processor_for_messages(messages)

        async def checkpoint_state(status: str) -> None:
            if checkpoint is not None:
                await checkpoint(status)
            else:
                await lifecycle.checkpoint(messages, status)

        if processor is not None:
            processor.start_tools(tool_calls_raw)
            await checkpoint_state("running")
        write_override = lifecycle.write_override
        has_write = (
            write_override(tool_calls_raw)
            if write_override is not None
            else self.policy.tool_batch_has_write(policy_context, tool_calls_raw)
        )
        if has_write:
            lifecycle.begin_transaction()

        transaction_finished = False
        reporter = lifecycle.metadata_reporter(processor, messages)
        question_reporter = lifecycle.question_reporter(processor, messages)
        try:
            with (
                scoped_tool_metadata_reporter(reporter),
                scoped_question_lifecycle_reporter(question_reporter),
            ):
                legacy_dispatch = lifecycle.dispatch_override_async
                dispatched = (
                    await legacy_dispatch(tool_calls_raw, has_write, messages)
                    if legacy_dispatch is not None
                    else await self.dispatch_async(
                        context, tool_calls_raw, has_write, messages,
                        policy_context=policy_context,
                    )
                )
            describe_interrupted = await lifecycle.describe_read_results(
                dispatched, messages,
            )
            consume_kwargs = {"on_tool": on_tool}
            if processor is not None:
                consume_kwargs["processor"] = processor
            consume_override = lifecycle.consume_override
            batch_state = (
                consume_override(dispatched, messages, **consume_kwargs)
                if consume_override is not None
                else self.results.consume(
                    context.projection, dispatched, messages, **consume_kwargs,
                )
            )
            if lifecycle.strict_completed(dispatched):
                batch_state["terminal"] = True
            lifecycle.finish_transaction(
                has_write,
                batch_state["all_succeeded"],
                messages,
            )
            transaction_finished = True
            signal = batch_state.get("handoff_signal")
            if signal is not None:
                transition = await lifecycle.apply_transition(
                    signal, messages, processor,
                )
                batch_state["agent_transition"] = transition
                batch_state["terminal"] = bool(
                    transition and transition.get("terminal")
                )
            if describe_interrupted:
                raise asyncio.CancelledError
        except BaseException:
            if processor is not None:
                processor.interrupt_unsettled()
                await checkpoint_state("interrupted")
            # Executor cancellation cannot stop an already-running write. The
            # scheduler drains it before re-raising, then this rollback keeps
            # late side effects from escaping an interrupted Agent turn.
            if has_write and not transaction_finished and lifecycle.transaction_active():
                lifecycle.finish_transaction(has_write, False, messages)
            raise

        if has_write and batch_state["all_succeeded"]:
            lifecycle.observer.post_write(dispatched, messages)
            self.policy.trace_tool_streak_reset(policy_context)
        lifecycle.observer.after_batch(messages, batch_state, on_text)
        lifecycle.observer.apply_plan_mode()
        if processor is not None and finish_step:
            finish_snapshot = await lifecycle.observer.capture_snapshot(processor)
            processor.finish_step(
                (usage.finish_reason if usage is not None else "") or "tool-calls",
                input_tokens=(usage.input_tokens if usage is not None else 0),
                output_tokens=(usage.output_tokens if usage is not None else 0),
                total_tokens=(usage.total_tokens if usage is not None else 0),
                reasoning_tokens=(usage.reasoning_tokens if usage is not None else 0),
                cache_read_tokens=(usage.cache_read_tokens if usage is not None else 0),
                cache_write_tokens=(usage.cache_write_tokens if usage is not None else 0),
                cost=(usage.cost if usage is not None and usage.cost_known else None),
                snapshot=finish_snapshot,
            )
            lifecycle.observer.record_patch(messages, processor, finish_snapshot)
            await checkpoint_state("running")
        action = (
            processor.process_result()
            if processor is not None
            else ("stop" if batch_state["blocked"] else "continue")
        )
        if batch_state.get("terminal"):
            action = "terminal"
        lifecycle.trace(
            "step_processor_result",
            result=action,
            blocked=bool(batch_state["blocked"]),
            tool_calls=len(tool_calls_raw),
        )
        return action


    def dispatch_sync(
        self,
        host,
        tool_calls_raw: list,
        has_write: bool,
        messages: list,
        *,
        policy_context: ToolPolicyContext | None = None,
    ) -> list:
        """只分发本轮允许执行的工具调用前缀。"""

        policy_context = policy_context or policy_context_from_legacy_host(host)
        original = tool_calls_raw[:config.MAX_TOOL_CALLS_PER_RESPONSE]
        will_execute = []
        guardrail_blocked: dict[int, ToolExecutionResult] = {}
        guardrails = _guardrail_runtime(host)
        before_tool = getattr(guardrails, "before_tool_sync", guardrails.before_tool)
        for index, tool_call in enumerate(original):
            guarded, rejected = asyncio.run(
                before_tool(
                    host, tool_call, messages,
                )
            )
            tool_calls_raw[index] = guarded
            will_execute.append(guarded)
            if rejected is not None:
                guardrail_blocked[index] = rejected
        batch_id, started = self.policy.begin_tool_batch(
            policy_context, will_execute, has_write,
        )
        segments: list[dict] = []
        blocked = self.policy.find_repeated_tool_calls(policy_context, will_execute)
        blocked = self.policy.resolve_doom_loop_permissions(
            policy_context, blocked, will_execute,
        )
        blocked.update(self.policy.agent_tool_rejections(policy_context, will_execute))
        blocked.update(self.policy.admission_tool_rejections(policy_context, will_execute))
        blocked.update(self.policy.strict_private_path_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.task_constraint_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.implementation_phase_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.closure_phase_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.strict_progress_rejections(policy_context, will_execute))
        blocked.update(guardrail_blocked)
        mode = "scheduled"
        try:
            if len(will_execute) > 1 and not blocked and not host.hooks.has_pre_tool_use_hooks():
                dispatched = _execute_scheduled(
                    host.executor,
                    will_execute,
                    lambda call: self.policy.tool_call_can_run_concurrently(
                        policy_context, call,
                    ),
                    on_segment=segments.append,
                )
            else:
                mode = "single" if len(will_execute) <= 1 else "sequential_guarded"
                dispatched = [
                    (
                        i,
                        tc,
                        blocked.get(i) or host._execute_tool_call_with_hooks(tc, i, messages),
                    )
                    for i, tc in enumerate(will_execute)
                ]
        except BaseException as exc:
            self.policy.finish_tool_batch_observation(
                policy_context,
                batch_id=batch_id,
                started=started,
                mode=mode,
                dispatched=[],
                segments=segments,
                error=str(exc) or type(exc).__name__,
            )
            raise
        dispatched = [
            (
                index,
                tool_call,
                result
                if index in guardrail_blocked
                else asyncio.run(
                    _guardrail_runtime(host).after_tool(
                        host, tool_call, result, messages,
                    )
                ),
            )
            for index, tool_call, result in dispatched
        ]
        self.policy.finish_tool_batch_observation(
            policy_context,
            batch_id=batch_id,
            started=started,
            mode=mode,
            dispatched=dispatched,
            segments=segments,
        )
        return dispatched

    async def dispatch_async(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list,
        has_write: bool,
        messages: list,
        *,
        policy_context: ToolPolicyContext | None = None,
    ) -> list:
        """Async variant for dispatching the executable tool prefix."""
        policy_context = policy_context or context.policy
        lifecycle = context.lifecycle
        original = tool_calls_raw[:config.MAX_TOOL_CALLS_PER_RESPONSE]
        will_execute = []
        guardrail_blocked: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(original):
            guarded, rejected = await lifecycle.before_tool(tool_call, messages)
            tool_calls_raw[index] = guarded
            will_execute.append(guarded)
            if rejected is not None:
                guardrail_blocked[index] = rejected
        batch_id, started = self.policy.begin_tool_batch(
            policy_context, will_execute, has_write,
        )
        segments: list[dict] = []
        blocked = self.policy.find_repeated_tool_calls(policy_context, will_execute)
        blocked = await self.policy.resolve_doom_loop_permissions_async(
            policy_context, blocked, will_execute,
        )
        blocked.update(self.policy.agent_tool_rejections(policy_context, will_execute))
        blocked.update(self.policy.admission_tool_rejections(policy_context, will_execute))
        blocked.update(self.policy.strict_private_path_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.task_constraint_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.implementation_phase_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.closure_phase_rejections(
            policy_context, will_execute,
        ))
        blocked.update(self.policy.strict_progress_rejections(policy_context, will_execute))
        blocked.update(guardrail_blocked)
        mode = "scheduled"
        try:
            if len(will_execute) > 1 and not blocked and not lifecycle.has_pre_tool_hooks():
                dispatched = await _execute_scheduled_async(
                    lifecycle.executor,
                    will_execute,
                    lambda call: self.policy.tool_call_can_run_concurrently(
                        policy_context, call,
                    ),
                    on_segment=segments.append,
                )
            else:
                mode = "single" if len(will_execute) <= 1 else "sequential_guarded"
                dispatched = []
                for i, tc in enumerate(will_execute):
                    result = blocked.get(i)
                    if result is None:
                        cancel_event = current_tool_cancel_event() or threading.Event()
                        result = await _to_thread_settled(
                            _execute_with_tool_cancellation,
                            cancel_event,
                            lifecycle.execute_one,
                            tc,
                            i,
                            messages,
                            cancel_callback=cancel_event.set,
                        )
                    dispatched.append((i, tc, result))
        except BaseException as exc:
            self.policy.finish_tool_batch_observation(
                policy_context,
                batch_id=batch_id,
                started=started,
                mode=mode,
                dispatched=[],
                segments=segments,
                error=str(exc) or type(exc).__name__,
            )
            raise
        transformed = []
        for index, tool_call, result in dispatched:
            if index not in guardrail_blocked:
                result = await lifecycle.after_tool(tool_call, result, messages)
            transformed.append((index, tool_call, result))
        dispatched = transformed
        self.policy.finish_tool_batch_observation(
            policy_context,
            batch_id=batch_id,
            started=started,
            mode=mode,
            dispatched=dispatched,
            segments=segments,
        )
        return dispatched


def _has_read_image_result(dispatched: list, capabilities) -> bool:
    """Return whether a non-vision host must describe Read image attachments."""
    if bool(getattr(capabilities, "supports_image_input", False)):
        return False
    return any(
        result.name == "read_file"
        and not result.dispatch_failed
        and bool(result.attachments)
        for _index, _tool_call, result in dispatched
    )


def _guardrail_runtime(host):
    """Resolve the production service or its compatibility implementation."""
    services = getattr(host, "runtime_services", None)
    return (
        services.guardrails
        if services is not None
        else ProductionGuardrailRuntime()
    )


def _input_preflight(host):
    """Resolve the production service or its compatibility implementation."""
    services = getattr(host, "runtime_services", None)
    return services.inputs if services is not None else ProductionInputPreflight()


def _legacy_dispatch_override(host, name: str):
    """Honor characterization harness overrides without coupling to AgentLoop."""
    candidate = getattr(host, name, None)
    if not callable(candidate):
        return None
    function = getattr(candidate, "__func__", candidate)
    if getattr(function, "__module__", "") == "nz_coder.runtime.execution.loop":
        return None
    return candidate


async def _checkpoint_async(
    host,
    messages: list,
    status: str,
    checkpoint: Callable[[str], Awaitable[None]] | None,
) -> None:
    """Persist through SessionRuntime, with fallback only outside active runs."""
    if checkpoint is not None:
        await checkpoint(status)
        return
    if getattr(host, "active_run_context", None) is not None:
        raise RuntimeError(
            "Active Tool Runtime requires a SessionRuntime checkpoint callback"
        )
    legacy = getattr(host, "_checkpoint_messages", None)
    if callable(legacy):
        legacy(messages, status)
