"""Projection of normalized Provider stream events into one coding turn."""
from __future__ import annotations

import time

from nz_coder import config
from nz_coder.message_schema import assistant_error_from_exception
from nz_coder.runtime.model_gateway import ModelCall, ModelCallPurpose, ModelCallStatus
from nz_coder.runtime.model_result import LLMResult
from nz_coder.runtime.services import (
    StreamToolExecutionCancelled,
    StreamToolExecutionFailed,
)
from nz_coder.runtime.session_processor import SessionProcessor


def project_streaming_turn(
    host,
    api_messages: list,
    on_token=None,
    message_part: dict | None = None,
    stream_tool_handler=None,
) -> LLMResult:
    """Run a Gateway stream and settle its Session/tool projection."""
    streamed_text: list[str] = []
    streamed_reasoning: list[str] = []
    streamed_tools: dict[int, dict] = {}
    tools_executed = False
    tool_outcome = ""
    tool_error: BaseException | None = None
    tool_wait_ms = 0.0

    class RetiredEvent:
        def is_set(self) -> bool:
            return host._message_part_is_retired(message_part)

    def on_event(event):
        nonlocal tools_executed, tool_outcome, tool_error, tool_wait_ms
        processor = getattr(host, "_active_session_processor", None)
        active_messages = getattr(host, "_active_processor_messages", None)
        if event.kind == "text":
            delta = str(event.data.get("delta") or "")
            streamed_text.append(delta)
            if message_part is not None:
                host._emit_message_delta(message_part, delta)
            if isinstance(processor, SessionProcessor) and message_part is not None:
                processor.stream_text(
                    "".join(streamed_text),
                    part_id=message_part["part_id"],
                )
        elif event.kind == "reasoning":
            delta = str(event.data.get("delta") or "")
            streamed_reasoning.append(delta)
            if isinstance(processor, SessionProcessor):
                processor.add_reasoning("".join(streamed_reasoning))
        elif event.kind == "tool_delta":
            index = int(event.data.get("index") or 0)
            streamed_tools[index] = _streamed_tool(event.data)
            if isinstance(processor, SessionProcessor):
                processor.stream_tool_delta(
                    index,
                    call_id=str(event.data.get("call_id") or ""),
                    name=str(event.data.get("name") or ""),
                    arguments=str(event.data.get("arguments") or ""),
                    metadata=(event.data.get("metadata") or None),
                )
        elif (
            event.kind == "finish"
            and stream_tool_handler is not None
            and streamed_tools
            and not tools_executed
        ):
            partial = LLMResult(
                content="".join(streamed_text),
                tool_calls=[streamed_tools[index] for index in sorted(streamed_tools)],
                extra=(
                    {"reasoning_content": "".join(streamed_reasoning)}
                    if streamed_reasoning else {}
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
        if isinstance(active_messages, list) and event.kind in {
            "text", "reasoning", "tool_delta",
        }:
            host._checkpoint_messages(active_messages, "running")
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
            base_observer(name, payload)
        if name != "model_call_retry" or not payload.get("streaming"):
            return
        processor = getattr(host, "_active_session_processor", None)
        if isinstance(processor, SessionProcessor):
            processor.fail_unsettled(str(payload.get("error") or "stream retry"))
        if message_part is not None:
            host._discard_message_part(message_part, "stream_retry")
        streamed_text.clear()
        streamed_reasoning.clear()
        streamed_tools.clear()

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
        raise

    result = host._gateway_outcome_result(outcome)
    if outcome.status is ModelCallStatus.COMPLETED:
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
    return host._active_tool_specs() if capabilities is None or capabilities.supports_tools else []


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
