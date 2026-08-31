"""Focused contracts for host-neutral Memory and completion verification."""
from __future__ import annotations

import asyncio

from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
from nz_coder.runtime.core.verification_context import VerificationExecutionContext
from nz_coder.runtime.execution.services import ProductionCompletionVerifier, ProductionMemoryService


class _Manager:
    def __init__(self) -> None:
        self.calls = 0
        self.kwargs = {}

    def has_memories(self):
        return True

    def build_prompt_block(self, **kwargs):
        self.calls += 1
        self.kwargs = kwargs
        return "remembered"


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


def test_memory_finalize_threads_provider_runtime_to_extraction(monkeypatch) -> None:
    import nz_coder.state.memory as memory_module
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "MEMORY_LLM_EXTRACT", True)
    monkeypatch.setattr(config, "MEMORY_ASYNC_WRITE", False)
    captured = {}

    async def fake_pipeline(_session_id, _messages, **kwargs):
        captured.update(kwargs)
        return {"status": "ok", "saved_count": 0}

    monkeypatch.setattr(memory_module, "run_auto_memory_pipeline_async", fake_pipeline)
    provider = object()
    capabilities = object()

    def observer(_name, _payload):
        return None

    context = MemoryExecutionContext(
        manager=_Manager(),
        session_id="s-extract",
        client=object(),
        model_id="native-model",
        tracer=None,
        lineage=None,
        recall=MemoryRecallState(),
        provider=provider,
        capabilities=capabilities,
        observer=observer,
    )

    asyncio.run(ProductionMemoryService().finalize(
        context,
        [{"role": "user", "content": "Remember this preference."}],
        "completed",
    ))

    assert captured["provider"] is provider
    assert captured["capabilities"] is capabilities
    assert captured["observer"] is observer


def test_llm_memory_finalize_is_not_detached_from_run_accounting(
    monkeypatch,
) -> None:
    """Provider-backed memory work must settle inside the terminal boundary."""
    import nz_coder.state.memory as memory_module
    import nz_coder.runtime.execution.services as services_module
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "MEMORY_LLM_EXTRACT", True)
    monkeypatch.setattr(config, "MEMORY_ASYNC_WRITE", True)
    captured = []
    detached = []

    async def fake_pipeline(_session_id, _messages, **_kwargs):
        captured.append("settled")
        return {"status": "ok", "saved_count": 0}

    def reject_background(coro):
        detached.append(True)
        coro.close()

    monkeypatch.setattr(memory_module, "run_auto_memory_pipeline_async", fake_pipeline)
    monkeypatch.setattr(services_module, "start_background_coro", reject_background)
    context = MemoryExecutionContext(
        manager=_Manager(),
        session_id="s-accounted-extract",
        client=object(),
        model_id="native-model",
        tracer=None,
        lineage=None,
        recall=MemoryRecallState(),
        provider=object(),
        capabilities=object(),
        observer=lambda _name, _payload: None,
    )

    asyncio.run(ProductionMemoryService().finalize(
        context,
        [{"role": "user", "content": "Remember the Provider boundary."}],
        "completed",
    ))

    assert captured == ["settled"]
    assert detached == []
