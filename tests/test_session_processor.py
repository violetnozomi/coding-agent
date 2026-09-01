"""Code-level lifecycle tests for the durable Agent Session processor."""
from __future__ import annotations

import json
import math

import pytest

from nz_coder.protocol.message_schema import (
    attach_message_identity,
    message_records,
    set_assistant_error,
)
from nz_coder.protocol.public_error import TrustedPublicMessage
from nz_coder.runtime.session.session_processor import SessionProcessor


def _assistant() -> dict:
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
        }],
    }
    attach_message_identity(message, "msg-lifecycle", session_id="session-a")
    return message


def test_processor_persists_complete_step_and_tool_lifecycle():
    message = _assistant()
    processor = SessionProcessor(message)

    processor.start_step("snapshot-before")
    processor.add_reasoning("private chain")
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])
    processor.complete_tool("call-1", "file body", title="Read a.py")
    processor.finish_step(
        "tool-calls",
        input_tokens=100,
        output_tokens=20,
        total_tokens=120,
        snapshot="snapshot-after",
    )

    parts = message_records([message], "session-a")[0]["parts"]
    assert {part["type"] for part in parts} == {
        "step-start", "reasoning", "tool", "step-finish",
    }
    reasoning = next(part for part in parts if part["type"] == "reasoning")
    assert reasoning["text"] == "private chain"
    tool = next(part for part in parts if part["type"] == "tool")
    assert tool["state"]["status"] == "completed"
    assert tool["state"]["input"] == {"path": "a.py"}
    assert tool["state"]["output"] == "file body"
    finish = next(part for part in parts if part["type"] == "step-finish")
    assert finish["reason"] == "tool-calls"
    assert finish["tokens"] == {"input": 100, "output": 20, "total": 120}
    assert message_records([message], "session-a")[0]["info"]["finish"] == "tool-calls"


def test_processor_patch_part_uses_step_start_snapshot_and_changed_files():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step("snap-" + "a" * 64)
    processor.finish_step("tool-calls", snapshot="snap-" + "b" * 64)
    processor.add_patch("snap-" + "a" * 64, ["app.py", "tests/test_app.py"])

    parts = message_records([message], "session-a")[0]["parts"]
    patch = next(part for part in parts if part["type"] == "patch")
    assert patch["hash"] == "snap-" + "a" * 64
    assert patch["files"] == ["app.py", "tests/test_app.py"]


def test_processor_interrupt_discards_unsettled_tool_output():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])

    assert processor.interrupt_unsettled() == 1

    tool = next(part for part in message["_nz_parts"] if part["type"] == "tool")
    assert tool["state"]["status"] == "error"
    assert tool["state"]["interrupted"] is True
    assert tool["state"]["error"] == "Tool execution aborted"
    assert "output" not in tool["state"]


def test_processor_retry_part_is_stable_and_replaced_by_attempt():
    message = _assistant()
    processor = SessionProcessor(message)

    first = processor.add_retry(
        1,
        TrustedPublicMessage("retry", "first", retryable=True),
        next_at=10,
    )
    second = processor.add_retry(
        1,
        TrustedPublicMessage("retry", "updated", retryable=True),
        next_at=20,
    )

    retries = [part for part in message["_nz_parts"] if part["type"] == "retry"]
    assert first["id"] == second["id"]
    assert len(retries) == 1
    assert retries[0]["message"] == "updated"
    assert retries[0]["next"] == 20.0
    assert retries[0]["error"] == {
        "name": "UnknownError",
        "data": {
            "message": "updated",
            "public_error": {
                "schema": "nz.public_error.v1",
                "code": "retry",
                "message": "updated",
                "retryable": True,
                "metadata": {},
            },
        },
    }
    assert retries[0]["time"]["created"] >= 0


def test_assistant_error_and_finish_publish_live_message_updates():
    message = _assistant()
    events = []

    def publish(event, properties):
        events.append((event, properties))

    processor = SessionProcessor(message, publish=publish)
    processor.start_step()
    set_assistant_error(
        message,
        TrustedPublicMessage("provider_error", "provider failed"),
        name="APIError",
        data={"message": "provider failed", "isRetryable": False},
        publish=publish,
    )
    processor.finish_step("error")

    message_updates = [properties for event, properties in events if event == "message.updated"]
    assert message_updates[0]["info"]["error"]["name"] == "APIError"
    assert "finish" not in message_updates[0]["info"]
    assert message_updates[-1]["message_id"] == "msg-lifecycle"
    assert message_updates[-1]["info"]["finish"] == "error"
    assert message_updates[-1]["info"]["error"]["data"]["message"] == "provider failed"


