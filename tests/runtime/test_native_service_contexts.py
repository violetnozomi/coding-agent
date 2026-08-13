"""Focused contracts for host-neutral Memory and completion verification."""
from __future__ import annotations

import asyncio

from nz_coder.runtime.core.memory_context import MemoryExecutionContext, MemoryRecallState
from nz_coder.runtime.core.verification_context import VerificationExecutionContext
from nz_coder.runtime.services import ProductionCompletionVerifier, ProductionMemoryService


class _Manager:
    def __init__(self) -> None:
        self.calls = 0

    def has_memories(self):
        return True

    def build_prompt_block(self, **_kwargs):
        self.calls += 1
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
