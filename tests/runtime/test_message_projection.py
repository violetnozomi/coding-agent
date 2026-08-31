"""Behavioral tests for durable-to-provider message projection."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.conversation.message_projection import project_provider_messages


def _assistant(call_id: str, *, provider_authored: bool = True) -> dict:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": "apply_patch", "arguments": "{}"},
        }],
    }
    if provider_authored:
        message["_nz_usage"] = {"input": 10, "output": 5, "total": 15}
    else:
        message["_nz_synthetic"] = True
    return message


def _write_result(call_id: str, content: str = "FULL DIFF") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
        "_nz_evidence_kind": "file_write",
        "_nz_mutated_resources": ["src/app.py"],
        "_nz_mutation_generation": 3,
    }


def test_latest_write_result_stays_complete_until_provider_observes_it() -> None:
    messages = [
        _assistant("write-1"),
        _write_result("write-1"),
        _assistant("verify-1", provider_authored=False),
        {"role": "tool", "tool_call_id": "verify-1", "content": "ok"},
    ]

    projected = project_provider_messages(messages)

    assert projected[1]["content"] == "FULL DIFF"


def test_acknowledged_write_result_is_compacted_only_in_provider_projection() -> None:
    messages = [
        _assistant("write-1"),
        _write_result("write-1", "FULL DIFF\n" * 500),
        _assistant("read-1"),
        {"role": "tool", "tool_call_id": "read-1", "content": "current file"},
    ]
    durable = deepcopy(messages)
    stats: dict = {}

    projected = project_provider_messages(messages, projection_stats=stats)

    assert messages == durable
    assert projected[1]["content"] == (
        "[Successful write result omitted after the model observed it: "
        "src/app.py (mutation generation 3). The full result remains in the "
        "durable session; the current workspace is the source of truth.]"
    )
    assert stats["acknowledged_write_results_compacted"] == 1
    assert stats["acknowledged_write_tokens_saved"] > 0
    assert stats["tool_result_tokens_saved"] > 0


def test_current_batch_remains_complete_after_older_write_is_compacted() -> None:
    messages = [
        _assistant("write-1"),
        _write_result("write-1", "OLD FULL DIFF\n" * 200),
        _assistant("write-2"),
        {
            **_write_result("write-2", "CURRENT FULL DIFF"),
            "_nz_mutation_generation": 4,
        },
    ]

    projected = project_provider_messages(messages)

    assert projected[1]["content"].startswith("[Successful write result omitted")
    assert projected[3]["content"] == "CURRENT FULL DIFF"


def test_agent_traces_projection_savings_for_provider_request() -> None:
    messages = [
        _assistant("write-1"),
        _write_result("write-1", "FULL DIFF\n" * 500),
        _assistant("read-1"),
        {"role": "tool", "tool_call_id": "read-1", "content": "current file"},
    ]
    events = []
    host = SimpleNamespace(
        model_capabilities=None,
        tracer=SimpleNamespace(
            log=lambda event, **data: events.append((event, data)),
        ),
    )

    AgentLoop._sanitize_messages(host, messages)

    projection_events = [
        data for event, data in events
        if event == "context_evidence_projected"
    ]
    assert len(projection_events) == 1
    assert projection_events[0]["acknowledged_write_results_compacted"] == 1
    assert projection_events[0]["acknowledged_write_tokens_saved"] > 0
    assert projection_events[0]["tool_result_tokens_saved"] > 0


def test_provider_projection_repairs_partial_tool_batch_without_mutating_session():
    """Every wire request must satisfy tool-call/result adjacency itself."""
    assistant = _assistant("call-settled")
    assistant["tool_calls"].append({
        "id": "call-orphan",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{}"},
    })
    messages = [
        {"role": "user", "content": "inspect files"},
        assistant,
        {"role": "tool", "tool_call_id": "call-settled", "content": "ok"},
        {"role": "tool", "tool_call_id": "unknown-result", "content": "bad"},
        {"role": "user", "content": "continue"},
    ]
    durable = deepcopy(messages)

    projected = project_provider_messages(messages)

    assert messages == durable
    projected_assistant = next(
        message for message in projected if message["role"] == "assistant"
    )
    assert [call["id"] for call in projected_assistant["tool_calls"]] == [
        "call-settled",
    ]
    assert [
        message["tool_call_id"]
        for message in projected
        if message["role"] == "tool"
    ] == ["call-settled"]


def test_provider_projection_keeps_text_but_removes_unanswered_tail_call():
    messages = [
        {"role": "user", "content": "inspect app.py"},
        {
            "role": "assistant",
            "content": "Starting inspection.",
            "tool_calls": [{
                "id": "call-interrupted",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
    ]

    projected = project_provider_messages(messages)

    assert projected[-1]["content"] == "Starting inspection."
    assert "tool_calls" not in projected[-1]


def test_provider_projection_uses_wire_only_placeholder_for_orphan_only_turn():
    """Orphan cleanup must preserve the assistant slot without polluting history."""
    messages = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-interrupted",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "user", "content": "continue after interrupt"},
    ]
    durable = deepcopy(messages)
    stats: dict = {}

    projected = project_provider_messages(messages, projection_stats=stats)

    assert messages == durable
    assert [message["role"] for message in projected] == [
        "user", "assistant", "user",
    ]
    assert projected[1]["content"] == "..."
    assert "tool_calls" not in projected[1]
    assert stats["empty_assistant_placeholders"] == 1


def test_agent_traces_provider_history_repair_without_token_savings():
    """Protocol repair is observable even when it does not reduce tokens."""
    messages = [
        {"role": "user", "content": "start"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-interrupted",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
        {"role": "user", "content": "continue"},
    ]
    events = []
    host = SimpleNamespace(
        model_capabilities=None,
        tracer=SimpleNamespace(
            log=lambda event, **data: events.append((event, data)),
        ),
    )

    AgentLoop._sanitize_messages(host, messages)

    repairs = [
        data for event, data in events
        if event == "context_evidence_projected"
    ]
    assert len(repairs) == 1
    assert repairs[0]["orphan_tool_calls_removed"] == 1
    assert repairs[0]["empty_assistant_placeholders"] == 1


def test_provider_projection_tolerates_corrupt_mutation_generation_metadata():
    """A damaged persisted evidence counter cannot prevent Session recovery."""
    messages = [
        _assistant("write-1"),
        {
            **_write_result("write-1", "FULL DIFF"),
            "_nz_mutation_generation": float("nan"),
        },
        _assistant("read-1"),
        {"role": "tool", "tool_call_id": "read-1", "content": "current file"},
    ]

    projected = project_provider_messages(messages)

    assert projected[1]["content"].startswith(
        "[Successful write result omitted after the model observed it:"
    )
    assert "mutation generation 0" in projected[1]["content"]
