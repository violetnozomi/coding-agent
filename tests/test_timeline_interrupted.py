"""Behavior coverage for safe transcript projections of interrupted tools."""
from __future__ import annotations

import copy

import pytest

from nz_coder.interface.timeline import build_transcript_document
from nz_coder.protocol.public_error import PublicError


_PRIVATE_ERROR = "Authorization=Bearer PRIVATE-ERROR-MARKER; prompt=PRIVATE-PROMPT"
_PRIVATE_MARKERS = ("Authorization", "PRIVATE-ERROR-MARKER", "PRIVATE-PROMPT")


def _history(state: dict, *, with_call: bool = False) -> list[dict]:
    message = {
        "role": "assistant",
        "content": "",
        "_nz_message_id": "assistant-1",
        "_nz_parts": [{
            "id": "part-1",
            "message_id": "assistant-1",
            "type": "tool",
            "tool": "bash",
            "call_id": "call-1",
            "state": state,
            "index": 0,
            "visible": True,
            "internal": False,
            "authoritative": True,
        }],
    }
    if with_call:
        message["tool_calls"] = [{
            "id": "call-1",
            "function": {"name": "bash", "arguments": '{"command":"sleep 30"}'},
        }]
    return [message]


@pytest.mark.parametrize("with_call", [False, True])
@pytest.mark.parametrize("compact_tools", [False, True])
@pytest.mark.parametrize("error", ["Tool execution aborted", _PRIVATE_ERROR])
def test_interrupted_tool_shows_safe_message_without_duplicate_or_mutation(
    with_call: bool, compact_tools: bool, error: str,
):
    messages = _history({
        "status": "error",
        "input": {"command": "sleep 30"},
        "time": {"start": 1, "end": 2},
        "error": error,
        "interrupted": True,
    }, with_call=with_call)
    original = copy.deepcopy(messages)

    document = build_transcript_document(
        "session-1", messages, compact_tools=compact_tools,
    )

    tools = [block for block in document.blocks if block.role == "toolpart"]
    assert len(tools) == 1
    tool = tools[0]
    assert (tool.message_id, tool.part_id) == ("assistant-1", "part-1")
    assert '"command": "sleep 30"' in tool.markdown
    assert "Tool execution interrupted." in tool.markdown
    assert "Request failed." not in tool.markdown
    assert error not in tool.markdown
    if compact_tools:
        assert "· interrupted" in tool.compact_markdown
        assert error not in tool.compact_markdown
    else:
        assert tool.compact_markdown is None
    rendered = tool.markdown + (tool.compact_markdown or "")
    assert all(marker not in rendered for marker in _PRIVATE_MARKERS)
    assert messages == original


@pytest.mark.parametrize("marker", [None, False, "true", 1])
@pytest.mark.parametrize("error", ["Tool execution aborted", _PRIVATE_ERROR])
def test_unconfirmed_interruption_keeps_errors_private(marker, error: str):
    state = {"status": "error", "input": {}, "error": error}
    if marker is not None:
        state["interrupted"] = marker

    document = build_transcript_document(
        "session-1", _history(state), compact_tools=True,
    )

    tool = next(block for block in document.blocks if block.role == "toolpart")
    assert "Request failed." in tool.markdown
    assert "Tool execution interrupted." not in tool.markdown
    assert "· error" in tool.compact_markdown
    assert error not in tool.markdown
    assert error not in tool.compact_markdown
    rendered = tool.markdown + tool.compact_markdown
    assert all(marker not in rendered for marker in _PRIVATE_MARKERS)


def test_ordinary_tool_error_preserves_trusted_public_message():
    document = build_transcript_document("session-1", _history({
        "status": "error",
        "input": {},
        "error": PublicError(
            "provider_error", "The provider request failed.", retryable=True,
        ).to_dict(),
    }), compact_tools=True)

    tool = next(block for block in document.blocks if block.role == "toolpart")
    assert "The provider request failed." in tool.markdown
    assert "Tool execution interrupted." not in tool.markdown
    assert "· error" in tool.compact_markdown


def test_completed_tool_keeps_output_even_with_stray_interrupted_flag():
    document = build_transcript_document("session-1", _history({
        "status": "completed",
        "input": {},
        "output": "command completed successfully",
        "interrupted": True,
    }), compact_tools=True)

    tool = next(block for block in document.blocks if block.role == "toolpart")
    assert "command completed successfully" in tool.markdown
    assert "Tool execution interrupted." not in tool.markdown
    assert "· completed" in tool.compact_markdown


def test_hidden_tool_details_hide_interrupted_tool():
    document = build_transcript_document("session-1", _history({
        "status": "error",
        "input": {},
        "error": _PRIVATE_ERROR,
        "interrupted": True,
    }, with_call=True), tool_details=False, compact_tools=True)

    assert all(block.role != "toolpart" for block in document.blocks)
    assert "Tool execution interrupted." not in document.markdown()
    assert _PRIVATE_ERROR not in document.markdown()
    assert all(marker not in document.markdown() for marker in _PRIVATE_MARKERS)
