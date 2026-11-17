"""Complete tool-batch lifecycle shared by Agent hosts.

The pipeline owns transaction ordering, dispatch settlement, result projection,
post-processing, and cancellation cleanup. Hosts provide policy callbacks but
do not reimplement this lifecycle.
"""
from __future__ import annotations

from contextlib import nullcontext

import asyncio
import copy
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.adapters.tool import (
    policy_context_from_legacy_host,
    projection_context_from_legacy_host,
    tool_context_from_legacy_host,
    commit_execution_handoff_result,
)
from nz_coder.runtime.core.tool_context import ToolExecutionContext, ToolPolicyContext
from nz_coder.runtime.agent.guardrails import GuardrailEscalateError
from nz_coder.runtime.agent.task_intent import submit_task_assessment
from nz_coder.runtime.agent.planning_runtime import submit_working_plan
from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
from nz_coder.runtime.agent.auto_mode import parse_tool_arguments
from nz_coder.runtime.conversation.input_preflight import ProductionInputPreflight
from nz_coder.tool_platform.execution import (
    ToolExecutionResult,
    skipped_after_approval,
    is_transactional_write_tool,
)
from nz_coder.runtime.tool_runtime.scheduler import (
    _execute_scheduled,
    _execute_scheduled_async,
    _execute_with_tool_cancellation,
    _new_tool_cancellation_scope,
)
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.runtime.tool_runtime.envelope import (
    approved_tool_call,
    normalize_raw_tool_calls,
)
from nz_coder.tools import (
    get_specs,
    scoped_dynamic_tool_snapshot,
    scoped_tool_metadata_reporter,
)
from nz_coder.protocol.public_error import to_public_error
from nz_coder.tools.question import scoped_question_lifecycle_reporter
from nz_coder.tool_platform.permissioning.command_authorization import CommandApprovalBatch


