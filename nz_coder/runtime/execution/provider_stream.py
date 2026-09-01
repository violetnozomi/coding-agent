"""Projection of normalized Provider stream events into one coding turn."""
from __future__ import annotations

import time

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import assistant_error_from_exception
from nz_coder.runtime.model_gateway import ModelCall, ModelCallPurpose, ModelCallStatus
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.execution.services import (
    StreamToolExecutionCancelled,
    StreamToolExecutionFailed,
)
from nz_coder.runtime.execution.stream_state import (
    StreamAttemptBuffer,
    StreamCheckpointScheduler,
)
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.tool_platform.exposure import expose_specs


def project_streaming_turn(
    host,
    api_messages: list,
    on_token=None,
    message_part: dict | None = None,
    stream_tool_handler=None,
) -> LLMResult:
    """Run a Gateway stream and settle its Session/tool projection."""
    tools_executed = False
    tool_outcome = ""
    tool_error: BaseException | None = None
    tool_wait_ms = 0.0
    processor = getattr(host, "_active_session_processor", None)
    attempt = StreamAttemptBuffer(
        host,
        message_part,
        processor=(processor if isinstance(processor, SessionProcessor) else None),
        publish=bool(
            message_part is None or message_part.get("public_streaming", True)
        ),
        delta_interval_seconds=getattr(
            config,
            "STREAM_DELTA_INTERVAL_SECONDS",
            0.05,
        ),
        delta_min_chars=getattr(config, "STREAM_DELTA_MIN_CHARS", 256),
    )
    active_messages = getattr(host, "_active_processor_messages", None)
    checkpoints = StreamCheckpointScheduler(
        host,
        active_messages,
        enabled=attempt.publish,
        interval_seconds=getattr(
            config,
            "STREAM_CHECKPOINT_INTERVAL_SECONDS",
            0.5,
        ),
        min_chars=getattr(config, "STREAM_CHECKPOINT_MIN_CHARS", 4096),
    )

    class RetiredEvent:
        def is_set(self) -> bool:
            return host._message_part_is_retired(message_part)

    def on_event(event):
        nonlocal tools_executed, tool_outcome, tool_error, tool_wait_ms
        mutation_chars = 0
        if event.kind == "text":
            delta = str(event.data.get("delta") or "")
            if not attempt.append_text(delta):
                return None
            mutation_chars = attempt.flush_text()
        elif event.kind == "reasoning":
            delta = str(event.data.get("delta") or "")
            if not attempt.append_reasoning(delta):
                return None
            mutation_chars = attempt.flush_reasoning()
        elif event.kind == "tool_delta":
            index = int(event.data.get("index") or 0)
            if not attempt.update_tool(index, _streamed_tool(event.data)):
                return None
            mutation_chars = len(str(event.data.get("arguments") or ""))
        elif (
            event.kind == "finish"
            and stream_tool_handler is not None
            and attempt.tools
            and not tools_executed
        ):
            flushed = (
                attempt.flush_text(force=True)
                + attempt.flush_reasoning(force=True)
            )
            if flushed:
                checkpoints.note(flushed)
                checkpoints.flush(force=True)
            partial = LLMResult(
                content=attempt.content,
                tool_calls=[attempt.tools[index] for index in sorted(attempt.tools)],
                extra=(
                    {"reasoning_content": attempt.reasoning_content}
                    if attempt.reasoning else {}
                ),
                finish_reason=str(event.data.get("reason") or "tool-calls"),
            )
            started = time.perf_counter()
            try:
                tool_outcome = stream_tool_handler(partial)
                tools_executed = True
            except BaseException as exc:
                tool_error = exc
                tools_executed = True
            finally:
                tool_wait_ms += (time.perf_counter() - started) * 1000
            return True
        if attempt.publish and mutation_chars and event.kind in {
            "text", "reasoning", "tool_delta",
        }:
            checkpoints.note(mutation_chars)
        return None

    call = ModelCall(
        purpose=ModelCallPurpose.CODING,
        messages=api_messages,
        tools=_active_tools(host),
        max_output_tokens=host._prompt_budget().output_reserve_tokens,
        streaming=True,
        timeout_seconds=config.PROVIDER_HARD_TIMEOUT_SECONDS,
        capability_options=dict(
            getattr(host, "model_capability_options", {}) or {}
        ),
    )
    gateway = host._gateway(
        max_retries=getattr(config, "PROVIDER_MAX_RETRIES", 3)
    )
    base_observer = gateway.observer

    def stream_observer(name: str, payload: dict) -> None:
        if base_observer is not None:
            try:
                base_observer(name, payload)
            except Exception:
                # Retry projection is product state, while the chained
                # observer is best-effort telemetry.  A broken trace sink must
                # not leave partial text/tool parts attached to the new attempt.
                pass
        if name != "model_call_retry" or not payload.get("streaming"):
            return
        active_processor = getattr(host, "_active_session_processor", None)
        if isinstance(active_processor, SessionProcessor):
            active_processor.fail_unsettled(str(payload.get("error") or "stream retry"))
        attempt.reset_after_retry("stream_retry")
        checkpoints.flush(force=True)

    gateway.observer = stream_observer
    try:
        outcome = gateway.complete_stream_sync(
            call,
            on_event=on_event,
            cancel_event=RetiredEvent() if message_part is not None else None,
            idle_timeout_seconds=getattr(
                config, "PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS", 60.0
            ),
        )
    except KeyboardInterrupt:
        if on_token:
            on_token(None)
        if message_part is not None:
            host._discard_message_part(message_part, "interrupted")
        processor = getattr(host, "_active_session_processor", None)
        if isinstance(processor, SessionProcessor):
            processor.interrupt_unsettled()
        checkpoints.flush(force=True)
        raise

    result = host._gateway_outcome_result(outcome)
    if outcome.status is ModelCallStatus.COMPLETED:
        flushed = (
            attempt.flush_text(force=True)
            + attempt.flush_reasoning(force=True)
        )
        if flushed:
            checkpoints.note(flushed)
        checkpoints.flush()
        host.recovery.record_success()
        return _settle_stream_tools(
            host,
            result,
            outcome,
            stream_tool_handler,
            tools_executed,
            tool_outcome,
            tool_error,
            tool_wait_ms,
        )
    processor = getattr(host, "_active_session_processor", None)
    if isinstance(processor, SessionProcessor):
        processor.fail_unsettled(outcome.error or outcome.status.value)
    if message_part is not None:
        reason = {
            ModelCallStatus.CONTEXT_OVERFLOW: "context_overflow",
            ModelCallStatus.CLIENT_ERROR: "stream_error",
            ModelCallStatus.CANCELLED: "cancelled",
        }.get(outcome.status, "stream_error")
        host._discard_message_part(message_part, reason)
    checkpoints.flush(force=True)
    return result


