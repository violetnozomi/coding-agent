"""Production model-call policy shared by every Agent Core consumer."""
from __future__ import annotations

import asyncio
import queue
import threading
import time
from dataclasses import replace
from typing import Callable

from nz_coder.runtime.agent_resilience import ProviderAttemptController
from nz_coder.runtime.model_gateway.errors import classify_provider_error
from nz_coder.runtime.model_gateway.models import (
    ModelCall,
    ModelCallOutcome,
    ModelStreamEvent,
)
from nz_coder.runtime.model_gateway.runtime import ResolvedModelRuntime
from nz_coder.runtime.model_gateway.usage import (
    extract_provider_reported_cost,
    normalize_usage,
    resolve_usage_cost,
)
from nz_coder.runtime.recovery import RecoveryState
from nz_coder.runtime.think_tags import ThinkTagDemux, demux_think_tags
from nz_coder.runtime.model_gateway.stream import iter_stream_with_timeouts
from nz_coder.providers.capabilities import configured_model_capabilities


def _field(owner, name: str):
    if isinstance(owner, dict):
        return owner.get(name)
    return getattr(owner, name, None)


def _tool_call_dict(value) -> dict:
    if isinstance(value, dict):
        return dict(value)
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump()
    legacy = getattr(value, "dict", None)
    if callable(legacy):
        return legacy()
    return {
        "id": str(getattr(value, "id", "") or ""),
        "type": str(getattr(value, "type", "function") or "function"),
        "function": {
            "name": str(getattr(getattr(value, "function", None), "name", "") or ""),
            "arguments": str(
                getattr(getattr(value, "function", None), "arguments", "") or ""
            ),
        },
    }


def _error_metadata(error: BaseException) -> dict:
    """Capture serializable Provider error identity for host projection."""
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    headers = getattr(error, "headers", None)
    if headers is None and response is not None:
        headers = getattr(response, "headers", None)
    body = getattr(error, "body", None)
    if body is None and response is not None:
        body = getattr(response, "text", None)
    details = {
        "name": type(error).__name__,
        "code": getattr(error, "code", None),
        "status_code": status,
        "body": body,
    }
    try:
        details["headers"] = dict(headers.items()) if headers is not None else None
    except (AttributeError, TypeError, ValueError):
        details["headers"] = None
    return {"error": details}


