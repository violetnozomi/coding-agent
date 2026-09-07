"""Complete tool-batch lifecycle shared by Agent hosts.

The pipeline owns transaction ordering, dispatch settlement, result projection,
post-processing, and cancellation cleanup. Hosts provide policy callbacks but
do not reimplement this lifecycle.
"""
from __future__ import annotations

import asyncio
import copy
import threading
from typing import Awaitable, Callable, TypeVar

from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.session.session_processor import SessionProcessor

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.foundation.async_utils import await_settled
from nz_coder.runtime.core.tool_context import ToolExecutionContext, ToolPolicyContext
from nz_coder.runtime.core.tool_contracts import (
    ApprovedToolBatch as ApprovedToolBatch, DispatchedTools, ToolBatchState, ToolDisplay, TextDisplay, ToolCheckpoint,
)
from nz_coder.runtime.agent.guardrails import GuardrailEscalateError
from nz_coder.runtime.agent.auto_mode import parse_tool_arguments
from nz_coder.tool_platform.execution import (
    ToolExecutionResult,
    is_transactional_write_tool,
)
from nz_coder.runtime.tool_runtime.scheduler import (
    _execute_scheduled,
    _execute_scheduled_async,
    _execute_with_tool_cancellation,
)
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.runtime.tool_runtime.envelope import (
    approved_tool_call,
    normalize_raw_tool_calls,
)
from nz_coder.tools import (
    current_tool_cancel_event,
    get_specs,
    scoped_dynamic_tool_snapshot,
    scoped_tool_metadata_reporter,
)
from nz_coder.protocol.public_error import to_public_error
from nz_coder.tools.question import scoped_question_lifecycle_reporter
from nz_coder.runtime.process.checkpoint_runtime import register_batch, settle_batch, overlay_recovery, abort_registered


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
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        messages: list[dict],
        on_tool: ToolDisplay | None = None,
        on_text: TextDisplay | None = None,
        *,
        processor: SessionProcessor | None = None,
        usage: LLMResult | None = None,
    ) -> str:
        """Execute one batch against one immutable dynamic-tool generation."""
        _require_context(context)
        _require_sync_entry()
        with scoped_dynamic_tool_snapshot():
            return self._execute_batch_sync_snapshot(
                context,
                tool_calls_raw,
                messages,
                on_tool=on_tool,
                on_text=on_text,
                processor=processor,
                usage=usage,
            )

    def approve_tool_calls_sync(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        messages: list[dict],
    ) -> ApprovedToolBatch:
        """Apply tool guardrails before any SessionProcessor publication."""
        _require_context(context)
        _require_sync_entry()
        original, repairs = normalize_raw_tool_calls(
            copy.deepcopy(list(tool_calls_raw[:current_run_settings().max_tool_calls])),
            _candidate_tool_names(),
        )
        _require_context(context)
        trace = context.lifecycle.trace
        for repair in repairs:
            trace("tool_call_repaired", **repair)
        calls: list[dict] = []
        blocked: dict[int, ToolExecutionResult] = {}
        before_tool = context.lifecycle.before_tool
        for index, tool_call in enumerate(original):
            guarded, rejected = _run_sync(before_tool(tool_call, messages))
            approved, rejected = _normalize_guarded_call(guarded, rejected)
            calls.append(approved)
            if rejected is not None:
                blocked[index] = rejected
        blocked.update(self._static_policy_rejections(
            context.policy,
            calls,
        ))
        return ApprovedToolBatch(calls, blocked)

    async def approve_tool_calls_async(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        messages: list[dict],
    ) -> ApprovedToolBatch:
        """Apply tool guardrails/admission while raw envelopes stay private."""
        _require_context(context)
        original, repairs = normalize_raw_tool_calls(
            copy.deepcopy(list(tool_calls_raw[:current_run_settings().max_tool_calls])),
            _candidate_tool_names(),
        )
        for repair in repairs:
            context.lifecycle.trace("tool_call_repaired", **repair)
        calls: list[dict] = []
        blocked: dict[int, ToolExecutionResult] = {}
        for index, tool_call in enumerate(original):
            guarded, rejected = await context.lifecycle.before_tool(
                tool_call,
                messages,
            )
            approved, rejected = _normalize_guarded_call(guarded, rejected)
            calls.append(approved)
            if rejected is not None:
                blocked[index] = rejected
        blocked.update(self._static_policy_rejections(context.policy, calls))
        return ApprovedToolBatch(calls, blocked)

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
        blocked.update(self.policy.implementation_phase_rejections(context, calls))
        blocked.update(self.policy.closure_phase_rejections(context, calls))
        blocked.update(self.policy.strict_progress_rejections(context, calls))
        return blocked

    def _execute_batch_sync_snapshot(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        messages: list[dict],
        on_tool: ToolDisplay | None = None,
        on_text: TextDisplay | None = None,
        *,
        processor: SessionProcessor | None = None,
        usage: LLMResult | None = None,
        approved_batch: ApprovedToolBatch | None = None,
    ) -> str:
        """执行一批工具调用，并分发执行后的状态更新。"""
        _require_context(context)
        policy_context = context.policy
        lifecycle = context.lifecycle
        if processor is None:
            processor = lifecycle.processor_for_messages(messages)
        approved_batch = approved_batch or self.approve_tool_calls_sync(
            context,
            tool_calls_raw,
            messages,
        )
        tool_calls_raw[:] = approved_batch.calls
        try:
            register_batch(messages, approved_batch.calls)
            if processor is not None:
                processor.start_tools(approved_batch.calls)
                _run_sync(lifecycle.checkpoint(messages, "running"))
        except BaseException as exc:
            abort_registered(approved_batch.calls, error=exc)
            raise
        write_override = lifecycle.write_override
        has_write = (
            write_override(tool_calls_raw)
            if write_override is not None
            else self.policy.tool_batch_has_write(policy_context, tool_calls_raw)
        )
        transaction_finished = False
        failure_stage = "transaction_begin"
        try:
            if has_write:
                lifecycle.transaction.begin()
            reporter = lifecycle.metadata_reporter(processor, messages)
            question_reporter = lifecycle.question_reporter(processor, messages)
            failure_stage = "dispatch_and_output_admission"
            with (
                scoped_tool_metadata_reporter(reporter),
                scoped_question_lifecycle_reporter(question_reporter),
            ):
                legacy_dispatch = lifecycle.dispatch_override_sync
                dispatched = (
                    legacy_dispatch(tool_calls_raw, has_write, messages)
                    if legacy_dispatch is not None
                    else self.dispatch_sync(
                        context,
                        tool_calls_raw,
                        has_write,
                        messages,
                        policy_context=policy_context,
                        approved_batch=approved_batch,
                    )
                )
            failure_stage = "progress_checkpoint"
            _run_sync(lifecycle.drain_progress())
            failure_stage = "media_description"
            describe_interrupted = False
            if _has_read_image_result(dispatched, lifecycle.model_capabilities):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    describe_interrupted = _run_sync(
                        lifecycle.describe_read_results(dispatched, messages)
                    )
                else:
                    lifecycle.trace(
                        "read_image_describe_skipped",
                        reason="sync_tool_pipeline_inside_event_loop",
                    )
            failure_stage = "result_settlement"
            batch_state = self._settle_results(context, dispatched, messages, on_tool, processor)
            failure_stage = "transaction_finish"
            lifecycle.transaction.finish(has_write, batch_state["all_succeeded"], messages)
            transaction_finished = True
            signal = batch_state.get("handoff_signal")
            if signal is not None:
                transition = _run_sync(lifecycle.apply_transition(signal, messages, processor))
                batch_state["agent_transition"] = transition
                batch_state["terminal"] = bool(
                    transition and transition.get("terminal")
                )
            if describe_interrupted:
                raise asyncio.CancelledError
        except BaseException as exc:
            _run_sync(self._cleanup_failed_batch(
                context, approved_batch.calls, messages, processor, has_write,
                transaction_finished, exc, lifecycle.checkpoint, failure_stage,
            ))
            raise

        self._post_batch(context, dispatched, messages, has_write, batch_state, on_text)
        if processor is not None:
            finish_snapshot = _run_sync(lifecycle.observer.capture_snapshot(processor))
            _complete_processor(processor, usage, finish_snapshot)
            lifecycle.observer.record_patch(messages, processor, finish_snapshot)
            _run_sync(lifecycle.checkpoint(messages, "running"))
        return self._result_action(context, processor, batch_state, len(tool_calls_raw))

    async def execute_batch_async(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        messages: list[dict],
        on_tool: ToolDisplay | None = None,
        on_text: TextDisplay | None = None,
        *,
        processor: SessionProcessor | None = None,
        usage: LLMResult | None = None,
        finish_step: bool = True,
        checkpoint: Callable[[str], Awaitable[None]] | None = None,
        approved_batch: ApprovedToolBatch | None = None,
    ) -> str:
        """Execute one async batch against one dynamic-tool generation."""
        with scoped_dynamic_tool_snapshot():
            return await self._execute_batch_async_snapshot(
                context,
                tool_calls_raw,
                messages,
                on_tool=on_tool,
                on_text=on_text,
                processor=processor,
                usage=usage,
                finish_step=finish_step,
                checkpoint=checkpoint,
                approved_batch=approved_batch,
            )

    async def _execute_batch_async_snapshot(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        messages: list[dict],
        on_tool: ToolDisplay | None = None,
        on_text: TextDisplay | None = None,
        *,
        processor: SessionProcessor | None = None,
        usage: LLMResult | None = None,
        finish_step: bool = True,
        checkpoint: Callable[[str], Awaitable[None]] | None = None,
        approved_batch: ApprovedToolBatch | None = None,
    ) -> str:
        """Async variant of one tool batch execution."""
        _require_context(context)
        policy_context = context.policy
        lifecycle = context.lifecycle
        if processor is None:
            processor = lifecycle.processor_for_messages(messages)

        async def checkpoint_state(status: str) -> None:
            await lifecycle.drain_progress()
            overlay_recovery(messages)
            if checkpoint is not None:
                await await_settled(checkpoint(status))
            else:
                await lifecycle.checkpoint(messages, status)

        approved_batch = approved_batch or await self.approve_tool_calls_async(
            context,
            tool_calls_raw,
            messages,
        )
        tool_calls_raw[:] = approved_batch.calls
        try:
            register_batch(messages, approved_batch.calls)
            if processor is not None:
                processor.start_tools(approved_batch.calls)
                await checkpoint_state("running")
        except BaseException as exc:
            abort_registered(approved_batch.calls, error=exc)
            raise
        write_override = lifecycle.write_override
        has_write = (
            write_override(tool_calls_raw)
            if write_override is not None
            else self.policy.tool_batch_has_write(policy_context, tool_calls_raw)
        )
        transaction_finished = False
        failure_stage = "transaction_begin"
        try:
            if has_write:
                lifecycle.transaction.begin()
            reporter = lifecycle.metadata_reporter(processor, messages)
            question_reporter = lifecycle.question_reporter(processor, messages)
            failure_stage = "dispatch_and_output_admission"
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
            failure_stage = "progress_checkpoint"
            await lifecycle.drain_progress()
            failure_stage = "media_description"
            describe_interrupted = await lifecycle.describe_read_results(
                dispatched, messages,
            )
            failure_stage = "result_settlement"
            batch_state = self._settle_results(context, dispatched, messages, on_tool, processor)
            failure_stage = "transaction_finish"
            lifecycle.transaction.finish(has_write, batch_state["all_succeeded"], messages)
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
        except BaseException as exc:
            async def interrupted_checkpoint(transcript: list[dict], status: str) -> None:
                await checkpoint_state(status)
            await self._cleanup_failed_batch(
                context, approved_batch.calls, messages, processor, has_write,
                transaction_finished, exc, interrupted_checkpoint, failure_stage,
            )
            raise

        self._post_batch(context, dispatched, messages, has_write, batch_state, on_text)
        if processor is not None and finish_step:
            finish_snapshot = await lifecycle.observer.capture_snapshot(processor)
            _complete_processor(processor, usage, finish_snapshot)
            lifecycle.observer.record_patch(messages, processor, finish_snapshot)
            await checkpoint_state("running")
        return self._result_action(context, processor, batch_state, len(tool_calls_raw))


    @staticmethod
    def _result_action(context: ToolExecutionContext, processor: SessionProcessor | None,
                       batch_state: ToolBatchState, call_count: int) -> str:
        action = (
            processor.process_result()
            if processor is not None
            else ("stop" if batch_state["blocked"] else "continue")
        )
        if batch_state.get("terminal"):
            action = "terminal"
        context.lifecycle.trace(
            "step_processor_result",
            result=action,
            blocked=bool(batch_state["blocked"]),
            tool_calls=call_count,
        )
        return action

    def _settle_results(self, context: ToolExecutionContext, dispatched: DispatchedTools,
                        messages: list[dict], on_tool: ToolDisplay | None,
                        processor: SessionProcessor | None) -> ToolBatchState:
        """Share admission, observations and completion classification across drivers."""
        settle_batch(dispatched, messages)
        consume = context.lifecycle.consume_override
        state = (
            consume(dispatched, messages, on_tool=on_tool, processor=processor)
            if consume is not None else self.results.consume(
                context.projection, dispatched, messages, on_tool=on_tool, processor=processor,
            )
        )
        if context.lifecycle.strict_completed(dispatched):
            state["terminal"] = True
        return state

    async def _cleanup_failed_batch(self, context: ToolExecutionContext, calls: list[dict],
                                    messages: list[dict], processor: SessionProcessor | None,
                                    has_write: bool, transaction_finished: bool,
                                    original: BaseException, checkpoint: ToolCheckpoint,
                                    failure_stage: str = "tool_batch") -> None:
        """Keep the primary error; cleanup failures cannot prevent compensation."""
        lifecycle = context.lifecycle
        identity = {
            "session_id": context.run.session.session_id if context.run is not None else "",
            "interaction_id": context.run.interaction_run_id if context.run is not None else "",
            "assistant_step_id": processor.message_id if processor is not None else "",
            "call_ids": [str(call.get("id", "")) for call in calls],
            "primary_error_type": type(original).__name__,
            "failure_stage": failure_stage,
        }

        def trace_failure(stage, error):
            # Do not emit exception strings, tool input, output or local secrets.
            try:
                lifecycle.trace("tool_batch_cleanup_failed", **identity,
                                stage=stage, error_type=type(error).__name__,
                                transaction_active=lifecycle.transaction.active)
            except Exception:
                pass
            add_note = getattr(original, "add_note", None)
            if callable(add_note):
                add_note(f"Tool batch cleanup {stage} failed ({type(error).__name__})")

        try:
            lifecycle.trace("tool_batch_failed", **identity,
                            transaction_active=lifecycle.transaction.active)
        except Exception:
            pass
        try:
            abort_registered(calls, error=original)
        except BaseException as error:
            trace_failure("execution_settlement", error)
        # Started workers have already drained at the scheduler boundary.
        # Compensate before publishing interrupted state, including after a
        # failed commit. A partial rollback remains active and is not success.
        if has_write and not transaction_finished and lifecycle.transaction.active:
            try:
                lifecycle.transaction.finish(has_write, False, messages)
            except BaseException as error:
                trace_failure("compensation", error)
        try:
            await lifecycle.drain_progress()
        except BaseException as error:
            trace_failure("progress", error)
        if processor is not None and not isinstance(original, GuardrailEscalateError):
            try:
                processor.interrupt_unsettled()
                await checkpoint(messages, "interrupted")
            except BaseException as error:
                trace_failure("checkpoint", error)


    def _post_batch(self, context: ToolExecutionContext, dispatched: DispatchedTools,
                    messages: list[dict], has_write: bool, batch_state: ToolBatchState,
                    on_text: TextDisplay | None) -> None:
        """Shared committed-write and product-effect ordering for both drivers."""
        if has_write and batch_state["all_succeeded"]:
            context.lifecycle.observer.post_write(dispatched, messages)
            self.policy.trace_tool_streak_reset(context.policy)
        context.lifecycle.observer.after_batch(messages, batch_state, on_text)
        context.lifecycle.observer.apply_plan_mode()


    def dispatch_sync(
        self,
        context: ToolExecutionContext,
        tool_calls_raw: list[dict],
        has_write: bool,
        messages: list[dict],
        *,
        policy_context: ToolPolicyContext | None = None,
        approved_batch: ApprovedToolBatch | None = None,
    ) -> DispatchedTools:
        """只分发本轮允许执行的工具调用前缀。"""

        policy_context = policy_context or context.policy
        approved_batch = approved_batch or self.approve_tool_calls_sync(
            context,
            tool_calls_raw,
            messages,
        )
        will_execute = approved_batch.calls
        guardrail_blocked = dict(approved_batch.blocked)
        batch_id, started = self.policy.begin_tool_batch(
            policy_context, will_execute, has_write,
        )
        segments: list[dict] = []
        blocked = self.policy.find_repeated_tool_calls(policy_context, will_execute)
        blocked = self.policy.resolve_doom_loop_permissions(
            policy_context, blocked, will_execute,
        )
        blocked.update(guardrail_blocked)
        mode = "scheduled"
        try:
            if len(will_execute) > 1 and not blocked and not context.lifecycle.has_pre_tool_hooks():
                dispatched = _execute_scheduled(
                    context.lifecycle.executor,
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
                        blocked.get(i) or context.lifecycle.execute_one(tc, i, messages),
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
                error=to_public_error(exc).message,
            )
            raise
        dispatched = [
            (
                index,
                tool_call,
                result
                if index in guardrail_blocked
                else _run_sync(context.lifecycle.after_tool(tool_call, result, messages)),
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
        tool_calls_raw: list[dict],
        has_write: bool,
        messages: list[dict],
        *,
        policy_context: ToolPolicyContext | None = None,
        approved_batch: ApprovedToolBatch | None = None,
    ) -> DispatchedTools:
        """Async variant for dispatching the executable tool prefix."""
        policy_context = policy_context or context.policy
        lifecycle = context.lifecycle
        approved_batch = approved_batch or await self.approve_tool_calls_async(
            context,
            tool_calls_raw,
            messages,
        )
        will_execute = approved_batch.calls
        guardrail_blocked = dict(approved_batch.blocked)
        batch_id, started = self.policy.begin_tool_batch(
            policy_context, will_execute, has_write,
        )
        segments: list[dict] = []
        blocked = self.policy.find_repeated_tool_calls(policy_context, will_execute)
        blocked = await self.policy.resolve_doom_loop_permissions_async(
            policy_context, blocked, will_execute,
        )
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
                error=to_public_error(exc).message,
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


def _complete_processor(processor: SessionProcessor, usage: LLMResult | None,
                        finish_snapshot: str | None) -> None:
    """One shared final usage/snapshot projection for sync and async drivers."""
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


def _normalize_guarded_call(guarded: dict, rejected: ToolExecutionResult | None) -> tuple[dict, ToolExecutionResult | None]:
    """Pure approved-input normalization, shared by both admission drivers."""
    function = guarded.get("function", {})
    parsed = parse_tool_arguments(function.get("arguments", {}))
    if parsed is None:
        parsed = {}
        rejected = rejected or _invalid_tool_arguments_result(guarded)
    return approved_tool_call(guarded, parsed).to_wire(), rejected


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



_T = TypeVar("_T")


def _require_sync_entry() -> None:
    """Reject before registration; a sync driver cannot own a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Use execute_batch_async inside a running event loop")


def _run_sync(operation: Awaitable[_T]) -> _T:
    """Sync I/O adapter; never nest a loop or block its own async checkpoint."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        async def run() -> _T:
            return await operation
        return asyncio.run(run())
    if asyncio.iscoroutine(operation):
        operation.close()
    raise RuntimeError("Synchronous ToolRuntime requires a thread without a running event loop; use execute_batch_async")


def _require_context(context: ToolExecutionContext) -> None:
    if not isinstance(context, ToolExecutionContext):
        raise TypeError("ToolRuntime requires ToolExecutionContext; adapt legacy hosts outside the core")