def _streamed_tool(data: dict) -> dict:
    return {
        "id": str(data.get("call_id") or ""),
        "type": "function",
        "function": {
            "name": str(data.get("name") or ""),
            "arguments": str(data.get("arguments") or ""),
        },
        **(
            {"provider_extra": dict(data["metadata"])}
            if isinstance(data.get("metadata"), dict) and data.get("metadata")
            else {}
        ),
    }


def _active_tools(host) -> list:
    capabilities = getattr(host, "model_capabilities", None)
    return (
        expose_specs(host._active_tool_specs())
        if capabilities is None or capabilities.supports_tools
        else []
    )


def _settle_stream_tools(
    host,
    result: LLMResult,
    outcome,
    handler,
    executed: bool,
    tool_outcome: str,
    tool_error: BaseException | None,
    wait_ms: float,
) -> LLMResult:
    if executed:
        result.tools_executed_in_stream = True
        result.tool_outcome = tool_outcome
        result.stream_tool_wait_ms = round(wait_ms, 3)
        result.duration_ms = round(max(0.0, result.duration_ms - wait_ms), 3)
        stream_error = outcome.provider_metadata.get("stream_error")
        if isinstance(stream_error, dict):
            result.post_tool_stream_error = str(
                stream_error.get("message") or "provider stream failed"
            )
            result.assistant_error = assistant_error_from_exception(
                RuntimeError(result.post_tool_stream_error),
                provider_id=host.provider_id,
                is_retryable=False,
            )
        if tool_error is not None:
            if isinstance(tool_error, StreamToolExecutionCancelled):
                raise tool_error
            result.post_tool_stream_error = str(tool_error)
            result.assistant_error = assistant_error_from_exception(tool_error)
        return result
    if handler is None or not result.tool_calls:
        return result
    started = time.perf_counter()
    try:
        result.tool_outcome = handler(result)
    except StreamToolExecutionCancelled:
        raise
    except StreamToolExecutionFailed as exc:
        result.post_tool_stream_error = str(exc)
        result.assistant_error = assistant_error_from_exception(exc)
    finally:
        result.stream_tool_wait_ms = round(
            (time.perf_counter() - started) * 1000,
            3,
        )
    result.tools_executed_in_stream = not bool(result.post_tool_stream_error)
    return result
