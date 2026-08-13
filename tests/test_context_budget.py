"""Tests for model-window-aware context budgeting and overflow persistence."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nz_coder.context import (
    estimate_request_tokens,
    micro_compact,
    persist_oversized_user_inputs,
    prompt_budget,
)
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.sessions import scoped_session


def test_prompt_budget_reserves_small_window_output_proportionally():
    budget = prompt_budget(context_tokens=64_000, output_tokens=32_000)

    assert budget.output_reserve_tokens == 16_000
    assert budget.usable_input_tokens == 48_000
    assert budget.soft_preflight_tokens == 40_800
    assert budget.expansion_budget_tokens == 7_200
    assert budget.tool_prune_protect_tokens == 12_000
    assert budget.tool_prune_minimum_tokens == 4_800


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


def test_micro_compact_preserves_all_results_in_current_two_user_turns():
    large = "source line\n" * 1000
    messages = [
        {"role": "user", "content": "review the repository"},
        {"role": "assistant", "content": "", "tool_calls": []},
        {"role": "tool", "tool_call_id": "one", "content": large},
        {"role": "tool", "tool_call_id": "two", "content": large},
        {
            "role": "user",
            "content": "<tool-failure-diagnostic>internal</tool-failure-diagnostic>",
            "_nz_synthetic": True,
        },
        {"role": "tool", "tool_call_id": "three", "content": large},
        {
            "role": "user",
            "content": "<verification-required>internal</verification-required>",
            "_nz_synthetic": True,
        },
        {"role": "tool", "tool_call_id": "four", "content": large},
    ]

    replaced = micro_compact(
        messages,
        budget=prompt_budget(context_tokens=4_000, output_tokens=1_000),
    )

    assert replaced == 0
    assert all(message["content"] == large for message in messages if message["role"] == "tool")


def test_micro_compact_prunes_only_old_turns_with_model_aware_budget():
    large = "source line\n" * 1000
    old_tool = {"role": "tool", "tool_call_id": "old", "content": large}
    recent_tool = {"role": "tool", "tool_call_id": "recent", "content": large}
    current_tool = {"role": "tool", "tool_call_id": "current", "content": large}
    messages = [
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "", "tool_calls": []},
        old_tool,
        {"role": "user", "content": "recent task"},
        recent_tool,
        {"role": "user", "content": "current task"},
        current_tool,
    ]

    replaced = micro_compact(
        messages,
        budget=prompt_budget(context_tokens=4_000, output_tokens=1_000),
    )

    assert replaced == 1
    assert "Do not repeat the identical call" in old_tool["content"]
    assert recent_tool["content"] == large
    assert current_tool["content"] == large


def test_oversized_user_input_is_persisted_with_readable_reference(tmp_path):
    messages = [{"role": "user", "content": "x" * 12_000}]

    with scoped_workdir(tmp_path), scoped_session("context-budget"):
        count = persist_oversized_user_inputs(messages, max_tokens=1_000)

    input_dir = (
        tmp_path
        / ".nz-coder"
        / "sessions"
        / "_artifacts"
        / "context-budget"
        / "runtime"
        / "user-inputs"
    )
    persisted = list(input_dir.glob("*.txt"))
    assert count == 1
    assert len(persisted) == 1
    assert persisted[0].read_text(encoding="utf-8") == "x" * 12_000
    assert "<oversized-user-input>" in messages[0]["content"]
    assert str(persisted[0].relative_to(tmp_path)) in messages[0]["content"]


def test_agent_compacts_when_tool_schema_pushes_full_request_over_budget(monkeypatch):
    from nz_coder import config
    from nz_coder.runtime import loop as loop_module
    from nz_coder.runtime.loop import AgentLoop

    monkeypatch.setattr(config, "MAX_CONTEXT_TOKENS", 12_000)
    monkeypatch.setattr(config, "MAX_OUTPUT_TOKENS", 2_000)
    monkeypatch.setattr(config, "SYSTEM_CONTEXT_BUDGET_TOKENS", 1_000)
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
    agent.tracer = Tracer()
    messages = [{"role": "user", "content": "small history"}]

    agent._compact_if_needed(messages)

    assert compact_calls
    assert messages == [{"role": "user", "content": "compacted"}]


def test_agent_soft_preflight_does_not_call_summary_model(monkeypatch):
    """InfCode cleans at 85% but compacts only after the usable hard limit."""
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder.context import _select_compaction_parts

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


def test_provider_reported_overflow_triggers_next_turn_compaction():
    from nz_coder.runtime.loop import AgentLoop

    class Tracer:
        def __init__(self):
            self.events = []

        def log(self, event, **payload):
            self.events.append((event, payload))

    agent = AgentLoop.__new__(AgentLoop)
    agent.tracer = Tracer()
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
    from nz_coder.runtime.loop import _last_assistant_usage_total

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


class _CompactionClient:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.request: dict = {}
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.request = kwargs
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.summary))])


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
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))],
        )


def test_auto_compact_preserves_recent_complete_turns(tmp_path, monkeypatch):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.sessions import scoped_session

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient("## Goal\n- finish migration")
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
    assert result[1:] == messages[2:]
    request_content = client.request["messages"][0]["content"]
    assert "## Critical Context" in request_content
    assert "old task" in request_content
    assert "recent task" not in request_content


def test_auto_compact_does_not_send_message_protocol_metadata(tmp_path, monkeypatch):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.sessions import scoped_session

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient("summary")
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

    request_content = client.request["messages"][0]["content"]
    assert "old task" in request_content
    assert "_nz_" not in request_content
    assert "private part" not in request_content


def test_auto_compact_updates_previous_anchored_summary(tmp_path, monkeypatch):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.sessions import scoped_session

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient("## Goal\n- refreshed goal")
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

    request_content = client.request["messages"][0]["content"]
    assert "<previous-summary>" in request_content
    assert "durable fact" in request_content
    assert "new head fact" in request_content
    assert result[1:] == messages[3:]
    assert "refreshed goal" in result[0]["content"]


def test_auto_compact_uses_provider_capability_snapshot(tmp_path, monkeypatch):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact
    from nz_coder.providers import OpenAICompatibleProvider, resolve_model_capabilities

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient("summary")
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
        )

    assert client.request["max_completion_tokens"] == 4000
    assert client.request["reasoning_effort"] == "high"
    assert "max_tokens" not in client.request
    assert "_capabilities" not in client.request


def test_auto_compact_persists_structured_tail_boundary(tmp_path, monkeypatch):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _CompactionClient("summary")
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

    marker = result[0]["_nz_compaction"]
    assert marker["auto"] is True
    assert marker["overflow"] is True
    assert marker["tail_start_id"] == "msg-recent"
    assert marker["head_message_ids"] == ["msg-old", "msg-old-answer"]
    archive = tmp_path / marker["archive"]
    assert archive.is_file()
    assert '"msg-old"' in archive.read_text(encoding="utf-8")


def test_auto_compact_retries_once_only_after_payload_shrinks(tmp_path, monkeypatch):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("Request Entity Too Large: FUNCTION_PAYLOAD_TOO_LARGE"),
        "recovered summary",
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
    assert "tool evidence" in client.requests[0]["messages"][0]["content"]
    assert "tool evidence" not in client.requests[1]["messages"][0]["content"]
    assert "Older tool outputs" in client.requests[1]["messages"][0]["content"]
    assert tool_message["_nz_tool_compacted_at"] > 0
    assert marker["retried"] is True
    assert marker["after_bytes"] < marker["before_bytes"]
    assert "recovered summary" in result[0]["content"]


def test_auto_compact_payload_recovery_degrades_only_tagged_expansion(
    tmp_path,
    monkeypatch,
):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact
    from nz_coder.state.input_expansion import render_expanded_message

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("context length overflow"),
        "recovered expansion summary",
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
    assert record["compacted"] is True
    assert record["compactionReason"] == "compaction-failed"
    assert old_user["content"].startswith("keep this natural instruction\n\n")
    assert "expanded evidence" not in old_user["content"]


def test_auto_compact_skips_retry_and_drops_aggregate_head_at_safe_tail(
    tmp_path,
    monkeypatch,
):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact

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

    with scoped_workdir(tmp_path), scoped_session("compact-aggregate-fallback"):
        result = auto_compact(messages, client, "fake-model")

    recovery = result[0]["_nz_compaction"]["payload_recovery"]
    assert len(client.requests) == 1
    assert recovery["retried"] is False
    assert recovery["fallback"] == "aggregate-head"
    assert "Earlier conversation history was omitted" in result[0]["content"]
    assert result[1:] == messages[2:]


def test_auto_compact_second_overflow_falls_back_after_single_retry(
    tmp_path,
    monkeypatch,
):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact

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

    with scoped_workdir(tmp_path), scoped_session("compact-retry-fallback"):
        result = auto_compact(messages, client, "fake-model")

    recovery = result[0]["_nz_compaction"]["payload_recovery"]
    assert len(client.requests) == 2
    assert recovery["retried"] is True
    assert recovery["fallback"] == "aggregate-head"
    assert "Earlier conversation history was omitted" in result[0]["content"]


def test_auto_compact_oversized_user_turn_uses_placeholder_without_retry(
    tmp_path,
    monkeypatch,
):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact, prompt_budget

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = _SequencedCompactionClient([
        RuntimeError("context window exceeded"),
    ])
    messages = [{"role": "user", "content": "x" * 20_000}]

    with scoped_workdir(tmp_path), scoped_session("compact-paste-fallback"):
        result = auto_compact(
            messages,
            client,
            "fake-model",
            budget=prompt_budget(context_tokens=2_000, output_tokens=500),
        )

    recovery = result[0]["_nz_compaction"]["payload_recovery"]
    assert len(client.requests) == 1
    assert recovery["fallback"] == "oversized-user-turn"
    assert "over-long pasted block" in result[0]["content"]


def test_auto_compact_without_shrink_or_safe_boundary_preserves_error(
    tmp_path,
    monkeypatch,
):
    from nz_coder import context as context_module
    from nz_coder.context import auto_compact

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