class ProductionModelGateway:
    """Own timeout, cancellation, retry, and response normalization policy."""

    def __init__(
        self,
        runtime: ResolvedModelRuntime,
        *,
        max_retries: int = 3,
        wait: Callable[[float], None] = time.sleep,
        poll_interval: float = 0.02,
        observer: Callable[[str, dict], None] | None = None,
        backoff_base: float = 2.0,
    ) -> None:
        self.runtime = runtime
        self.max_retries = max(0, int(max_retries))
        self.wait = wait
        self.poll_interval = min(0.05, max(0.001, float(poll_interval)))
        self.observer = observer
        self.backoff_base = max(0.0, float(backoff_base))

    def _observe(self, name: str, **payload) -> None:
        if self.observer is not None:
            self.observer(name, dict(payload))

    def _request_kwargs(self, call: ModelCall) -> dict:
        runtime = self.runtime
        tools = list(call.tools) if runtime.capabilities.supports_tools else []
        kwargs = {
            "model": runtime.request_model_id,
            "messages": list(call.messages),
            "max_tokens": call.max_output_tokens,
        }
        if (
            tools
            or call.purpose.value == "coding"
            or bool(call.metadata.get("force_tools_field"))
        ):
            kwargs["tools"] = tools
        if call.tool_choice is not None and tools:
            kwargs["tool_choice"] = call.tool_choice
        if call.response_format is not None:
            kwargs["response_format"] = call.response_format
        if call.streaming and runtime.capabilities.supports_streaming:
            kwargs["stream"] = True
        kwargs.update(call.capability_options)
        if getattr(runtime.provider, "uses_capability_snapshot", False):
            kwargs["_capabilities"] = runtime.capabilities
        return kwargs

    def _attempt(self, call: ModelCall, cancel_event: threading.Event | None):
        if cancel_event is not None and cancel_event.is_set():
            raise _CallCancelled("model call cancelled before dispatch")
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        settled = threading.Event()

        def publish(kind: str, value: object) -> None:
            if settled.is_set():
                return
            try:
                result_queue.put_nowait((kind, value))
            except queue.Full:
                return

        def invoke() -> None:
            try:
                response = self.runtime.provider.create_completion(
                    self.runtime.client,
                    **self._request_kwargs(call),
                )
            except BaseException as exc:
                publish("error", exc)
            else:
                publish("result", response)

        worker = threading.Thread(
            target=invoke,
            name="nz-model-call",
            daemon=True,
        )
        worker.start()
        deadline = time.monotonic() + call.timeout_seconds
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise _CallCancelled("model call cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Provider hard timeout after {call.timeout_seconds:g}s"
                    )
                try:
                    kind, value = result_queue.get(
                        timeout=min(self.poll_interval, remaining)
                    )
                except queue.Empty:
                    continue
                if kind == "error":
                    raise value
                return value
        finally:
            settled.set()

    def _normalize(
        self,
        response,
        *,
        started: float,
        attempts: int,
    ) -> ModelCallOutcome:
        choice = response.choices[0]
        message = choice.message
        visible, tagged_reasoning = demux_think_tags(_field(message, "content") or "")
        native_reasoning = str(_field(message, "reasoning_content") or "")
        reasoning = "".join((native_reasoning, tagged_reasoning))
        calls = tuple(_tool_call_dict(item) for item in (_field(message, "tool_calls") or ()))
        provider_metadata = _field(message, "provider_extra")
        if not isinstance(provider_metadata, dict):
            provider_metadata = {}
        raw_usage = _field(response, "usage")
        usage = normalize_usage(raw_usage)
        cost, cost_source = resolve_usage_cost(
            usage,
            self.runtime.pricing,
            extract_provider_reported_cost(raw_usage),
        )
        finish_reason = str(
            _field(choice, "finish_reason") or ("tool-calls" if calls else "stop")
        )
        return ModelCallOutcome.completed(
            content=visible,
            reasoning=reasoning,
            tool_calls=calls,
            provider_metadata=provider_metadata,
            finish_reason=finish_reason,
            usage=usage,
            cost=cost,
            cost_source=cost_source,
            duration_ms=round(max(0.0, time.perf_counter() - started) * 1000, 3),
            attempts=attempts,
        )

    def normalize_response(self, response) -> ModelCallOutcome:
        """Normalize an already settled adapter response for compatibility hosts."""
        return self._normalize(response, started=time.perf_counter(), attempts=1)

    def complete_sync(
        self,
        call: ModelCall,
        cancel_event: threading.Event | None = None,
    ) -> ModelCallOutcome:
        """Execute and settle one buffered logical call."""
        if call.streaming:
            raise ValueError("complete_sync requires a buffered ModelCall")
        started = time.perf_counter()
        if cancel_event is not None and cancel_event.is_set():
            return ModelCallOutcome.cancelled(
                duration_ms=0.0,
                attempts=1,
            )
        controller = ProviderAttemptController(
            max_retries=self.max_retries,
            allow_non_streaming_fallback=False,
        )
        recovery = RecoveryState()
        recovery.max_retries = self.max_retries
        recovery.backoff_base = self.backoff_base
        attempt = 0
        active_call = call
        response_format_fallback_used = False
        while True:
            attempt += 1
            self._observe(
                "model_call_start",
                purpose=call.purpose.value,
                attempt=attempt,
            )
            try:
                response = self._attempt(active_call, cancel_event)
            except _CallCancelled as exc:
                outcome = ModelCallOutcome.cancelled(
                    error=str(exc),
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                    attempts=attempt,
                )
                self._observe("model_call_finish", status=outcome.status.value, attempt=attempt)
                return outcome
            except BaseException as exc:
                classification = classify_provider_error(exc)
                duration = round((time.perf_counter() - started) * 1000, 3)
                if classification == "context_overflow":
                    return ModelCallOutcome.context_overflow(
                        str(exc),
                        provider_metadata=_error_metadata(exc),
                        duration_ms=duration,
                        attempts=attempt,
                    )
                if classification == "client_error":
                    if (
                        active_call.response_format is not None
                        and bool(active_call.metadata.get("allow_response_format_fallback"))
                        and not response_format_fallback_used
                    ):
                        response_format_fallback_used = True
                        active_call = replace(active_call, response_format=None)
                        self._observe(
                            "model_call_response_format_fallback",
                            purpose=call.purpose.value,
                            attempt=attempt,
                            error=str(exc),
                        )
                        continue
                    return ModelCallOutcome.client_error(
                        str(exc),
                        provider_metadata=_error_metadata(exc),
                        duration_ms=duration,
                        attempts=attempt,
                    )
                retryable = classification == "retryable"
                recovery.record_error(exc)
                decision = controller.decide(
                    exc,
                    attempt=attempt,
                    streaming=False,
                    stable_boundary=False,
                    retryable=retryable,
                )
                if decision.action != "retry":
                    return ModelCallOutcome.aborted(
                        str(exc),
                        retryable=retryable,
                        provider_metadata=_error_metadata(exc),
                        duration_ms=duration,
                        attempts=attempt,
                    )
                wait_seconds = recovery.backoff_seconds(exc)
                self._observe(
                    "model_call_retry",
                    purpose=call.purpose.value,
                    attempt=attempt,
                    wait_seconds=wait_seconds,
                    error=str(exc),
                )
                self.wait(wait_seconds)
                if cancel_event is not None and cancel_event.is_set():
                    return ModelCallOutcome.cancelled(
                        duration_ms=round(
                            (time.perf_counter() - started) * 1000, 3
                        ),
                        attempts=attempt,
                    )
                continue
            outcome = self._normalize(response, started=started, attempts=attempt)
            self._observe("model_call_finish", status=outcome.status.value, attempt=attempt)
            return outcome

    async def complete(
        self,
        call: ModelCall,
        cancel_event: threading.Event | None = None,
    ) -> ModelCallOutcome:
        """Run the buffered Gateway without blocking the event loop."""
        owned_cancel = cancel_event or threading.Event()
        task = asyncio.create_task(
            asyncio.to_thread(self.complete_sync, call, owned_cancel)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            owned_cancel.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            raise

    def complete_stream_sync(
        self,
        call: ModelCall,
        *,
        on_event: Callable[[ModelStreamEvent], None] | None = None,
        cancel_event: threading.Event | None = None,
        idle_timeout_seconds: float = 60.0,
    ) -> ModelCallOutcome:
        """Settle one stream while emitting Provider-neutral ordered events."""
        if not call.streaming:
            raise ValueError("complete_stream_sync requires a streaming ModelCall")
        if not self.runtime.capabilities.supports_streaming:
            return self.complete_sync(replace(call, streaming=False), cancel_event)
        started = time.perf_counter()
        controller = ProviderAttemptController(
            max_retries=self.max_retries,
            allow_non_streaming_fallback=True,
        )
        recovery = RecoveryState()
        recovery.max_retries = self.max_retries
        recovery.backoff_base = self.backoff_base
        attempt = 0

        def emit(kind: str, **data):
            if on_event is not None:
                return on_event(ModelStreamEvent(kind, data))
            return None

        while True:
            attempt += 1
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls: dict[int, dict] = {}
            provider_metadata: dict = {}
            usage = normalize_usage(None)
            provider_cost = None
            finish_reason = ""
            finish_emitted = False
            first_token_ms = None
            stream = None
            think_tags = ThinkTagDemux()
            stable_boundary = False
            side_effect_committed = False

            def consume_think(events) -> None:
                nonlocal stable_boundary, first_token_ms
                for event in events:
                    if not event.text:
                        continue
                    if first_token_ms is None:
                        first_token_ms = round(
                            (time.perf_counter() - started) * 1000, 3
                        )
                    if event.type == "text-delta":
                        text_parts.append(event.text)
                        emit("text", delta=event.text)
                    else:
                        reasoning_parts.append(event.text)
                        emit("reasoning", delta=event.text)
                    stable_boundary = True

            def terminal(status: str, error: str = "", retryable: bool = False):
                cost, source = resolve_usage_cost(usage, self.runtime.pricing, provider_cost)
                values = {
                    "content": "".join(text_parts),
                    "reasoning": "".join(reasoning_parts),
                    "tool_calls": tuple(tool_calls[index] for index in sorted(tool_calls)),
                    "provider_metadata": provider_metadata,
                    "finish_reason": finish_reason or ("tool-calls" if tool_calls else "stop"),
                    "usage": usage,
                    "cost": cost,
                    "cost_source": source,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                    "first_token_ms": first_token_ms,
                    "attempts": attempt,
                }
                if status == "completed":
                    return ModelCallOutcome.completed(**values)
                if status == "context_overflow":
                    return ModelCallOutcome.context_overflow(error, **values)
                if status == "client_error":
                    return ModelCallOutcome.client_error(error, **values)
                if status == "cancelled":
                    return ModelCallOutcome.cancelled(error, **values)
                return ModelCallOutcome.aborted(
                    error,
                    retryable=retryable,
                    **values,
                )

            try:
                if cancel_event is not None and cancel_event.is_set():
                    return terminal("cancelled", "model stream cancelled")
                stream = self.runtime.provider.create_completion(
                    self.runtime.client,
                    **self._request_kwargs(call),
                )
                for chunk in iter_stream_with_timeouts(
                    stream,
                    idle_timeout_seconds=idle_timeout_seconds,
                    hard_timeout_seconds=call.timeout_seconds,
                    cancelled=(cancel_event.is_set if cancel_event is not None else None),
                ):
                    raw_usage = _field(chunk, "usage")
                    observed_usage = normalize_usage(raw_usage)
                    if any(observed_usage.as_legacy_dict().values()):
                        usage = observed_usage
                        emit("usage", **usage.as_legacy_dict())
                    observed_cost = extract_provider_reported_cost(raw_usage)
                    if observed_cost is not None:
                        provider_cost = observed_cost
                    choices = _field(chunk, "choices") or ()
                    choice = choices[0] if choices else None
                    observed_finish = _field(choice, "finish_reason")
                    if observed_finish:
                        finish_reason = str(observed_finish)
                    delta = _field(choice, "delta")
                    if delta is None:
                        continue
                    content = _field(delta, "content")
                    if content:
                        consume_think(think_tags.push(str(content)))
                    native_reasoning = _field(delta, "reasoning_content")
                    if native_reasoning:
                        value = str(native_reasoning)
                        reasoning_parts.append(value)
                        emit("reasoning", delta=value)
                        stable_boundary = True
                    extra = _field(delta, "provider_extra")
                    if isinstance(extra, dict) and extra:
                        provider_metadata.update(extra)
                        emit("provider_metadata", metadata=extra)
                    for item in (_field(delta, "tool_calls") or ()):
                        index = int(_field(item, "index") or 0)
                        entry = tool_calls.setdefault(index, {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        })
                        call_id = _field(item, "id")
                        if call_id:
                            entry["id"] = str(call_id)
                        function = _field(item, "function")
                        name = _field(function, "name") or _field(item, "name")
                        arguments = (
                            _field(function, "arguments")
                            if function is not None
                            else _field(item, "arguments")
                        )
                        if name:
                            entry["function"]["name"] += str(name)
                        if arguments:
                            entry["function"]["arguments"] += str(arguments)
                        item_extra = _field(item, "provider_extra")
                        if isinstance(item_extra, dict) and item_extra:
                            entry.setdefault("provider_extra", {}).update(item_extra)
                        emit(
                            "tool_delta",
                            index=index,
                            call_id=entry["id"],
                            name=entry["function"]["name"],
                            arguments=entry["function"]["arguments"],
                            metadata=entry.get("provider_extra") or {},
                        )
                        stable_boundary = True
                    if observed_finish:
                        consume_think(think_tags.finish())
                        side_effect_committed = bool(
                            emit("finish", reason=finish_reason)
                        ) or side_effect_committed
                        finish_emitted = True
                consume_think(think_tags.finish())
                if cancel_event is not None and cancel_event.is_set():
                    return terminal("cancelled", "model stream cancelled")
                if not finish_emitted:
                    emit("finish", reason=finish_reason or ("tool-calls" if tool_calls else "stop"))
                return terminal("completed")
            except BaseException as exc:
                consume_think(think_tags.finish())
                classification = classify_provider_error(exc)
                if classification == "context_overflow":
                    return terminal("context_overflow", str(exc))
                if classification == "client_error":
                    return terminal("client_error", str(exc))
                retryable = classification == "retryable"
                if side_effect_committed:
                    provider_metadata["stream_error"] = {
                        "message": str(exc),
                        **_error_metadata(exc)["error"],
                    }
                    return terminal("completed")
                decision = controller.decide(
                    exc,
                    attempt=attempt,
                    streaming=True,
                    stable_boundary=stable_boundary,
                    retryable=retryable,
                )
                self._observe(
                    "model_stream_recovery",
                    action=decision.action,
                    reason=decision.reason,
                    attempt=attempt,
                )
                if decision.action == "non_streaming_fallback":
                    fallback = self.complete_sync(
                        replace(call, streaming=False),
                        cancel_event,
                    )
                    return replace(
                        fallback,
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        attempts=attempt + fallback.attempts,
                    )
                if decision.action != "retry":
                    return terminal("aborted", str(exc), retryable)
                recovery.record_error(exc)
                wait_seconds = recovery.backoff_seconds(exc)
                self._observe(
                    "model_call_retry",
                    purpose=call.purpose.value,
                    attempt=attempt,
                    wait_seconds=wait_seconds,
                    error=str(exc),
                    streaming=True,
                )
                self.wait(wait_seconds)
            finally:
                if stream is not None:
                    from nz_coder.runtime.model_gateway.stream import close_stream
                    close_stream(stream)


class _CallCancelled(RuntimeError):
    """Internal control-flow marker for cooperative cancellation."""


class OpenAIClientBridgeProvider:
    """Keep legacy OpenAI-client compatibility inside the sole SDK boundary."""

    name = "openai-compatible"

    def create_client(self):
        raise RuntimeError("OpenAIClientBridgeProvider requires an injected client")

    def create_completion(self, client, **kwargs):
        return client.chat.completions.create(**kwargs)

    def capabilities(self, model_id: str):
        return configured_model_capabilities(self.name, model_id)
