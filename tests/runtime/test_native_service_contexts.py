"""Focused contracts for host-neutral Memory and completion verification."""
from __future__ import annotations

import asyncio

from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
from nz_coder.runtime.core.verification_context import VerificationExecutionContext
from nz_coder.runtime.execution.services import ProductionCompletionVerifier, ProductionMemoryService


class _Manager:
    def __init__(self) -> None:
        self.scope_id = "fake-scope"
        self.calls = 0
        self.kwargs = {}

    def generation(self):
        return (1, 0)

    def has_memories(self):
        return True

    def build_prompt_snapshot(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return {"block": "remembered", "generation": (1, 0)}


def test_memory_service_uses_focused_context_and_run_owned_cache() -> None:
    manager = _Manager()
    state = MemoryRecallState()
    context = MemoryExecutionContext(
        manager=manager,
        session_id="s1",
        client=None,
        model_id="model",
        tracer=lambda *_args, **_kwargs: None,
        lineage=None,
        recall=state,
    )
    service = ProductionMemoryService()

    assert service.prompt_block(context, "query") == "remembered"
    assert service.prompt_block(context, "query") == "remembered"
    assert manager.calls == 1
    assert state.last_query == "query"


def test_completion_verifier_uses_override_without_host() -> None:
    context = VerificationExecutionContext(
        override=lambda _messages, _status, _content: "revise",
        review=lambda _messages, _content: _async_value("completed"),
    )

    result = asyncio.run(ProductionCompletionVerifier().verify(
        context, [], "completed", "answer",
    ))

    assert result == "revise"


def test_completion_verifier_awaits_focused_review_callback() -> None:
    context = VerificationExecutionContext(
        override=None,
        review=lambda _messages, content: _async_value(
            "completed" if content == "answer" else "revise"
        ),
    )

    result = asyncio.run(ProductionCompletionVerifier().verify(
        context, [], "completed", "answer",
    ))

    assert result == "completed"


async def _async_value(value):
    return value


def test_memory_service_threads_provider_runtime_to_rerank(monkeypatch) -> None:
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "MEMORY_LLM_RERANK", True)
    manager = _Manager()
    provider = object()
    capabilities = object()

    def observer(_name, _payload):
        return None

    context = MemoryExecutionContext(
        manager=manager,
        session_id="s-provider",
        client=object(),
        model_id="native-model",
        tracer=None,
        lineage=None,
        recall=MemoryRecallState(),
        provider=provider,
        capabilities=capabilities,
        observer=observer,
    )

    ProductionMemoryService().prompt_block(context, "query")

    assert manager.kwargs["rerank_provider"] is provider
    assert manager.kwargs["rerank_capabilities"] is capabilities
    assert manager.kwargs["rerank_observer"] is observer
