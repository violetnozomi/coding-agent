"""Tests for model-window-aware context budgeting and overflow persistence."""
from __future__ import annotations

from tests.fixtures.compaction_summary import summary_json

import copy
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from nz_coder.state.context import (
    estimate_tokens,
    estimate_request_tokens,
    micro_compact,
    persist_large_output,
    persist_oversized_user_inputs,
    prompt_budget,
)
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import scoped_session


def _completed_tool_batch(*results: dict, tool_name: str = "read_file") -> list[dict]:
    return [{
        "role": "assistant",
        "content": "Inspect the evidence.",
        "tool_calls": [{
            "id": result["tool_call_id"],
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        } for result in results],
    }, *results]


def test_prompt_budget_reserves_small_window_output_proportionally():
    budget = prompt_budget(context_tokens=64_000, output_tokens=32_000)

    assert budget.output_reserve_tokens == 16_000
    assert budget.usable_input_tokens == 48_000
    assert budget.soft_preflight_tokens == 40_800
    assert budget.expansion_budget_tokens == 7_200
    assert budget.tool_prune_protect_tokens == 12_000
    assert budget.tool_prune_minimum_tokens == 4_800


def test_prompt_budget_caps_replayed_history_for_large_context_models(monkeypatch):
    """A huge physical window must not imply replaying its full history each turn."""
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "CONTEXT_REPLAY_COMPACTION_TOKENS", 24_000)

    budget = prompt_budget(context_tokens=1_000_000, output_tokens=64_000)

    assert budget.usable_input_tokens == 936_000
    assert budget.replay_compaction_tokens == 24_000


def test_prompt_budget_can_disable_replay_cost_compaction(monkeypatch):
    """Operators can retain capacity-only compaction for cache-heavy providers."""
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "CONTEXT_REPLAY_COMPACTION_TOKENS", 0)

    budget = prompt_budget(context_tokens=1_000_000, output_tokens=64_000)

    assert budget.replay_compaction_tokens == 0


def test_default_context_compaction_is_capacity_only(monkeypatch):
    """Early lossy replay compaction must remain an explicit operator opt-in."""
    from nz_coder.foundation import config

    assert config.DEFAULT_CONTEXT_REPLAY_COMPACTION_TOKENS == 0
    monkeypatch.setattr(
        config,
        "CONTEXT_REPLAY_COMPACTION_TOKENS",
        config.DEFAULT_CONTEXT_REPLAY_COMPACTION_TOKENS,
    )

    budget = prompt_budget(context_tokens=1_000_000, output_tokens=64_000)

    assert budget.replay_compaction_tokens == 0


