"""Tests for SessionProcessor's stable message mutation sink."""
from __future__ import annotations

import copy

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, PARTS_KEY
from nz_coder.runtime.session.session_processor import SessionProcessor


def _message():
    return {
        "role": "assistant",
        "content": "",
        MESSAGE_ID_KEY: "msg-processor-sink",
        PARTS_KEY: [],
    }


def _tool_call(call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
    }


def test_processor_sink_observes_text_tool_and_finish_in_order():
    """Every stable message transition reaches the Session owner exactly once."""
    observed = []
    processor = SessionProcessor(
        _message(),
        on_message_updated=lambda message: observed.append(copy.deepcopy(message)),
    )

    processor.stream_text("hello", part_id="part-text-1")
    processor.register_tool_calls([_tool_call()])
    processor.finish_step("tool-calls")

    assert [snapshot[PARTS_KEY][-1]["type"] for snapshot in observed] == [
        "text",
        "tool",
        "step-finish",
    ]
    assert observed[-1]["content"] == "hello"


def test_child_cost_mutation_notifies_sink():
    """Cost-only state changes cannot bypass the Session owner."""
    observed = []
    processor = SessionProcessor(
        _message(),
        on_message_updated=lambda message: observed.append(copy.deepcopy(message)),
    )

    processor.add_child_cost(0.25)

    assert len(observed) == 1
