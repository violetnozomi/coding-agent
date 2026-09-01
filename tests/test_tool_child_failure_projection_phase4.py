"""Public projection contracts for nested tools and process children."""
from __future__ import annotations

import json


_SECRET = "Authorization=Bearer NESTED-SECRET"


def _failed_part(output=_SECRET):
    return {
        "id": "part-tool",
        "message_id": "msg-tool",
        "type": "tool",
        "tool": "bash",
        "call_id": "call-tool",
        "state": {"status": "error", "input": {}, "output": output},
    }


def test_nested_failed_tool_output_is_sanitized_in_session_event():
    from nz_coder.protocol.session_events import SessionEventBus

    bus = SessionEventBus(session_id="session-tool")
    try:
        event = bus.publish("message.part.updated", {"part": _failed_part()})
        payload = event.to_dict()
    finally:
        bus.close()

    assert "NESTED-SECRET" not in repr(payload)
    assert payload["properties"]["part"]["state"]["error"]["schema"] == (
        "nz.public_error.v1"
    )


def test_nested_failed_tool_output_is_sanitized_in_journal(tmp_path):
    from nz_coder.protocol.session_events import SessionEventBus

    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="session-tool",
        replay_capacity=8,
        journal_path=journal,
    )
    bus.publish("message.part.updated", {"part": _failed_part()})
    bus.close()

    assert "NESTED-SECRET" not in journal.read_text(encoding="utf-8")


def test_nested_failed_tool_output_is_sanitized_in_snapshot():
    from nz_coder.protocol.message_schema import message_records

    records = message_records([{
        "role": "assistant",
        "content": "",
        "_nz_message_id": "msg-tool",
        "_nz_session_id": "session-tool",
        "_nz_parts": [_failed_part()],
    }], "session-tool")

    assert "NESTED-SECRET" not in repr(records)
    assert records[0]["parts"][0]["state"]["error"]["schema"] == (
        "nz.public_error.v1"
    )


def test_nested_failed_tool_output_is_sanitized_in_trace(tmp_path):
    from nz_coder.state.trace import TraceRecorder

    recorder = TraceRecorder(trace_dir=tmp_path)
    recorder.log("message.part.updated", part=_failed_part())

    assert "NESTED-SECRET" not in recorder.path.read_text(encoding="utf-8")


def test_successful_tool_output_remains_visible():
    from nz_coder.protocol.message_schema import project_public_tool_part

    part = _failed_part("safe output")
    part["state"]["status"] = "completed"

    assert project_public_tool_part(part)["state"]["output"] == "safe output"


def test_trusted_failed_tool_summary_remains_visible():
    from nz_coder.protocol.message_schema import project_public_tool_part
    from nz_coder.protocol.public_error import TrustedPublicMessage

    part = _failed_part(TrustedPublicMessage(
        "tool_failed",
        "The formatter rejected this file.",
    ))
    projected = project_public_tool_part(part)

    assert projected["state"]["output"] == "The formatter rejected this file."
    assert projected["state"]["error"]["code"] == "tool_failed"


class _Connection:
    def __init__(self):
        self.payload = None
        self.closed = False

    def send(self, payload):
        self.payload = payload

    def close(self):
        self.closed = True


def test_child_process_exception_uses_public_error_wire(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import _run_subagent_process

    def fail(*_args, **_kwargs):
        raise RuntimeError("Authorization=Bearer CHILD-SECRET")

    monkeypatch.setattr(subagent, "run_subagent", fail)
    connection = _Connection()
    _run_subagent_process(connection, {
        "workspace": str(tmp_path),
        "parent_session_id": "parent",
        "prompt": "work",
        "agent_type": "worker",
        "session_id": "child",
    })

    assert connection.payload["schema"] == "nz.child_result.v1"
    assert connection.payload["ok"] is False
    assert connection.payload["public_error"]["schema"] == "nz.public_error.v1"
    assert "CHILD-SECRET" not in repr(connection.payload)


def test_child_process_exception_text_not_stored_in_background_result():
    from nz_coder.runtime.agent.agent_manager import _decode_child_process_envelope

    result, public = _decode_child_process_envelope({
        "schema": "nz.child_result.v1",
        "ok": False,
        "public_error": {
            "schema": "nz.public_error.v1",
            "code": "internal_error",
            "message": "An internal error occurred.",
            "retryable": False,
            "metadata": {},
        },
    })

    assert result == "An internal error occurred."
    assert public.code == "internal_error"


def test_child_process_secret_not_present_in_workflow_summary():
    from nz_coder.runtime.agent.agent_manager import _decode_child_process_envelope

    result, _public = _decode_child_process_envelope({
        "schema": "nz.child_result.v1",
        "ok": False,
        "public_error": {
            "schema": "nz.public_error.v1",
            "code": "internal_error",
            "message": "An internal error occurred.",
            "retryable": False,
            "metadata": {},
        },
        "result": "Authorization=Bearer CHILD-SECRET",
    })

    assert "CHILD-SECRET" not in result


def test_child_process_malformed_error_envelope_fails_closed():
    from nz_coder.runtime.agent.agent_manager import _decode_child_process_envelope

    result, public = _decode_child_process_envelope({
        "ok": False,
        "result": "Authorization=Bearer CHILD-SECRET",
    })

    assert result == "The child process returned an invalid result."
    assert public.code == "child_process_protocol_error"


def test_child_process_success_result_contract_is_unchanged():
    from nz_coder.runtime.agent.agent_manager import _decode_child_process_envelope

    result, public = _decode_child_process_envelope({
        "schema": "nz.child_result.v1",
        "ok": True,
        "result": "completed",
    })

    assert result == "completed"
    assert public is None
    json.dumps({"result": result})
