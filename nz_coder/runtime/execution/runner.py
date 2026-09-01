"""Shared provider/tool state machine for every NZ-Coder Agent profile."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass

from nz_coder.protocol.message_schema import (
    ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY,
    INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY,
    attach_message_identity,
    ensure_message_identities,
    is_synthetic_user_message,
    set_assistant_error,
    stamp_user_message,
)
from nz_coder.protocol.public_error import (
    PublicError,
    PublicRuntimeError,
    TrustedPublicMessage,
    to_public_error,
)
from nz_coder.runtime.verification.recovery import is_context_overflow_error
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.conversation.context_manager import (
    CompactionAttemptState as _CompactionAttemptState,
    CompactionAttemptsExhausted as _CompactionAttemptsExhausted,
)
from nz_coder.runtime.verification.completion_gate import (
    COMPLETION_GATE_REANIMATE_BUDGET,
    append_completion_guidance,
)
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.verification.verification_contract import (
    VerificationContract,
    effective_acceptance_generation,
)
from nz_coder.runtime.execution.turn_economy import (
    begin_provider_turn,
    early_tool_completion_ready,
    settle_provider_turn,
)
from nz_coder.runtime.execution.work_budget import WorkBudgetController
from nz_coder.runtime.execution.commit_boundary import (
    FailedAttemptSettlement,
    OutputVisibility,
    approve_model_result,
    commit_approved_model_result,
    settle_failed_attempt,
)
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.request import RunOptions, RunRequest
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.core.middleware import MiddlewarePipeline
from nz_coder.runtime.core.events import RuntimeEventMiddleware
from nz_coder.runtime.conversation.model_result import _normalize_llm_result_metrics
from nz_coder.runtime.core.execution_context import (
    nominal_agent_turns,
    scoped_broad_test_guard,
    scoped_declared_test_scopes,
    scoped_runtime_overrides,
)
from nz_coder.state.input_expansion import (
    compact_stored as compact_stored_input_expansions,
)

_MAX_STEPS_PROMPT = """CRITICAL - EMERGENCY HARD-CAP CALL

This is the last call available under the emergency hard cap. The nominal closure reserve has already been consumed.

STRICT REQUIREMENTS:
1. Do not start broad exploration, repository-wide search, or a new subagent.
2. If one already-identified local repair remains, use the narrowest known-file edit and focused verification now.
3. Otherwise provide a truthful text summary and list unresolved requirements.

