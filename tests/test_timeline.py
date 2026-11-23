"""Tests for terminal Session timeline and saved-session projections."""
from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from nz_coder.interface import timeline
from nz_coder.protocol.message_schema import ensure_message_identities, legacy_messages


def _history():
    return [
        {"role": "user", "content": "Fix the parser"},
        {
            "role": "assistant",
            "content": "I will inspect it.",
            "tool_calls": [{"function": {"name": "read_file"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "source"},
        {"role": "assistant", "content": "Parser fixed."},
        {"role": "user", "content": "<reminder>save useful memory</reminder>"},
        {"role": "user", "content": "Add regression tests"},
        {
            "role": "assistant",
            "content": "Tests added.",
            "tool_calls": [{"function": {"name": "bash"}}],
        },
    ]


def test_conversation_turns_hide_synthetic_user_messages():
    turns = timeline.conversation_turns(_history())

    assert len(turns) == 2
    assert turns[0].user_text == "Fix the parser"
    assert turns[0].assistant_text == "Parser fixed."
    assert turns[0].tools == ("read_file",)
    assert turns[0].end == 5
    assert turns[1].user_text == "Add regression tests"
    assert turns[1].tools == ("bash",)


def test_latest_assistant_text_prefers_visible_typed_text_parts():
    messages = [
        {"role": "assistant", "content": "old"},
        {
            "role": "assistant",
            "content": "legacy fallback",
            "_nz_parts": [
                {"type": "text", "text": "visible"},
                {"type": "text", "text": "hidden", "ignored": True},
                {"type": "reasoning", "text": "private"},
            ],
        },
    ]

    assert timeline.latest_assistant_text(messages) == "visible"


def test_terminal_transcript_hides_non_text_structured_content():
    document = timeline.build_transcript_document(
        "session-structured",
        [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": {"internal": [1, 2, 3]}},
        ],
    )

    rendered = document.markdown()
    assert "internal" not in rendered
    assert "[1, 2, 3]" not in rendered


def test_fork_history_keeps_complete_turn_and_returns_deep_copy():
    history = _history()

    forked = timeline.fork_history(history, 1)

    assert forked == history[:5]
    forked[0]["content"] = "changed"
    assert history[0]["content"] == "Fix the parser"


def test_fork_history_rejects_invalid_turn():
    with pytest.raises(ValueError, match="between 1 and 2"):
        timeline.fork_history(_history(), 3)


def test_history_controls_do_not_take_turn_numbers_or_become_clickable_user_messages():
    messages = [
        {"role": "user", "content": "mixed future summary", "_nz_compaction": {}},
        {"role": "user", "content": "Explain the <session-summary> element.", "_nz_message_id": "msg-first"},
        {"role": "user", "content": "run continuation", "_nz_continuation": {}},
        {"role": "user", "content": [{"type": "text", "text": "again"}], "_nz_message_id": "msg-second"},
    ]
    turns = timeline.conversation_turns(messages)
    assert [(turn.number, turn.message_id) for turn in turns] == [(1, "msg-first"), (2, "msg-second")]
    document = timeline.build_transcript_document("parent", messages)
    assert [block.message_id for block in document.blocks] == ["msg-first", "msg-second"]
    assert "Explain the <session-summary> element." in document.markdown()
    with pytest.raises(ValueError, match="must be restored"):
        timeline.fork_history(messages, 1)
    with pytest.raises(ValueError, match="must be restored"):
        timeline.render_timeline(messages)


def test_hidden_user_and_late_descendants_are_not_public_timeline_or_transcript_rows():
    messages = [
        {"role": "user", "content": "first", "_nz_message_id": "msg-first"},
        {"role": "user", "content": "secret question", "_nz_message_id": "msg-hidden", "_nz_visible": False},
        {"role": "assistant", "content": "secret positional reply"},
        {"role": "user", "content": "second", "_nz_message_id": "msg-second"},
        {"role": "assistant", "content": "secret late reply", "_nz_parent_id": "msg-hidden",
         "tool_calls": [{"id": "secret-id", "function": {"name": "secret tool"}}]},
        {"role": "tool", "content": "secret receipt", "tool_call_id": "secret-id"},
        {"role": "assistant", "content": "public reply", "_nz_parent_id": "msg-second"},
    ]
    turns = timeline.conversation_turns(messages)
    assert [(turn.number, turn.user_text, turn.assistant_text) for turn in turns] == [
        (1, "first", ""), (2, "second", "public reply")]
    assert timeline.fork_history(messages, 1) == messages[:1]
    assert "secret" not in timeline.build_transcript_document("session", messages).markdown()
    assert "secret" not in timeline.format_turn_detail("session", turns[1], messages, remote=True)


def test_fork_stops_before_the_next_submission_even_if_a_result_arrives_late():
    messages = [
        {"role": "user", "content": "same", "_nz_message_id": "msg-first"},
        {"role": "assistant", "content": "started", "_nz_parent_id": "msg-first",
         "tool_calls": [{"id": "tool-1", "function": {"name": "read_file", "arguments": "{}"}}]},
        {"role": "user", "content": "same", "_nz_message_id": "msg-second", "_nz_time": {"created": 5}},
        {"role": "tool", "tool_call_id": "tool-1", "content": "late receipt"},
        {"role": "assistant", "content": "late reply", "_nz_parent_id": "msg-first"},
    ]
    first = timeline.conversation_turns(messages)[0]
    detail = timeline.format_turn_detail("parent", first, messages)
    assert "late receipt" in detail and "late reply" in detail
    assert timeline.fork_history(messages, 1) == messages[:2]
    # 边界前的助手消息也可能在后面被原地结算。无法复原旧状态时明确拒绝。
    messages[1]["_nz_parts"] = [{"type": "tool", "call_id": "tool-1",
                                 "state": {"status": "completed", "output": "late receipt"}}]
    with pytest.raises(timeline.HistoryUnavailable):
        timeline.fork_history(messages, 1)
    messages[1]["_nz_parts"][0]["state"]["time"] = {"start": 1, "end": 9}
    with pytest.raises(timeline.HistoryUnavailable):
        timeline.fork_history(messages, 1)
    assert timeline.fork_history(messages, 2) == messages


def test_historical_fork_requires_evidence_for_tool_output_filled_in_place():
    state = {"status": "completed", "output": "settled"}
    assistant = {"role": "assistant", "content": "", "_nz_parts": [
        {"type": "tool", "call_id": "one", "state": state}]}
    messages = [{"role": "user", "content": "first"}, assistant, {"role": "user", "content": "second"}]
    with pytest.raises(timeline.HistoryUnavailable):
        timeline.fork_history(messages, 1)

    receipt = {"role": "tool", "tool_call_id": "one", "content": "settled"}
    messages.insert(2, receipt)
    assert timeline.fork_history(messages, 1) == messages[:3]
    receipt["content"] = "different result"
    with pytest.raises(timeline.HistoryUnavailable):
        timeline.fork_history(messages, 1)
    messages.remove(receipt)

    messages[-1]["_nz_time"] = {"created": 7}
    assistant["_nz_time"] = {"completed": 6}
    assert timeline.fork_history(messages, 1) == messages[:2]
    del assistant["_nz_time"]
    state["time"] = {"end": 6}
    assert timeline.fork_history(messages, 1) == messages[:2]


@pytest.mark.parametrize("remote", [False, True])
def test_late_tool_result_belongs_to_the_request_that_issued_the_call(remote):
    def assistant(parent, run, tool_id):
        return {"role": "assistant", "content": "", "_nz_parent_id": parent,
                "_nz_interaction_run_id": run, "tool_calls": [
                    {"id": tool_id, "function": {"name": "read_file", "arguments": "{}"}}]}

    messages = [
        {"role": "user", "content": "First request", "_nz_message_id": "msg-first"},
        {"role": "user", "content": "Second request", "_nz_message_id": "msg-second"},
        assistant("msg-first", "one", "shared-call"),
        assistant("msg-second", "two", "shared-call"),
        {"role": "tool", "tool_call_id": "shared-call", "content": "Ambiguous output"},
        {"role": "tool", "tool_call_id": "shared-call", "_nz_interaction_run_id": "one",
         "content": "Output for first request"},
        {"role": "tool", "tool_call_id": "shared-call", "_nz_interaction_run_id": "two",
         "content": "Output for second request"},
    ]
    first, second = [timeline.format_turn_detail("test", turn, messages, remote=remote)
                     for turn in timeline.conversation_turns(messages)]
    assert "Output for first request" in first and "Output for second request" not in first
    assert "Output for second request" in second and "Output for first request" not in second
    assert "Ambiguous output" not in first + second


def test_forked_session_title_increments_infcode_suffix():
    assert timeline.forked_session_title("Parser repair") == "Parser repair (fork #1)"
    assert timeline.forked_session_title("Parser repair (fork #1)") == "Parser repair (fork #2)"


def test_timeline_preview_uses_terminal_columns_for_multilingual_text():
    from nz_coder.interface.presentation_tokens import terminal_text_width

    value = timeline._preview("分析👨\u200d💻 authentication regression", 12)

    assert terminal_text_width(value) <= 12
    assert not value.endswith("\u200d")


def test_timeline_prefers_parent_graph_and_durable_tool_parts():
    messages = [
        {"role": "user", "content": "first", "_nz_message_id": "msg-user-1"},
        {"role": "user", "content": "second", "_nz_message_id": "msg-user-2"},
        {
            "role": "assistant",
            "content": "answer one",
            "_nz_parent_id": "msg-user-1",
            "_nz_parts": [{
                "id": "part-tool-1",
                "message_id": "msg-answer-1",
                "type": "tool",
                "tool": "read_file",
                "call_id": "call-1",
                "state": {
                    "status": "completed",
                    "input": {"path": "app.py"},
                    "output": "ok",
                    "title": "Read",
                    "metadata": {},
                    "time": {"start": 1, "end": 2},
                },
            }],
            "_nz_message_id": "msg-answer-1",
        },
        {
            "role": "assistant",
            "content": "answer two",
            "_nz_parent_id": "msg-user-2",
            "_nz_message_id": "msg-answer-2",
        },
    ]
    for message in messages:
        message["_nz_session_id"] = "session-a"

    turns = timeline.conversation_turns(messages)

    assert turns[0].assistant_text == "answer one"
    assert turns[0].tools == ("read_file",)
    assert turns[1].assistant_text == "answer two"


def test_transcript_document_keeps_message_identity_and_toolpart_projections():
    messages = [
        {"role": "user", "content": "inspect", "_nz_message_id": "user-1"},
        {
            "role": "assistant",
            "content": "done",
            "_nz_message_id": "assistant-1",
            "tool_calls": [{
                "id": "call-1",
                "function": {"name": "read_file", "arguments": '{"path":"app.py"}'},
            }],
            "_nz_parts": [{
                "id": "part-1",
                "type": "tool",
                "tool": "read_file",
                "call_id": "call-1",
                "state": {
                    "status": "completed",
                    "input": {"path": "app.py"},
                    "output": "source",
                },
            }],
        },
    ]

    document = timeline.build_transcript_document(
        "session-1", messages, compact_tools=True,
    )

    assert [(block.message_id, block.role) for block in document.blocks[:2]] == [
        ("user-1", "user"),
        ("assistant-1", "assistant"),
    ]
    tool = document.blocks[2]
    assert tool.part_id == "part-1"
    assert "completed" in tool.compact_markdown
    assert "source" in tool.markdown


def test_rebind_fork_history_rekeys_message_part_and_parent_graph():
    source = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
    ]
    ensure_message_identities(source, "session-source")
    source_message_ids = {message["_nz_message_id"] for message in source}
    source_part_ids = {
        part["id"] for message in source for part in message["_nz_parts"]
    }

    rebound = timeline.rebind_fork_history(source, "session-fork")

    assert legacy_messages(rebound) == legacy_messages(source)
    assert {message["_nz_session_id"] for message in rebound} == {"session-fork"}
    assert not source_message_ids.intersection(
        {message["_nz_message_id"] for message in rebound}
    )
    assert not source_part_ids.intersection(
        {part["id"] for message in rebound for part in message["_nz_parts"]}
    )
    assert rebound[1]["_nz_parent_id"] == rebound[0]["_nz_message_id"]
    assert all(
        part["message_id"] == message["_nz_message_id"]
        for message in rebound
        for part in message["_nz_parts"]
    )


def test_timeline_renders_turn_prompt_agent_summary_and_tools():
    history = _history()
    history[0]["_nz_summary"] = {
        "diffs": [{
            "file": "parser.py",
            "additions": 3,
            "deletions": 1,
            "status": "modified",
        }],
    }
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    console.print(timeline.render_timeline(history))

    rendered = output.getvalue()
    assert "Session timeline" in rendered
    assert "Fix the parser" in rendered
    assert "Parser fixed." in rendered
    assert "read_file" in rendered
    assert "1 files +3/-1" in rendered
    assert "<reminder>" not in rendered


def test_saved_sessions_table_uses_metadata_without_agent_creation(monkeypatch, tmp_path):
    path = tmp_path / "session-one.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(timeline, "list_sessions", lambda limit=20: [path])
    monkeypatch.setattr(timeline, "active_session_id", lambda: "session-one")
    monkeypatch.setattr(
        timeline,
        "load_session",
        lambda _session_id: {
            "timestamp": "2026-08-03 12:00:00",
            "messages": [{}, {}],
            "model": "model-test",
            "mode": "plan",
        },
    )
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    console.print(timeline.render_sessions())

    rendered = output.getvalue()
    assert "session-one" in rendered
    assert "model-test" in rendered
    assert "plan" in rendered
    assert "2" in rendered


def test_saved_sessions_table_includes_unsaved_active_session(monkeypatch):
    monkeypatch.setattr(timeline, "list_sessions", lambda limit=20: [])
    monkeypatch.setattr(timeline, "active_session_id", lambda: "active-new")
    monkeypatch.setattr(
        timeline,
        "load_session",
        lambda session_id: {
            "timestamp": "2026-08-03 12:00:00",
            "messages": [],
            "model": "model-test",
            "mode": "default",
        } if session_id == "active" else {},
    )
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    console.print(timeline.render_sessions())

    rendered = output.getvalue()
    assert "●" in rendered
    assert "active-new" in rendered


def test_session_options_expose_picker_metadata(monkeypatch, tmp_path):
    path = tmp_path / "session-one.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(timeline, "list_sessions", lambda limit=20: [path])
    monkeypatch.setattr(timeline, "active_session_id", lambda: "session-one")
    monkeypatch.setattr(
        timeline,
        "load_session",
        lambda _session_id: {
            "timestamp": "2026-08-03 12:00:00",
            "messages": [{}, {}, {}],
            "model": "model-test",
            "mode": "plan",
        },
    )

    options = timeline.session_options()

    assert len(options) == 1
    assert options[0].session_id == "session-one"
    assert options[0].active is True
    assert options[0].message_count == 3
    assert options[0].model == "model-test"