def test_stream_tool_delta_creates_pending_part_before_complete_call():
    message = _assistant()
    message["tool_calls"] = []
    processor = SessionProcessor(message)
    processor.start_step()

    first = processor.stream_tool_delta(
        0,
        call_id="call-stream",
        name="read_file",
        arguments='{"path":',
    )
    second = processor.stream_tool_delta(
        0,
        call_id="call-stream",
        name="read_file",
        arguments='{"path":"live.py"}',
    )

    assert first["id"] == second["id"]
    assert first["state"]["status"] == "pending"
    assert first["state"]["input"] == {}
    assert second["state"]["input"] == {"path": "live.py"}
    assert second["index"] == 0

    complete = [{
        "id": "call-stream",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"live.py"}'},
    }]
    processor.register_tool_calls(complete)
    tools = [part for part in message["_nz_parts"] if part["type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["id"] == first["id"]


def test_stream_tool_delta_reconciles_provisional_call_id_by_index():
    message = _assistant()
    message["tool_calls"] = []
    processor = SessionProcessor(message)
    processor.start_step()

    provisional = processor.stream_tool_delta(
        0,
        name="read_file",
        arguments='{"path":',
    )
    complete = [{
        "id": "call-late-id",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"late.py"}'},
    }]
    processor.register_tool_calls(complete)

    tools = [part for part in message["_nz_parts"] if part["type"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["id"] == provisional["id"]
    assert tools[0]["call_id"] == "call-late-id"
    assert tools[0]["state"]["input"] == {"path": "late.py"}


def test_processor_preserves_provider_and_result_metadata():
    message = _assistant()
    message["tool_calls"][0]["provider_extra"] = {
        "thoughtSignature": "provider-signature",
    }
    processor = SessionProcessor(message)
    processor.start_step()
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])
    processor.complete_tool(
        "call-1",
        "User dismissed the question.",
        title="Question dismissed",
        metadata={"answers": [], "dismissed": True},
    )

    tool = next(part for part in message["_nz_parts"] if part["type"] == "tool")
    assert tool["metadata"] == {"thoughtSignature": "provider-signature"}
    assert tool["state"]["title"] == "Question dismissed"
    assert tool["state"]["metadata"] == {"answers": [], "dismissed": True}

    projected = message_records([message], "session-a")[0]["parts"]
    persisted = next(part for part in projected if part["type"] == "tool")
    assert persisted["metadata"] == {"thoughtSignature": "provider-signature"}
    assert persisted["state"]["metadata"]["dismissed"] is True


def test_processor_persists_and_publishes_running_tool_metadata():
    message = _assistant()
    events = []
    processor = SessionProcessor(
        message,
        publish=lambda event, properties: events.append((event, properties)),
    )
    processor.start_step()
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])

    updated = processor.update_tool_metadata(
        "call-1",
        title="Reading a.py",
        metadata={"output": "first line", "workdir": "/workspace"},
    )

    assert updated is not None
    assert updated["state"]["status"] == "running"
    assert updated["state"]["title"] == "Reading a.py"
    assert updated["state"]["metadata"]["output"] == "first line"
    projected = message_records([message], "session-a")[0]["parts"]
    running = next(part for part in projected if part["type"] == "tool")
    assert running["state"]["status"] == "running"
    assert running["state"]["metadata"] == {
        "output": "first line",
        "workdir": "/workspace",
    }
    assert events[-1][0] == "message.part.updated"
    assert events[-1][1]["part"]["state"]["status"] == "running"

    processor.complete_tool("call-1", "done")
    assert processor.update_tool_metadata("call-1", metadata={"output": "late"}) is None


def test_processor_question_parts_complete_and_survive_projection():
    message = _assistant()
    events = []
    processor = SessionProcessor(
        message,
        publish=lambda event, properties: events.append((event, properties)),
    )
    processor.start_question(
        "call-1",
        "question-shared",
        [{
            "header": "Scope",
            "question": "Which scope?",
            "options": [
                {"label": "File", "description": "Current file."},
                {"label": "Repo", "description": "Whole repository."},
            ],
            "multiple": False,
        }],
    )
    processor.complete_question("call-1", [["File"]])

    projected = message_records([message], "session-a")[0]["parts"]
    question = next(part for part in projected if part["type"] == "question")
    summary = next(part for part in projected if part["type"] == "question-summary")
    assert question["request_id"] == "question-shared"
    assert question["status"] == "completed"
    assert question["response"] == {"answers": [["File"]]}
    assert question["questions"][0]["custom"] is True
    assert summary["tool_call_id"] == "call-1"
    assert summary["answers"] == [["File"]]
    assert [item[0] for item in events] == [
        "message.part.updated",
        "message.part.updated",
        "message.part.updated",
    ]


