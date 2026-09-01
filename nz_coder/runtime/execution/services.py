"""Production service graph consumed by the shared AgentRunner."""
from __future__ import annotations

import asyncio
import concurrent.futures
from contextvars import copy_context
from functools import partial
import hashlib
import json
import threading
import time
from typing import Awaitable, Callable

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, SYNTHETIC_USER_KEY
from nz_coder.foundation.async_utils import start_background_coro
from nz_coder.runtime.conversation.context_manager import ProductionContextManager
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.model_context import ModelExecutionContext
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.runtime.execution.host import ProductionRuntimeHost
from nz_coder.runtime.model_gateway import ModelCall, ModelCallPurpose, ModelCallStatus
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
        outcome = context.gateway(max_retries=max_retries).complete_sync(ModelCall(
            purpose=ModelCallPurpose.CODING,
            messages=messages,
            tools=self._tools(context),
            max_output_tokens=context.prompt_budget().output_reserve_tokens,
            timeout_seconds=config.PROVIDER_HARD_TIMEOUT_SECONDS,
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
        outcome = context.gateway().complete_sync(ModelCall(
            purpose=ModelCallPurpose.CODING,
            messages=[
                {"role": "system", "content": str(system)},
                {"role": "user", "content": str(prompt)},
            ],
            tools=[],
            max_output_tokens=min(
                8000,
                context.prompt_budget().output_reserve_tokens,
            ),
            timeout_seconds=config.PROVIDER_HARD_TIMEOUT_SECONDS,
        ))
        if outcome.status is not ModelCallStatus.COMPLETED:
            raise RuntimeError(outcome.error or outcome.status.value)
        return outcome.content

    @staticmethod
    def _tools(context: ModelExecutionContext) -> list:
        capabilities = context.capabilities()
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
        execution_context = copy_context()
        compatibility_override = context.complete_override
        if callable(compatibility_override):
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
            if bridge is not None:
                bridge.cancel()
            if message_part is not None:
                context.retire_message_part(message_part, "cancelled")
            grace = max(
                0.0,
                float(getattr(config, "PROVIDER_CANCEL_GRACE_SECONDS", 0.25)),
            )
            if not future.done() and grace > 0:
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=grace)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except BaseException:
                    pass
            if future.done():
                _consume_future_result(future)
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
        publish_session_event(str(name), event.payload)


class ProductionMemoryService:
    """Own recall caching and terminal learning for production runs."""

    def prompt_block(self, context, query: str) -> str:
        if strict_local_tools():
            return ""
        has_memories = (
            context.manager.has_memories()
            if hasattr(context.manager, "has_memories")
            else bool(getattr(context.manager, "memories", {}))
        )
        if not has_memories:
            return ""
        if query and query == context.recall.last_query:
            return context.recall.last_block
        block = context.manager.build_prompt_block(
            query=query or None,
            max_items=5,
            max_chars=2000,
            rerank_client=context.client if config.MEMORY_LLM_RERANK else None,
            model=context.model_id if config.MEMORY_LLM_RERANK else None,
            rerank_provider=(
                context.provider if config.MEMORY_LLM_RERANK else None
            ),
            rerank_capabilities=(
                context.capabilities if config.MEMORY_LLM_RERANK else None
            ),
            rerank_observer=(
                context.observer if config.MEMORY_LLM_RERANK else None
            ),
        )
        if query:
            context.recall.last_query = query
            context.recall.last_block = block
            context.commit_recall(context.recall)
        return block

    async def finalize(self, context, messages: list, status: str) -> None:
        if strict_local_tools():
            return
        snapshot = self._snapshot(messages)
        memory_review_key = self._review_key(context, snapshot)

        async def persist() -> None:
            from nz_coder.state.memory import run_auto_memory_pipeline_async

            client = context.client if config.MEMORY_LLM_EXTRACT else None
            model = context.model_id if config.MEMORY_LLM_EXTRACT else None
            summary = await run_auto_memory_pipeline_async(
                context.session_id,
                snapshot,
                client=client,
                model=model,
                tracer=context.tracer,
                provider=context.provider,
                capabilities=context.capabilities,
                observer=context.observer,
            )
            if summary.get("saved_count"):
                context.recall.last_query = ""
                context.recall.last_block = ""
                context.commit_recall(context.recall)
            self._record_outcome(context, memory_review_key, summary)

        # A detached Provider call can outlive this run and charge its usage to
        # the next run's mutable ledger.  Keep only local/deterministic writes
        # eligible for background execution.
        if config.MEMORY_ASYNC_WRITE and not config.MEMORY_LLM_EXTRACT:
            start_background_coro(persist())
        else:
            await persist()

    def finalize_sync(self, context, messages: list, status: str) -> None:
        """Compatibility path for the legacy synchronous finalizer."""
        if strict_local_tools():
            return
        snapshot = self._snapshot(messages)
        memory_review_key = self._review_key(context, snapshot)

        def persist() -> None:
            from nz_coder.state.memory import run_auto_memory_pipeline

            client = context.client if config.MEMORY_LLM_EXTRACT else None
            model = context.model_id if config.MEMORY_LLM_EXTRACT else None
            summary = run_auto_memory_pipeline(
                context.session_id,
                snapshot,
                client=client,
                model=model,
                tracer=context.tracer,
                provider=context.provider,
                capabilities=context.capabilities,
                observer=context.observer,
            )
            if summary.get("saved_count"):
                context.recall.last_query = ""
                context.recall.last_block = ""
                context.commit_recall(context.recall)
            self._record_outcome(context, memory_review_key, summary)

        if config.MEMORY_ASYNC_WRITE and not config.MEMORY_LLM_EXTRACT:
            context = copy_context()
            threading.Thread(target=lambda: context.run(persist), daemon=True).start()
        else:
            persist()

    @staticmethod
    def _snapshot(messages: list) -> list[dict]:
        return [
            {
                "role": message.get("role"),
                "content": message.get("content", ""),
                MESSAGE_ID_KEY: message.get(MESSAGE_ID_KEY),
                SYNTHETIC_USER_KEY: bool(message.get(SYNTHETIC_USER_KEY, False)),
            }
            for message in messages
            if isinstance(message, dict)
        ]

    @staticmethod
    def _review_key(context, snapshot: list[dict]) -> str:
        identities = [
            str(item.get(MESSAGE_ID_KEY) or "")
            for item in snapshot
            if item.get(MESSAGE_ID_KEY)
        ]
        payload = json.dumps(
            {"session_id": context.session_id, "message_ids": identities},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _record_outcome(context, review_key: str, summary: dict) -> None:
        lineage = context.lineage
        if lineage is None or not isinstance(summary, dict):
            return
        saved_names = [
            str(item)[:200]
            for item in summary.get("saved_names", [])
            if str(item).strip()
        ][:20]
        digest = {
            "review_key": review_key,
            "status": str(summary.get("status") or "unknown")[:80],
            "candidate_count": max(0, int(summary.get("candidate_count", 0) or 0)),
            "saved_count": max(0, int(summary.get("saved_count", 0) or 0)),
            "saved_names": saved_names,
        }
        lineage.append_unique("memory_outcome_digest", review_key, digest)
        lineage.append_unique("memory_review_receipt", review_key, {
            "review_key": review_key,
            "proposal_ids": saved_names,
            "status": "completed" if saved_names else "no_action",
        })
        if saved_names:
            lineage.append_unique("client_notice", review_key, {
                "source": "memory-agent",
                "content": "Memory updated: " + "; ".join(saved_names[:3]),
                "proposal_ids": saved_names,
            })


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