def test_request_estimate_includes_tool_schemas():
    messages = [{"role": "user", "content": "fix it"}]
    tools = [{
        "type": "function",
        "function": {
            "name": "large_tool",
            "description": "x" * 4000,
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    assert estimate_request_tokens(messages, tools) > estimate_request_tokens(messages)


def test_token_estimate_counts_cjk_as_characters_not_json_escape_bytes():
    """CJK input must not be inflated by ``\\uXXXX`` serialization escapes."""
    estimate = estimate_tokens("中" * 400)

    assert 390 <= estimate <= 420


def test_micro_compact_preserves_latest_two_batches_across_control_messages():
    large = "source line\n" * 1000
    messages = [
        {"role": "user", "content": "review the repository"},
        *_completed_tool_batch(
            {"role": "tool", "tool_call_id": "one", "content": large},
            {"role": "tool", "tool_call_id": "two", "content": large},
        ),
        {
            "role": "user",
            "content": "<tool-failure-diagnostic>internal</tool-failure-diagnostic>",
            "_nz_synthetic": True,
        },
        *_completed_tool_batch(
            {"role": "tool", "tool_call_id": "three", "content": large},
            {"role": "tool", "tool_call_id": "four", "content": large},
        ),
        {
            "role": "user",
            "content": "<verification-required>internal</verification-required>",
            "_nz_synthetic": True,
        },
    ]

    replaced = micro_compact(
        messages,
        budget=prompt_budget(context_tokens=4_000, output_tokens=1_000),
    )

    assert replaced == 0
    assert all(message["content"] == large for message in messages if message["role"] == "tool")


def test_micro_compact_prunes_only_old_batches_with_model_aware_budget():
    large = "source line\n" * 1000
    old_tool = {"role": "tool", "tool_call_id": "old", "content": large}
    recent_tool = {"role": "tool", "tool_call_id": "recent", "content": large}
    current_tool = {"role": "tool", "tool_call_id": "current", "content": large}
    messages = [
        {"role": "user", "content": "old task"},
        *_completed_tool_batch(old_tool),
        {"role": "user", "content": "recent task"},
        *_completed_tool_batch(recent_tool),
        {"role": "user", "content": "current task"},
        *_completed_tool_batch(current_tool),
    ]

    replaced = micro_compact(
        messages,
        budget=prompt_budget(context_tokens=4_000, output_tokens=1_000),
    )

    assert replaced == 1
    assert "Do not repeat the identical call" in old_tool["content"]
    assert recent_tool["content"] == large
    assert current_tool["content"] == large


def test_micro_compact_ignores_corrupt_persisted_assistant_timestamp():
    """A hand-edited Session timestamp must not break every later request."""
    large = "source line\n" * 1000
    messages = [
        {"role": "user", "content": "old"},
        *_completed_tool_batch({"role": "tool", "tool_call_id": "old", "content": large}),
        {"role": "user", "content": "recent"},
        *_completed_tool_batch({"role": "tool", "tool_call_id": "recent", "content": "small"}),
        {"role": "user", "content": "current"},
        *_completed_tool_batch({"role": "tool", "tool_call_id": "current", "content": "small"}),
        {"role": "assistant", "content": "working", "_timestamp": "broken"},
    ]

    replaced = micro_compact(
        messages,
        budget=prompt_budget(context_tokens=4_000, output_tokens=1_000),
    )

    assert replaced == 1


@pytest.mark.parametrize("steps", [40, 60])
@pytest.mark.parametrize("idle", [False, True])
def test_micro_compact_cleans_old_reads_during_one_long_user_request(steps, idle):
    """A single autonomous task must not protect every old read indefinitely."""
    raw = "source line\n" * 1000
    messages = [{"role": "user", "content": "Fix the parser and preserve all existing requirements."}]
    results = []
    for index in range(steps):
        result = {"role": "tool", "tool_call_id": f"read-{index}", "content": raw}
        results.append(result)
        messages.extend(_completed_tool_batch(result))
    if idle:
        messages[-2]["_timestamp"] = 1
    original_non_tools = copy.deepcopy([m for m in messages if m["role"] != "tool"])

    replaced = micro_compact(messages, budget=prompt_budget(100_000, 8_000))

    assert 0 < replaced <= steps - 2
    assert results[0]["content"] != raw
    assert all(result["content"] == raw for result in results[-2:])
    assert [m for m in messages if m["role"] != "tool"] == original_non_tools
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [f"read-{i}" for i in range(steps)]


def test_micro_compact_counts_parallel_calls_as_one_batch():
    """The newest two multi-tool batches must keep every result, not just the last two calls."""
    raw = "source line\n" * 1000
    messages = [{"role": "user", "content": "Inspect the parser."}]
    old_results = []
    for index in range(3):
        result = {"role": "tool", "tool_call_id": f"old-{index}", "content": raw}
        old_results.append(result)
        messages.extend(_completed_tool_batch(result))
    recent_results = []
    for batch in range(2):
        results = [{"role": "tool", "tool_call_id": f"recent-{batch}-{i}", "content": raw} for i in range(3)]
        recent_results.extend(results)
        # 内部诊断和纯文本回复不应当被算成新的工具调用批次。
        messages.extend(_completed_tool_batch(*results))
        messages.append({"role": "user", "content": "<tool-failure-diagnostic>context only</tool-failure-diagnostic>",
                         "_nz_synthetic": True})
        messages.append({"role": "assistant", "content": "Continue inspecting."})

    replaced = micro_compact(messages, budget=prompt_budget(4_000, 1_000))

    assert replaced == 3
    assert all(result["content"] != raw for result in old_results)
    assert all(result["content"] == raw for result in recent_results)


def test_micro_compact_preserves_partial_batch_in_addition_to_two_complete_batches():
    """Receiving one parallel result must not age a completed batch or clear the partial result."""
    raw = "source line\n" * 1000
    messages = [{"role": "user", "content": "Inspect the parser."}]
    completed = []
    for index in range(4):
        result = {"role": "tool", "tool_call_id": f"done-{index}", "content": raw}
        completed.append(result)
        messages.extend(_completed_tool_batch(result))
    partial = {"role": "tool", "tool_call_id": "partial-first", "content": raw}
    pending = {"role": "tool", "tool_call_id": "partial-second", "content": raw}
    messages.extend(_completed_tool_batch(partial, pending)[:-1])

    replaced = micro_compact(messages, budget=prompt_budget(4_000, 1_000))

    assert replaced == 2
    assert all(result["content"] != raw for result in completed[:2])
    assert all(result["content"] == raw for result in completed[-2:])
    assert partial["content"] == raw


def test_micro_compact_keeps_older_unfinished_batch_after_later_complete_batches():
    """An interrupted batch must not become eligible solely because later work completed."""
    raw = "source line\n" * 1000
    partial = {"role": "tool", "tool_call_id": "interrupted-first", "content": raw}
    pending = {"role": "tool", "tool_call_id": "interrupted-second", "content": raw}
    messages = [
        {"role": "user", "content": "Inspect the parser."},
        *_completed_tool_batch(partial, pending)[:-1],
        {"role": "user", "content": "Resume after interruption."},
        {"role": "user", "content": "Continue checking."},
    ]
    completed = []
    for index in range(3):
        result = {"role": "tool", "tool_call_id": f"later-{index}", "content": raw}
        completed.append(result)
        messages.extend(_completed_tool_batch(result))

    replaced = micro_compact(messages, budget=prompt_budget(4_000, 1_000))

    assert replaced == 1
    assert partial["content"] == raw
    assert completed[0]["content"] != raw
    assert all(result["content"] == raw for result in completed[-2:])


def test_micro_compact_does_not_treat_user_followups_as_completed_tool_batches():
    """New user messages cannot make an unfinished batch eligible for cleanup."""
    raw = "source line\n" * 1000
    partial = {"role": "tool", "tool_call_id": "first", "content": raw}
    pending = {"role": "tool", "tool_call_id": "second", "content": raw}
    messages = [
        {"role": "user", "content": "Inspect the parser."},
        *_completed_tool_batch(partial, pending)[:-1],
        {"role": "user", "content": "Continue."},
        {"role": "user", "content": "Check progress."},
    ]

    assert micro_compact(messages, budget=prompt_budget(4_000, 1_000)) == 0
    assert partial["content"] == raw


def test_micro_compact_matches_reused_call_ids_with_their_own_batch():
    """A call ID reused after a retry must not protect every older result with that ID."""
    raw = "source line\n" * 1000
    messages = [{"role": "user", "content": "Inspect the parser."}]
    results = []
    for _ in range(3):
        result = {"role": "tool", "tool_call_id": "reused-call", "content": raw}
        results.append(result)
        messages.extend(_completed_tool_batch(result))

    assert micro_compact(messages, budget=prompt_budget(4_000, 1_000)) == 1
    assert results[0]["content"] != raw
    assert all(result["content"] == raw for result in results[-2:])


def test_micro_compact_keeps_old_batches_inside_the_token_protection_budget():
    """Crossing the two-batch boundary alone must not trigger cleanup."""
    raw = "source line\n" * 50
    messages = [{"role": "user", "content": "Inspect the parser."}]
    for index in range(4):
        messages.extend(_completed_tool_batch({"role": "tool", "tool_call_id": str(index), "content": raw}))

    assert micro_compact(messages, budget=prompt_budget(4_000, 1_000)) == 0
    assert all(m["content"] == raw for m in messages if m["role"] == "tool")


@pytest.mark.parametrize("idle", [False, True])
@pytest.mark.parametrize("failure_kind", ["verification", "error_prefix"])
def test_micro_compact_keeps_structured_failures_and_change_evidence(idle, failure_kind):
    """Age-based cleanup must not erase failure or mutation facts lacking traceback text."""
    import time

    raw = "diagnostic detail\n" * 1000
    read_result = {"role": "tool", "tool_call_id": "read", "content": raw,
                   "_nz_evidence_kind": "file_read", "_nz_resource": "src/parser.py"}
    write_result = {"role": "tool", "tool_call_id": "edit", "content": raw,
                    "_nz_evidence_kind": "file_write", "_nz_mutated_resources": ["src/parser.py"]}
    failure_result = {"role": "tool", "tool_call_id": "verify", "content": raw,
                      "_nz_evidence_kind": "verification", "_nz_verification_passed": False}
    if failure_kind == "error_prefix":
        failure_result = {"role": "tool", "tool_call_id": "read-error", "content": "Error: " + raw}
    original_failure = failure_result["content"]
    messages = [
        {"role": "user", "content": "Fix parser.py."},
        *_completed_tool_batch(read_result),
        *_completed_tool_batch(write_result, tool_name="edit_file"),
        *_completed_tool_batch(failure_result, tool_name="verify_changed_files"),
        {"role": "user", "content": "Continue the correction."},
        *_completed_tool_batch({"role": "tool", "tool_call_id": "recent", "content": "small"}),
        {"role": "user", "content": "Check the final result."},
        *_completed_tool_batch({"role": "tool", "tool_call_id": "current", "content": "small"}),
    ]
    if idle:
        messages.append({"role": "assistant", "content": "Continue checking.",
                         "_timestamp": time.time() - 3600})

    replaced = micro_compact(messages, budget=prompt_budget(4_000, 1_000))

    assert replaced == 1
    assert read_result["content"] != raw
    assert write_result["content"] == raw
    assert failure_result["content"] == original_failure
    assert "read-only" in read_result["content"]
    assert "state-changing" in read_result["content"]


def test_micro_compact_keeps_existing_artifact_readable_without_its_preview(tmp_path, monkeypatch):
    """Pruning a large-result preview must keep its opaque address usable and compact only once."""
    import re
    from nz_coder.state import context as context_module
    from nz_coder.tools.artifacts import read_tool_result

    monkeypatch.setattr(context_module, "TRIGGER_CHARS", 1)
    monkeypatch.setattr(context_module, "PREVIEW_CHARS", 4_000)
    raw = "UNIQUE_ORIGINAL_OUTPUT\n" + "raw output detail\n" * 1000
    with scoped_workdir(tmp_path), scoped_session("compact-artifact-handle"):
        visible = persist_large_output("large-read", raw)
        artifact_id = re.search(r"artifact_[a-f0-9]{32}", visible).group()
        result = {"role": "tool", "tool_call_id": "large-read", "content": visible}
        messages = [
            {"role": "user", "content": "Inspect the source."},
            *_completed_tool_batch(result),
            {"role": "user", "content": "Continue."},
            *_completed_tool_batch({"role": "tool", "tool_call_id": "recent", "content": "small"}),
            {"role": "user", "content": "Verify."},
            *_completed_tool_batch({"role": "tool", "tool_call_id": "current", "content": "small"}),
        ]

        assert micro_compact(messages, budget=prompt_budget(4_000, 1_000)) == 1
        assert artifact_id in result["content"]
        assert "read_tool_result" in result["content"]
        assert "UNIQUE_ORIGINAL_OUTPUT" not in result["content"]
        assert len(result["content"]) < len(visible)
        assert "UNIQUE_ORIGINAL_OUTPUT" in read_tool_result(artifact_id, max_bytes=128)
        assert micro_compact(messages, budget=prompt_budget(4_000, 1_000)) == 0


@pytest.mark.parametrize("kind", ["file_write", "failed_verification", "passed_verification", "artifact"])
def test_summary_input_prioritizes_existing_evidence_over_repeatable_reads(kind):
    """Recent source dumps must not consume the entire summary budget before known evidence."""
    from nz_coder.state.context import _select_summary_input

    critical = {"role": "tool", "tool_call_id": "critical",
                "content": "IMPORTANT_EVIDENCE " + "diagnostic detail " * 25}
    if kind == "file_write":
        critical.update(_nz_evidence_kind="file_write", _nz_mutated_resources=["src/parser.py"])
    elif kind in {"failed_verification", "passed_verification"}:
        critical.update(_nz_evidence_kind="verification",
                        _nz_verification_passed=kind == "passed_verification")
    else:
        critical["content"] += "\nFull output artifact: artifact_0123456789abcdef0123456789abcdef"
    head = [
        {"role": "user", "content": "Fix src/parser.py."}, critical,
        {"role": "tool", "tool_call_id": "recent-read", "content": "read source " * 120,
         "_nz_evidence_kind": "file_read", "_nz_resource": "src/parser.py"},
    ]
    original = copy.deepcopy(head)

    selected = _select_summary_input(head, max_tokens=500)

    assert any("IMPORTANT_EVIDENCE" in str(m.get("content", "")) for m in selected)
    assert selected[0]["content"] == "Fix src/parser.py."
    assert estimate_tokens(selected) <= 500
    assert all(not key.startswith("_nz_") for message in selected for key in message)
    assert head == original


def test_summary_input_prefers_known_failure_over_a_newer_artifact_preview():
    """A recent large read with an artifact must not outrank a known failure."""
    from nz_coder.state.context import _select_summary_input

    failure = {"role": "tool", "tool_call_id": "failed-test", "content": "KEEP_FAILURE " + "detail " * 70,
               "_nz_evidence_kind": "verification", "_nz_verification_passed": False}
    head = [
        {"role": "user", "content": "Fix parser.py."}, failure,
        {"role": "tool", "tool_call_id": "recent-read", "content": "read source " * 120
         + "\nartifact_0123456789abcdef0123456789abcdef"},
    ]

    selected = _select_summary_input(head, max_tokens=500)

    assert any("KEEP_FAILURE" in str(m.get("content", "")) for m in selected)


def test_micro_compact_retains_incomplete_capture_qualification(tmp_path):
    """Keeping an artifact address must not make an incomplete capture appear complete."""
    from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector

    with scoped_workdir(tmp_path), scoped_session("compact-partial-artifact"):
        projected = ToolResultProjector(budget=ToolResultBudget(512)).project(
            "partial-output", "partial progress\n" * 1000, artifact_complete=False,
        )
        result = {"role": "tool", "tool_call_id": "partial-output", "content": projected.text}
        messages = [
            {"role": "user", "content": "Inspect progress."},
            *_completed_tool_batch(result),
            {"role": "user", "content": "Continue."},
            *_completed_tool_batch({"role": "tool", "tool_call_id": "recent", "content": "small"}),
            {"role": "user", "content": "Verify."},
            *_completed_tool_batch({"role": "tool", "tool_call_id": "current", "content": "small"}),
            {"role": "assistant", "content": "Checking.", "_timestamp": 1},
        ]

        assert micro_compact(messages, budget=prompt_budget(4_000, 1_000)) == 1

    assert projected.artifact_path in result["content"]
    assert "incomplete" in result["content"]


def test_oversized_user_input_is_persisted_with_readable_reference(tmp_path):
    messages = [{"role": "user", "content": "x" * 12_000}]

    with scoped_workdir(tmp_path), scoped_session("context-budget"):
        count = persist_oversized_user_inputs(messages, max_tokens=1_000)
        from nz_coder.tool_platform.artifacts import ArtifactStore

        store = ArtifactStore(tmp_path, "context-budget")
        artifact_id = next(iter(store._load_manifest()["entries"]))
    assert count == 1
    assert store.read(artifact_id) == "x" * 12_000
    assert "<oversized-user-input>" in messages[0]["content"]
    assert artifact_id in messages[0]["content"]
    assert str(store.directory) not in messages[0]["content"]


def test_reused_tool_call_id_does_not_alias_different_large_outputs(
    tmp_path,
    monkeypatch,
):
    """Durable history references must remain content-stable across retries."""
    from nz_coder.state import context as context_module

    monkeypatch.setattr(context_module, "TRIGGER_CHARS", 1)
    with scoped_workdir(tmp_path), scoped_session("duplicate-tool-call"):
        first = persist_large_output("same-call", "first-output")
        second = persist_large_output("same-call", "second-output")

        from nz_coder.tool_platform.artifacts import ArtifactStore

        store = ArtifactStore(tmp_path, "duplicate-tool-call")
    persisted = sorted(store.directory.glob("artifact_*.txt"))
    assert len(persisted) == 2
    assert {path.read_text(encoding="utf-8") for path in persisted} == {
        "first-output",
        "second-output",
    }
    assert first != second


def test_agent_reports_when_fixed_tool_schema_cannot_fit_after_compaction(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution import loop as loop_module
    from nz_coder.runtime.execution.loop import AgentLoop

    monkeypatch.setattr(config, "MAX_CONTEXT_TOKENS", 12_000)
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 2_000)
    monkeypatch.setattr(config, "SYSTEM_CONTEXT_BUDGET_TOKENS", 1_000)
    monkeypatch.setattr(config, "MODEL_ID", "gpt-test")
    monkeypatch.setattr(
        loop_module,
        "get_specs",
        lambda: [{
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "x" * 40_000,
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )
    compact_calls = []

    def fake_compact(messages, client, model, **_kwargs):
        compact_calls.append(list(messages))
        return [{"role": "user", "content": "compacted"}]

    monkeypatch.setattr(loop_module, "auto_compact", fake_compact)

    class Tracer:
        def log(self, event, **payload):
            return None

    agent = AgentLoop.__new__(AgentLoop)
    agent.system_prompt = "system"
    agent.client = object()
    agent._model_gateway_observer_for_run = lambda: None
    agent.tracer = Tracer()
    from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot, message_version
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    agent._freeze_api_request = lambda rows: FrozenRequestSnapshot(
        "system", "", "", message_version(rows), project_provider_messages,
        tuple(agent._active_tool_specs()),
    )
    messages = [{"role": "user", "content": "small history"}]

    with pytest.raises(RuntimeError, match="cannot shrink"):
        agent._compact_if_needed(messages)

    assert compact_calls
    assert messages == [{"role": "user", "content": "small history"}]


def test_agent_soft_preflight_does_not_call_summary_model(monkeypatch):
    """InfCode cleans at 85% but compacts only after the usable hard limit."""
    from nz_coder.runtime.execution.loop import AgentLoop

    class Tracer:
        def __init__(self):
            self.events = []

        def log(self, event, **payload):
            self.events.append((event, payload))

    agent = AgentLoop.__new__(AgentLoop)
    agent.tracer = Tracer()
    agent._prompt_budget = lambda: prompt_budget(10_000, 2_000)
    estimates = iter([7_000, 7_200])
    agent._projected_request_tokens = lambda _messages: next(estimates)
    agent._compact_messages = lambda _messages: (_ for _ in ()).throw(
        AssertionError("soft preflight must not invoke summary compaction")
    )
    messages = [{"role": "user", "content": "review"}]

    agent._compact_if_needed(messages)

    assert messages == [{"role": "user", "content": "review"}]
    assert any(event == "context_preflight_over_soft" for event, _ in agent.tracer.events)


def test_compaction_tail_can_split_oversized_recent_turn_at_assistant_boundary():
    from nz_coder.state.context import _select_compaction_parts

    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "x" * 20_000},
        {"role": "assistant", "content": "latest concise result"},
    ]
    head, tail = _select_compaction_parts(
        messages,
        prompt_budget(context_tokens=8_000, output_tokens=2_000),
    )

    assert head[-1]["role"] == "user"
    assert tail == [{"role": "assistant", "content": "latest concise result"}]


def test_compaction_tail_preserves_recent_atomic_suffix_with_one_human_turn():
    """A long single-prompt agent run must not summarize its active tool batch."""
    from nz_coder.state.context import _select_compaction_parts

    recent_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "write-2"}],
    }
    recent_result = {
        "role": "tool",
        "tool_call_id": "write-2",
        "content": "recent verification evidence",
    }
    messages = [
        {"role": "user", "content": "implement the feature"},
        {"role": "assistant", "content": "old reasoning " + "x" * 16_000},
        {"role": "tool", "tool_call_id": "old", "content": "old output"},
        recent_assistant,
        recent_result,
    ]

    head, tail = _select_compaction_parts(
        messages,
        prompt_budget(context_tokens=8_000, output_tokens=2_000),
    )

    assert head == messages[:3]
    assert tail == [recent_assistant, recent_result]