def test_processor_interrupt_terminates_pending_question_display():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])
    processor.start_question("call-1", "question-cancel", [{
        "header": "Scope",
        "question": "Which scope?",
        "options": [
            {"label": "File", "description": "Current file."},
            {"label": "Repo", "description": "Whole repository."},
        ],
    }])

    assert processor.interrupt_unsettled() == 2
    question = next(
        part for part in message["_nz_parts"] if part["type"] == "question"
    )
    assert question["status"] == "terminated"


def test_stream_text_updates_durable_message_without_duplicate_part():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()

    first = processor.stream_text("hel", part_id="part-stream-text")
    second = processor.stream_text("hello", part_id="part-stream-text")

    text_parts = [part for part in message["_nz_parts"] if part["type"] == "text"]
    assert first["id"] == second["id"] == "part-stream-text"
    assert message["content"] == "hello"
    assert len(text_parts) == 1
    assert text_parts[0]["text"] == "hello"


def test_processor_tool_denial_returns_stop_after_settling_part():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])

    processor.settle_tool(
        "call-1",
        "Denied by user",
        failed=True,
        denied=True,
    )

    tool = next(part for part in message["_nz_parts"] if part["type"] == "tool")
    assert tool["state"]["status"] == "error"
    assert processor.process_result() == "stop"


def test_processor_can_continue_after_denial_when_explicitly_configured():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()
    processor.register_tool_calls(message["tool_calls"])
    processor.start_tools(message["tool_calls"])

    processor.settle_tool(
        "call-1",
        "Denied by user",
        failed=True,
        denied=True,
        continue_on_deny=True,
    )

    assert processor.process_result() == "continue"


def test_length_warning_is_ignored_for_model_but_survives_projection():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()

    warning = processor.add_length_warning(
        has_text=False,
        has_reasoning=True,
        has_tools=False,
    )

    projected = message_records([message], "session-a")[0]["parts"]
    part = next(item for item in projected if item.get("ignored") is True)
    assert "produced no actionable output" in warning
    assert part["text"] == warning
    assert part["ignored"] is True


def test_step_finish_projects_reasoning_and_cache_usage():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()
    processor.finish_step(
        "stop",
        input_tokens=100,
        output_tokens=30,
        total_tokens=130,
        reasoning_tokens=12,
        cache_read_tokens=40,
        cache_write_tokens=5,
        cost=0.012345,
    )

    projected = message_records([message], "session-a")[0]["parts"]
    finish = next(part for part in projected if part["type"] == "step-finish")
    assert finish["tokens"] == {
        "input": 100,
        "output": 30,
        "total": 130,
        "reasoning": 12,
        "cache": {"read": 40, "write": 5},
    }
    assert finish["cost"] == 0.012345
    assert message_records([message], "session-a")[0]["info"]["cost"] == 0.012345


def test_child_cost_is_aggregated_on_assistant_but_not_step_finish():
    message = _assistant()
    processor = SessionProcessor(message)
    processor.start_step()
    message["_nz_cost"] = 0.10

    assert processor.add_child_cost(0.25) == 0.25
    processor.finish_step("tool-calls", cost=0.10)

    record = message_records([message], "session-a")[0]
    finish = next(part for part in record["parts"] if part["type"] == "step-finish")
    assert record["info"]["cost"] == pytest.approx(0.35)
    assert finish["cost"] == pytest.approx(0.10)
    assert message["_nz_child_cost"] == pytest.approx(0.25)
    assert record["info"]["time"]["completed"] >= record["info"]["time"]["created"]


def test_empty_tool_calls_finish_is_downgraded_to_stop():
    message = _assistant()
    message["tool_calls"] = []
    processor = SessionProcessor(message)
    processor.start_step()

    finish = processor.finish_step("tool_calls")

    assert finish["reason"] == "stop"


def test_processor_repairs_nonfinite_persisted_timing_and_usage():
    """Corrupt resume metadata cannot create another non-JSON Session state."""
    message = _assistant()
    message["_nz_time"] = {"created": float("inf")}
    message["_nz_parts"] = [{
        "id": "part-corrupt-start",
        "message_id": message["_nz_message_id"],
        "type": "step-start",
        "time": {"start": float("nan")},
    }]

    processor = SessionProcessor(message)
    finish = processor.finish_step(
        "stop",
        input_tokens=float("nan"),
        output_tokens=float("inf"),
        total_tokens=-1,
    )

    assert math.isfinite(message["_nz_time"]["created"])
    assert math.isfinite(message["_nz_parts"][0]["time"]["start"])
    assert finish["tokens"] == {"input": 0, "output": 0, "total": 0}
    json.dumps(message, allow_nan=False)