@dataclass
class ApprovedToolBatch:
    """Tool calls that crossed repair/guardrail admission exactly once."""

    calls: list[dict]
    blocked: dict[int, ToolExecutionResult] = field(default_factory=dict)
    approvals: CommandApprovalBatch = field(default_factory=CommandApprovalBatch, repr=False)


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

    def approve_tool_calls_sync(
        self,
        host,
        tool_calls_raw: list,
        messages: list,
    ) -> ApprovedToolBatch:
        """Apply tool guardrails before any SessionProcessor publication."""
        original, repairs = normalize_raw_tool_calls(
            copy.deepcopy(list(tool_calls_raw[:current_run_settings().max_tool_calls])),
            _candidate_tool_names(),
        )
        trace = getattr(getattr(host, "tracer", None), "log", lambda *_a, **_k: None)
        for repair in repairs:
            trace("tool_call_repaired", **repair)
        calls: list[dict] = []
        blocked: dict[int, ToolExecutionResult] = {}
        approvals = CommandApprovalBatch()
        guardrails = _guardrail_runtime(host)
        before_tool = getattr(guardrails, "before_tool_sync", guardrails.before_tool)
        stopped = False
        for index, tool_call in enumerate(original):
            if stopped:
                guarded, rejected = tool_call, skipped_after_approval(tool_call)
            else:
                with approvals.collecting(index):
                    guarded, rejected = asyncio.run(before_tool(host, tool_call, messages))
            function = guarded.get("function", {})
            parsed = parse_tool_arguments(function.get("arguments", {}))
            if parsed is None:
                parsed = {}
                rejected = rejected or _invalid_tool_arguments_result(guarded)
            calls.append(approved_tool_call(guarded, parsed).to_wire())
            if rejected is not None:
                blocked[index] = rejected
                stopped = stopped or rejected.approval_stopped
        for index, result in self._static_policy_rejections(policy_context_from_legacy_host(host), calls).items():
            blocked.setdefault(index, result)
        return ApprovedToolBatch(calls, blocked, approvals)

    async def approve_tool_calls_async(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list,
        messages: list,
    ) -> ApprovedToolBatch:
        """Apply tool guardrails/admission while raw envelopes stay private."""
        original, repairs = normalize_raw_tool_calls(
            copy.deepcopy(list(tool_calls_raw[:current_run_settings().max_tool_calls])),
            _candidate_tool_names(),
        )
        for repair in repairs:
            context.lifecycle.trace("tool_call_repaired", **repair)
        calls: list[dict] = []
        blocked: dict[int, ToolExecutionResult] = {}
        approvals = CommandApprovalBatch()
        stopped = False
        for index, tool_call in enumerate(original):
            if stopped:
                guarded, rejected = tool_call, skipped_after_approval(tool_call)
            else:
                with approvals.collecting(index):
                    guarded, rejected = await context.lifecycle.before_tool(tool_call, messages)
            function = guarded.get("function", {})
            parsed = parse_tool_arguments(function.get("arguments", {}))
            if parsed is None:
                parsed = {}
                rejected = rejected or _invalid_tool_arguments_result(guarded)
            calls.append(approved_tool_call(guarded, parsed).to_wire())
            if rejected is not None:
                blocked[index] = rejected
                stopped = stopped or rejected.approval_stopped
        for index, result in self._static_policy_rejections(context.policy, calls).items():
            blocked.setdefault(index, result)
        return ApprovedToolBatch(calls, blocked, approvals)

    def _static_policy_rejections(
        self,
        context: ToolPolicyContext,
        calls: list[dict],
    ) -> dict[int, ToolExecutionResult]:
        """Finish non-interactive admission before a ToolPart is registered."""
        blocked: dict[int, ToolExecutionResult] = {}
        blocked.update(self.policy.agent_tool_rejections(context, calls))
        blocked.update(self.policy.admission_tool_rejections(context, calls))
        blocked.update(self.policy.strict_private_path_rejections(context, calls))
        blocked.update(self.policy.task_constraint_rejections(context, calls))
        blocked.update(self.policy.edit_recovery_write_rejections(context, calls))
        blocked.update(self.policy.implementation_phase_rejections(context, calls))
        blocked.update(self.policy.closure_phase_rejections(context, calls))
        blocked.update(self.policy.strict_progress_rejections(context, calls))
        return blocked

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
        approved_batch: ApprovedToolBatch | None = None,
    ) -> str:
        """执行一批工具调用，并分发执行后的状态更新。"""
        policy_context = policy_context_from_legacy_host(host)
        projection_context = projection_context_from_legacy_host(host)
        repeat_run = getattr(host, "active_run_context", None)
        repeat_source = host.recovery.capture_repeat_source(
            host.runtime_state, repeat_run, getattr(host, "current_agent_name", ""), messages,
        )
        resolver = getattr(host, "_processor_for_latest_assistant", None)
        if processor is None and callable(resolver):
            processor = resolver(messages)
        approved_batch = approved_batch or self.approve_tool_calls_sync(
            host,
            tool_calls_raw,
            messages,
        )
        tool_calls_raw[:] = approved_batch.calls
        if processor is not None:
            processor.start_tools(approved_batch.calls)
            host._checkpoint_messages(messages, "running")
        write_override = _legacy_dispatch_override(host, "_tool_batch_has_write")
        has_write = (
            write_override(tool_calls_raw)
            if write_override is not None
            else self.policy.tool_batch_has_write(policy_context, tool_calls_raw)
        )
        rollback_needed = has_write
        try:
            if has_write:
                host.txn.begin()
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
                        approved_batch=approved_batch,
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
            # 回滚本轮只尝试一次；提交失败仍需进入异常回滚。
            if not batch_state["all_succeeded"]:
                rollback_needed = False
            host._finish_tool_transaction(
                has_write,
                batch_state["all_succeeded"],
                messages,
            )
            rollback_needed = False
            if host._strict_verification_completed(dispatched):
                batch_state["terminal"] = True
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
        except BaseException as error:
            failures = []
            if rollback_needed and host.txn.active:
                try:
                    host._finish_tool_transaction(has_write, False, messages)
                except BaseException as cleanup_error:
                    failures.append(("transaction", cleanup_error))
            if processor is not None:
                try:
                    processor.interrupt_unsettled()
                    host._checkpoint_messages(messages, "interrupted")
                except BaseException as cleanup_error:
                    failures.append(("checkpoint", cleanup_error))
            _raise_batch_failure(error, failures, host.tracer.log)

        _record_repeat_evidence(
            host.recovery, host.runtime_state, repeat_run,
            getattr(host, "current_agent_name", ""), repeat_source, dispatched, messages,
        )

        if has_write and batch_state["all_succeeded"]:
            record_committed = getattr(host.executor, "record_committed_writes", None)
            if callable(record_committed):
                record_committed(dispatched)
            if host._admission_session is not None:
                for _index, _tool_call, result in dispatched:
                    host._admission_session.record_committed_mutation(result)
            with repeat_run._state_lock if repeat_run is not None else nullcontext():
                host.recovery.reset_tool_call_history(reason="workspace_changed")
            self.policy.trace_tool_streak_reset(policy_context)
            host._refresh_patch_risk(messages)
            host._refresh_code_index(dispatched)
            host._attach_lsp_write_diagnostics(dispatched, messages)
        from nz_coder.runtime.agent.planning_runtime import observe_execution_handoff
        observe_execution_handoff(host.runtime_state, dispatched, messages,
                                  committed_writes=has_write and batch_state["all_succeeded"])
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
        approved_batch: ApprovedToolBatch | None = None,
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
                approved_batch=approved_batch,
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
        approved_batch: ApprovedToolBatch | None = None,
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

        approved_batch = approved_batch or await self.approve_tool_calls_async(
            context,
            tool_calls_raw,
            messages,
        )
        tool_calls_raw[:] = approved_batch.calls
        if processor is not None:
            processor.start_tools(approved_batch.calls)
            await checkpoint_state("running")
        write_override = lifecycle.write_override
        has_write = (
            write_override(tool_calls_raw)
            if write_override is not None
            else self.policy.tool_batch_has_write(policy_context, tool_calls_raw)
        )
        rollback_needed = has_write
        try:
            if has_write:
                lifecycle.begin_transaction()
            reporter = lifecycle.metadata_reporter(processor, messages)
            question_reporter = lifecycle.question_reporter(processor, messages)
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
                        approved_batch=approved_batch,
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
            if not batch_state["all_succeeded"]:
                rollback_needed = False
            lifecycle.finish_transaction(
                has_write,
                batch_state["all_succeeded"],
                messages,
            )
            rollback_needed = False
            if lifecycle.strict_completed(dispatched):
                batch_state["terminal"] = True
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
        except BaseException as error:
            failures = []
            # 调度器排空工作线程后先结清事务，存盘或Part故障不能跳过恢复。
            if rollback_needed and lifecycle.transaction_active():
                try:
                    lifecycle.finish_transaction(has_write, False, messages)
                except BaseException as cleanup_error:
                    failures.append(("transaction", cleanup_error))
            if processor is not None and not isinstance(error, GuardrailEscalateError):
                try:
                    processor.interrupt_unsettled()
                    await checkpoint_state("interrupted")
                except BaseException as cleanup_error:
                    failures.append(("checkpoint", cleanup_error))
            _raise_batch_failure(error, failures, lifecycle.trace)

        if not context.request_superseded():
            _record_repeat_evidence(
                context.policy.recovery, context.policy.runtime_state, context.run,
                context.current_agent_name() if context.run is not None else "",
                context.repeat_source, dispatched, messages,
            )

        if has_write and batch_state["all_succeeded"]:
            record_committed = getattr(lifecycle.executor, "record_committed_writes", None)
            if callable(record_committed):
                record_committed(dispatched)
            try:
                await lifecycle.observer.post_write_async(dispatched,messages)
            except BaseException as error:
                # 写事务已经提交；重复取消也不能截断 patch 和检查点结算。
                if processor is not None:
                    async def settle_committed_write():
                        failures = []
                        try:
                            finish_snapshot = await lifecycle.observer.capture_snapshot(processor)
                            lifecycle.observer.record_patch(messages, processor, finish_snapshot)
                        except BaseException as cleanup_error:
                            failures.append(("patch", cleanup_error))
                        try:
                            processor.interrupt_unsettled()
                            await checkpoint_state("interrupted")
                        except BaseException as cleanup_error:
                            failures.append(("checkpoint", cleanup_error))
                        return failures

                    settlement = asyncio.create_task(settle_committed_write())
                    while not settlement.done():
                        try:
                            await asyncio.shield(settlement)
                        except asyncio.CancelledError:
                            continue
                    _raise_batch_failure(error, settlement.result(), lifecycle.trace)
                raise
            self.policy.trace_tool_streak_reset(policy_context)
        batch_state["dispatched"] = dispatched
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
        approved_batch: ApprovedToolBatch | None = None,
    ) -> list:
        """只分发本轮允许执行的工具调用前缀。"""

        policy_context = policy_context or policy_context_from_legacy_host(host)
        approved_batch = approved_batch or self.approve_tool_calls_sync(
            host,
            tool_calls_raw,
            messages,
        )
        def execute_approved(call, index):
            with approved_batch.approvals.executing(index, call):
                return host._execute_tool_call_with_hooks(call, index, messages)

        try:
            will_execute = approved_batch.calls
            guardrail_blocked = dict(approved_batch.blocked)
            batch_id, started = self.policy.begin_tool_batch(
                policy_context, will_execute, has_write,
            )
            segments: list[dict] = []
            blocked = dict(guardrail_blocked)
            if not any(result.approval_stopped for result in blocked.values()):
                blocked = {index: result for index, result in
                           self.policy.find_repeated_tool_calls(policy_context, will_execute).items()
                           if index not in guardrail_blocked}
                blocked = self.policy.resolve_doom_loop_permissions(policy_context, blocked, will_execute)
                blocked.update(guardrail_blocked)
            blocked = _stop_unstarted_calls(will_execute, blocked)
            mode = "scheduled"
            try:
                if len(will_execute) > 1 and not blocked and not host.hooks.has_pre_tool_use_hooks():
                    dispatched = _execute_scheduled(
                        _BoundToolExecution(execute_approved),
                        will_execute,
                        lambda call: self.policy.tool_call_can_run_concurrently(
                            policy_context, call,
                        ),
                        on_segment=segments.append,
                    )
                else:
                    mode = "single" if len(will_execute) <= 1 else "sequential_guarded"
                    dispatched = []
                    stopped = False
                    for i, tc in enumerate(will_execute):
                        result = blocked.get(i) or (
                            skipped_after_approval(tc) if stopped else execute_approved(tc, i)
                        )
                        dispatched.append((i, tc, result))
                        stopped = stopped or result.approval_stopped
            except BaseException as exc:
                self.policy.finish_tool_batch_observation(
                    policy_context,
                    batch_id=batch_id,
                    started=started,
                    mode=mode,
                    dispatched=[],
                    segments=segments,
                    error=to_public_error(exc).message,
                )
                raise
            transformed = []
            for index, tool_call, result in dispatched:
                if index not in guardrail_blocked:
                    result = asyncio.run(_guardrail_runtime(host).after_tool(host, tool_call, result, messages))
                    # 同步独立调用没有冻结的模型请求来源，只返回明确拒绝，不能发布判断。
                    result = submit_task_assessment(
                        policy_context.runtime_state, None, tool_call, result, messages, run_context=None,
                    )
                    result = submit_working_plan(
                        policy_context.runtime_state, None, tool_call, result, messages, run_context=None,
                    )
                transformed.append((index, tool_call, commit_execution_handoff_result(host, tool_call, result, messages)))
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
        finally:
            approved_batch.approvals.close()

    async def dispatch_async(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list,
        has_write: bool,
        messages: list,
        *,
        policy_context: ToolPolicyContext | None = None,
        approved_batch: ApprovedToolBatch | None = None,
    ) -> list:
        """Async variant for dispatching the executable tool prefix."""
        policy_context = policy_context or context.policy
        lifecycle = context.lifecycle
        approved_batch = approved_batch or await self.approve_tool_calls_async(
            context,
            tool_calls_raw,
            messages,
        )
        def execute_approved(call, index, _messages=None):
            with approved_batch.approvals.executing(index, call):
                return lifecycle.execute_one(call, index, messages)

        try:
            will_execute = approved_batch.calls
            guardrail_blocked = dict(approved_batch.blocked)
            batch_id, started = self.policy.begin_tool_batch(
                policy_context, will_execute, has_write,
            )
            segments: list[dict] = []
            blocked = dict(guardrail_blocked)
            if not any(result.approval_stopped for result in blocked.values()):
                blocked = {index: result for index, result in
                           self.policy.find_repeated_tool_calls(policy_context, will_execute).items()
                           if index not in guardrail_blocked}
                blocked = await self.policy.resolve_doom_loop_permissions_async(policy_context, blocked, will_execute)
                blocked.update(guardrail_blocked)
            blocked = _stop_unstarted_calls(will_execute, blocked)
            mode = "scheduled"
            try:
                if len(will_execute) > 1 and not blocked and not lifecycle.has_pre_tool_hooks():
                    dispatched = await _execute_scheduled_async(
                        _BoundToolExecution(execute_approved),
                        will_execute,
                        lambda call: self.policy.tool_call_can_run_concurrently(
                            policy_context, call,
                        ),
                        on_segment=segments.append,
                    )
                else:
                    mode = "single" if len(will_execute) <= 1 else "sequential_guarded"
                    dispatched = []
                    stopped = False
                    for i, tc in enumerate(will_execute):
                        result = blocked.get(i)
                        if result is None and stopped:
                            result = skipped_after_approval(tc)
                        if result is None:
                            cancel_event = _new_tool_cancellation_scope()
                            result = await _to_thread_settled(
                                _execute_with_tool_cancellation,
                                cancel_event,
                                execute_approved,
                                tc,
                                i,
                                messages,
                                cancel_callback=cancel_event.set,
                            )
                        dispatched.append((i, tc, result))
                        stopped = stopped or result.approval_stopped
            except BaseException as exc:
                self.policy.finish_tool_batch_observation(
                    policy_context,
                    batch_id=batch_id,
                    started=started,
                    mode=mode,
                    dispatched=[],
                    segments=segments,
                    error=to_public_error(exc).message,
                )
                raise
            transformed = []
            for index, tool_call, result in dispatched:
                if index not in guardrail_blocked:
                    result = await lifecycle.after_tool(tool_call, result, messages)
                    result = submit_task_assessment(
                        context.policy.runtime_state, context.assessment_scope, tool_call, result, messages,
                        run_context=context.run, path=context.runtime_state_path,
                        superseded=context.request_superseded,
                    )
                    result = submit_working_plan(
                        context.policy.runtime_state, context.working_plan_scope, tool_call, result, messages,
                        run_context=context.run, path=context.runtime_state_path,
                        superseded=context.request_superseded,
                    )
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
        finally:
            approved_batch.approvals.close()


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