def test_compaction_tail_ignores_durable_only_message_metadata():
    """Session parts must not crowd provider-visible recent evidence out of the tail."""
    from nz_coder.state.context import _select_compaction_parts

    recent_assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "write-2"}],
        "_nz_parts": [{"snapshot": "x" * 20_000}],
    }
    recent_result = {
        "role": "tool",
        "tool_call_id": "write-2",
        "content": "recent verification evidence",
        "_nz_parts": [{"durable": "y" * 20_000}],
    }
    messages = [
        {"role": "user", "content": "implement the feature"},
        {"role": "assistant", "content": "old reasoning " + "x" * 16_000},
        recent_assistant,
        recent_result,
    ]

    head, tail = _select_compaction_parts(
        messages,
        prompt_budget(context_tokens=8_000, output_tokens=2_000),
    )

    assert head == messages[:2]
    assert tail == [recent_assistant, recent_result]


def test_provider_reported_overflow_triggers_next_turn_compaction():
    from nz_coder.runtime.execution.loop import AgentLoop

    class Tracer:
        def __init__(self):
            self.events = []

        def log(self, event, **payload):
            self.events.append((event, payload))

    agent = AgentLoop.__new__(AgentLoop)
    agent.tracer = Tracer()
    from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot, message_version
    from nz_coder.runtime.conversation.message_projection import project_provider_messages
    agent._freeze_api_request = lambda rows: FrozenRequestSnapshot(
        "", "", "", message_version(rows), project_provider_messages, (),
    )
    agent._prompt_budget = lambda: prompt_budget(10_000, 2_000)
    agent._projected_request_tokens = lambda _messages: 4_000
    agent._compact_messages = lambda _messages: [{"role": "user", "content": "compacted"}]
    messages = [
        {"role": "user", "content": "task"},
        {
            "role": "assistant",
            "content": "result",
            "_nz_usage": {"input": 7_500, "output": 500, "total": 8_000},
        },
    ]

    agent._compact_if_needed(messages)

    assert messages == [{"role": "user", "content": "compacted"}]
    compact = next(payload for event, payload in agent.tracer.events if event == "compact")
    assert compact["trigger"] == "provider_usage"


