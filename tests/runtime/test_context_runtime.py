"""Behavior tests for host-free Context Runtime preparation."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import threading
import time

import pytest

from nz_coder.runtime.conversation.context_manager import (
    CompactionAttemptState,
    ProductionContextManager,
)
from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.state.context import PromptBudget


def _budget(
    *,
    usable: int = 100,
    soft: int = 80,
    replay: int = 0,
) -> PromptBudget:
    return PromptBudget(
        context_tokens=128,
        output_reserve_tokens=28,
        usable_input_tokens=usable,
        soft_preflight_tokens=soft,
        expansion_budget_tokens=32,
        tool_prune_protect_tokens=16,
        tool_prune_minimum_tokens=4,
        context_metadata_missing=False,
        replay_compaction_tokens=replay,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_tokens", float("inf")),
        ("usable_input_tokens", float("nan")),
        ("replay_compaction_tokens", "bad"),
        ("soft_preflight_tokens", True),
    ],
)
def test_prompt_budget_rejects_malformed_port_values(field, value) -> None:
    """A custom Context adapter cannot inject unsafe budget arithmetic."""
    with pytest.raises(ValueError, match=field):
        replace(_budget(), **{field: value})


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


def test_context_runtime_compacts_replayed_history_before_physical_limit(
    tmp_path,
) -> None:
    """Removing the replay-cost trigger must restore expensive full-history replay."""
    compact_calls: list[list[dict]] = []
    stamped: list[list[dict]] = []
    events: list[tuple[str, dict]] = []
    budget = _budget(usable=100, soft=80, replay=60)
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=budget,
        projected_tokens=lambda _messages: 70,
        compact=lambda messages: (
            compact_calls.append(list(messages))
            or [{"role": "user", "content": "summary"}]
        ),
        stamp_auto_compaction=lambda messages: stamped.append(list(messages)),
        trace=lambda event, **payload: events.append((event, payload)),
        projected_replay_tokens=lambda _messages: 61,
    )
    messages = [{"role": "user", "content": "long coding history"}]

    compacted = ProductionContextManager().prepare_sync(context, messages)

    assert compacted is True
    assert compact_calls == [[{"role": "user", "content": "long coding history"}]]
    assert messages == [{"role": "user", "content": "summary"}]
    assert stamped == [[{"role": "user", "content": "summary"}]]
    compact_event = next(payload for event, payload in events if event == "compact")
    assert compact_event["trigger"] == "replay_cost"
    assert compact_event["replay_token_estimate"] == 61
    assert compact_event["replay_limit"] == 60


def test_context_runtime_does_not_compact_fixed_request_overhead(
    tmp_path,
) -> None:
    """Tool schemas and system text alone must not trigger a summary call."""
    compact_calls: list[list[dict]] = []
    budget = _budget(usable=100, soft=80, replay=60)
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=budget,
        projected_tokens=lambda _messages: 95,
        compact=lambda messages: compact_calls.append(messages) or messages,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda _event, **_payload: None,
        projected_replay_tokens=lambda _messages: 59,
    )
    messages = [{"role": "user", "content": "small history"}]

    compacted = ProductionContextManager().prepare_sync(context, messages)

    assert compacted is False
    assert compact_calls == []


@pytest.mark.parametrize("replay_value", [float("nan"), float("inf"), "bad"])
def test_context_runtime_ignores_invalid_optional_replay_metric(
    tmp_path,
    replay_value,
) -> None:
    """A custom replay projector cannot crash the shared Context Runtime."""
    events = []
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(usable=100, soft=80, replay=60),
        projected_tokens=lambda _messages: 12,
        compact=lambda messages: messages,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda event, **payload: events.append((event, payload)),
        projected_replay_tokens=lambda _messages: replay_value,
    )

    assert ProductionContextManager().prepare_sync(
        context,
        [{"role": "user", "content": "small"}],
    ) is False
    assert any(
        event == "context_metric_repaired"
        and payload["metric"] == "projected_replay_tokens"
        for event, payload in events
    )


def test_context_runtime_falls_back_when_required_projector_is_invalid(
    tmp_path,
) -> None:
    """Required port corruption uses a conservative local token estimate."""
    reports = []
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(usable=100, soft=80),
        projected_tokens=lambda _messages: float("nan"),
        compact=lambda messages: messages,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda _event, **_payload: None,
        report_pressure=lambda payload: reports.append(payload),
    )

    assert ProductionContextManager().prepare_sync(
        context,
        [{"role": "user", "content": "small"}],
    ) is False
    assert isinstance(reports[0]["used_tokens"], int)
    assert reports[0]["used_tokens"] >= 0


def test_context_runtime_marks_cost_compaction_as_non_overflow(tmp_path) -> None:
    """Cost control must not be persisted as a provider overflow recovery."""
    budget = _budget(usable=100, soft=80, replay=60)

    def stamp(messages: list[dict]) -> None:
        messages[0]["_nz_compaction"].update({
            "auto": True,
            "overflow": True,
        })

    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=budget,
        projected_tokens=lambda _messages: 70,
        compact=lambda _messages: [{
            "role": "user",
            "content": "summary",
            "_nz_compaction": {},
        }],
        stamp_auto_compaction=stamp,
        trace=lambda _event, **_payload: None,
        projected_replay_tokens=lambda _messages: 61,
    )
    messages = [{"role": "user", "content": "long history"}]

    ProductionContextManager().prepare_sync(context, messages)

    assert messages[0]["_nz_compaction"]["trigger"] == "replay_cost"
    assert messages[0]["_nz_compaction"]["overflow"] is False


def test_context_runtime_async_compacts_replayed_history(tmp_path) -> None:
    """Async terminal runs must use the same replay-cost boundary as sync runs."""
    budget = _budget(usable=100, soft=80, replay=60)
    events: list[tuple[str, dict]] = []
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=budget,
        projected_tokens=lambda _messages: 70,
        compact=lambda _messages: [{"role": "user", "content": "summary"}],
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda event, **payload: events.append((event, payload)),
        projected_replay_tokens=lambda _messages: 61,
    )
    messages = [{"role": "user", "content": "long history"}]

    compacted = asyncio.run(
        ProductionContextManager().prepare_async(context, messages)
    )

    assert compacted is True
    assert messages == [{"role": "user", "content": "summary"}]
    compact_event = next(payload for event, payload in events if event == "compact")
    assert compact_event["trigger"] == "replay_cost"


def test_context_runtime_cancellation_interrupts_inflight_compaction(tmp_path) -> None:
    """Ctrl+C must signal the blocking Provider boundary before awaiting its thread."""
    release = threading.Event()
    cancellation_forwarded = threading.Event()

    def compact(_messages):
        release.wait(timeout=1)
        return [{"role": "user", "content": "summary"}]

    def cancel_compaction():
        cancellation_forwarded.set()
        release.set()

    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(usable=100, soft=80, replay=60),
        projected_tokens=lambda _messages: 70,
        compact=compact,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda _event, **_payload: None,
        projected_replay_tokens=lambda _messages: 61,
        cancel_compaction=cancel_compaction,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            ProductionContextManager().prepare_async(
                context,
                [{"role": "user", "content": "long history"}],
            )
        )
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert cancellation_forwarded.is_set()


def test_context_runtime_does_not_mutate_history_hidden_by_continuation_boundary(
    tmp_path,
) -> None:
    """A provider-only resume projection must leave the durable prefix intact."""
    events = []
    old_output = "historical tool evidence " + "x" * 500
    messages = [
        {"role": "user", "content": "first activation"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": old_output},
        {"role": "user", "content": "second activation"},
        {
            "role": "assistant",
            "content": "stopped",
            "_timestamp": time.time() - 3_600,
            "_nz_continuation": {
                "version": 1,
                "status": "max_turns",
                "summary": "Goal: finish the task",
            },
        },
        {"role": "user", "content": "continue"},
    ]
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=_budget(),
        projected_tokens=lambda _messages: 12,
        compact=lambda value: value,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda event, **payload: events.append((event, payload)),
    )

    compacted = ProductionContextManager().prepare_sync(context, messages)

    assert compacted is False
    assert messages[2]["content"] == old_output
    assert events == [(
        "context_continuation_boundary",
        {"status": "max_turns", "dropped_messages": 5},
    )]


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