def _candidate_tool_names() -> list[str]:
    return [
        str(spec.get("function", {}).get("name") or "")
        for spec in get_specs()
        if str(spec.get("function", {}).get("name") or "")
    ]


def _invalid_tool_arguments_result(tool_call: dict) -> ToolExecutionResult:
    """Settle malformed Provider JSON before the call enters public state."""
    function = tool_call.get("function", {})
    name = str(function.get("name") or "unknown")
    return ToolExecutionResult(
        name=name,
        tool_input={},
        output=(
            f"Error: Invalid JSON arguments for {name}: "
            "tool arguments must be a valid JSON object."
        ),
        executed=False,
        dispatch_failed=True,
        command_failed=False,
        is_write=is_transactional_write_tool(name),
        permission_denied=False,
        metadata={"reason_code": "invalid_tool_arguments"},
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


class _BoundToolExecution:
    """调度只改变执行顺序；每条路径仍经过相同的宿主观察边界。"""


    def __init__(self, execute_one):
        self.execute_one = execute_one


def _stop_unstarted_calls(calls: list[dict], blocked: dict[int, ToolExecutionResult]) -> dict[int, ToolExecutionResult]:
    """前置审批已停止时，已准入但尚未执行的前缀也不能晚执行。"""
    if not any(result.approval_stopped for result in blocked.values()):
        return blocked
    return {index: blocked[index] if index in blocked and blocked[index].approval_stopped else skipped_after_approval(call)
            for index, call in enumerate(calls)}


def _raise_batch_failure(error, failures, trace):
    """执行错误保持为主因；独立清理错误留因果和安全诊断。"""
    for stage, failure in failures:
        try:
            trace("tool_batch_cleanup_failed", stage=stage, public_error=to_public_error(failure).to_dict())
        except Exception:
            pass
    if failures:
        raise error from failures[0][1]
    raise error


def _record_repeat_evidence(recovery, state, run, persona, source, dispatched, messages):
    def record():
        for _index, _call, result in dispatched:
            recovery.record_tool_result_evidence(
                result.name, result.tool_input, result.output,
                executed=result.executed, dispatch_failed=result.dispatch_failed,
                command_failed=result.command_failed,
                cache_hit=bool((result.metadata or {}).get("read_cache_hit")),
                metadata=result.metadata, source=source,
            )
    if run is None:
        record()
    else:
        with run._state_lock:
            # 比较请求前来源，不在切换或写入后把迟到结果重新归属。
            if source is None or recovery.repeat_source_current(source, state, run, persona, messages):
                record()