def test_provider_usage_before_latest_compaction_boundary_is_not_reused():
    from nz_coder.runtime.execution.loop import _last_assistant_usage_total

    messages = [
        {
            "role": "user",
            "content": "<session-summary>done</session-summary>",
            "_nz_compaction": {"auto": True, "created_at": 100.0},
        },
        {
            "role": "assistant",
            "content": "preserved tail",
            "_timestamp": 99.0,
            "_nz_usage": {"total": 9_000},
        },
    ]

    assert _last_assistant_usage_total(messages) == 0
    messages.append({
        "role": "assistant",
        "content": "new result",
        "_timestamp": 101.0,
        "_nz_usage": {"total": 2_000},
    })
    assert _last_assistant_usage_total(messages) == 2_000


def test_corrupt_persisted_compaction_usage_degrades_without_crashing():
    """Context pressure survives nonfinite fields in an old Session record."""
    from nz_coder.runtime.execution.loop import _last_assistant_usage_total

    messages = [
        {
            "role": "user",
            "content": "summary",
            "_nz_compaction": {"created_at": float("nan")},
        },
        {
            "role": "assistant",
            "content": "answer",
            "_timestamp": float("inf"),
            "_nz_usage": {
                "total": float("nan"),
                "input": 120,
                "output": 30,
            },
        },
    ]

    assert _last_assistant_usage_total(messages) == 150


