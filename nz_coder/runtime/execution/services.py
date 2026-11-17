"""Production service graph consumed by the shared AgentRunner."""
from __future__ import annotations

import asyncio
import sqlite3
import concurrent.futures
from contextvars import copy_context
from functools import partial
import threading
import time
from typing import Awaitable, Callable

from nz_coder.runtime.conversation.context_manager import ProductionContextManager
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.model_context import ModelExecutionContext, current_request_tools
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.runtime.core.run_context import current_run_context, scoped_run_context
from nz_coder.runtime.execution.host import ProductionRuntimeHost
from nz_coder.runtime.model_gateway import ModelCall, ModelCallPurpose, ModelCallStatus
from nz_coder.runtime.conversation.model_result import (
    LLMResult,
    ModelCompletionError,
)
from nz_coder.state.context import estimate_request_tokens
from nz_coder.protocol.public_error import to_public_error
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime
from nz_coder.runtime.conversation.input_preflight import ProductionInputPreflight
from nz_coder.runtime.agent.agent_transition_runtime import ProductionAgentTransitionRuntime
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.tool_platform.exposure import ToolExposureMiddleware


class StreamToolExecutionCancelled(Exception):
    """Internal bridge signal after async tool execution has settled cancel."""


class StreamToolExecutionFailed(Exception):
    """Internal bridge signal for a non-retryable local tool batch failure."""


