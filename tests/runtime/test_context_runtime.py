"""Behavior tests for host-free Context Runtime preparation."""
from __future__ import annotations

import asyncio

from nz_coder.runtime.context_manager import (
    CompactionAttemptState,
    ProductionContextManager,
)
from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.state.context import PromptBudget


def _budget(*, usable: int = 100, soft: int = 80) -> PromptBudget:
    return PromptBudget(
        context_tokens=128,
        output_reserve_tokens=28,
        usable_input_tokens=usable,
        soft_preflight_tokens=soft,
        expansion_budget_tokens=32,
        tool_prune_protect_tokens=16,
        tool_prune_minimum_tokens=4,
        context_metadata_missing=False,
    )


def test_context_runtime_returns_without_compaction_below_soft_limit(tmp_path) -> None:
    """Calling compact below the soft limit would be an unnecessary context loss."""
    compact_calls: list[list[dict]] = []
    events: list[tuple[str, dict]] = []
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(),
        projected_tokens=lambda _messages: 12,
        compact=lambda messages: compact_calls.append(messages) or messages,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda event, **payload: events.append((event, payload)),
    )
    messages = [{"role": "user", "content": "small request"}]

    compacted = ProductionContextManager().prepare_sync(context, messages)

    assert compacted is False
    assert compact_calls == []
    assert events == []


def test_context_runtime_reports_live_pressure_before_early_return(tmp_path) -> None:
    reports = []
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(usable=100, soft=80),
        projected_tokens=lambda _messages: 12,
        compact=lambda messages: messages,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda _event, **_payload: None,
        report_pressure=lambda payload: reports.append(payload),
    )

    ProductionContextManager().prepare_sync(
        context, [{"role": "user", "content": "small"}],
    )

    assert reports == [{
        "context_window": 128,
        "used_tokens": 12,
        "reserve_tokens": 28,
    }]


def test_context_runtime_async_compacts_above_hard_limit(tmp_path) -> None:
    """Ignoring the hard limit must fail this test by leaving history unchanged."""
    compact_calls: list[list[dict]] = []
    stamped: list[list[dict]] = []
    events: list[tuple[str, dict]] = []

    def compact(messages: list[dict]) -> list[dict]:
        compact_calls.append(list(messages))
        return [{"role": "system", "content": "summary"}]

    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(usable=50, soft=40),
        projected_tokens=lambda _messages: 75,
        compact=compact,
        stamp_auto_compaction=lambda messages: stamped.append(list(messages)),
        trace=lambda event, **payload: events.append((event, payload)),
    )
    messages = [{"role": "user", "content": "large request"}]
    attempts = CompactionAttemptState()

    compacted = asyncio.run(
        ProductionContextManager().prepare_async(
            context,
            messages,
            attempt_state=attempts,
        )
    )

    assert compacted is True
    assert compact_calls == [[{"role": "user", "content": "large request"}]]
    assert messages == [{"role": "system", "content": "summary"}]
    assert stamped == [[{"role": "system", "content": "summary"}]]
    assert attempts.attempts == 1
    assert [event for event, _payload in events].count("compact") == 1