class _CompactionClient:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.request: dict = {}
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=self.summary), finish_reason="stop",
        )])


class _SequencedCompactionClient:
    def __init__(self, outcomes) -> None:
        self.outcomes = iter(outcomes)
        self.requests: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome), finish_reason="stop")],
        )


def test_auto_compact_reports_compaction_provider_usage(tmp_path, monkeypatch):
    """Compaction is a paid model call and must share the Agent usage ledger."""
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('keep working'))
    observed = []
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("compact-observer"):
        auto_compact(
            messages,
            client,
            "fake-model",
            observer=lambda name, payload: observed.append((name, payload)),
        )

    starts = [payload for name, payload in observed if name == "model_call_start"]
    finishes = [payload for name, payload in observed if name == "model_call_finish"]
    assert len(starts) == len(finishes) == 1
    assert starts[0]["purpose"] == "compaction"
    assert finishes[0]["purpose"] == "compaction"


def test_auto_compact_transcript_is_strict_json(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact
    from nz_coder.state.sessions import session_transcript_dir

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('keep working'))
    messages = [{
        "role": "user",
        "content": "old task",
        "_nz_extension": {"score": float("nan")},
    }]

    with scoped_workdir(tmp_path), scoped_session("compact-strict-json"):
        auto_compact(messages, client, "fake-model")
        transcript = next(session_transcript_dir().glob("transcript_*.jsonl"))

    restored = json.loads(
        transcript.read_text(encoding="utf-8").strip(),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )
    assert restored["_nz_extension"] == {"score": None}


