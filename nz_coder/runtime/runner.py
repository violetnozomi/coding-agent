"""Shared provider/tool state machine for every NZ-Coder Agent profile."""
from __future__ import annotations

import asyncio
import copy
import threading
import time

from nz_coder.message_schema import (
    ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY,
    MESSAGE_ID_KEY,
    attach_message_identity,
    ensure_message_identities,
    is_synthetic_user_message,
    set_assistant_error,
)
from nz_coder.recovery import is_context_overflow_error
from nz_coder.runtime.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.context_manager import (
    CompactionAttemptState as _CompactionAttemptState,
    CompactionAttemptsExhausted as _CompactionAttemptsExhausted,
)
from nz_coder.runtime.session_processor import SessionProcessor
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.request import RunOptions, RunRequest
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.core.middleware import MiddlewarePipeline
from nz_coder.runtime.core.events import RuntimeEventMiddleware
from nz_coder.state.input_expansion import (
    compact_stored as compact_stored_input_expansions,
)

_MAX_STEPS_PROMPT = """CRITICAL - MAXIMUM STEPS REACHED

The maximum number of steps allowed for this task has been reached. Tools are disabled until next user input. Respond with text only.

STRICT REQUIREMENTS:
1. Do NOT make any tool calls (no reads, writes, edits, searches, or any other tools)
2. MUST provide a text response summarizing work done so far
3. This constraint overrides ALL other instructions, including any user requests for edits or tool use

Response must include:
- Statement that maximum steps for this agent have been reached
- Summary of what has been accomplished so far
- List of any remaining tasks that were not completed
- Recommendations for what should be done next

Any attempt to use tools is a critical violation. Respond with text ONLY."""


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
        if isinstance(services, RuntimeServices):
            declared = (
                RuntimeEventMiddleware(services.events),
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
        services = self._services
        if not isinstance(services, RuntimeServices):
            raise TypeError("Native AgentRunner requires a RuntimeServices graph")
        factory = self._execution_context_factory
        if not callable(factory):
            raise TypeError("Native AgentRunner requires an execution context factory")
        run_context = await services.session_runtime.open(request)
        run_context.cancellation = options.cancellation
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
                execution_context, services, run_context, run_context.transcript,
                options.on_tool, options.on_text, options.on_token, run_stream,
            )

        try:
            result = await self._middleware.run("run", run_context, execute_run)
        except asyncio.CancelledError:
            await services.session_runtime.finalize(run_context, RunStatus.CANCELLED)
            raise
        except Exception:
            await services.session_runtime.finalize(run_context, RunStatus.ERROR)
            raise
        await services.session_runtime.finalize(run_context, _result_status(result))
        return result, run_context

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
                from nz_coder.runtime.host import ProductionRuntimeHost
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
        # The legacy lifecycle already owns the mature SessionEventBus facts.
        # Suppress only the additive core projection to avoid duplicate UI events.
        run_context.metadata["suppress_runtime_events"] = True
        host.active_run_context = run_context
        execute = None
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
                from nz_coder.runtime.host import ProductionRuntimeHost
                runtime_host = ProductionRuntimeHost()
        else:
            async def execute(_host, run_messages, tool_cb, text_cb, token_cb, run_stream):
                return await self._run_turns(
                    runner_context_from_legacy_host(
                        _host, services, run_context,
                    ),
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
        await context.policy.run_input_guardrails(messages)
        ensure_message_identities(messages, context.session_id)
        await services.session_runtime.checkpoint(run_context, "running")
        compaction_attempts = _CompactionAttemptState()
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
                context.hooks.on_pre_send(messages)
                context.messages.bind_user_contexts(messages)
                message_part = context.messages.new_message_part(turn_index + 1)
                assistant_message = {"role": "assistant", "content": ""}
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
                    nonlocal model_result_materialized
                    await resolve_start_snapshot()
                    context.messages.materialize_llm_result(
                        stream_result,
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
                    async def execute_model():
                        return await services.model.complete_turn(
                            resolve_model_runtime_context(), api_messages,
                            stream=stream,
                            on_token=(None if context.policy.has_output_guardrail() else on_token),
                            message_part=message_part,
                            stream_tool_handler=execute_stream_tools,
                        )
                    result = await self._middleware.run(
                        "model", run_context, execute_model,
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
                    context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                    set_assistant_error(
                        assistant_message,
                        "Request interrupted by user",
                        name="MessageAbortedError",
                        publish=context.messages.publish_event,
                    )
                    processor.interrupt_unsettled()
                    processor.finish_step("cancelled")
                    await services.session_runtime.checkpoint(run_context, "cancelled")
                    raise
                except KeyboardInterrupt:
                    context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                    set_assistant_error(
                        assistant_message,
                        "Request interrupted by user",
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
                finally:
                    context.messages.bind_active_processor(None, None)

                try:
                    start_snapshot = await resolve_start_snapshot()
                except asyncio.CancelledError:
                    context.snapshots.retire(start_snapshot_task, start_snapshot_cancel)
                    set_assistant_error(
                        assistant_message,
                        "Request interrupted by user",
                        name="MessageAbortedError",
                        publish=context.messages.publish_event,
                    )
                    processor.interrupt_unsettled()
                    processor.finish_step("cancelled")
                    await services.session_runtime.checkpoint(run_context, "cancelled")
                    raise
                if result.post_tool_stream_error:
                    if not model_result_materialized:
                        context.messages.materialize_llm_result(
                            result,
                            assistant_message=assistant_message,
                            processor=processor,
                            message_part=message_part,
                            messages=messages,
                        )
                        model_result_materialized = True
                    else:
                        context.messages.reconcile_llm_result(
                            result,
                            assistant_message=assistant_message,
                            processor=processor,
                            message_part=message_part,
                            messages=messages,
                        )
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
                    structured = result.assistant_error or {
                        "name": "APIError",
                        "data": {
                            "message": "Provider request failed after retries",
                            "isRetryable": False,
                        },
                    }
                    set_assistant_error(
                        assistant_message,
                        "Provider request failed after retries",
                        name=str(structured.get("name") or "UnknownError"),
                        data=(
                            structured.get("data")
                            if isinstance(structured.get("data"), dict)
                            else None
                        ),
                        publish=context.messages.publish_event,
                    )
                    processor.fail_unsettled("Provider request failed after retries")
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
                    error = result.compaction_error or "Input exceeds context window of this model"
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

                if (
                    not result.tool_calls
                    and not context.control.has_agent_call_stack()
                    and result.finish_reason not in {"error", "length"}
                ):
                    result.content = await context.policy.run_output_guardrail(
                        result.content or "", messages,
                    )
                if not model_result_materialized:
                    context.messages.materialize_llm_result(
                        result,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        messages=messages,
                    )
                    model_result_materialized = True
                else:
                    context.messages.reconcile_llm_result(
                        result,
                        assistant_message=assistant_message,
                        processor=processor,
                        message_part=message_part,
                        messages=messages,
                    )
                context.messages.observe_llm_result(
                    result,
                    message_part=message_part,
                    turn=turn_index + 1,
                )

                if result.finish_reason in {"error", "length"}:
                    if result.tool_calls and not result.tools_executed_in_stream:
                        processor.fail_unsettled(
                            "Tool call was not executed because the Provider response "
                            f"finished with {result.finish_reason!r}"
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
                            provider_error,
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
                    )
                    context.hooks.on_turn_end(messages, "completed")
                    visible = f"{result.content}\n\n{warning}" if result.content else warning
                    return await context.lifecycle.finalize(
                        messages,
                        "completed",
                        on_text,
                        on_token,
                        stream,
                        content_text=visible,
                    )

                if result.tools_executed_in_stream:
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
                        context.hooks.on_turn_end(messages, "completed")
                        return await context.lifecycle.finalize(
                            messages,
                            "completed",
                            on_text,
                            on_token,
                            stream,
                            content_text=final_content,
                        )
                    context.control.persist_runtime_state(active=True)
                    await context.planning.replan()
                    context.hooks.on_turn_end(messages, "continue")
                    continue

                if not result.tool_calls:
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
                    gate_status = await context.policy.verify_completion(
                        messages, "completed", result.content or "",
                    )
                    if gate_status == "continue":
                        context.hooks.on_turn_end(messages, "continue")
                        continue
                    context.hooks.on_turn_end(messages, gate_status)
                    final_content = result.content
                    if gate_status == "stopped_by_hook":
                        final_content = context.control.stop_hook_reason() or result.content
                    return await context.lifecycle.finalize(
                        messages,
                        gate_status,
                        on_text,
                        on_token,
                        stream,
                        content_text=final_content,
                    )

                async def execute_batch():
                    return await services.tools.execute_batch_async(
                        resolve_tool_runtime_context(), result.tool_calls, messages,
                        on_tool, on_text, processor=processor, usage=result,
                    )
                step_result = await self._middleware.run(
                    "tool_batch", run_context, execute_batch,
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
                    context.hooks.on_turn_end(messages, "completed")
                    return await context.lifecycle.finalize(
                        messages,
                        "completed",
                        on_text,
                        on_token,
                        stream,
                        content_text=final_content,
                    )
                context.control.persist_runtime_state(active=True)
                await context.planning.replan()
                context.hooks.on_turn_end(messages, "continue")

            return await context.lifecycle.finalize(
                messages, "max_turns", on_text, on_token, stream,
                max_turns=max_turns,
            )
        except KeyboardInterrupt:
            return await context.lifecycle.finalize(
                messages, "interrupted", on_text, on_token, stream,
            )


def _result_status(result: object) -> RunStatus:
    value = result.get("status") if isinstance(result, dict) else None
    normalized = str(value or "error")
    aliases = {
        "completed_unverified": RunStatus.COMPLETED,
        "aborted": RunStatus.INTERRUPTED,
        "blocked": RunStatus.ERROR,
    }
    if normalized in aliases:
        return aliases[normalized]
    try:
        return RunStatus(normalized)
    except ValueError:
        return RunStatus.ERROR


def _typed_result(
    request: RunRequest, context: RunContext, raw_result: object,
) -> RunResult:
    payload = raw_result if isinstance(raw_result, dict) else {}
    final_text = str(payload.get("content") or "")
    if not final_text:
        for message in reversed(context.transcript):
            if isinstance(message, dict) and message.get("role") == "assistant":
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