class _StreamToolBridge:
    """Run an async tool batch from the synchronous Provider stream worker."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        handler: Callable[[object], Awaitable[str]],
    ) -> None:
        self._loop = loop
        self._handler = handler
        self._lock = threading.Lock()
        self._future: concurrent.futures.Future | None = None
        self._cancelled = False

    def execute(self, result: object) -> str:
        with self._lock:
            if self._cancelled:
                raise StreamToolExecutionCancelled
            future = asyncio.run_coroutine_threadsafe(
                self._handler(result),
                self._loop,
            )
            self._future = future
            if self._cancelled:
                future.cancel()
        try:
            return str(future.result())
        except concurrent.futures.CancelledError as error:
            raise StreamToolExecutionCancelled from error
        except Exception as error:
            raise StreamToolExecutionFailed(str(error)) from error
        finally:
            with self._lock:
                if self._future is future:
                    self._future = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            future = self._future
        if future is not None:
            future.cancel()


class ProductionTurnModelRuntime:
    """Own the model-turn boundary used by the production Runner."""

    def complete_turn_sync(
        self,
        context: ModelExecutionContext,
        messages: list,
        *,
        stream: bool,
        on_token,
        message_part: dict | None,
        stream_tool_handler,
    ):
        overflow = self.preflight_request(context, messages)
        if overflow is not None:
            return overflow
        capabilities = context.capabilities()
        if stream and (capabilities is None or capabilities.supports_streaming):
            return context.call_streaming(
                messages,
                on_token,
                message_part=message_part,
                stream_tool_handler=stream_tool_handler,
            )
        if stream and capabilities is not None:
            context.trace(
                "provider_capability_fallback",
                capability="streaming",
                model=context.active_model_id(),
                provider=capabilities.provider,
            )
        return context.call_non_streaming(messages)

    def preflight_request(
        self,
        context: ModelExecutionContext,
        messages: list,
        *,
        tools: list | None = None,
    ) -> LLMResult | None:
        """Return a local typed outcome before any main Provider turn starts."""
        return self._request_budget_overflow(context, messages, tools=tools)

    @staticmethod
    def _request_budget_overflow(
        context: ModelExecutionContext,
        messages: list,
        *,
        tools: list | None = None,
    ) -> LLMResult | None:
        """Stop an over-budget outbound request before the Provider call.

        Context preparation estimates history and tool schemas before the prompt
        builder adds dynamic state. This final boundary sees the exact assembled
        messages and tool schemas, so an overflow can take the existing bounded
        compaction recovery path instead of becoming an opaque Provider error.
        """
        budget = context.prompt_budget()
        limit = getattr(budget, "usable_input_tokens", 0) if budget is not None else 0
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            return None
        request_tools = (
            ProductionTurnModelRuntime._tools(context)
            if tools is None
            else tools
        )
        estimated = estimate_request_tokens(messages, request_tools)
        if estimated <= limit:
            return None
        context.trace(
            "request_budget_overflow",
            estimated_tokens=estimated,
            usable_input_tokens=limit,
            tool_count=len(request_tools),
        )
        return LLMResult(
            needs_compaction=True,
            compaction_error=(
                "The assembled model request exceeds the usable input budget "
                f"({estimated} > {limit} tokens)."
            ),
            failure_source="local_request_budget",
        )

    def complete_buffered(
        self,
        context: ModelExecutionContext,
        messages: list,
        *,
        max_retries: int | None = None,
        call_started: float | None = None,
        attempts: int = 1,
        observe_status: bool = False,
    ):
        overflow = self.preflight_request(context, messages)
        if overflow is not None:
            return overflow
        with scoped_run_context(context.run_context or current_run_context()):
            gateway = context.gateway(max_retries=max_retries)
        outcome = gateway.complete_sync(ModelCall(
            purpose=ModelCallPurpose.CODING,
            messages=messages,
            tools=self._tools(context),
            max_output_tokens=context.prompt_budget().output_reserve_tokens,
            timeout_seconds=current_run_settings().provider_hard_timeout,
        ))
        if observe_status and outcome.status is ModelCallStatus.COMPLETED:
            context.record_success()
        elif observe_status and outcome.status is ModelCallStatus.CONTEXT_OVERFLOW:
            context.trace("context_overflow", error=outcome.error)
        elif observe_status and outcome.status is ModelCallStatus.CLIENT_ERROR:
            context.trace("api_error", count=1, error=outcome.error)
        result = context.project_outcome(outcome)
        result.attempts = max(result.attempts, int(attempts))
        if call_started is not None:
            result.duration_ms = round(
                max(0.0, time.perf_counter() - call_started) * 1000,
                3,
            )
        return result

    def complete_text(
        self, context: ModelExecutionContext, system: str, prompt: str,
    ) -> str:
        result = self.complete_text_result(context, system, prompt)
        if (
            result.needs_compaction
            or result.diagnostic
            or result.aborted
            or result.assistant_error is not None
        ):
            raise ModelCompletionError(result)
        return str(result.content or "")

    def complete_text_result(
        self, context: ModelExecutionContext, system: str, prompt: str,
    ) -> LLMResult:
        """Complete a tools-disabled text call through the typed model boundary."""
        messages = [
            {"role": "system", "content": str(system)},
            {"role": "user", "content": str(prompt)},
        ]
        overflow = self.preflight_request(context, messages, tools=[])
        if overflow is not None:
            return overflow
        with scoped_run_context(context.run_context or current_run_context()):
            gateway = context.gateway()
        outcome = gateway.complete_sync(ModelCall(
            purpose=ModelCallPurpose.CODING,
            messages=messages,
            tools=[],
            max_output_tokens=min(
                8000,
                context.prompt_budget().output_reserve_tokens,
            ),
            timeout_seconds=current_run_settings().provider_hard_timeout,
        ))
        return context.project_outcome(outcome)

    @staticmethod
    def _tools(context: ModelExecutionContext) -> list:
        capabilities = context.capabilities()
        frozen = current_request_tools()
        if frozen is not None:
            return frozen
        return (
            context.active_tool_specs()
            if capabilities is None or capabilities.supports_tools
            else []
        )

    async def complete_turn(
        self,
        context: ModelExecutionContext,
        messages: list,
        *,
        stream: bool,
        on_token,
        message_part: dict,
        stream_tool_handler,
    ):
        loop = asyncio.get_running_loop()
        bridge = (
            _StreamToolBridge(loop, stream_tool_handler)
            if stream and stream_tool_handler is not None
            else None
        )
        with scoped_run_context(context.run_context or current_run_context()):
            execution_context = copy_context()
        compatibility_override = context.complete_override
        if callable(compatibility_override):
            overflow = self.preflight_request(context, messages)
            if overflow is not None:
                return overflow
            operation = partial(
                compatibility_override,
                messages,
                stream,
                on_token,
                message_part,
                bridge.execute if bridge is not None else None,
            )
        else:
            operation = partial(
                self.complete_turn_sync,
                context,
                messages,
                stream=stream,
                on_token=on_token,
                message_part=message_part,
                stream_tool_handler=bridge.execute if bridge is not None else None,
            )
        future = loop.run_in_executor(
            None,
            execution_context.run,
            operation,
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancel_error:
            if context.run_context is not None:
                context.run_context.request_cancel()
            if bridge is not None:
                bridge.cancel()
            if message_part is not None:
                context.retire_message_part(message_part, "cancelled")
            grace = current_run_settings().provider_cancel_grace
            if not future.done() and grace > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=grace)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except BaseException:
                    pass
            if future.done():
                try:
                    result = future.result()
                except BaseException:
                    pass
                else:
                    if context.run_context is not None and isinstance(result, LLMResult):
                        from nz_coder.runtime.core.result import TokenUsage
                        from nz_coder.runtime.conversation.model_result import _normalize_llm_result_metrics
                        _normalize_llm_result_metrics(result)
                        context.run_context.add_usage(TokenUsage(
                            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                            cached_read_tokens=result.cache_read_tokens, cached_write_tokens=result.cache_write_tokens,
                            reasoning_tokens=result.reasoning_tokens))
            else:
                future.add_done_callback(_consume_future_result)
            raise cancel_error


def _consume_future_result(future) -> None:  # noqa: ANN001
    """Observe a fenced late worker result without reviving its generation."""
    try:
        future.result()
    except BaseException:
        pass


class ProductionRuntimeEventSink:
    """Project Runner/service lifecycle events onto the Session event bus."""

    def publish(self, event) -> None:
        from nz_coder.protocol.session_events import publish_session_event
        name = getattr(event.name, "value", event.name)
        # 产品终态有唯一持久边界；core 回调仍由其自己的 sink 正常接收。
        if name in {"session.run.completed", "session.run.failed", "session.run.cancelled"}:
            return
        publish_session_event(str(name), event.payload)


class ProductionMemoryService:
    """按持久代次缓存背景投影；终态不自动改写知识。"""

    def prompt_block(self, context, query: str) -> str:
        if strict_local_tools():
            return ""
        settings = current_run_settings()
        try:
            generation = tuple(context.manager.generation())
            from nz_coder.state.memory import configured_embedding_provider
            provider = configured_embedding_provider()
            retrieval_identity = (settings.memory_retrieval_mode, settings.memory_embedding_model, getattr(provider, "identity", "lexical"))
            key = (context.manager.scope_id, generation, query, settings.memory_llm_rerank, retrieval_identity)
            if key == context.recall.cache_key:
                return context.recall.last_block
            projection = context.manager.build_prompt_snapshot(
                query=query or None, max_items=5, max_chars=2000, embedding_provider=provider,
                rerank_client=context.client if settings.memory_llm_rerank else None,
                model=context.model_id if settings.memory_llm_rerank else None,
                rerank_provider=context.provider if settings.memory_llm_rerank else None,
                rerank_capabilities=context.capabilities if settings.memory_llm_rerank else None,
                rerank_observer=context.observer if settings.memory_llm_rerank else None,
                rerank_runtime=context.model_runtime if settings.memory_llm_rerank else None,
            )
            block = projection["block"]
            if settings.memory_retrieval_mode == "hybrid" and provider is None:
                block = "<memory-diagnostic>Semantic encoder is not configured/available; using lexical retrieval.</memory-diagnostic>\n" + block
            key = (context.manager.scope_id, tuple(projection["generation"]), query, settings.memory_llm_rerank, retrieval_identity)
        except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
            # 不把记忆存储故障伪装成“确实没有相关记忆”。
            return f"<long-term-memory status='unavailable'>{to_public_error(exc).message}</long-term-memory>"
        context.recall.last_query = query
        context.recall.last_block = block
        context.recall.cache_key = key
        context.commit_recall(context.recall)
        return block

    async def finalize(self, context, messages: list, status: str) -> None:
        # 所有显式写入已由工具边界结清；此处不启动新模型或后台任务。
        return None

    def finalize_sync(self, context, messages: list, status: str) -> None:
        return None


class ProductionCompletionVerifier:
    """Own the natural-stop verification and reflection hook boundary."""

    async def verify(
        self,
        context,
        messages: list,
        status: str,
        content: str,
    ) -> str:
        if context.override is not None:
            return context.override(messages, status, content)
        return await context.review(messages, content)


def build_runtime_services(*, session_store=None, events=None) -> RuntimeServices:
    """Construct a validated stateless production service graph."""
    return RuntimeServices(
        model=ProductionTurnModelRuntime(),
        tools=ProductionToolRuntime(),
        context=ProductionContextManager(),
        session_runtime=SessionRuntime(session_store or LegacyJsonSessionStore()),
        events=events or ProductionRuntimeEventSink(),
        host=ProductionRuntimeHost(),
        memory=ProductionMemoryService(),
        verifier=ProductionCompletionVerifier(),
        lifecycle=ProductionRunLifecycle(),
        guardrails=ProductionGuardrailRuntime(),
        inputs=ProductionInputPreflight(),
        transitions=ProductionAgentTransitionRuntime(),
        middleware=(ToolExposureMiddleware(),),
    )