def test_agent_compaction_threads_its_gateway_observer(monkeypatch):
    """The product host must not replace an observed Gateway with a silent one."""
    from nz_coder.runtime.execution import loop as loop_module
    from nz_coder.runtime.execution.loop import AgentLoop

    captured = {}

    def fake_compact(_messages, _client, _model, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(loop_module, "auto_compact", fake_compact)
    agent = AgentLoop.__new__(AgentLoop)
    agent.client = object()
    agent.provider = None
    agent._active_model_id = lambda: "fake-model"
    agent._prompt_budget = lambda: prompt_budget(10_000, 2_000)
    def observer(_name, _payload):
        return None

    agent._model_gateway_observer_for_run = lambda: observer

    agent._compact_messages([{"role": "user", "content": "summarize"}])

    assert captured["observer"] is observer


def test_auto_compact_honors_cancel_before_provider_dispatch(tmp_path, monkeypatch):
    """An interrupted terminal run must not start a 600-second summary call."""
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('unused'))
    cancelled = threading.Event()
    cancelled.set()

    with scoped_workdir(tmp_path), scoped_session("compact-cancelled"):
        with pytest.raises(asyncio.CancelledError):
            auto_compact(
                [{"role": "user", "content": "large history"}],
                client,
                "fake-model",
                cancel_event=cancelled,
            )

    assert client.request == {}