Do not claim completion without current evidence."""

_EMPTY_COMPLETION_RETRY_BUDGET = 1
_EMPTY_COMPLETION_PROMPT = """<empty-assistant-recovery>
The previous model response ended with no visible text and no tool calls. Continue the same task now. Either call the next necessary tool or provide a concise user-visible final answer. Do not return hidden reasoning alone.
</empty-assistant-recovery>"""
_OUTPUT_LIMIT_CONTINUATION_BUDGET = 2


def _acknowledges_max_steps(content: str) -> bool:
    """Return whether the assistant explicitly reports budget exhaustion."""
    text = " ".join(str(content or "").lower().split())
    english = (
        any(marker in text for marker in ("maximum step", "maximum number of steps"))
        and any(marker in text for marker in ("reached", "exhausted", "limit"))
    )
    chinese = (
        any(marker in text for marker in ("最大步数", "最大步骤", "步数限制", "步骤限制"))
        and any(marker in text for marker in ("达到", "已达", "耗尽", "限制"))
    )
    return english or chinese


@dataclass(frozen=True)
class _TerminalBoundaryDecision:
    """One deterministic decision after all boundary evidence has settled."""

    action: str
    status: str = ""
    content: str = ""
    reason: str = ""


class AgentRunner:
    """Own the one production execution state machine shared by every profile."""

    def __init__(
        self,
        services: RuntimeServices | None = None,
        *,
        execution_context_factory=None,
        middleware=(),
    ) -> None:
        self._services = services
        self._execution_context_factory = execution_context_factory
        declared = (
            middleware.middleware
            if isinstance(middleware, MiddlewarePipeline)
            else tuple(middleware)
        )
        self._runtime_events = None
        if isinstance(services, RuntimeServices):
            self._runtime_events = RuntimeEventMiddleware(services.events)
            declared = (
                RuntimeEventMiddleware(
                    services.events,
                    emit_run_events=False,
                ),
                *services.middleware,
                *declared,
            )
        self._middleware = MiddlewarePipeline(declared)

    async def run(
        self,
        request_or_host,
        messages: list | None = None,
        on_tool=None,
        on_text=None,
        on_token=None,
        stream: bool = True,
        *,
        options: RunOptions | None = None,
    ) -> dict:
        """Execute a native request or adapt one legacy Agent host."""
        if isinstance(request_or_host, RunRequest):
            if messages is not None:
                raise TypeError("Native AgentRunner.run does not accept messages separately")
            selected = options or RunOptions()
            if not isinstance(selected, RunOptions):
                raise TypeError("Native AgentRunner.run options must be RunOptions")
            return await self._run_request(request_or_host, selected)
        if options is not None:
            raise TypeError("Legacy AgentRunner.run does not accept RunOptions")
        if messages is None:
            raise TypeError("Legacy AgentRunner.run requires messages")
        return await self._run_legacy(
            request_or_host,
            messages,
            on_tool=on_tool,
            on_text=on_text,
            on_token=on_token,
            stream=stream,
        )

    async def _run_request(self, request: RunRequest, options: RunOptions) -> dict:
        result, _context = await self._execute_request(request, options)
        return result

    async def _execute_request(
        self, request: RunRequest, options: RunOptions,
    ) -> tuple[dict, RunContext]:
        """Execute one immutable request without constructing an AgentLoop."""
        with (
            scoped_runtime_overrides(
                max_agent_turns=_request_max_turns(request),
            ),
            scoped_broad_test_guard(),
            scoped_declared_test_scopes(),
        ):
            return await self._execute_request_in_scope(request, options)

    async def _execute_request_in_scope(
        self, request: RunRequest, options: RunOptions,
    ) -> tuple[dict, RunContext]:
        """Execute after binding per-request runtime policy to this async task."""
        services = self._services
        if not isinstance(services, RuntimeServices):
            raise TypeError("Native AgentRunner requires a RuntimeServices graph")
        factory = self._execution_context_factory
        if not callable(factory):
            raise TypeError("Native AgentRunner requires an execution context factory")
        run_context = await services.session_runtime.open(request)
        run_context.cancellation = options.cancellation
        event_bus = options.event_bus
        if event_bus is not None:
            create_publisher = getattr(event_bus, "for_interaction", None)
            if callable(create_publisher):
                run_context.metadata["event_publisher"] = create_publisher(
                    run_context.interaction_run_id,
                    agent_invocation_id=request.agent.name,
                    parent_interaction_run_id=str(
                        request.parent_interaction_run_id or ""
                    ),
                    parent_agent_invocation_id=str(request.parent_agent_id or ""),
                )
        execution_context = factory(run_context, services)
        if not isinstance(execution_context, RunnerExecutionContext):
            raise TypeError("execution context factory must return RunnerExecutionContext")
        run_stream = request.stream if options.stream is None else options.stream
        execution_context.hooks.trace(
            "agent_runner_enter",
            runner="native",
            runtime_profile=request.profile.name,
            active_agent=request.agent.name,
            stream=bool(run_stream),
        )

        async def execute_run():
            return await self._run_turns(
                execution_context,
                services,
                run_context,
                run_context.transcript,
                options.on_tool,
                options.on_text,
                options.on_token,
                run_stream,
            )

        runtime_events = self._runtime_events
        if runtime_events is not None:
            await runtime_events.before_run(run_context)
        try:
            result = await self._middleware.run("run", run_context, execute_run)
            await services.session_runtime.finalize(
                run_context,
                _result_status(result),
            )
        except asyncio.CancelledError as error:
            if not run_context.finalized:
                await self._finalize_after_run_error(
                    services, run_context, RunStatus.CANCELLED,
                    execution_context, error,
                )
            if runtime_events is not None:
                await runtime_events.on_run_error(run_context, error)
            raise
        except KeyboardInterrupt as error:
            if not run_context.finalized:
                await self._finalize_after_run_error(
                    services, run_context, RunStatus.INTERRUPTED,
                    execution_context, error,
                )
            if runtime_events is not None:
                await runtime_events.on_run_error(run_context, error)
            raise
        except Exception as error:
            if not run_context.finalized:
                await self._finalize_after_run_error(
                    services, run_context, RunStatus.ERROR,
                    execution_context, error,
                )
            if runtime_events is not None:
                await runtime_events.on_run_error(run_context, error)
            raise
        if runtime_events is not None:
            await runtime_events.after_run(run_context, result)
        return result, run_context

    @staticmethod
    async def _finalize_after_run_error(
        services: RuntimeServices,
        run_context: RunContext,
        status: RunStatus,
        execution_context: RunnerExecutionContext | None,
        original_error: BaseException,
    ) -> None:
        """Attempt catch cleanup without ever replacing the original failure."""
        try:
            await services.session_runtime.finalize(run_context, status)
        except BaseException as finalization_error:
            try:
                if execution_context is not None:
                    execution_context.hooks.trace(
                        "session_error_finalize_failed",
                        original_error_type=type(original_error).__name__,
                        original_error=str(original_error)[:1000],
                        finalization_error_type=type(finalization_error).__name__,
                        finalization_error=str(finalization_error)[:1000],
                    )
            except BaseException:
                pass

    async def run_result(
        self,
        request: RunRequest,
        options: RunOptions | None = None,
    ) -> RunResult:
        """Execute a native request and return the stable typed result contract."""
        result, context = await self._execute_request(request, options or RunOptions())
        return _typed_result(request, context, result)

    async def _run_legacy(self, host, messages: list, on_tool=None, on_text=None,
                          on_token=None, stream: bool = True) -> dict:
        """Bind legacy production resources and enter the canonical state machine."""
        # Import only inside the explicit compatibility entry.  The native
        # request path above has no dependency on legacy AgentLoop adapters.
        from nz_coder.runtime.adapters.runner import (
            run_request_from_legacy_host,
            runner_context_from_legacy_host,
        )

        tracer = getattr(host, "tracer", None)
        log = getattr(tracer, "log", None)
        if callable(log):
            log(
                "agent_runner_enter",
                runner="shared",
                runtime_profile=str(getattr(host, "runtime_profile", "direct")),
                active_agent=str(getattr(host, "current_agent_name", "") or "worker"),
                stream=bool(stream),
            )
        services = self._services or getattr(host, "runtime_services", None)
        compatibility_override = vars(host).get("_run")
        if callable(compatibility_override) and not isinstance(services, RuntimeServices):
            async def execute_legacy(
                _host,
                run_messages,
                tool_cb,
                text_cb,
                token_cb,
                run_stream,
            ):
                return await compatibility_override(
                    run_messages,
                    tool_cb,
                    text_cb,
                    token_cb,
                    run_stream,
                )

            runtime_host = getattr(host, "runtime_host", None)
            if runtime_host is None:
                from nz_coder.runtime.execution.host import ProductionRuntimeHost
                runtime_host = ProductionRuntimeHost()
            return await runtime_host.run(
                host,
                messages,
                on_tool=on_tool,
                on_text=on_text,
                on_token=on_token,
                stream=stream,
                execute=execute_legacy,
            )
        if not isinstance(services, RuntimeServices):
            raise TypeError("AgentRunner requires a RuntimeServices graph")
        request = run_request_from_legacy_host(host, messages, stream)
        run_context = await services.session_runtime.open(request)
        event_bus = getattr(host, "event_bus", None)
        create_publisher = getattr(event_bus, "for_interaction", None)
        if callable(create_publisher):
            host.event_publisher = create_publisher(
                run_context.interaction_run_id,
                agent_invocation_id=str(getattr(host, "agent_id", "") or ""),
            )
            background = getattr(host, "background_agents", None)
            bind_publisher = getattr(background, "bind_event_publisher", None)
            if callable(bind_publisher):
                bind_publisher(host.event_publisher)
        # The legacy lifecycle already owns the mature SessionEventBus facts.
        # Suppress only the additive core projection to avoid duplicate UI events.
        run_context.metadata["suppress_runtime_events"] = True
        host.active_run_context = run_context
        execute = None
        execution_context = None
        if callable(compatibility_override):
            async def execute(_host, run_messages, tool_cb, text_cb, token_cb, run_stream):
                return await compatibility_override(
                    run_messages,
                    tool_cb,
                    text_cb,
                    token_cb,
                    run_stream,
                )
            runtime_host = getattr(host, "runtime_host", None)
            if runtime_host is None:
                from nz_coder.runtime.execution.host import ProductionRuntimeHost
                runtime_host = ProductionRuntimeHost()
        else:
            execution_context = runner_context_from_legacy_host(
                host, services, run_context,
            )

            async def execute(_host, run_messages, tool_cb, text_cb, token_cb, run_stream):
                return await self._run_turns(
                    execution_context,
                    services,
                    run_context,
                    run_messages,
                    tool_cb,
                    text_cb,
                    token_cb,
                    run_stream,
                )

            runtime_host = services.host
        try:
            result = await runtime_host.run(
                host,
                run_context.transcript,
                on_tool=on_tool,
                on_text=on_text,
                on_token=on_token,
                stream=stream,
                execute=execute,
            )
        except asyncio.CancelledError as error:
            await self._finalize_after_run_error(
                services, run_context, RunStatus.CANCELLED,
                execution_context, error,
            )
            raise
        except KeyboardInterrupt as error:
            await self._finalize_after_run_error(
                services, run_context, RunStatus.INTERRUPTED,
                execution_context, error,
            )
            raise
        except Exception as error:
            await self._finalize_after_run_error(
                services, run_context, RunStatus.ERROR,
                execution_context, error,
            )
            raise
        else:
            await services.session_runtime.finalize(
                run_context,
                _result_status(result),
            )
            return result
        finally:
            messages[:] = copy.deepcopy(run_context.transcript)
            host.active_run_context = None

    async def _run_turns(self, context: RunnerExecutionContext, services: RuntimeServices,
                   run_context: RunContext, messages: list,
                   on_tool=None, on_text=None,
                   on_token=None, stream: bool = True) -> dict:
        """运行 agent loop 直到模型停止调用工具。"""
        max_turns, start_turn = context.lifecycle.initialize(messages, stream)
        restored_budget_zones = list(
            getattr(context.runtime_state, "budget_zones_emitted", []) or []
        )
        restored_budget_zones.extend(
            str(message.get("_nz_work_budget_zone"))
            for message in messages
            if isinstance(message, dict) and message.get("_nz_work_budget_zone")
        )
        work_budget = WorkBudgetController(
            max_turns,
            emitted=tuple(dict.fromkeys(restored_budget_zones)),
            nominal_turns=nominal_agent_turns(),
        )
        await context.policy.run_input_guardrails(messages)
        ensure_message_identities(messages, context.session_id)
        await services.session_runtime.checkpoint(run_context, "running")
        compaction_attempts = _CompactionAttemptState()
        empty_completion_retries = 0
        output_limit_continuations = 0
        tool_runtime_context = None
        model_runtime_context = None

        def resolve_model_runtime_context():
            nonlocal model_runtime_context
            if model_runtime_context is None:
                model_runtime_context = context.execution.model()
            return model_runtime_context

        def resolve_tool_runtime_context():
            nonlocal tool_runtime_context
            if tool_runtime_context is None:
                tool_runtime_context = context.execution.tools()
            return tool_runtime_context
        try:
            await context.planning.generate(messages)
            context_runtime = None
            for turn_index in range(start_turn, max_turns):
                # Match InfCode's queued-followup boundary: the previous model
                # step (including inline/local tools) is fully settled, but a
                # superseded turn must not start another Provider round-trip.
                if context.control.has_queued_followup():
                    context.hooks.trace(
                        "prompt_followup_detected",
                        completed_steps=turn_index - start_turn,
                    )
                    return await context.lifecycle.finalize(
                        messages,
                        "interrupted",
                        on_text,
                        on_token,
                        stream,
                    )
                context.control.drain_background_messages(messages)
                context.runtime_state.turn_count = turn_index + 1
                context.runtime_state.work_phase = work_budget.phase(turn_index)
                context.runtime_state.budget_pressure_zone = work_budget.zone(
                    turn_index
                )
                context.hooks.on_turn_start(messages)
                try:
                    if context_runtime is None:
                        context_runtime = context.execution.context()
                    await services.context.prepare_async(
                        context_runtime,
                        messages,
                        on_text=on_text,
                        attempt_state=compaction_attempts,
                    )
                except _CompactionAttemptsExhausted as exc:
                    context.messages.persist_compaction_exhaustion(messages, exc)
                    context.hooks.on_turn_end(messages, "error")
                    return await context.lifecycle.finalize(
                        messages, "error", on_text, on_token, stream,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    owner = next(
                        (
                            message for message in reversed(messages)
                            if isinstance(message, dict)
                            and message.get("role") == "assistant"
                        ),
                        None,
                    )
                    if owner is None:
                        owner = {"role": "assistant", "content": ""}
                        context.messages.bind_assistant_context(owner)
                        attach_message_identity(owner, session_id=context.session_id)
                        messages.append(owner)
                        processor = SessionProcessor(
                            owner,
                            publish=context.messages.publish_event,
                            on_message_updated=(
                                lambda _message: run_context.session.mark_dirty()
                            ),
                        )
                        processor.start_step()
                        processor.finish_step("error")
                    set_assistant_error(
                        owner,
                        exc,
                        name=(
                            "ContextOverflowError"
                            if is_context_overflow_error(exc)
                            else "UnknownError"
                        ),
                        publish=context.messages.publish_event,
                    )
                    await services.session_runtime.checkpoint(run_context, "error")
                    context.hooks.trace(
                        "context_compaction_failed",
                        error=str(exc),
                        context_overflow=is_context_overflow_error(exc),
                        attempts=compaction_attempts.attempts,
                        trigger="pre_send",
                    )
                    context.hooks.on_turn_end(messages, "error")
                    return await context.lifecycle.finalize(
                        messages, "error", on_text, on_token, stream,
                    )
                budget_notice = work_budget.next_notice(turn_index)
                if budget_notice is not None:
                    guidance = {
                        "role": "user",
                        "content": budget_notice.message,
                        "_nz_synthetic": True,
                        "_nz_work_budget_zone": budget_notice.zone,
                    }
                    attach_message_identity(guidance, session_id=context.session_id)
                    messages.append(guidance)
                    context.runtime_state.budget_zones_emitted = list(
                        work_budget.emitted
                    )
                    context.control.persist_runtime_state(active=True)
                    context.hooks.trace(
                        "work_budget_pressure",
                        zone=budget_notice.zone,
                        completed_turns=budget_notice.completed_turns,
                        max_turns=budget_notice.max_turns,
                    )
                    action = self._verification_action(
                        context,
                        budget_notice.zone,
                    )
                    if action.kind == "stage":
                        await self._execute_scheduled_verification(
                            context,
                            services,
                            run_context,
                            messages,
                            action,
                            resolve_tool_runtime_context,
                            on_tool,
                            on_text,
                        )
                    elif action.kind == "acceptance":
                        await self._execute_due_verification_contract(
                            context,
                            services,
                            run_context,
                            messages,
                            budget_notice.zone,
                            resolve_tool_runtime_context,
                            on_tool,
                            on_text,
                        )
                context.hooks.on_pre_send(messages)
                context.messages.bind_user_contexts(messages)
                message_part = context.messages.new_message_part(turn_index + 1)
                output_guarded = context.policy.has_output_guardrail()
                internal_agent_result = context.control.has_agent_call_stack()
                message_part["public_streaming"] = not (
                    output_guarded or internal_agent_result
                )
                assistant_message = {"role": "assistant", "content": ""}
                assistant_message[INTERACTION_RUN_ID_KEY] = (
                    run_context.interaction_run_id
                )
                assistant_message["_nz_visible"] = not internal_agent_result
                assistant_message["_nz_internal"] = internal_agent_result
                assistant_message["_nz_authoritative"] = True
                context.messages.bind_assistant_context(assistant_message)
                attach_message_identity(
                    assistant_message,
                    message_part["message_id"],
                    session_id=context.session_id,
                )
                parent_id = next(
                    (
                        item.get(MESSAGE_ID_KEY)
                        for item in reversed(messages)
                        if isinstance(item, dict)
                        and item.get("role") == "user"
                        and not is_synthetic_user_message(item)
                        and isinstance(item.get(MESSAGE_ID_KEY), str)
                    ),
                    "",
                )
                if parent_id:
                    assistant_message[ASSISTANT_PARENT_KEY] = parent_id
                assistant_message[ASSISTANT_TIME_KEY] = {"created": time.time()}
                messages.append(assistant_message)
                processor = SessionProcessor(
                    assistant_message,
                    publish=context.messages.publish_event,
                    on_message_updated=(
                        lambda _message: run_context.session.mark_dirty()
                    ),
                )
                # NZ-Coder providers never execute tools inside the model
                # stream. Capture may therefore overlap model generation, but
                # it is always awaited before local tool dispatch.
                start_snapshot_cancel = threading.Event()
                start_snapshot_task = asyncio.create_task(asyncio.to_thread(
                    context.snapshots.capture,
                    "step-start",
                    message_part["message_id"],
                    start_snapshot_cancel,
                ))
                start_snapshot: str | None = None
                start_snapshot_awaited = False
                model_result_materialized = False
                approved_tool_batch = None
                record_provider_turn = None
                failure_settlement = FailedAttemptSettlement()

                async def approve_tool_batch(tool_calls: list) -> object | None:
                    approve = getattr(
                        services.tools,
                        "approve_tool_calls_async",
                        None,
                    )
                    if not callable(approve):
                        return None
                    return await approve(
                        resolve_tool_runtime_context(),
                        tool_calls,
                        messages,
                    )

                async def settle_policy_exception(
                    exc: BaseException,
                    failure_kind: str,
                ) -> PublicError:
                    public = to_public_error(exc)
                    if callable(record_provider_turn):
                        record_provider_turn(finish_reason="error")
                    await settle_failed_attempt(
                        context=context,
                        services=services,
                        run_context=run_context,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        public_error=public,
                        failure_kind=failure_kind,
                        settlement=failure_settlement,
                        snapshot_task=start_snapshot_task,
                        snapshot_cancel=start_snapshot_cancel,
                    )
                    return public

                async def approve_policy_stage(
                    candidate,
                    visibility: OutputVisibility,
                ):
                    nonlocal approved_tool_batch
                    failure_kind = "output_guardrail"
                    try:
                        if candidate.tool_calls and approved_tool_batch is None:
                            failure_kind = "tool_guardrail"
                            approved_tool_batch = await approve_tool_batch(
                                candidate.tool_calls,
                            )
                        if approved_tool_batch is not None:
                            candidate.tool_calls = approved_tool_batch.calls
                        failure_kind = "output_guardrail"
                        approved = await approve_model_result(
                            context=context,
                            result=candidate,
                            messages=messages,
                            visibility=visibility,
                        )
                        return approved
                    except (asyncio.CancelledError, KeyboardInterrupt) as exc:
                        await settle_policy_exception(exc, "policy")
                        raise
                    except BaseException as exc:
                        public = await settle_policy_exception(
                            exc,
                            failure_kind,
                        )
                        raise PublicRuntimeError(public) from None

                async def resolve_start_snapshot() -> str | None:
                    nonlocal start_snapshot, start_snapshot_awaited
                    if not start_snapshot_awaited:
                        start_snapshot = await context.snapshots.await_start(
                            start_snapshot_task,
                            start_snapshot_cancel,
                        )
                        start_snapshot_awaited = True
                        if start_snapshot:
                            processor.set_step_snapshot(start_snapshot)
                            await services.session_runtime.checkpoint(run_context, "running")
                    return start_snapshot

                async def execute_stream_tools(stream_result: object) -> str:
                    nonlocal model_result_materialized, approved_tool_batch
                    await resolve_start_snapshot()
                    visibility = (
                        OutputVisibility.INTERNAL_AGENT_RESULT
                        if context.control.has_agent_call_stack()
                        else OutputVisibility.USER_VISIBLE
                    )
                    approved = await approve_policy_stage(
                        stream_result,
                        visibility,
                    )
                    if output_guarded:
                        # A tool-forming response is not a completed user answer.
                        approved.result.content = ""
                        approved.result.extra = dict(approved.result.extra or {})
                        approved.result.extra.pop("reasoning_content", None)
                    commit_approved_model_result(
                        approved,
                        context=context,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        messages=messages,
                    )
                    model_result_materialized = True
                    async def execute_batch():
                        return await services.tools.execute_batch_async(
                            resolve_tool_runtime_context(), stream_result.tool_calls,
                            messages, on_tool, on_text, processor=processor,
                            usage=stream_result, finish_step=False,
                            approved_batch=approved_tool_batch,
                        )
                    return await self._middleware.run(
                        "tool_batch", run_context, execute_batch,
                    )

                processor.start_step()
                await services.session_runtime.checkpoint(run_context, "running")
                try:
                    context.messages.bind_active_processor(processor, messages)
                    await context.policy.prepare_user_images(messages, assistant_message)
                    await context.policy.prepare_user_documents(messages, assistant_message)
                    api_messages = context.messages.build_api_messages(messages)
                    if turn_index + 1 >= max_turns:
                        api_messages = [
                            *api_messages,
                            {"role": "assistant", "content": _MAX_STEPS_PROMPT},
                        ]
                        context.hooks.trace("max_steps_prompt_injected", step=turn_index + 1)
                    provider_turn = begin_provider_turn(
                        context.runtime_state,
                        messages,
                        turn_index + 1,
                    )
                    context.hooks.trace(
                        "provider_turn_started",
                        **provider_turn.to_dict(),
                    )
                    provider_turn_recorded = False

                    def record_provider_turn(
                        *,
                        tool_calls=(),
                        finish_reason: str = "",
                    ) -> None:
                        nonlocal provider_turn_recorded
                        if provider_turn_recorded:
                            return
                        observation = settle_provider_turn(
                            provider_turn,
                            context.runtime_state,
                            tool_calls=tool_calls,
                            finish_reason=finish_reason,
                        )
                        record = observation.to_dict()
                        observe = getattr(
                            context.runtime_state,
                            "observe_provider_turn",
                            None,
                        )
                        if callable(observe):
                            observe(record)
                        context.hooks.trace("provider_turn_settled", **record)
                        provider_turn_recorded = True

                    async def execute_model():
                        return await services.model.complete_turn(
                            resolve_model_runtime_context(), api_messages,
                            stream=stream,
                            on_token=(None if output_guarded else on_token),
                            message_part=message_part,
                            stream_tool_handler=execute_stream_tools,
                        )
                    result = await self._middleware.run(
                        "model", run_context, execute_model,
                    )
                    if output_guarded:
                        result.extra = dict(result.extra or {})
                        result.extra.pop("reasoning_content", None)
                        if result.tool_calls:
                            result.content = ""
                    repaired_metrics = _normalize_llm_result_metrics(result)
                    if repaired_metrics:
                        context.hooks.trace(
                            "model_result_metrics_repaired",
                            fields=list(repaired_metrics),
                        )
                    context.messages.apply_usage_cost(result)
                    run_context.add_usage(TokenUsage(
                        input_tokens=max(0, int(result.input_tokens or 0)),
                        output_tokens=max(0, int(result.output_tokens or 0)),
                        cached_read_tokens=max(0, int(result.cache_read_tokens or 0)),
                        cached_write_tokens=max(0, int(result.cache_write_tokens or 0)),
                        reasoning_tokens=max(0, int(result.reasoning_tokens or 0)),
                    ))
                except asyncio.CancelledError:
                    if callable(record_provider_turn):
                        record_provider_turn(finish_reason="error")
                    context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                    set_assistant_error(
                        assistant_message,
                        TrustedPublicMessage(
                            "cancelled",
                            "Request interrupted by user",
                            retryable=True,
                        ),
                        name="MessageAbortedError",
                        publish=context.messages.publish_event,
                    )
                    processor.interrupt_unsettled()
                    processor.finish_step("cancelled")
                    await services.session_runtime.checkpoint(run_context, "cancelled")
                    raise
                except KeyboardInterrupt:
                    if callable(record_provider_turn):
                        record_provider_turn(finish_reason="error")
                    context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                    set_assistant_error(
                        assistant_message,
                        TrustedPublicMessage(
                            "cancelled",
                            "Request interrupted by user",
                            retryable=True,
                        ),
                        name="MessageAbortedError",
                        publish=context.messages.publish_event,
                    )
                    processor.interrupt_unsettled()
                    processor.finish_step("cancelled")
                    await services.session_runtime.checkpoint(run_context, "interrupted")
                    return await context.lifecycle.finalize(
                        messages,
                        "interrupted",
                        on_text,
                        on_token,
                        stream,
                    )
                except BaseException:
                    if callable(record_provider_turn):
                        record_provider_turn(finish_reason="error")
                    raise
                finally:
                    context.messages.bind_active_processor(None, None)

                try:
                    start_snapshot = await resolve_start_snapshot()
                except asyncio.CancelledError:
                    context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                    set_assistant_error(
                        assistant_message,
                        TrustedPublicMessage(
                            "cancelled",
                            "Request interrupted by user",
                            retryable=True,
                        ),
                        name="MessageAbortedError",
                        publish=context.messages.publish_event,
                    )
                    processor.interrupt_unsettled()
                    processor.finish_step("cancelled")
                    await services.session_runtime.checkpoint(run_context, "cancelled")
                    raise
                if result.post_tool_stream_error:
                    record_provider_turn(
                        tool_calls=result.tool_calls,
                        finish_reason="error",
                    )
                    approved = await approve_policy_stage(
                        result,
                        (
                            OutputVisibility.INTERNAL_AGENT_RESULT
                            if context.control.has_agent_call_stack()
                            else OutputVisibility.USER_VISIBLE
                        ),
                    )
                    result = approved.result
                    commit_approved_model_result(
                        approved,
                        context=context,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        messages=messages,
                        reconcile=model_result_materialized,
                    )
                    model_result_materialized = True
                    error = result.post_tool_stream_error
                    structured = result.assistant_error or {
                        "name": "APIError",
                        "data": {"message": error, "isRetryable": False},
                    }
                    set_assistant_error(
                        assistant_message,
                        error,
                        name=str(structured.get("name") or "UnknownError"),
                        data=(
                            structured.get("data")
                            if isinstance(structured.get("data"), dict)
                            else None
                        ),
                        publish=context.messages.publish_event,
                    )
                    processor.fail_unsettled(error)
                    finish_snapshot = (
                        await context.snapshots.capture_async(
                            "step-finish",
                            message_part["message_id"],
                        ) if start_snapshot else None
                    )
                    processor.finish_step(
                        "error",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        total_tokens=result.total_tokens,
                        reasoning_tokens=result.reasoning_tokens,
                        cache_read_tokens=result.cache_read_tokens,
                        cache_write_tokens=result.cache_write_tokens,
                        cost=(result.cost if result.cost_known else None),
                        snapshot=finish_snapshot,
                    )
                    context.snapshots.record_patch(messages, processor, finish_snapshot)
                    await services.session_runtime.checkpoint(run_context, "error")
                    context.messages.observe_llm_result(
                        result,
                        message_part=message_part,
                        turn=turn_index + 1,
                    )
                    context.hooks.on_turn_end(messages, "error")
                    return await context.lifecycle.finalize(
                        messages,
                        "error",
                        on_text,
                        on_token,
                        stream,
                        content_text=result.content,
                    )
                if result.aborted:
                    record_provider_turn(finish_reason="error")
                    structured = result.assistant_error or {
                        "name": "APIError",
                        "data": {
                            "message": "Provider request failed after retries",
                            "isRetryable": False,
                        },
                    }
                    set_assistant_error(
                        assistant_message,
                        TrustedPublicMessage(
                            "provider_error",
                            "Provider request failed after retries",
                        ),
                        name=str(structured.get("name") or "UnknownError"),
                        data=(
                            structured.get("data")
                            if isinstance(structured.get("data"), dict)
                            else None
                        ),
                        publish=context.messages.publish_event,
                    )
                    processor.fail_unsettled(TrustedPublicMessage(
                        "provider_error",
                        "Provider request failed after retries",
                    ))
                    processor.finish_step(
                        "error",
                        snapshot=(
                            await context.snapshots.capture_async(
                                "step-finish", message_part["message_id"],
                            ) if start_snapshot else None
                        ),
                    )
                    await services.session_runtime.checkpoint(run_context, "aborted")
                    return await context.lifecycle.finalize(messages, "aborted", on_text, on_token, stream)
                if result.needs_compaction:
                    record_provider_turn(finish_reason="error")
                    error = PublicError(
                        "context_overflow",
                        "The request exceeded the model context window.",
                        retryable=True,
                    )
                    set_assistant_error(
                        assistant_message,
                        error,
                        name="ContextOverflowError",
                        publish=context.messages.publish_event,
                    )
                    processor.fail_unsettled(error)
                    processor.finish_step(
                        "context-overflow",
                        snapshot=(
                            await context.snapshots.capture_async(
                                "step-finish", message_part["message_id"],
                            ) if start_snapshot else None
                        ),
                    )
                    await services.session_runtime.checkpoint(run_context, "compacting")
                    outcome = processor.process_result(needs_compaction=True)
                    context.hooks.trace(
                        "step_processor_result",
                        result=outcome,
                        reason="context-overflow",
                    )
                    if outcome != "compact":
                        raise RuntimeError(f"invalid context-overflow outcome: {outcome}")
                    try:
                        attempt = compaction_attempts.reserve()
                    except _CompactionAttemptsExhausted as exc:
                        context.messages.persist_compaction_exhaustion(
                            messages,
                            exc,
                            target=assistant_message,
                        )
                        context.hooks.on_turn_end(messages, "error")
                        return await context.lifecycle.finalize(
                            messages, "error", on_text, on_token, stream,
                        )
                    degraded = compact_stored_input_expansions(
                        messages,
                        "context-overflow",
                    )
                    if degraded:
                        context.hooks.trace(
                            "context_input_expansion_compacted",
                            reason="context-overflow",
                            count=degraded,
                        )
                    if on_text:
                        on_text("[context overflow: compacting]")
                    try:
                        compacted = await _to_thread_settled(
                            context.messages.compact_messages,
                            messages,
                            overflow=True,
                        )
                    except Exception as exc:
                        set_assistant_error(
                            assistant_message,
                            exc,
                            name=(
                                "ContextOverflowError"
                                if is_context_overflow_error(exc)
                                else "UnknownError"
                            ),
                            publish=context.messages.publish_event,
                        )
                        await services.session_runtime.checkpoint(run_context, "error")
                        context.hooks.trace(
                            "context_compaction_failed",
                            error=str(exc),
                            context_overflow=is_context_overflow_error(exc),
                            attempts=attempt,
                        )
                        context.hooks.on_turn_end(messages, "error")
                        return await context.lifecycle.finalize(
                            messages, "error", on_text, on_token, stream,
                        )
                    context.messages.stamp_auto_compaction(compacted)
                    messages[:] = compacted
                    await services.session_runtime.checkpoint(run_context, "running")
                    context.hooks.trace(
                        "compact",
                        trigger="provider_context_overflow",
                        attempts=attempt,
                        degraded_input_expansions=degraded,
                    )
                    context.hooks.on_turn_end(messages, "compact")
                    continue
                if result.diagnostic is not None:
                    record_provider_turn(finish_reason="error")
                    structured = result.assistant_error or {
                        "name": "APIError",
                        "data": {
                            "message": result.diagnostic,
                            "isRetryable": False,
                        },
                    }
                    set_assistant_error(
                        assistant_message,
                        result.diagnostic,
                        name=str(structured.get("name") or "UnknownError"),
                        data={
                            **(
                                structured.get("data")
                                if isinstance(structured.get("data"), dict)
                                else {"message": result.diagnostic}
                            ),
                        },
                        publish=context.messages.publish_event,
                    )
                    processor.fail_unsettled(result.diagnostic)
                    processor.finish_step(
                        "error",
                        snapshot=(
                            await context.snapshots.capture_async(
                                "step-finish", message_part["message_id"],
                            ) if start_snapshot else None
                        ),
                    )
                    await services.session_runtime.checkpoint(run_context, "running")
                    context.messages.inject_api_diagnostic(messages, result.diagnostic)
                    continue

                visibility = (
                    OutputVisibility.INTERNAL_AGENT_RESULT
                    if context.control.has_agent_call_stack()
                    else OutputVisibility.USER_VISIBLE
                )
                approved = await approve_policy_stage(result, visibility)
                result = approved.result
                commit_approved_model_result(
                    approved,
                    context=context,
                    assistant_message=assistant_message,
                    processor=processor,
                    message_part=message_part,
                    messages=messages,
                    reconcile=model_result_materialized,
                )
                if not model_result_materialized:
                    model_result_materialized = True
                context.messages.observe_llm_result(
                    result,
                    message_part=message_part,
                    turn=turn_index + 1,
                )

                if result.finish_reason in {"error", "length"}:
                    record_provider_turn(
                        tool_calls=result.tool_calls,
                        finish_reason=result.finish_reason,
                    )
                    if result.tool_calls and not result.tools_executed_in_stream:
                        processor.fail_unsettled(
                            TrustedPublicMessage(
                                "tool_not_executed",
                                "Tool call was not executed because the Provider "
                                f"response finished with {result.finish_reason!r}",
                            )
                        )
                    finish_snapshot = (
                        await context.snapshots.capture_async(
                            "step-finish", message_part["message_id"],
                        ) if start_snapshot else None
                    )
                    processor.finish_step(
                        result.finish_reason,
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        total_tokens=result.total_tokens,
                        reasoning_tokens=result.reasoning_tokens,
                        cache_read_tokens=result.cache_read_tokens,
                        cache_write_tokens=result.cache_write_tokens,
                        cost=(result.cost if result.cost_known else None),
                        snapshot=finish_snapshot,
                    )
                    if result.tools_executed_in_stream:
                        context.snapshots.record_patch(messages, processor, finish_snapshot)
                    if result.finish_reason == "error":
                        provider_error = (
                            "The provider ended the response with an error before "
                            "returning details. Start a new message to retry."
                        )
                        set_assistant_error(
                            assistant_message,
                            TrustedPublicMessage(
                                "provider_error",
                                provider_error,
                            ),
                            name="APIError",
                            data={"message": provider_error, "isRetryable": False},
                            publish=context.messages.publish_event,
                        )
                        await services.session_runtime.checkpoint(run_context, "error")
                        context.hooks.trace(
                            "provider_finish_error",
                            finish_reason=result.finish_reason,
                        )
                        if stream and on_text:
                            on_text(provider_error)
                        context.hooks.on_turn_end(messages, "error")
                        return await context.lifecycle.finalize(
                            messages,
                            "error",
                            on_text,
                            on_token,
                            stream,
                            content_text=(
                                result.content
                                if stream or not result.content
                                else f"{result.content}\n\n{provider_error}"
                            ) or provider_error,
                        )
                    warning = processor.add_length_warning(
                        has_text=bool(result.content.strip()),
                        has_reasoning=bool(
                            str(result.extra.get("reasoning_content") or "").strip()
                        ),
                        has_tools=bool(result.tool_calls),
                    )
                    await services.session_runtime.checkpoint(run_context, "running")
                    context.hooks.trace(
                        "model_output_limit",
                        reasoning_only=(
                            bool(str(result.extra.get("reasoning_content") or "").strip())
                            and not result.content.strip()
                            and not result.tool_calls
                        ),
                        continuation_count=output_limit_continuations,
                        continuation_budget=_OUTPUT_LIMIT_CONTINUATION_BUDGET,
                    )
                    visible = f"{result.content}\n\n{warning}" if result.content else warning
                    can_continue = bool(
                        not result.tools_executed_in_stream
                        and output_limit_continuations
                        < _OUTPUT_LIMIT_CONTINUATION_BUDGET
                        and turn_index + 1 < max_turns
                    )
                    if can_continue:
                        output_limit_continuations += 1
                        if result.tool_calls:
                            failure = (
                                "[Tool Error] This tool call was not executed because "
                                "the Provider response hit its output limit. Emit a "
                                "new, complete tool call with smaller arguments."
                            )
                            for call in result.tool_calls:
                                call_id = str(call.get("id") or "").strip()
                                if call_id:
                                    messages.append({
                                        "role": "tool",
                                        "tool_call_id": call_id,
                                        "content": failure,
                                        "_nz_synthetic": True,
                                        "_nz_output_limit_repair": True,
                                    })
                            recovery_instruction = (
                                "The previous response hit the output limit while "
                                "forming tool calls. None of those incomplete calls "
                                "ran. Retry only the remaining action with complete, "
                                "smaller tool arguments."
                            )
                        else:
                            recovery_instruction = (
                                "The previous response hit the output limit before a "
                                "complete visible answer. Continue exactly where it "
                                "stopped and finish concisely. If work remains, split "
                                "it into smaller tool calls."
                            )
                        continuation = stamp_user_message({
                            "role": "user",
                            "content": (
                                "<output-limit-continuation>\n"
                                + recovery_instruction
                                + "\n</output-limit-continuation>"
                            ),
                            "_nz_synthetic": True,
                            "_nz_output_limit_continuation": True,
                        })
                        attach_message_identity(
                            continuation,
                            session_id=context.session_id,
                        )
                        messages.append(continuation)
                        context.control.persist_runtime_state(active=True)
                        await services.session_runtime.checkpoint(
                            run_context,
                            "running",
                        )
                        context.hooks.trace(
                            "model_output_limit_continuation",
                            count=output_limit_continuations,
                            budget=_OUTPUT_LIMIT_CONTINUATION_BUDGET,
                            had_tool_calls=bool(result.tool_calls),
                        )
                        context.hooks.on_turn_end(messages, "continue")
                        continue
                    exhausted = (
                        "Output-limit recovery was exhausted; the run stopped without "
                        "claiming the truncated response was complete."
                    )
                    final_output = f"{visible}\n\n{exhausted}"
                    set_assistant_error(
                        assistant_message,
                        TrustedPublicMessage(
                            "output_limit_recovery_exhausted",
                            exhausted,
                            retryable=True,
                        ),
                        name="ModelOutputLimitError",
                        data={"message": exhausted, "isRetryable": True},
                        publish=context.messages.publish_event,
                    )
                    await services.session_runtime.checkpoint(run_context, "error")
                    context.hooks.on_turn_end(messages, "error")
                    return await context.lifecycle.finalize(
                        messages,
                        "error",
                        on_text,
                        on_token,
                        stream,
                        content_text=final_output,
                    )

                if result.tools_executed_in_stream:
                    record_provider_turn(
                        tool_calls=result.tool_calls,
                        finish_reason=result.finish_reason or "tool-calls",
                    )
                    finish_snapshot = (
                        await context.snapshots.capture_async(
                            "step-finish",
                            message_part["message_id"],
                        ) if start_snapshot else None
                    )
                    processor.finish_step(
                        result.finish_reason or "tool-calls",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        total_tokens=result.total_tokens,
                        reasoning_tokens=result.reasoning_tokens,
                        cache_read_tokens=result.cache_read_tokens,
                        cache_write_tokens=result.cache_write_tokens,
                        cost=(result.cost if result.cost_known else None),
                        snapshot=finish_snapshot,
                    )
                    context.snapshots.record_patch(messages, processor, finish_snapshot)
                    await services.session_runtime.checkpoint(run_context, "running")
                    step_result = result.tool_outcome or processor.process_result()
                    if step_result == "stop":
                        context.hooks.on_turn_end(messages, "blocked")
                        return await context.lifecycle.finalize(
                            messages,
                            "blocked",
                            on_text,
                            on_token,
                            stream,
                            content_text=result.content,
                        )
                    if step_result == "terminal":
                        final_content = await context.policy.terminal_content(
                            result.content or "", messages,
                        )
                        settlement = await self._settle_terminal_boundary(
                            context,
                            services,
                            run_context,
                            messages,
                            boundary="streamed_tool_terminal",
                            content=final_content,
                            completed_turns=turn_index + 1,
                            work_budget=work_budget,
                            resolve_tool_runtime_context=resolve_tool_runtime_context,
                            on_tool=on_tool,
                            on_text=on_text,
                            natural_completion=True,
                        )
                        if settlement.action == "finalize":
                            context.hooks.on_turn_end(messages, settlement.status)
                            return await context.lifecycle.finalize(
                                messages,
                                settlement.status,
                                on_text,
                                on_token,
                                stream,
                                content_text=settlement.content,
                                **(
                                    {"max_turns": max_turns}
                                    if settlement.status == "max_turns" else {}
                                ),
                            )
                    settlement = await self._settle_terminal_boundary(
                        context,
                        services,
                        run_context,
                        messages,
                        boundary="streamed_tool_batch",
                        content=result.content or "",
                        completed_turns=turn_index + 1,
                        work_budget=work_budget,
                        resolve_tool_runtime_context=resolve_tool_runtime_context,
                        on_tool=on_tool,
                        on_text=on_text,
                        natural_completion=False,
                    )
                    if settlement.action == "finalize":
                        context.hooks.on_turn_end(messages, settlement.status)
                        return await context.lifecycle.finalize(
                            messages,
                            settlement.status,
                            on_text,
                            on_token,
                            stream,
                            content_text=settlement.content,
                            **(
                                {"max_turns": max_turns}
                                if settlement.status == "max_turns" else {}
                            ),
                        )
                    context.control.persist_runtime_state(active=True)
                    await context.planning.replan()
                    context.hooks.on_turn_end(messages, "continue")
                    continue

                if not result.tool_calls:
                    record_provider_turn(
                        finish_reason=result.finish_reason or "stop",
                    )
                    processor.finish_step(
                        result.finish_reason or "stop",
                        input_tokens=result.input_tokens,
                        output_tokens=result.output_tokens,
                        total_tokens=result.total_tokens,
                        reasoning_tokens=result.reasoning_tokens,
                        cache_read_tokens=result.cache_read_tokens,
                        cache_write_tokens=result.cache_write_tokens,
                        cost=(result.cost if result.cost_known else None),
                        snapshot=(
                            await context.snapshots.capture_async(
                                "step-finish", message_part["message_id"],
                            ) if start_snapshot else None
                        ),
                    )
                    await services.session_runtime.checkpoint(run_context, "running")
                    if not str(result.content or "").strip():
                        if (
                            empty_completion_retries
                            < _EMPTY_COMPLETION_RETRY_BUDGET
                            and turn_index + 1 < max_turns
                        ):
                            empty_completion_retries += 1
                            correction = stamp_user_message({
                                "role": "user",
                                "content": _EMPTY_COMPLETION_PROMPT,
                                "_nz_synthetic": True,
                                "_nz_empty_completion_retry": True,
                            })
                            attach_message_identity(
                                correction,
                                session_id=context.session_id,
                            )
                            messages.append(correction)
                            context.control.persist_runtime_state(active=True)
                            await services.session_runtime.checkpoint(
                                run_context,
                                "running",
                            )
                            context.hooks.trace(
                                "empty_assistant_completion_retry",
                                count=empty_completion_retries,
                                budget=_EMPTY_COMPLETION_RETRY_BUDGET,
                                reasoning_only=bool(
                                    str(
                                        result.extra.get("reasoning_content") or ""
                                    ).strip()
                                ),
                            )
                            context.hooks.on_turn_end(messages, "continue")
                            continue
                        empty_error = (
                            "The model returned an empty response twice; the run was "
                            "stopped without claiming completion. Retry the request or "
                            "choose another model/provider."
                        )
                        set_assistant_error(
                            assistant_message,
                            TrustedPublicMessage(
                                "empty_model_response",
                                empty_error,
                                retryable=True,
                            ),
                            name="EmptyModelResponseError",
                            data={
                                "message": empty_error,
                                "isRetryable": True,
                            },
                            publish=context.messages.publish_event,
                        )
                        await services.session_runtime.checkpoint(
                            run_context,
                            "error",
                        )
                        context.hooks.trace(
                            "empty_assistant_completion_exhausted",
                            count=empty_completion_retries + 1,
                            budget=_EMPTY_COMPLETION_RETRY_BUDGET,
                        )
                        context.hooks.on_turn_end(messages, "error")
                        return await context.lifecycle.finalize(
                            messages,
                            "error",
                            on_text,
                            on_token,
                            stream,
                            content_text=empty_error,
                        )
                    if context.policy.resolve_structured_output(
                        result.content or "", messages,
                    ):
                        await services.session_runtime.checkpoint(run_context, "running")
                        context.hooks.on_turn_end(messages, "continue")
                        continue
                    if context.control.has_agent_call_stack():
                        transition = context.policy.return_from_as_tool(
                            messages, result.content or "",
                        )
                        await context.control.notify_agent_switched(transition)
                        await services.session_runtime.checkpoint(run_context, "running")
                        context.hooks.on_turn_end(messages, "continue")
                        continue
                    idle_yield = getattr(context.control, "idle_yield", None)
                    if (
                        turn_index + 1 < max_turns
                        and callable(idle_yield)
                        and await idle_yield(messages)
                    ):
                        context.control.persist_runtime_state(active=True)
                        await services.session_runtime.checkpoint(
                            run_context,
                            "running",
                        )
                        context.hooks.trace(
                            "idle_yield_resumed",
                            completed_turns=turn_index + 1,
                        )
                        context.hooks.on_turn_end(messages, "continue")
                        continue
                    settlement = await self._settle_terminal_boundary(
                        context,
                        services,
                        run_context,
                        messages,
                        boundary="natural_completion",
                        content=result.content or "",
                        completed_turns=turn_index + 1,
                        work_budget=work_budget,
                        resolve_tool_runtime_context=resolve_tool_runtime_context,
                        on_tool=on_tool,
                        on_text=on_text,
                        natural_completion=True,
                    )
                    if settlement.action == "continue":
                        context.hooks.on_turn_end(messages, "continue")
                        continue
                    gate_status = settlement.status
                    context.hooks.on_turn_end(messages, gate_status)
                    final_content = settlement.content
                    if gate_status == "stopped_by_hook":
                        final_content = context.control.stop_hook_reason() or final_content
                    return await context.lifecycle.finalize(
                        messages,
                        gate_status,
                        on_text,
                        on_token,
                        stream,
                        content_text=final_content,
                        **(
                            {"max_turns": max_turns}
                            if gate_status == "max_turns" else {}
                        ),
                    )

                async def execute_batch():
                    return await services.tools.execute_batch_async(
                        resolve_tool_runtime_context(), result.tool_calls, messages,
                        on_tool, on_text, processor=processor, usage=result,
                        approved_batch=approved_tool_batch,
                    )
                try:
                    step_result = await self._middleware.run(
                        "tool_batch", run_context, execute_batch,
                    )
                except (asyncio.CancelledError, KeyboardInterrupt):
                    raise
                except BaseException as exc:
                    public = to_public_error(exc)
                    if public.code not in {
                        "guardrail_blocked",
                        "guardrail_review_required",
                    }:
                        raise
                    await settle_policy_exception(exc, "tool_guardrail")
                    raise PublicRuntimeError(public) from None
                record_provider_turn(
                    tool_calls=result.tool_calls,
                    finish_reason=result.finish_reason or "tool-calls",
                )
                if step_result == "stop":
                    context.hooks.on_turn_end(messages, "blocked")
                    return await context.lifecycle.finalize(
                        messages,
                        "blocked",
                        on_text,
                        on_token,
                        stream,
                        content_text=result.content,
                    )
                if step_result == "terminal":
                    final_content = await context.policy.terminal_content(
                        result.content or "", messages,
                    )
                    settlement = await self._settle_terminal_boundary(
                        context,
                        services,
                        run_context,
                        messages,
                        boundary="tool_terminal",
                        content=final_content,
                        completed_turns=turn_index + 1,
                        work_budget=work_budget,
                        resolve_tool_runtime_context=resolve_tool_runtime_context,
                        on_tool=on_tool,
                        on_text=on_text,
                        natural_completion=True,
                    )
                    if settlement.action == "finalize":
                        context.hooks.on_turn_end(messages, settlement.status)
                        return await context.lifecycle.finalize(
                            messages,
                            settlement.status,
                            on_text,
                            on_token,
                            stream,
                            content_text=settlement.content,
                            **(
                                {"max_turns": max_turns}
                                if settlement.status == "max_turns" else {}
                            ),
                        )
                settlement = await self._settle_terminal_boundary(
                    context,
                    services,
                    run_context,
                    messages,
                    boundary="tool_batch",
                    content=result.content or "",
                    completed_turns=turn_index + 1,
                    work_budget=work_budget,
                    resolve_tool_runtime_context=resolve_tool_runtime_context,
                    on_tool=on_tool,
                    on_text=on_text,
                    natural_completion=False,
                )
                if settlement.action == "finalize":
                    context.hooks.on_turn_end(messages, settlement.status)
                    return await context.lifecycle.finalize(
                        messages,
                        settlement.status,
                        on_text,
                        on_token,
                        stream,
                        content_text=settlement.content,
                        **(
                            {"max_turns": max_turns}
                            if settlement.status == "max_turns" else {}
                        ),
                    )
                context.control.persist_runtime_state(active=True)
                await context.planning.replan()
                context.hooks.on_turn_end(messages, "continue")

            settlement = await self._settle_terminal_boundary(
                context,
                services,
                run_context,
                messages,
                boundary="loop_exhausted",
                content="",
                completed_turns=max_turns,
                work_budget=work_budget,
                resolve_tool_runtime_context=resolve_tool_runtime_context,
                on_tool=on_tool,
                on_text=on_text,
                natural_completion=False,
            )
            return await context.lifecycle.finalize(
                messages,
                settlement.status or "max_turns",
                on_text,
                on_token,
                stream,
                content_text=settlement.content,
                max_turns=max_turns,
            )
        except asyncio.CancelledError:
            return await context.lifecycle.finalize(
                messages, "cancelled", on_text, on_token, stream,
            )
        except KeyboardInterrupt:
            return await context.lifecycle.finalize(
                messages, "interrupted", on_text, on_token, stream,
            )

    async def _settle_terminal_boundary(
        self,
        context: RunnerExecutionContext,
        services: RuntimeServices,
        run_context: RunContext,
        messages: list,
        *,
        boundary: str,
        content: str,
        completed_turns: int,
        work_budget: WorkBudgetController,
        resolve_tool_runtime_context,
        on_tool=None,
        on_text=None,
        natural_completion: bool,
    ) -> _TerminalBoundaryDecision:
        """Settle exact acceptance and hard requirements before status changes."""
        at_nominal_boundary = completed_turns >= work_budget.nominal_turns
        at_hard_cap = completed_turns >= work_budget.max_turns
        state = context.runtime_state
        zone = work_budget.zone(completed_turns)
        if zone != "green":
            action = self._verification_action(context, zone)
            command = str(getattr(action, "command", "") or "")
            context.hooks.trace(
                "verification_scheduler_decision",
                zone=zone,
                kind=action.kind,
                stage=action.stage,
                command_fingerprint=(
                    hashlib.sha256(command.encode("utf-8")).hexdigest()[:12]
                    if command else ""
                ),
                mutation_generation=action.mutation_generation,
                reason=action.reason,
            )
            if action.kind == "stage":
                await self._execute_scheduled_verification(
                    context,
                    services,
                    run_context,
                    messages,
                    action,
                    resolve_tool_runtime_context,
                    on_tool,
                    on_text,
                )
        early_tool_candidate = bool(
            not natural_completion
            and not at_nominal_boundary
            and early_tool_completion_ready(state)
        )
        if (
            not natural_completion
            and not at_nominal_boundary
            and not early_tool_candidate
        ):
            return _TerminalBoundaryDecision(
                action="continue",
                reason="tool_boundary_before_nominal_sla",
            )

        contract_executed = await self._execute_due_verification_contract(
            context,
            services,
            run_context,
            messages,
            "completion",
            resolve_tool_runtime_context,
            on_tool,
            on_text,
        )
        generation = int(getattr(state, "mutation_generation", 0) or 0)
        acceptance_generation = effective_acceptance_generation(state)
        contract_required = False
        contract_passed = True
        stored_contract = getattr(state, "verification_contract", None)
        if isinstance(stored_contract, dict) and stored_contract:
            try:
                contract = VerificationContract.from_dict(stored_contract)
            except (TypeError, ValueError):
                contract = None
            if contract is not None:
                contract_required = bool(
                    getattr(state, "has_diff", False)
                    and contract.command
                    and contract.targets
                )
                if contract_required:
                    contract_passed = bool(
                        contract.passed is True
                        and contract.attempted_generation == acceptance_generation
                    )

        ledger_provider = getattr(state, "requirement_ledger_snapshot", None)

        def unresolved_requirement_ids() -> tuple[str, ...]:
            if not (
                callable(ledger_provider)
                and getattr(state, "requirement_ledger", None)
            ):
                return ()
            return tuple(
                item.requirement.id for item in ledger_provider().unresolved()
            )

        unresolved_ids = unresolved_requirement_ids()
        evidence_complete = contract_passed and not unresolved_ids
        semantic_pending_for = getattr(state, "semantic_review_pending_only", None)
        semantic_pending_before = bool(
            callable(semantic_pending_for) and semantic_pending_for()
        )
        last_review_generation = int(
            getattr(state, "completion_review_generation", -1) or -1
        )
        semantic_review_due = bool(
            semantic_pending_before
            and (
                natural_completion
                or generation != last_review_generation
            )
        )
        early_completion_review_due = bool(
            early_tool_candidate
            and generation != last_review_generation
        )

        status = ""
        reason = ""
        should_run_completion_review = bool(
            contract_passed
            and (evidence_complete or semantic_pending_before)
            and (
                natural_completion
                or (at_nominal_boundary and semantic_review_due)
                or early_completion_review_due
            )
        )
        if should_run_completion_review:
            if semantic_pending_before or early_tool_candidate:
                state.completion_review_generation = generation
            review_content = content or self._deterministic_terminal_summary(state)
            status = await context.policy.verify_completion(
                messages,
                "completed",
                review_content,
            )
            unresolved_ids = unresolved_requirement_ids()
            evidence_complete = contract_passed and not unresolved_ids
            if status == "continue":
                evidence_complete = False
                reason = (
                    "semantic_review_requires_revision"
                    if semantic_pending_before else "completion_gate_requires_more_work"
                )
                observe_rejection = getattr(
                    state,
                    "observe_completion_review_rejection",
                    None,
                )
                if callable(observe_rejection):
                    observe_rejection(reason)
            elif semantic_pending_before and evidence_complete:
                reason = "semantic_review_and_ledger_satisfied"
            elif not evidence_complete:
                reason = "semantic_review_evidence_unavailable"
        elif evidence_complete and at_nominal_boundary and contract_required:
            status = "completed"
            reason = "exact_acceptance_and_ledger_satisfied"
        elif not contract_passed:
            reason = "exact_acceptance_not_passed"
        elif unresolved_ids:
            reason = "hard_requirements_unresolved"
        else:
            reason = "tool_boundary_has_no_terminal_acceptance"

        review_stopped = bool(
            status
            and status not in {"continue", "completed", "completed_unverified"}
        )
        if review_stopped:
            decision = _TerminalBoundaryDecision(
                action="finalize",
                status=status,
                content=(
                    self._deterministic_failure_summary(
                        state,
                        unresolved_ids=unresolved_ids,
                    )
                    if status == "max_turns"
                    else content or self._deterministic_failure_summary(
                        state,
                        unresolved_ids=unresolved_ids,
                    )
                ),
                reason=reason or "completion_review_stopped",
            )
        elif status and status != "continue" and evidence_complete:
            if (
                at_hard_cap
                and status in {"completed", "completed_unverified"}
                and _acknowledges_max_steps(content)
            ):
                status = "max_turns"
                reason = "model_reported_hard_cap_exhaustion"
            if status == "max_turns":
                final_content = self._deterministic_failure_summary(
                    state,
                    unresolved_ids=unresolved_ids,
                )
            elif boundary in {"tool_batch", "streamed_tool_batch"}:
                final_content = self._deterministic_terminal_summary(state)
            else:
                final_content = content or self._deterministic_terminal_summary(state)
            decision = _TerminalBoundaryDecision(
                action="finalize",
                status=status,
                content=final_content,
                reason=reason or "completion_gate_satisfied",
            )
        elif status == "continue" and not at_hard_cap:
            decision = _TerminalBoundaryDecision(
                action="continue",
                reason=reason or "completion_gate_requires_more_work",
            )
        elif not at_nominal_boundary:
            decision = _TerminalBoundaryDecision(
                action="continue",
                reason=reason or "boundary_not_ready",
            )
        elif not at_hard_cap:
            decision = _TerminalBoundaryDecision(
                action="continue",
                reason=reason or "soft_work_budget_continues",
            )
        else:
            decision = _TerminalBoundaryDecision(
                action="finalize",
                status="max_turns",
                content=self._deterministic_failure_summary(
                    state,
                    unresolved_ids=unresolved_ids,
                ),
                reason=reason or "hard_cap_exhausted",
            )

        if (
            natural_completion
            and unresolved_ids
            and reason == "hard_requirements_unresolved"
            and not at_hard_cap
        ):
            gate, appended = append_completion_guidance(messages, state)
            if appended:
                context.hooks.trace(
                    "requirement_completion_blocked",
                    missing_ids=list(gate.missing_ids),
                    prompt_count=state.completion_gate_prompts,
                    mutation_generation=generation,
                    source="terminal_boundary",
                )
                if decision.action == "finalize":
                    decision = _TerminalBoundaryDecision(
                        action="continue",
                        reason="hard_requirements_unresolved",
                    )
            elif (
                int(getattr(state, "completion_gate_prompts", 0) or 0)
                >= COMPLETION_GATE_REANIMATE_BUDGET
            ):
                context.hooks.trace(
                    "requirement_completion_budget_exhausted",
                    missing_ids=list(gate.missing_ids),
                    prompt_count=state.completion_gate_prompts,
                    mutation_generation=generation,
                    source="terminal_boundary",
                )
                if at_hard_cap:
                    decision = _TerminalBoundaryDecision(
                        action="finalize",
                        status="max_turns",
                        content=self._deterministic_failure_summary(
                            state,
                            unresolved_ids=unresolved_ids,
                        ),
                        reason="hard_cap_exhausted",
                    )
                else:
                    decision = _TerminalBoundaryDecision(
                        action="continue",
                        reason="hard_requirements_unresolved",
                    )

        context.control.persist_runtime_state(active=decision.action != "finalize")
        context.hooks.trace(
            "terminal_boundary_settled",
            boundary=boundary,
            decision=decision.action,
            status=decision.status,
            reason=decision.reason,
            completed_turns=completed_turns,
            nominal_turns=work_budget.nominal_turns,
            hard_cap=work_budget.max_turns,
            contract_executed=contract_executed,
            contract_required=contract_required,
            contract_passed=contract_passed,
            mutation_generation=generation,
            unresolved_requirements=list(unresolved_ids),
            semantic_review_pending_before=semantic_pending_before,
            semantic_review_pending_after=bool(
                callable(semantic_pending_for) and semantic_pending_for()
            ),
            semantic_review_generation=int(
                getattr(state, "completion_review_generation", -1) or -1
            ),
            early_tool_completion_candidate=early_tool_candidate,
        )
        await services.session_runtime.checkpoint(
            run_context,
            "running",
        )
        return decision

    @staticmethod
    def _deterministic_terminal_summary(state) -> str:
        """Describe only persisted facts when a final tool turn has no prose."""
        paths = [str(path) for path in getattr(state, "changed_files", []) if str(path)]
        changed = ", ".join(paths[:8]) or "the requested workspace files"
        command = str(
            (getattr(state, "verification_contract", {}) or {}).get("command") or ""
        ).strip()
        summary = f"Completed the requested changes in {changed}."
        if command:
            summary += f" Exact acceptance passed: `{command}`."
        return summary

    @staticmethod
    def _deterministic_failure_summary(
        state,
        *,
        unresolved_ids: tuple[str, ...] = (),
    ) -> str:
        """Return a factual non-empty summary when hard-cap settlement fails."""
        paths = [str(path) for path in getattr(state, "changed_files", []) if str(path)]
        changed = ", ".join(paths[:8]) or "none recorded"
        command = str(
            (getattr(state, "verification_contract", {}) or {}).get("command") or ""
        ).strip()
        summary = "Stopped at the work limit without claiming completion."
        summary += f" Changed files: {changed}."
        if command:
            stored = getattr(state, "verification_contract", {}) or {}
            passed_current = bool(
                stored.get("passed") is True
                and int(stored.get("attempted_generation") or -1)
                == effective_acceptance_generation(state)
            )
            if passed_current:
                summary += f" Exact acceptance passed: `{command}`."
            else:
                summary += f" Exact acceptance did not pass: `{command}`."
        if unresolved_ids:
            summary += " Unresolved requirements: " + ", ".join(unresolved_ids) + "."
        return summary

    @staticmethod
    def _verification_action(context: RunnerExecutionContext, zone: str):
        """Resolve one budget-zone action from ledger and staged evidence."""
        from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

        state = context.runtime_state
        stored_contract = getattr(state, "verification_contract", None)
        unresolved = ("contract-unavailable",)
        ledger_snapshot = getattr(state, "requirement_ledger_snapshot", None)
        if callable(ledger_snapshot) and getattr(state, "requirement_ledger", None):
            unresolved = tuple(
                item.requirement.id for item in ledger_snapshot().unresolved()
            )
        status = {}
        status_provider = getattr(context.policy, "verification_status", None)
        if callable(status_provider):
            status = status_provider()
        return VerificationScheduler().action(
            zone,
            verification_status=status,
            unresolved_requirements=unresolved,
            has_exact_contract=bool(
                isinstance(stored_contract, dict)
                and stored_contract.get("command")
            ),
            exact_attempts=(
                max(0, int(stored_contract.get("attempts", 0) or 0))
                if isinstance(stored_contract, dict) else 0
            ),
            mutation_generation=int(
                getattr(state, "mutation_generation", 0) or 0
            ),
            source_mutation_generation=int(
                getattr(state, "source_mutation_generation", 0) or 0
            ),
            scheduled_generations=dict(
                getattr(state, "scheduled_verification_generations", {}) or {}
            ),
        )

    async def _execute_scheduled_verification(
        self,
        context: RunnerExecutionContext,
        services: RuntimeServices,
        run_context: RunContext,
        messages: list,
        action,
        resolve_tool_runtime_context,
        on_tool=None,
        on_text=None,
    ) -> bool:
        """Run one cheap/targeted scheduler command through the normal Bash path."""
        command = str(getattr(action, "command", "") or "").strip()
        stage = str(getattr(action, "stage", "") or "").strip()
        if not command or stage not in {"static", "targeted"}:
            return False
        state = context.runtime_state
        generation = int(
            getattr(action, "mutation_generation", None)
            if getattr(action, "mutation_generation", None) is not None
            else getattr(state, "mutation_generation", 0)
        )
        scheduled = getattr(state, "scheduled_verification_generations", None)
        if not isinstance(scheduled, dict):
            scheduled = {}
            state.scheduled_verification_generations = scheduled
        scheduled[stage] = generation
        context.control.persist_runtime_state(active=True)
        call_id = f"verification-stage-{stage}-{generation}"
        tool_call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({
                    "command": command,
                    "_nz_runtime_verification_stage": stage,
                }, ensure_ascii=False),
            },
        }
        approved_batch = None
        approve = getattr(services.tools, "approve_tool_calls_async", None)
        if callable(approve):
            approved_batch = await approve(
                resolve_tool_runtime_context(),
                [tool_call],
                messages,
            )
            tool_call = approved_batch.calls[0]
        message_part = context.messages.new_message_part(
            max(1, int(getattr(state, "turn_count", 0) or 0)),
        )
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call],
            "_nz_synthetic": True,
            "_nz_verification_stage": stage,
            INTERACTION_RUN_ID_KEY: run_context.interaction_run_id,
        }
        context.messages.bind_assistant_context(assistant)
        attach_message_identity(
            assistant,
            message_part["message_id"],
            session_id=context.session_id,
        )
        assistant[ASSISTANT_TIME_KEY] = {"created": time.time()}
        messages.append(assistant)
        processor = SessionProcessor(
            assistant,
            publish=context.messages.publish_event,
            on_message_updated=(lambda _message: run_context.session.mark_dirty()),
        )
        processor.start_step()
        processor.register_tool_calls([tool_call])
        await services.session_runtime.checkpoint(run_context, "running")
        before = len(messages)

        async def execute_batch():
            return await services.tools.execute_batch_async(
                resolve_tool_runtime_context(),
                [tool_call],
                messages,
                on_tool,
                on_text,
                processor=processor,
                approved_batch=approved_batch,
            )

        await self._middleware.run("tool_batch", run_context, execute_batch)
        output = next(
            (
                str(message.get("content") or "")
                for message in messages[before:]
                if isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_call_id") == call_id
            ),
            "Error: scheduled verification produced no tool result",
        )
        passed = not output.startswith(
            ("Error:", "Denied", "Command exited with code")
        )
        observe_requirements = getattr(
            state,
            "observe_requirement_verification",
            None,
        )
        if callable(observe_requirements):
            observe_requirements(
                command,
                passed=passed,
                acceptance=False,
            )
        context.control.persist_runtime_state(active=True)
        context.hooks.trace(
            "verification_stage_executed",
            stage=stage,
            command=command,
            mutation_generation=generation,
            passed=passed,
            output_len=len(output),
        )
        await services.session_runtime.checkpoint(run_context, "running")
        return True

    async def _execute_due_verification_contract(
        self,
        context: RunnerExecutionContext,
        services: RuntimeServices,
        run_context: RunContext,
        messages: list,
        zone: str,
        resolve_tool_runtime_context,
        on_tool=None,
        on_text=None,
    ) -> bool:
        """Run one user-declared acceptance command through the normal tool path."""
        state = context.runtime_state
        stored = getattr(state, "verification_contract", None)
        if not isinstance(stored, dict) or not stored:
            return False
        try:
            contract = VerificationContract.from_dict(stored)
            generation = effective_acceptance_generation(state)
            due = contract.is_due(
                zone=zone,
                has_diff=bool(getattr(state, "has_diff", False)),
                mutation_generation=generation,
            )
        except (TypeError, ValueError):
            context.hooks.trace("verification_contract_invalid", zone=zone)
            return False
        if not due or not contract.command or not contract.targets:
            return False
        readiness = getattr(state, "verification_contract_ready", None)
        if callable(readiness) and not readiness(zone):
            context.hooks.trace(
                "verification_contract_deferred",
                zone=zone,
                mutation_generation=generation,
                open_todo_items=int(
                    getattr(state, "open_todo_items", 0) or 0
                ),
            )
            return False

        call_id = f"verification-contract-{generation}-{contract.attempts + 1}"
        tool_call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps(
                    {
                        "command": contract.command,
                        "_nz_runtime_contract": True,
                    },
                    ensure_ascii=False,
                ),
            },
        }
        approved_batch = None
        approve = getattr(services.tools, "approve_tool_calls_async", None)
        if callable(approve):
            approved_batch = await approve(
                resolve_tool_runtime_context(),
                [tool_call],
                messages,
            )
            tool_call = approved_batch.calls[0]
        message_part = context.messages.new_message_part(
            max(1, int(getattr(state, "turn_count", 0) or 0)),
        )
        assistant = {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call],
            "_nz_synthetic": True,
            "_nz_verification_contract": True,
            INTERACTION_RUN_ID_KEY: run_context.interaction_run_id,
        }
        context.messages.bind_assistant_context(assistant)
        attach_message_identity(
            assistant,
            message_part["message_id"],
            session_id=context.session_id,
        )
        assistant[ASSISTANT_TIME_KEY] = {"created": time.time()}
        messages.append(assistant)
        processor = SessionProcessor(
            assistant,
            publish=context.messages.publish_event,
            on_message_updated=(lambda _message: run_context.session.mark_dirty()),
        )
        processor.start_step()
        processor.register_tool_calls([tool_call])
        await services.session_runtime.checkpoint(run_context, "running")
        before = len(messages)

        async def execute_batch():
            return await services.tools.execute_batch_async(
                resolve_tool_runtime_context(),
                [tool_call],
                messages,
                on_tool,
                on_text,
                processor=processor,
                approved_batch=approved_batch,
            )

        await self._middleware.run("tool_batch", run_context, execute_batch)
        output = next(
            (
                str(message.get("content") or "")
                for message in messages[before:]
                if isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_call_id") == call_id
            ),
            "Error: verification command produced no tool result",
        )
        passed = not output.startswith(
            ("Error:", "Denied", "Command exited with code")
        )
        observe_contract = getattr(
            context.policy,
            "observe_verification_contract",
            None,
        )
        if callable(observe_contract):
            observe_contract(contract.command, output, passed)
        contract.record_attempt(
            generation,
            passed=passed,
            output=output,
            source="runtime",
            zone=zone,
        )
        state.verification_contract = contract.to_dict()
        observe_requirements = getattr(
            state,
            "observe_requirement_verification",
            None,
        )
        if callable(observe_requirements):
            observe_requirements(
                contract.command,
                passed=passed,
                acceptance=True,
            )
        if passed:
            if hasattr(state, "verification_generation"):
                state.verification_generation = int(
                    getattr(state, "mutation_generation", generation) or generation
                )
            if hasattr(state, "changed_files_verified"):
                state.changed_files_verified = True
            if hasattr(state, "py_compile_ok"):
                state.py_compile_ok = True
        else:
            if hasattr(state, "changed_files_verified"):
                state.changed_files_verified = False
            if hasattr(state, "py_compile_ok"):
                state.py_compile_ok = False
            record_failure = getattr(state, "_record_verification_failure", None)
            if callable(record_failure):
                record_failure(output)
        context.control.persist_runtime_state(active=True)
        context.hooks.trace(
            "verification_contract_executed",
            zone=zone,
            mutation_generation=generation,
            workspace_mutation_generation=int(
                getattr(state, "mutation_generation", generation) or generation
            ),
            passed=passed,
            command=contract.command,
            targets=list(contract.targets),
            output_len=len(output),
        )
        await services.session_runtime.checkpoint(run_context, "running")
        return True


def _result_status(result: object) -> RunStatus:
    value = result.get("status") if isinstance(result, dict) else None
    normalized = str(value or "error")
    aliases = {
        "completed_unverified": RunStatus.COMPLETED,
        "aborted": RunStatus.ERROR,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return RunStatus(normalized)
    except ValueError:
        return RunStatus.ERROR


def _request_max_turns(request: RunRequest) -> int | None:
    value = request.metadata.get("max_turns")
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("RunRequest metadata max_turns must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "RunRequest metadata max_turns must be a positive integer"
        ) from exc
    if normalized < 1:
        raise ValueError("RunRequest metadata max_turns must be a positive integer")
    return normalized


def _typed_result(
    request: RunRequest, context: RunContext, raw_result: object,
) -> RunResult:
    payload = raw_result if isinstance(raw_result, dict) else {}
    final_text = str(payload.get("content") or "")
    if not final_text:
        for message in reversed(context.transcript):
            if isinstance(message, dict) and message.get("role") == "assistant":
                if (
                    message.get("_nz_internal") is True
                    or message.get("_nz_visible") is False
                ):
                    continue
                content = message.get("content")
                if isinstance(content, str) and content:
                    final_text = content
                    break
    status = context.terminal_status or _result_status(raw_result)
    return RunResult(
        status=status,
        final_text=final_text,
        messages=context.transcript,
        usage=context.usage,
        session_id=request.session_id,
        active_agent=context.active_agent,
        error=str(payload.get("last_error") or "") if status is RunStatus.ERROR else "",
        metadata={
            **copy.deepcopy(context.metadata),
            "runtime": copy.deepcopy(payload.get("runtime") or {}),
            "raw_status": str(payload.get("status") or status.value),
        },
    )
