"""Shared provider/tool state machine for every NZ-Coder Agent profile."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace

from nz_coder.protocol.message_schema import (
    ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY,
    DEFERRED_MODEL_TEXT_KEY,
    INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY,
    attach_message_identity,
    ensure_message_identities,
    is_synthetic_user_message,
    is_user_request_message,
    set_assistant_error,
    stamp_user_message,
)
from nz_coder.protocol.public_error import (
    PublicError,
    PublicRuntimeError,
    TrustedPublicMessage,
    public_error_from_wire,
    to_public_error,
)
from nz_coder.runtime.verification.recovery import is_context_overflow_error
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.core.model_context import scoped_request_tools
from nz_coder.runtime.conversation.request_snapshot import message_version
from nz_coder.runtime.agent.task_intent import capture_assessment_scope
from nz_coder.runtime.agent.planning_runtime import capture_working_plan_scope
from nz_coder.runtime.conversation.continuation_context import build_current_facts
from nz_coder.runtime.conversation.context_manager import (
    accept_compaction,
    replay_compaction_pressure,
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
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage, normalize_run_status
from nz_coder.runtime.core.run_context import RunContext, scoped_run_context
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.core.middleware import MiddlewarePipeline
from nz_coder.runtime.core.events import RuntimeEventMiddleware
from nz_coder.runtime.conversation.model_result import _normalize_llm_result_metrics
from nz_coder.runtime.core.execution_context import (
    nominal_agent_turns,
    scoped_runtime_overrides,
)
from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.foundation.workspace_trust import (
    active_config_snapshot,
    load_config_snapshot,
    scoped_config_snapshot,
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


@dataclass(frozen=True)
class _VerificationExecution:
    """尝试过检查与审批停止是两件事，所有合成入口显式传播。"""

    attempted: bool = False
    blocked_reason: str = ""


class AgentRunner:
    """Own the one production execution state machine shared by every profile."""

    def __init__(
        self,
        services: RuntimeServices | None = None,
        *,
        execution_context_factory=None,
        context_factory_abort=None,
        middleware=(),
    ) -> None:
        self._services = services
        self._execution_context_factory = execution_context_factory
        self._context_factory_abort = context_factory_abort
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
        snapshot = (
            options.config_snapshot
            or active_config_snapshot(request.workspace)
            or load_config_snapshot(request.workspace)
        )
        if snapshot.workspace.resolve() != request.workspace.resolve():
            raise ValueError("ConfigSnapshot belongs to a different workspace")
        settings = RunSettings.from_snapshot(snapshot)
        with (
            scoped_workdir(request.workspace),
            scoped_config_snapshot(snapshot),
            scoped_run_settings(settings),
            scoped_runtime_overrides(
                max_agent_turns=_request_max_turns(request),
            ),
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
        run_context = await services.session_runtime.open(request, session_lease=options.session_lease)
        from contextlib import ExitStack

        scopes = ExitStack()
        try:
            run_context.cancellation = options.cancellation
            from nz_coder.tools.scratchpad import Scratchpad, scoped_scratchpad

            ensure_message_identities(run_context.transcript, request.session_id)
            message_id = next((message[MESSAGE_ID_KEY] for message in reversed(run_context.transcript)
                               if is_user_request_message(message)), None)
            pad = (Scratchpad.for_user_message(request.session_id, message_id)
                   if message_id is not None else Scratchpad(persistent=False))
            scopes.enter_context(scoped_scratchpad(pad))
            scopes.enter_context(scoped_run_context(run_context))
        except BaseException:
            scopes.close()
            await self._abort_preparation(run_context)
            raise
        with scopes:
            return await self._execute_opened_request(request, options, run_context, factory)

    async def _execute_opened_request(
        self, request: RunRequest, options: RunOptions, run_context: RunContext, factory,
    ) -> tuple[dict, RunContext]:
        services = self._services
        try:
            execution_context = factory(run_context, services)
            if not isinstance(execution_context, RunnerExecutionContext):
                raise TypeError("execution context factory must return RunnerExecutionContext")
        except BaseException:
            await self._abort_preparation(run_context)
            raise
        run_stream = request.stream if options.stream is None else options.stream

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
        try:
            execution_context.hooks.trace(
                "agent_runner_enter", runner="native",
                runtime_profile=request.profile.name,
                active_agent=request.agent.name, stream=bool(run_stream),
            )
            if runtime_events is not None:
                await runtime_events.before_run(run_context)
            result = await self._middleware.run("run", run_context, execute_run)
            verification = result.get("verification") if isinstance(result, dict) else None
            if isinstance(verification, dict):
                run_context.metadata["verification"] = copy.deepcopy(verification)
                run_context.session.metadata["verification"] = copy.deepcopy(verification)
            if isinstance(result, dict):
                authored = str(result.get("authored_content") or "")
                run_context.metadata["authored_content"] = authored
                run_context.session.metadata["authored_content"] = authored
            await self._finalize_session(
                services, run_context, _result_status(result), execution_context.lifecycle, raw_result=result,
            )
        except asyncio.CancelledError as error:
            run_context.request_cancel()
            if not run_context.finalized:
                await self._finalize_after_run_error(
                    services, run_context, RunStatus.CANCELLED,
                    execution_context, error, execution_context.lifecycle,
                )
            if runtime_events is not None:
                await runtime_events.on_run_error(run_context, error)
            raise
        except KeyboardInterrupt as error:
            if not run_context.finalized:
                await self._finalize_after_run_error(
                    services, run_context, RunStatus.INTERRUPTED,
                    execution_context, error, execution_context.lifecycle,
                )
            if runtime_events is not None:
                await runtime_events.on_run_error(run_context, error)
            raise
        except Exception as error:
            from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
            if isinstance(error, CheckpointRestoreError) and not run_context._execution_started:
                await self._abort_preparation(run_context)
                raise
            if not run_context.finalized:
                await self._finalize_after_run_error(
                    services, run_context, RunStatus.ERROR,
                    execution_context, error, execution_context.lifecycle,
                )
            if runtime_events is not None:
                await runtime_events.on_run_error(run_context, error)
            raise
        if runtime_events is not None:
            await runtime_events.after_run(run_context, result)
        return result, run_context

    async def _abort_preparation(self, run_context) -> None:
        from nz_coder.foundation.async_utils import to_thread_settled

        def rollback():
            if self._context_factory_abort is not None:
                self._context_factory_abort(run_context)
            # 与 writer 退休在同一个不可取消的线程调用中完成，重复取消不能漏释放。
            self._services.session_runtime.abandon(run_context)

        await to_thread_settled(rollback)

    @staticmethod
    async def _finalize_session(services, run_context, status, lifecycle, *, error=None, raw_result=None) -> None:
        if run_context.finalized:
            return
        if error is not None and run_context._terminal_error is None:
            run_context._terminal_error = error
        retirement = asyncio.create_task(lifecycle.settle_session_checkpoints())
        while True:
            try:
                await asyncio.shield(retirement)
                break
            except asyncio.CancelledError:
                run_context.request_cancel()
                if retirement.cancelled():
                    raise
                # 退休仍归同一个任务所有；反复 stop 不能跳过冻结与原结果结算。
                continue
        # 退休等待期间也可能收到取消；此处原子选择并冻结，异常路径同样可结算。
        with run_context._state_lock:
            status = run_context.terminal_status or status
            try:
                run_context.freeze_execution(status)
            except asyncio.CancelledError:
                run_context.request_cancel()
                status = RunStatus.CANCELLED
                run_context.freeze_execution(status)
        from nz_coder.runtime.core.result import freeze_terminal_result
        freeze_terminal_result(run_context, raw_result)
        await services.session_runtime.finalize(run_context, status)
        lifecycle.publish_terminal(error)

    @staticmethod
    async def _finalize_after_run_error(
        services: RuntimeServices,
        run_context: RunContext,
        status: RunStatus,
        execution_context: RunnerExecutionContext | None,
        original_error: BaseException,
        lifecycle,
    ) -> None:
        """Attempt catch cleanup without ever replacing the original failure."""
        try:
            await AgentRunner._finalize_session(services, run_context, run_context.terminal_status or status, lifecycle,
                                                error=original_error)
        except BaseException as finalization_error:
            try:
                if execution_context is not None:
                    execution_context.hooks.trace(
                        "session_error_finalize_failed",
                        original_error_type=type(original_error).__name__,
                        original_error=to_public_error(original_error).to_dict(),
                        finalization_error_type=type(finalization_error).__name__,
                        finalization_error=to_public_error(
                            finalization_error
                        ).to_dict(),
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
            lifecycle_from_legacy_host,
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
        run_context = await services.session_runtime.open(
            request, session_lease=getattr(host, "_requested_session_lease", None),
        )
        try:
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
                lifecycle = lifecycle_from_legacy_host(host, services, run_context)
            else:
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
                execution_context = runner_context_from_legacy_host(
                    host, services, run_context,
                )
                lifecycle = execution_context.lifecycle
        except BaseException:
            await self._abort_preparation(run_context)
            host.active_run_context = None
            raise
        try:
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
                execution_context, error, lifecycle,
            )
            raise
        except KeyboardInterrupt as error:
            await self._finalize_after_run_error(
                services, run_context, RunStatus.INTERRUPTED,
                execution_context, error, lifecycle,
            )
            raise
        except Exception as error:
            from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
            if isinstance(error, CheckpointRestoreError) and not run_context._execution_started:
                await self._abort_preparation(run_context)
                raise
            await self._finalize_after_run_error(
                services, run_context, RunStatus.ERROR,
                execution_context, error, lifecycle,
            )
            raise
        else:
            run_context.metadata["raw_status"] = str(result.get("status") or "completed")
            await self._finalize_session(
                services, run_context, _result_status(result), lifecycle, raw_result=result,
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
        run_context._execution_started = True
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
        assessment_scope = None
        working_plan_scope = None
        repeat_source = None
        recovery = context.execution.recovery
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
            return replace(tool_runtime_context, assessment_scope=assessment_scope, working_plan_scope=working_plan_scope, repeat_source=repeat_source)
        task_message = next((copy.deepcopy(message) for message in reversed(messages)
                             if is_user_request_message(message)), None)
        try:
            run_context.check_cancelled()
            await context.planning.generate(messages)
            run_context.check_cancelled()
            context_runtime = None
            retry_same_turn = False

            def logical_turns():
                """Yield logical turns, repeating only local budget recovery."""
                nonlocal retry_same_turn
                current = start_turn
                while current < max_turns:
                    yield current
                    if retry_same_turn:
                        retry_same_turn = False
                    else:
                        current += 1

            for turn_index in logical_turns():
                assessment_scope = None
                working_plan_scope = None
                repeat_source = None
                run_context.metadata["authored_content"] = ""
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
                memory_under_pressure = False
                settings = current_run_settings()
                model_context = resolve_model_runtime_context()
                if (task_message is not None and settings.working_memory_reminder_steps > 0
                        and model_context.working_memory_allowed()
                        and any(spec.get("function", {}).get("name") == "update_scratchpad"
                                for spec in model_context.active_tool_specs())):
                    from nz_coder.runtime.conversation.working_memory import maintain_working_memory
                    from nz_coder.tools.scratchpad import current_scratchpad

                    if context_runtime is None:
                        context_runtime = context.execution.context()
                    projected = context_runtime.projected_tokens(messages)
                    replay_pressure = replay_compaction_pressure(context_runtime, messages)
                    memory_under_pressure = (
                        projected > context_runtime.budget.soft_preflight_tokens or replay_pressure
                    )
                    if replay_pressure:
                        context.hooks.trace("working_memory_replay_pressure",
                                            completed_steps=current_scratchpad().completed_steps)
                    await maintain_working_memory(
                        model_context, current_scratchpad(),
                        task={"message_id": task_message[MESSAGE_ID_KEY],
                              "content": task_message.get("content", "")},
                        history=messages, projected_tokens=projected,
                        current_facts=lambda: build_current_facts(
                            context.runtime_state, workspace=run_context.request.workspace,
                        ),
                        interval=settings.working_memory_reminder_steps,
                        superseded=context.control.has_queued_followup,
                        under_pressure=memory_under_pressure,
                    )
                    # await 期间用户可以追加消息；所有维护结局都必须经过同一停止边界。
                    if context.control.has_queued_followup():
                        return await context.lifecycle.finalize(
                            messages, "interrupted", on_text, on_token, stream,
                        )
                context.runtime_state.turn_count = turn_index + 1
                context.runtime_state.work_phase = work_budget.phase(turn_index)
                context.runtime_state.budget_pressure_zone = work_budget.zone(
                    turn_index
                )
                context.hooks.on_turn_start(messages)
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
                    verification_execution = _VerificationExecution()
                    if action.kind == "stage":
                        verification_execution = await self._execute_scheduled_verification(
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
                        verification_execution = await self._execute_due_verification_contract(
                            context,
                            services,
                            run_context,
                            messages,
                            budget_notice.zone,
                            resolve_tool_runtime_context,
                            on_tool,
                            on_text,
                        )
                    if verification_execution.blocked_reason:
                        context.hooks.on_turn_end(messages, "blocked")
                        return await context.lifecycle.finalize(
                            messages, "blocked", on_text, on_token, stream,
                            content_text=verification_execution.blocked_reason,
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
                if run_context.uses_receipt_based_delivery or run_context.defer_model_report:
                    message_part[DEFERRED_MODEL_TEXT_KEY] = True
                    assistant_message[DEFERRED_MODEL_TEXT_KEY] = True
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
                    for settlement_attempt in range(1, 3):
                        try:
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
                        except BaseException as settlement_error:
                            try:
                                context.hooks.trace(
                                    "failed_attempt_settlement_failed",
                                    settlement_error=to_public_error(
                                        settlement_error
                                    ).to_dict(),
                                    settlement_attempt=settlement_attempt,
                                    settlement_completed=(
                                        failure_settlement.completed
                                    ),
                                )
                            except BaseException:
                                pass
                    if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                        raise exc
                    raise PublicRuntimeError(public) from None

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

                def acknowledge_repeat(result):
                    from nz_coder.runtime.conversation.request_snapshot import acknowledge_usable_repeat_notice
                    acknowledge_usable_repeat_notice(
                        request_snapshot, result, recovery, context.runtime_state, run_context,
                        context.control.current_agent_name(), messages,
                    )

                async def execute_stream_tools(stream_result: object) -> str:
                    nonlocal model_result_materialized, approved_tool_batch
                    run_context.check_cancelled()
                    await resolve_start_snapshot()
                    run_context.check_cancelled()
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
                    run_context.check_cancelled()
                    commit_approved_model_result(
                        approved,
                        context=context,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        messages=messages,
                        source_provider_id=str(run_context.request.provider or ""),
                        source_provider_instance_id=(
                            _provider_instance_id(resolve_model_runtime_context())
                        ),
                        source_model_id=str(run_context.request.model or ""),
                    )
                    acknowledge_repeat(approved.result)
                    model_result_materialized = True
                    async def execute_batch():
                        run_context.check_cancelled()
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
                    # 来源在发起请求前固定；流式和普通工具批次共享，不在结算时套当前指令。
                    assessment_scope = capture_assessment_scope(
                        context.runtime_state, run_context, assistant_message[MESSAGE_ID_KEY],
                    )
                    working_plan_scope = capture_working_plan_scope(
                        context.runtime_state, run_context, assistant_message[MESSAGE_ID_KEY], messages,
                    )
                    repeat_source = recovery.capture_repeat_source(
                        context.runtime_state, run_context, context.control.current_agent_name(), messages,
                    )
                    try:
                        if context_runtime is None:
                            context_runtime = context.execution.context()
                        # 可选记忆重排含同步模型调用；取消先通知本轮，再等冻结写入实际退出。
                        request_snapshot = await _to_thread_settled(
                            context_runtime.freeze_request,
                            messages,
                            cancel_callback=run_context.request_cancel,
                        )
                        request_snapshot = replace(request_snapshot, retained=(assistant_message,))
                        if turn_index + 1 >= max_turns:
                            request_snapshot = replace(request_snapshot, suffix=({"role": "assistant", "content": _MAX_STEPS_PROMPT},))
                            context.hooks.trace("max_steps_prompt_injected", step=turn_index + 1)
                        compacted = await services.context.prepare_async(
                            context_runtime,
                            messages,
                            on_text=on_text,
                            attempt_state=compaction_attempts,
                            request_snapshot=request_snapshot,
                        )
                        if compacted and repeat_source is not None:
                            repeat_source = recovery.refresh_repeat_source_after_compaction(
                                repeat_source, context.runtime_state, run_context,
                                context.control.current_agent_name(), messages,
                            )
                            if repeat_source is None:
                                raise asyncio.CancelledError
                        if request_snapshot.repeat_notice != recovery.repeat_notice_snapshot(
                            context.runtime_state, run_context, context.control.current_agent_name(), messages,
                        ):
                            # 仅撤回失效观察，保留本请求已经冻结的记忆、工具及指令。
                            request_snapshot = replace(request_snapshot, repeat_notice=None)
                        if repeat_source is not None and not recovery.repeat_source_current(
                            repeat_source, context.runtime_state, run_context, context.control.current_agent_name(), messages,
                        ):
                            raise asyncio.CancelledError
                        if compacted:
                            # 候选安装必须先落盘，再开始下一次模型请求。
                            await services.session_runtime.checkpoint(run_context, "running")
                    except _CompactionAttemptsExhausted as exc:
                        start_snapshot_awaited = True
                        context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                        processor.interrupt_unsettled()
                        processor.finish_step("error")
                        context.messages.persist_compaction_exhaustion(messages, exc)
                        context.hooks.on_turn_end(messages, "error")
                        return await context.lifecycle.finalize(
                            messages, "error", on_text, on_token, stream,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        start_snapshot_awaited = True
                        context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                        processor.interrupt_unsettled()
                        processor.finish_step("error")
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
                    if request_snapshot.working_memory_revision is not None:
                        memory_under_pressure = (
                            context_runtime.projected_tokens(messages)
                            > context_runtime.budget.soft_preflight_tokens
                            or replay_compaction_pressure(context_runtime, messages)
                        )
                    run_context.check_cancelled()
                    api_messages = request_snapshot.project(messages)
                    actual_request_tokens = request_snapshot.estimate(messages)
                    preflight = getattr(services.model, "preflight_request", None)
                    preflight_result = (
                        preflight(resolve_model_runtime_context(), api_messages, tools=request_snapshot.tools)
                        if callable(preflight)
                        else None
                    )
                    provider_turn = None
                    if preflight_result is None:
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
                        memory_step: bool = False,
                    ) -> None:
                        nonlocal provider_turn_recorded
                        if provider_turn_recorded or provider_turn is None:
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
                        if memory_step and request_snapshot.working_memory_revision is not None:
                            from nz_coder.tools.scratchpad import current_scratchpad

                            pad = current_scratchpad()
                            error = pad.complete_step(
                                request_snapshot.working_memory_revision,
                                under_pressure=memory_under_pressure,
                            )
                            if error:
                                context.hooks.trace("working_memory_schedule_save_failed", error=error)
                            else:
                                context.hooks.trace("working_memory_step_completed", revision=pad.revision,
                                                    completed_steps=pad.completed_steps)

                    async def execute_model():
                        run_context.check_cancelled()
                        with scoped_request_tools(request_snapshot.tools):
                            return await services.model.complete_turn(
                                resolve_model_runtime_context(), api_messages,
                                stream=stream,
                                on_token=(None if output_guarded or message_part.get(DEFERRED_MODEL_TEXT_KEY) else on_token),
                                message_part=message_part,
                                stream_tool_handler=execute_stream_tools,
                            )
                    result = (
                        preflight_result
                        if preflight_result is not None
                        else await self._middleware.run(
                            "model", run_context, execute_model,
                        )
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
                    run_context.check_cancelled()
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
                    run_context.check_cancelled()
                    commit_approved_model_result(
                        approved,
                        context=context,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        messages=messages,
                        reconcile=model_result_materialized,
                        source_provider_id=str(run_context.request.provider or ""),
                        source_provider_instance_id=(
                            _provider_instance_id(resolve_model_runtime_context())
                        ),
                        source_model_id=str(run_context.request.model or ""),
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
                    saved_error = set_assistant_error(
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
                    processor.fail_unsettled(public_error_from_wire(saved_error["data"]["public_error"]))
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
                    context.hooks.trace(
                        "context_overflow",
                        source=result.failure_source or "provider_context_overflow",
                        error=result.compaction_error,
                    )
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
                    recovery_source_version = message_version(messages)
                    try:
                        compactor = getattr(context_runtime, "compact_overflow", None)
                        if not callable(compactor):
                            compactor = getattr(context_runtime, "compact", None)
                        using_context_compactor = callable(compactor)
                        if not using_context_compactor:
                            compactor = context.messages.compact_messages
                        compacted = await _to_thread_settled(
                            compactor,
                            copy.deepcopy(messages),
                            **({"overflow": True} if not using_context_compactor else {}),
                            cancel_callback=lambda: (
                                run_context.request_cancel(),
                                context_runtime.cancel_compaction() if callable(getattr(context_runtime, "cancel_compaction", None)) else None,
                            ),
                        )
                        accept_compaction(
                            context_runtime, messages, compacted,
                            snapshot=replace(request_snapshot, retained=()),
                            source_version=recovery_source_version,
                            trigger=result.failure_source or "provider_context_overflow",
                            physical=True, attempt_state=compaction_attempts,
                            before_tokens=actual_request_tokens,
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
                    await services.session_runtime.checkpoint(run_context, "running")
                    context.hooks.trace(
                        "compact",
                        trigger=(
                            "local_request_budget"
                            if result.failure_source == "local_request_budget"
                            else "provider_context_overflow"
                        ),
                        attempts=attempt,
                        degraded_input_expansions=degraded,
                    )
                    context.hooks.on_turn_end(messages, "compact")
                    if result.failure_source == "local_request_budget":
                        # A local preflight did not consume a main-model turn;
                        # retry the same logical slot after bounded compaction.
                        retry_same_turn = True
                    # Both local preflight failures and real Provider overflows
                    # have been settled and compacted.  Do not submit the failed
                    # result to approval/empty-response handling.  A Provider
                    # overflow advances to the next logical slot; only the local
                    # preflight branch above reuses this slot.
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
                run_context.check_cancelled()
                commit_approved_model_result(
                    approved,
                    context=context,
                    assistant_message=assistant_message,
                    processor=processor,
                    message_part=message_part,
                    messages=messages,
                    reconcile=model_result_materialized,
                    source_provider_id=str(run_context.request.provider or ""),
                    source_provider_instance_id=(
                        _provider_instance_id(resolve_model_runtime_context())
                    ),
                    source_model_id=str(run_context.request.model or ""),
                )
                acknowledge_repeat(result)
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
                    has_visible_progress = bool(result.content.strip() or result.tool_calls)
                    has_reasoning = bool(
                        result.reasoning_tokens
                        or str(result.extra.get("reasoning_content") or "").strip()
                    )
                    warning = processor.add_length_warning(
                        has_text=bool(result.content.strip()),
                        has_reasoning=has_reasoning,
                        has_tools=bool(result.tool_calls),
                    )
                    await services.session_runtime.checkpoint(run_context, "running")
                    context.hooks.trace(
                        "model_output_limit",
                        reasoning_only=has_reasoning and not has_visible_progress,
                        continuation_count=output_limit_continuations,
                        continuation_budget=_OUTPUT_LIMIT_CONTINUATION_BUDGET,
                    )
                    visible = f"{result.content}\n\n{warning}" if result.content else warning
                    can_continue = bool(
                        not result.tools_executed_in_stream
                        and has_visible_progress
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
                    # 已执行的副作用不能重放；只有实际用完继续次数才报告额度耗尽。
                    if result.tools_executed_in_stream:
                        stop_code = "output_limit_after_tool_execution"
                        stop_message = (
                            "Output-limit recovery was stopped because streamed tool "
                            "execution had already started; no tool calls were replayed."
                        )
                    elif output_limit_continuations >= _OUTPUT_LIMIT_CONTINUATION_BUDGET:
                        stop_code = "output_limit_recovery_exhausted"
                        stop_message = (
                            "Output-limit recovery was exhausted; the run stopped without "
                            "claiming the truncated response was complete."
                        )
                    elif not has_visible_progress and turn_index + 1 < max_turns:
                        # 没有可续写内容且预算未变，重复请求只会再次消耗同一额度。
                        stop_code = "output_limit_no_visible_progress"
                        stop_message = (
                            "The model exhausted its output limit with no visible text "
                            "or tool calls. The run stopped instead of repeating the "
                            "request under unchanged output settings. Check the model "
                            "capability and output budget configuration before retrying."
                        )
                    else:
                        stop_code = "output_limit_turn_budget_exhausted"
                        stop_message = (
                            "The model hit its output limit on the final allowed turn; "
                            "the run stopped without starting another request."
                        )
                    context.hooks.trace(
                        "model_output_limit_stopped",
                        reason=stop_code,
                        continuation_count=output_limit_continuations,
                        continuation_budget=_OUTPUT_LIMIT_CONTINUATION_BUDGET,
                    )
                    final_output = f"{visible}\n\n{stop_message}"
                    set_assistant_error(
                        assistant_message,
                        TrustedPublicMessage(
                            stop_code,
                            stop_message,
                            retryable=True,
                        ),
                        name="ModelOutputLimitError",
                        data={"message": stop_message, "isRetryable": True},
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

                compaction_attempts.observe_response(result)
                if result.tools_executed_in_stream:
                    record_provider_turn(
                        tool_calls=result.tool_calls,
                        finish_reason=result.finish_reason or "tool-calls",
                        memory_step=True,
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
                            content_text=processor.blocked_reason,
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
                    context.hooks.on_turn_end(messages, "continue")
                    continue

                if not result.tool_calls:
                    record_provider_turn(
                        finish_reason=result.finish_reason or "stop",
                        memory_step=bool(str(result.content or "").strip()),
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
                    run_context.metadata["authored_content"] = result.content or ""
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
                    run_context.check_cancelled()
                    return await services.tools.execute_batch_async(
                        resolve_tool_runtime_context(), result.tool_calls, messages,
                        on_tool, on_text, processor=processor, usage=result,
                        approved_batch=approved_tool_batch,
                    )
                try:
                    step_result = await self._middleware.run(
                        "tool_batch", run_context, execute_batch,
                    )
                    run_context.check_cancelled()
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
                    memory_step=True,
                )
                if step_result == "stop":
                    context.hooks.on_turn_end(messages, "blocked")
                    return await context.lifecycle.finalize(
                        messages,
                        "blocked",
                        on_text,
                        on_token,
                        stream,
                        content_text=processor.blocked_reason,
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
            run_context.request_cancel()
            return await context.lifecycle.finalize(
                messages, "cancelled", on_text, on_token, stream,
            )
        except KeyboardInterrupt:
            return await context.lifecycle.finalize(
                messages, "interrupted", on_text, on_token, stream,
            )

    async def _settle_terminal_boundary(
        self, context, services, run_context, messages, *, boundary, content,
        completed_turns, work_budget, resolve_tool_runtime_context,
        on_tool=None, on_text=None, natural_completion: bool,
    ) -> _TerminalBoundaryDecision:
        if strict_local_tools():
            return await self._settle_strict_terminal_boundary(
                context, services, run_context, messages, boundary=boundary, content=content,
                completed_turns=completed_turns, work_budget=work_budget,
                resolve_tool_runtime_context=resolve_tool_runtime_context,
                on_tool=on_tool, on_text=on_text, natural_completion=natural_completion)
        from nz_coder.runtime.verification.completion_gate import completion_feedback
        from nz_coder.runtime.verification.domain.models import candidate_fingerprint

        state = context.runtime_state
        domain = run_context.verification
        at_hard_cap = completed_turns >= work_budget.max_turns
        # 普通任务先把工具结果交回模型；验收通过本身不是模型的交付决定。
        if not natural_completion:
            if not at_hard_cap:
                return _TerminalBoundaryDecision("continue", reason="model_work_continues")
            return _TerminalBoundaryDecision("finalize", "max_turns",
                self._deterministic_failure_summary(state), "hard_cap_exhausted")
        context.policy.prepare_completion()

        def current_identity():
            snapshot = domain.snapshot()
            users = tuple((item.get(MESSAGE_ID_KEY), repr(item.get("content")))
                          for item in messages if is_user_request_message(item))
            return snapshot.version, snapshot.authority_revision, users

        original_identity = current_identity()
        try:
            execution = await self._execute_due_verification_contract(
                context, services, run_context, messages, "completion",
                resolve_tool_runtime_context, on_tool, on_text)
            run_context.check_cancelled()
            context.policy.prepare_completion()
            if execution.blocked_reason:
                return _TerminalBoundaryDecision("finalize", "blocked", execution.blocked_reason, "permission_stopped")
            if current_identity() != original_identity:
                run_context.metadata["authored_content"] = ""
                context.control.invalidate_completion_payload()
                return (_TerminalBoundaryDecision("finalize", "max_turns", "", "hard_cap_exhausted")
                        if at_hard_cap else _TerminalBoundaryDecision("continue", reason="candidate_invalidated"))

            candidate = content or self._deterministic_terminal_summary(state)
            fingerprint = candidate_fingerprint(candidate, context.control.structured_output())
            domain.set_completion_candidate(fingerprint)
            snapshot = domain.snapshot()
            assessment = snapshot.assess_completion("completed")
            status = await context.policy.settle_completion(messages, candidate,
                review=assessment.review_required, guidance=completion_feedback(snapshot, assessment))
            run_context.check_cancelled()
            context.policy.prepare_completion()
            if status != "blocked" and current_identity() != original_identity:
                run_context.metadata["authored_content"] = ""
                context.control.invalidate_completion_payload()
                return (_TerminalBoundaryDecision("finalize", "max_turns", "", "hard_cap_exhausted")
                        if at_hard_cap else _TerminalBoundaryDecision("continue", reason="candidate_invalidated"))
            snapshot = domain.snapshot()
            assessment = snapshot.assess_completion("completed_unverified" if status == "completed_unverified" else "completed")
            if status == "continue":
                decision = (_TerminalBoundaryDecision("finalize", "max_turns",
                            self._deterministic_failure_summary(state), "hard_cap_exhausted")
                            if at_hard_cap else _TerminalBoundaryDecision("continue", reason="completion_requires_revision"))
            elif status == "blocked" or (status == "completed_unverified" and assessment.status == "blocked"):
                # 必需交付证据缺失时，用户看到具体阻塞原因，避免发布未经证实的完成声明。
                reason = context.control.stop_hook_reason() or self._deterministic_failure_summary(state,
                    unresolved_ids=assessment.blocking_ids)
                decision = _TerminalBoundaryDecision("finalize", "blocked", reason, "completion_stopped")
            else:
                decision = _TerminalBoundaryDecision("finalize", assessment.status, candidate, "current_verification_facts")
            context.control.persist_runtime_state(active=decision.action != "finalize")
            checks = snapshot.acceptance_checks
            context.hooks.trace("terminal_boundary_settled", boundary=boundary,
                decision=decision.action, status=decision.status, reason=decision.reason,
                completed_turns=completed_turns, nominal_turns=work_budget.nominal_turns,
                hard_cap=work_budget.max_turns, contract_executed=execution.attempted,
                contract_required=bool(checks), contract_passed=bool(checks) and all(item["status"] == "passed" for item in checks),
                mutation_generation=snapshot.version.generation,
                unresolved_requirements=[item.requirement.id for item in snapshot.requirements if item.status != "satisfied"],
                early_tool_completion_candidate=False,
                completion_review_required=assessment.review_required,
                candidate_fingerprint=snapshot.candidate_fingerprint)
            await services.session_runtime.checkpoint(run_context, "running")
            return decision
        finally:
            # hook 可在取消前直接改动磁盘；先核对实际工作区再判断候选是否过期。
            context.policy.prepare_completion()
            if current_identity() != original_identity:
                run_context.metadata["authored_content"] = ""
                context.control.invalidate_completion_payload()



    async def _settle_strict_terminal_boundary(
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
        candidate_version = (
            getattr(state, "source_mutation_generation", 0),
            effective_acceptance_generation(state),
        )
        candidate_users = tuple(
            (item.get("id"), repr(item.get("content"))) for item in messages
            if isinstance(item, dict) and item.get("role") == "user"
            and not item.get("_nz_synthetic")
        )
        prompts_before = int(getattr(state, "completion_gate_prompts", 0) or 0)
        verification_status = getattr(context.policy, "verification_status", None)
        ordinary = verification_status() if callable(verification_status) else {}
        candidate_domain_version = (
            ordinary.get("source_mutation_generation"),
            ordinary.get("acceptance_mutation_generation"),
            ordinary.get("mutation_scope_unknown"),
        )
        def candidate_invalidated() -> bool:
            current = verification_status() if callable(verification_status) else {}
            current_version = (
                current.get("source_mutation_generation"),
                current.get("acceptance_mutation_generation"),
                current.get("mutation_scope_unknown"),
            )
            current_users = tuple(
                (item.get("id"), repr(item.get("content"))) for item in messages
                if isinstance(item, dict) and item.get("role") == "user"
                and not item.get("_nz_synthetic")
            )
            return natural_completion and (
                candidate_version != (getattr(state, "source_mutation_generation", 0),
                                      effective_acceptance_generation(state))
                or candidate_domain_version != current_version
                or candidate_users != current_users
            )

        ordinary_pending = bool(ordinary.get("verification_needed"))
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
                try:
                    verification_execution = await self._execute_scheduled_verification(
                        context,
                        services,
                        run_context,
                        messages,
                        action,
                        resolve_tool_runtime_context,
                        on_tool,
                        on_text,
                    )
                finally:
                    if candidate_invalidated():
                        run_context.metadata["authored_content"] = ""
                if verification_execution.blocked_reason:
                    return _TerminalBoundaryDecision("finalize", "blocked", verification_execution.blocked_reason, "permission_stopped")
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

        try:
            contract_execution = await self._execute_due_verification_contract(
                context,
                services,
                run_context,
                messages,
                "completion",
                resolve_tool_runtime_context,
                on_tool,
                on_text,
            )
        finally:
            if candidate_invalidated():
                run_context.metadata["authored_content"] = ""
        if contract_execution.blocked_reason:
            return _TerminalBoundaryDecision("finalize", "blocked", contract_execution.blocked_reason, "permission_stopped")
        if natural_completion and not at_hard_cap:
            try:
                verification_execution = await self._execute_completion_verifications(
                    context, services, run_context, messages,
                    resolve_tool_runtime_context, on_tool, on_text,
                )
            finally:
                if candidate_invalidated():
                    run_context.metadata["authored_content"] = ""
            if verification_execution.blocked_reason:
                return _TerminalBoundaryDecision("finalize", "blocked", verification_execution.blocked_reason, "permission_stopped")
        if candidate_invalidated():
            # 补测改变源码或真实任务后，旧正文不能用于认证当前状态。
            run_context.metadata["authored_content"] = ""
            if at_hard_cap:
                return _TerminalBoundaryDecision("finalize", "max_turns", "", "hard_cap_exhausted")
            return _TerminalBoundaryDecision("continue", reason="candidate_invalidated")
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

        ordinary = verification_status() if callable(verification_status) else {}
        ordinary_pending = bool(ordinary.get("verification_needed"))
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
            not ordinary_pending
            and contract_passed
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
            elif status == "completed_unverified" and semantic_pending_before:
                status = "blocked"
                reason = "required_review_unavailable"
            elif semantic_pending_before and evidence_complete:
                reason = "semantic_review_and_ledger_satisfied"
            elif not evidence_complete:
                reason = "semantic_review_evidence_unavailable"
            if (
                status == "blocked"
                and unresolved_ids
                and int(getattr(state, "completion_gate_prompts", 0) or 0)
                >= COMPLETION_GATE_REANIMATE_BUDGET
            ):
                # 最后一步已达硬上限时，不能把真实回合耗尽降格为补救耗尽。
                if at_hard_cap:
                    status = "max_turns"
                    reason = "hard_cap_exhausted"
                else:
                    reason = "requirement_completion_budget_exhausted"
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
            failure_summary = self._deterministic_failure_summary(
                state, unresolved_ids=unresolved_ids,
            )
            if reason == "requirement_completion_budget_exhausted":
                failure_summary = "Completion guidance budget exhausted. " + failure_summary
            decision = _TerminalBoundaryDecision(
                action="finalize",
                status=status,
                content=(
                    failure_summary
                    if status in {"max_turns", "blocked"}
                    else content or failure_summary
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

        ordinary_guidance = ""
        if natural_completion and ordinary_pending:
            from nz_coder.runtime.verification.completion_gate import ordinary_verification_guidance
            ordinary_guidance = ordinary_verification_guidance(ordinary, run_context.request.workspace)
        # 普通验证未结清时无法启动语义评审，先指导复测，避免要求模型空等评审。
        ordinary_guidance_due = bool(
            ordinary_guidance
            and int(getattr(state, "completion_gate_prompts", 0) or 0)
            < COMPLETION_GATE_REANIMATE_BUDGET
        )
        # 钩子已批准的修订由其自身额度约束，普通指导不能再次覆盖为停止。
        if (
            natural_completion
            and unresolved_ids
            and not ordinary_guidance_due
            and status not in {"continue", "cancelled", "interrupted", "error", "max_turns", "blocked", "stopped_by_hook"}
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
                # 补偿提示预算耗尽即结清，不能继续消耗全局模型轮数。
                decision = _TerminalBoundaryDecision(
                    action="finalize",
                    status="blocked",
                    content=(
                        "Completion guidance budget exhausted. "
                        + self._deterministic_failure_summary(
                            state, unresolved_ids=unresolved_ids,
                        )
                    ),
                    reason="requirement_completion_budget_exhausted",
                )

        # 普通验证和台账由同一边界扣数；已追加台账指导时不再追加第二条。
        ordinary = verification_status() if callable(verification_status) else {}
        ordinary_pending = bool(ordinary.get("verification_needed"))
        if (ordinary_pending and decision.status != "stopped_by_hook"
                and (natural_completion or decision.status in {"completed", "completed_unverified"})):
            if at_hard_cap:
                decision = _TerminalBoundaryDecision(
                    action="finalize", status="max_turns",
                    content=self._deterministic_failure_summary(state, unresolved_ids=unresolved_ids),
                    reason="hard_cap_exhausted",
                )
            elif decision.status not in {"cancelled", "interrupted", "error", "blocked"}:
                from nz_coder.runtime.verification.completion_gate import ordinary_verification_guidance
                guidance = ordinary_guidance or ordinary_verification_guidance(ordinary, run_context.request.workspace)
                count = int(getattr(state, "completion_gate_prompts", 0) or 0)
                if count > prompts_before:
                    decision = _TerminalBoundaryDecision(action="continue", reason="shared_completion_guidance")
                elif guidance and count < COMPLETION_GATE_REANIMATE_BUDGET:
                    state.completion_gate_prompts = count + 1
                    messages.append(stamp_user_message({
                        "role": "user", "content": guidance, "_nz_synthetic": True,
                        "_nz_completion_gate": True,
                    }))
                    decision = _TerminalBoundaryDecision(action="continue", reason="ordinary_verification_pending")
                elif not unresolved_ids:
                    decision = _TerminalBoundaryDecision(
                        action="finalize", status="completed_unverified", content=content,
                        reason="ordinary_verification_unconfirmed",
                    )
        if decision.action == "finalize" and decision.status in {"completed", "completed_unverified"}:
            # 是否推迟语义审查不能决定用户停止规则是否执行；仅当前候选结算一次。
            rule_status = await context.policy.check_terminal_rules(messages, decision.status, decision.content)
            if rule_status == "continue" and not at_hard_cap:
                decision = _TerminalBoundaryDecision(action="continue", reason="terminal_rules_requires_more_work")
            elif rule_status == "completed_unverified" and decision.status == "completed":
                decision = _TerminalBoundaryDecision(
                    action="finalize", status="completed_unverified",
                    content=decision.content, reason="terminal_rules_unconfirmed",
                )
            elif rule_status not in {"completed", "completed_unverified"}:
                decision = _TerminalBoundaryDecision(
                    action="finalize", status="max_turns" if rule_status == "continue" else rule_status,
                    content=decision.content, reason="terminal_rules_stopped",
                )
        if decision.status == "stopped_by_hook":
            decision = replace(decision, status="blocked", content=context.control.stop_hook_reason() or decision.content)
        elif decision.status == "blocked" and context.control.stop_hook_reason():
            decision = replace(decision, content=context.control.stop_hook_reason())
        if decision.action == "finalize" and run_context.verification is not None:
            projected = run_context.verification.snapshot().assess_completion(decision.status).status
            if projected != decision.status:
                decision = replace(decision, status=projected,
                    reason="current_verification_facts", content=(
                        self._deterministic_failure_summary(state, unresolved_ids=unresolved_ids)
                        if projected == "blocked" else decision.content))
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
            contract_executed=contract_execution.attempted,
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
        from nz_coder.runtime.verification.verification_contract import declared_acceptance
        checks = declared_acceptance(state)
        summary = f"Workspace changes: {changed}."
        for check in checks:
            summary += f" Declared acceptance {check['status']}: \x60{check['command']}\x60."
        return summary

    @staticmethod
    def _deterministic_failure_summary(
        state,
        *,
        unresolved_ids: tuple[str, ...] = (),
    ) -> str:
        """描述未满足项；具体停止原因由唯一终态给出，不能把受阻说成轮数到限。"""
        paths = [str(path) for path in getattr(state, "changed_files", []) if str(path)]
        changed = ", ".join(paths[:8]) or "none recorded"
        from nz_coder.runtime.verification.verification_contract import declared_acceptance
        summary = "Stopped without claiming completion."
        summary += f" Changed files: {changed}."
        for check in declared_acceptance(state):
            summary += f" Exact acceptance {check['status']}: \x60{check['command']}\x60."
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

    async def _execute_completion_verifications(
        self, context, services, run_context, messages,
        resolve_tool_runtime_context, on_tool=None, on_text=None,
    ) -> _VerificationExecution:
        from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

        state = context.runtime_state
        generation = effective_acceptance_generation(state)
        prefix = f"completion:{run_context.interaction_run_id}:{generation}:"
        scheduled = state.scheduled_verification_generations
        if prefix + "stopped" in scheduled:
            return _VerificationExecution()
        attempted = sum(key.startswith(prefix) for key in scheduled)
        executed = False
        while not strict_local_tools() or attempted < 6:
            run_context.check_cancelled()
            status = context.policy.verification_status()
            actions = VerificationScheduler().completion_actions(
                status, mutation_generation=int(state.mutation_generation),
            )
            selected = None
            for action in actions:
                _prefix, key = self._verification_attempt_key(state, run_context, action)
                if key not in scheduled:
                    selected = (action, key)
                    break
            if selected is None:
                return _VerificationExecution(executed)
            action, key = selected
            attempted += 1
            outcome = await self._execute_scheduled_verification(
                context, services, run_context, messages, action,
                resolve_tool_runtime_context, on_tool, on_text,
            )
            executed = executed or outcome.attempted
            if outcome.blocked_reason:
                return outcome
            if (prefix + "stopped" in scheduled or not outcome.attempted
                    or not strict_local_tools() and effective_acceptance_generation(state) != generation):
                return _VerificationExecution(executed)

        return _VerificationExecution()

    @staticmethod
    def _verification_attempt_key(state, run_context, action) -> tuple[str, str]:
        from nz_coder.runtime.verification.domain.identity import command_identity
        generation = effective_acceptance_generation(state)
        prefix = f"completion:{run_context.interaction_run_id}:{generation}:"
        identity = command_identity(action.command, action.cwd)
        fingerprint = hashlib.sha256(json.dumps(
            [identity.argv, identity.cwd, identity.environment, identity.opaque], ensure_ascii=False,
        ).encode()).hexdigest()[:16]
        return prefix, prefix + fingerprint

    @staticmethod
    def _scheduled_verification_passed(status, action) -> bool:
        from nz_coder.runtime.verification.domain.identity import command_identity

        identity = command_identity(action.command, action.cwd)
        matching = [item
            for stage in (status.get("verification_pipeline") or {}).get("stages", [])
            for item in stage.get("commands", [])
            if (item.get("required") or item.get("id") == action.check_id)
            and command_identity(item.get("retry_command") or item.get("command") or "", item["cwd"]) == identity]
        return bool(matching) and all(item.get("status") in {"passed", "covered"} for item in matching)

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
    ) -> _VerificationExecution:
        """Run one cheap/targeted scheduler command through the normal Bash path."""
        domain = run_context.verification
        snapshot = domain.snapshot() if domain is not None else None
        spec = next((s for s in snapshot.specs if s.id == action.check_id), None) if snapshot else None
        if spec is None or not (spec.required or snapshot.targeted_pending and spec.capability == "test"):
            return _VerificationExecution()
        if snapshot.decision(spec.id).status in {"passed", "covered"} or not snapshot.retry_eligible(spec):
            return _VerificationExecution()
        command, cwd, stage = spec.command.original, spec.command.cwd, spec.stage
        if command != action.command or cwd != action.cwd:
            return _VerificationExecution()
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
        prefix, key = self._verification_attempt_key(state, run_context, action)
        if key in scheduled or prefix + "stopped" in scheduled or (strict_local_tools() and sum(k.startswith(prefix) for k in scheduled) >= 6):
            return _VerificationExecution()
        acceptance_generation = effective_acceptance_generation(state)
        # 所有原生调度入口共用记录；旧预算调度不能绕过收尾的次数和停止边界。
        scheduled[key] = acceptance_generation
        scheduled[stage] = generation
        context.control.persist_runtime_state(active=True)
        fingerprint = hashlib.sha256(json.dumps(
            [run_context.interaction_run_id, command, cwd], ensure_ascii=False,
        ).encode()).hexdigest()[:12]
        call_id = f"verification-stage-{stage}-{generation}-{fingerprint}"
        tool_call = {
            "id": call_id,
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({
                    "command": command,
                    "workdir": cwd,
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
        current = domain.snapshot()
        current_spec = next((s for s in current.specs if s.id == action.check_id), None)
        if current_spec != spec or current.version != snapshot.version:
            return _VerificationExecution()
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
            run_context.check_cancelled()
            return await services.tools.execute_batch_async(
                resolve_tool_runtime_context(),
                [tool_call],
                messages,
                on_tool,
                on_text,
                processor=processor,
                approved_batch=approved_batch,
            )

        from nz_coder.runtime.adapters.verification_runtime import scoped_verification_invocation
        with scoped_verification_invocation("runtime", "scheduled"):
            batch_outcome = await self._middleware.run("tool_batch", run_context, execute_batch)
            run_context.check_cancelled()
        tool_message = next(
            (
                message
                for message in messages[before:]
                if isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_call_id") == call_id
            ),
            {},
        )
        output = str(tool_message.get("content") or "")
        # 需求证据由普通工具观察链统一写入；展示文字不能重判进程成功。
        status_provider = getattr(context.policy, "verification_status", None)
        verification_status = status_provider() if callable(status_provider) else {}
        passed = self._scheduled_verification_passed(verification_status, action)
        if strict_local_tools() and (not passed or effective_acceptance_generation(state) != acceptance_generation):
            scheduled[prefix + "stopped"] = acceptance_generation
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
        return _VerificationExecution(True, processor.blocked_reason if batch_outcome == "stop" else "")

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
    ) -> _VerificationExecution:
        """Run one user-declared acceptance command through the normal tool path."""
        if not strict_local_tools():
            if zone != "completion":
                return _VerificationExecution()
            return await self._execute_completion_verifications(
                context, services, run_context, messages, resolve_tool_runtime_context, on_tool, on_text)
        state = context.runtime_state
        stored = getattr(state, "verification_contract", None)
        if not isinstance(stored, dict) or not stored:
            return _VerificationExecution()
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
            return _VerificationExecution()
        # 完整 discover 由原命令确定范围，空文件选择器不代表合同无效。
        if not due or not contract.command:
            return _VerificationExecution()
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
            return _VerificationExecution()

        # 明确验收与阶段调度走同一执行/审批/去重路径，不能额外获得一次重跑。
        from nz_coder.runtime.verification.verification_scheduler import VerificationAction
        from nz_coder.runtime.verification.domain.identity import command_identity
        domain = run_context.verification
        if domain is None:
            return _VerificationExecution()
        identity = command_identity(contract.command, str(run_context.request.workspace))
        spec = next((item for item in domain.snapshot().specs
                     if item.required and item.source == "user_declared" and item.command == identity), None)
        if spec is None:
            return _VerificationExecution()
        action = VerificationAction(kind="stage", stage=spec.stage, command=spec.command.original,
            cwd=spec.command.cwd, check_id=spec.id, mutation_generation=state.mutation_generation,
            reason="execute existing user acceptance")
        result = await self._execute_scheduled_verification(
            context, services, run_context, messages, action, resolve_tool_runtime_context, on_tool, on_text)
        if result.attempted:
            context.hooks.trace("verification_contract_executed", zone=zone, mutation_generation=generation,
                workspace_mutation_generation=state.mutation_generation,
                passed=state.verification_contract.get("passed") is True,
                command=contract.command, targets=list(contract.targets))
        return result


def _result_status(result: object) -> RunStatus:
    value = result.get("status") if isinstance(result, dict) else None
    return normalize_run_status(value)


def _provider_instance_id(context) -> str:
    """Read an optional private-state identity from the focused model context."""
    resolve = getattr(context, "provider_instance_id", None)
    return str(resolve() or "") if callable(resolve) else ""


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
    from nz_coder.runtime.core.result import TERMINAL_RESULT_KEY, TokenUsage
    record = context.session.metadata[TERMINAL_RESULT_KEY]
    result = record["last_result"]
    return RunResult(
        status=RunStatus(result["status"]), final_text=result["answer"],
        messages=context.transcript, usage=TokenUsage(**result["usage"]),
        session_id=request.session_id, active_agent=result["active_agent"],
        error=result["error"], metadata=result["metadata"],
    )