def test_auto_compact_preserves_recent_complete_turns(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('finish migration'))
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("compact-tail"):
        result = auto_compact(messages, client, "fake-model")

    assert "<session-summary>" in result[0]["content"]
    assert result[1:] == [messages[0], *messages[2:]]
    request_content = client.request["messages"][-1]["content"]
    assert '"findings":' in request_content
    assert "old task" in request_content
    assert "recent task" not in request_content


def test_auto_compact_summary_input_always_anchors_original_task(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact, prompt_budget

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('preserve the original task'))
    original_task = (
        "ORIGINAL TASK ANCHOR: repair the array stacking bug. "
        + ("preserve this requirement; " * 80)
    )
    messages = [{"role": "user", "content": original_task}]
    messages.extend(
        {
            "role": "assistant",
            "content": f"investigation evidence {index}: " + ("x" * 4_000),
        }
        for index in range(10)
    )

    with scoped_workdir(tmp_path), scoped_session("compact-task-anchor"):
        auto_compact(
            messages,
            client,
            "fake-model",
            budget=prompt_budget(context_tokens=10_000, output_tokens=2_000),
        )

    assert original_task in client.request["messages"][-1]["content"]


def test_auto_compact_uses_text_only_specialist_system_prompt(
    tmp_path,
    monkeypatch,
):
    """The compactor must not inherit coding-agent tool-call behavior."""
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('keep working'))

    with scoped_workdir(tmp_path), scoped_session("compact-specialist-prompt"):
        auto_compact(
            [{"role": "user", "content": "repair the parser"}],
            client,
            "fake-model",
        )

    system, user = client.request["messages"]
    assert system["role"] == "system"
    assert "JSON object" in system["content"]
    assert "Do NOT call any tools" in system["content"]
    assert user["role"] == "user"


def test_auto_compact_rejects_tool_protocol_without_installing_summary(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(
        '<｜｜DSML｜｜tool_calls>\n'
        '<｜｜DSML｜｜invoke name="read_file">'
    )
    original_task = "Repair concat so missing variables are preserved"
    messages = [
        {"role": "user", "content": original_task},
        {"role": "assistant", "content": "investigating"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    original = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("compact-invalid-summary"):
        with pytest.raises(RuntimeError, match="Invalid compaction summary"):
            auto_compact(messages, client, "fake-model")
    assert messages == original


def test_auto_compact_does_not_send_message_protocol_metadata(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('summary'))
    messages = [
        {
            "role": "user",
            "content": "old task",
            "_nz_message_id": "msg-private",
            "_nz_session_id": "compact-private",
            "_nz_parts": [{"text": "private part"}],
        },
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("compact-private"):
        auto_compact(messages, client, "fake-model")

    request_content = client.request["messages"][-1]["content"]
    assert "old task" in request_content
    assert "_nz_" not in request_content
    assert "private part" not in request_content


def test_auto_compact_updates_previous_anchored_summary(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('refreshed goal'))
    messages = [
        {"role": "user", "content": "<session-summary>\ndurable fact\n</session-summary>"},
        {"role": "user", "content": "new head fact"},
        {"role": "assistant", "content": "head result"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("compact-anchor"):
        result = auto_compact(messages, client, "fake-model")

    request_content = client.request["messages"][-1]["content"]
    assert "<previous-summary>" in request_content
    assert "durable fact" in request_content
    assert "new head fact" in request_content
    assert result[1:] == [messages[1], *messages[3:]]
    assert "refreshed goal" in result[0]["content"]


def test_auto_compact_uses_provider_capability_snapshot(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact
    from nz_coder.providers import OpenAICompatibleProvider, resolve_model_capabilities

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('summary'))
    provider = OpenAICompatibleProvider(
        api_key="key",
        base_url="https://example.test/v1",
        client_factory=lambda **_kwargs: client,
    )
    capabilities = resolve_model_capabilities(
        "openai-compatible",
        "gpt-5-codex",
        variant="high",
    )
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "recent"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("provider-compact"):
        auto_compact(
            messages,
            client,
            "gpt-5-codex",
            provider=provider,
            capabilities=capabilities,
            budget=prompt_budget(128_000, 32_000),
        )

    assert client.request["max_completion_tokens"] == 32_000
    assert client.request["reasoning_effort"] == "high"
    assert "max_tokens" not in client.request
    assert "_capabilities" not in client.request


def test_auto_compact_persists_structured_tail_boundary(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient(summary_json('summary'))
    messages = [
        {"role": "user", "content": "old", "_nz_message_id": "msg-old"},
        {"role": "assistant", "content": "old result", "_nz_message_id": "msg-old-answer"},
        {"role": "user", "content": "recent", "_nz_message_id": "msg-recent"},
        {"role": "assistant", "content": "recent result", "_nz_message_id": "msg-recent-answer"},
        {"role": "user", "content": "latest", "_nz_message_id": "msg-latest"},
        {"role": "assistant", "content": "latest result", "_nz_message_id": "msg-latest-answer"},
    ]

    with scoped_workdir(tmp_path), scoped_session("structured-compact"):
        result = auto_compact(messages, client, "fake-model", auto=True, overflow=True)
        archives = list(context_module.session_transcript_dir().glob("transcript_*.jsonl"))

    marker = result[0]["_nz_compaction"]
    assert marker["auto"] is True
    assert marker["overflow"] is True
    assert marker["tail_start_id"] == "msg-recent"
    assert marker["head_message_ids"] == ["msg-old", "msg-old-answer"]
    assert marker["archive"].startswith(
        "user-state://transcripts/structured-compact/"
    )
    assert len(archives) == 1
    assert '"msg-old"' in archives[0].read_text(encoding="utf-8")


def test_auto_compact_retries_once_only_after_payload_shrinks(tmp_path, monkeypatch):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("Request Entity Too Large: FUNCTION_PAYLOAD_TOO_LARGE"),
        summary_json('recovered summary'),
    ])
    large_output = "tool evidence\n" * 2_000
    tool_message = {
        "role": "tool",
        "tool_call_id": "old-call",
        "content": large_output,
    }
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "old-call",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]},
        tool_message,
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("compact-payload-retry"):
        result = auto_compact(messages, client, "fake-model")

    marker = result[0]["_nz_compaction"]["payload_recovery"]
    assert len(client.requests) == 2
    assert "tool evidence" in client.requests[0]["messages"][-1]["content"]
    assert "tool evidence" not in client.requests[1]["messages"][-1]["content"]
    assert "Older tool outputs" in client.requests[1]["messages"][-1]["content"]
    assert tool_message["content"] == large_output
    assert "_nz_tool_compacted_at" not in tool_message
    assert marker["retried"] is True
    assert marker["after_bytes"] < marker["before_bytes"]
    assert "recovered summary" in result[0]["content"]


def test_auto_compact_payload_recovery_degrades_only_tagged_expansion(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact
    from nz_coder.state.input_expansion import render_expanded_message

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("context length overflow"),
        summary_json('recovered expansion summary'),
    ])
    old_user = {
        "role": "user",
        "content": "keep this natural instruction",
        "_nz_user_text": "keep this natural instruction",
        "_nz_input_expansions": [{
            "kind": "file",
            "source": "large.txt",
            "resolved": True,
            "text": "expanded evidence\n" * 2_000,
        }],
    }
    render_expanded_message(old_user)
    messages = [
        old_user,
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    with scoped_workdir(tmp_path), scoped_session("compact-expansion-retry"):
        result = auto_compact(messages, client, "fake-model")

    record = old_user["_nz_input_expansions"][0]
    recovery = result[0]["_nz_compaction"]["payload_recovery"]
    assert len(client.requests) == 2
    assert recovery["degraded_input_expansions"] == 1
    assert "compacted" not in record
    assert "compactionReason" not in record
    assert old_user["content"].startswith("keep this natural instruction\n\n")
    assert "expanded evidence" in old_user["content"]
    assert "expanded evidence" not in client.requests[1]["messages"][-1]["content"]


def test_auto_compact_no_shrink_keeps_history_and_reports_error(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("maximum context length exceeded"),
    ])
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    original = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("compact-aggregate-error"):
        with pytest.raises(RuntimeError, match="maximum context length exceeded"):
            auto_compact(messages, client, "fake-model")
    assert len(client.requests) == 1
    assert messages == original


def test_auto_compact_second_overflow_preserves_history_after_single_retry(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("request entity too large"),
        RuntimeError("context window exceeded"),
    ])
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "tool", "tool_call_id": "old", "content": "x" * 10_000},
        {"role": "user", "content": "recent task"},
        {"role": "assistant", "content": "recent result"},
        {"role": "user", "content": "latest task"},
        {"role": "assistant", "content": "latest result"},
    ]

    original = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("compact-retry-error"):
        with pytest.raises(RuntimeError, match="context window exceeded"):
            auto_compact(messages, client, "fake-model")
    assert len(client.requests) == 2
    assert messages == original


def test_auto_compact_oversized_user_turn_stays_intact_when_provider_fails(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact, prompt_budget

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("context window exceeded"),
    ])
    messages = [{"role": "user", "content": "x" * 20_000}]

    original = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("compact-paste-error"):
        with pytest.raises(RuntimeError, match="context window exceeded"):
            auto_compact(
                messages,
                client,
                "fake-model",
                budget=prompt_budget(context_tokens=2_000, output_tokens=500),
            )
    assert len(client.requests) == 1
    assert messages == original


def test_auto_compact_without_shrink_or_safe_boundary_preserves_error(
    tmp_path,
    monkeypatch,
):
    from nz_coder.state import context as context_module
    from nz_coder.state.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    error = RuntimeError("request entity too large")
    client = _SequencedCompactionClient([error])

    with scoped_workdir(tmp_path), scoped_session("compact-visible-error"):
        with pytest.raises(RuntimeError, match="request entity too large"):
            auto_compact(
                [{"role": "user", "content": "small task"}],
                client,
                "fake-model",
            )

    assert len(client.requests) == 1
