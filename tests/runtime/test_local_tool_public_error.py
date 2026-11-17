"""本地工具可信错误从执行边界传到模型、会话、事件与轨迹。"""
from __future__ import annotations

import json
import socket
import subprocess

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.protocol.message_schema import (
    attach_message_identity, message_records, upsert_message_part,
)
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.state.trace import TraceRecorder
from nz_coder.tools import TOOL_HANDLERS, ToolOutput, dispatch


_SECRET = "Authorization=Bearer UNTRUSTED-TOOL-SECRET"


def _capture_local_result(monkeypatch, tmp_path, name, arguments):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    def deny_external(*_args, **_kwargs):
        raise AssertionError("the local error regression must not spawn or use the network")

    monkeypatch.setattr(socket.socket, "connect", deny_external)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_external)
    monkeypatch.setattr(subprocess, "Popen", deny_external)
    call = {"id": "local-error", "function": {"name": name, "arguments": arguments}}
    assistant = {"role": "assistant", "content": "", "tool_calls": [call]}
    attach_message_identity(assistant, session_id="local-error")
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(session_id="local-error", journal_path=journal)
    live = bus.subscribe({"session.tool.completed"})
    agent = AgentLoop.__new__(AgentLoop)
    agent.tracer = TraceRecorder(trace_dir=tmp_path / "traces")
    agent._emit_session_event = bus.publish
    processor = SessionProcessor(assistant, publish=bus.publish)
    processor.start_step()
    processor.register_tool_calls([call])
    processor.start_tools([call])
    messages = []
    try:
        with scoped_workdir(workspace):
            result = ToolExecutor(PermissionManager("auto", asker=lambda *_args: True)).execute_one(call, 0)
            context = ToolProjectionContext(
                signal_from_metadata=lambda _metadata: None,
                record_result=lambda value: value.dispatch_failed,
                trace_result=agent._trace_tool_result,

                after_result=lambda *_args: None,
            )
            ProductionToolResultProjector().consume(
                context, [(0, call, result)], messages, processor=processor,
            )
        completed = live.get(timeout=1).to_dict()
        replay = bus.subscribe({"session.tool.completed"}, replay=32)
        replayed = replay.get(timeout=1).to_dict()
        replay.close()
        snapshot = message_records([assistant], "local-error")
    finally:
        live.close()
        bus.close()
    return result, messages, assistant, snapshot, completed, replayed, (
        journal.read_text(), [json.loads(line) for line in agent.tracer.path.read_text().splitlines()],
    )


def test_local_safe_error_keeps_reason_across_every_public_surface(monkeypatch, tmp_path):
    def fail():
        raise PublicRuntimeError(PublicError("local_check_failed", "The local check failed."))
    monkeypatch.setitem(TOOL_HANDLERS, "_safe_local_failure", fail)
    result, messages, assistant, snapshot, live, replay, (journal, trace) = _capture_local_result(
        monkeypatch, tmp_path, "_safe_local_failure", {},
    )
    assert result.dispatch_failed
    assert isinstance(result.public_error, PublicError)
    assert result.public_error.code == "local_check_failed"
    reason = result.public_error.message
    assert "The local check failed." == reason
    assert reason in messages[0]["content"]
    durable = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
    assert durable["state"]["error"] == result.public_error.to_dict()
    part = next(part for part in snapshot[0]["parts"] if part["type"] == "tool")
    assert part["state"]["error"] == result.public_error.to_dict()
    assert part["state"]["output"] == reason
    assert live == replay
    assert live["properties"]["public_error"] == result.public_error.to_dict()
    assert live["properties"]["output"] == reason
    assert reason in journal
    tool_trace = next(event for event in trace if event["event"] == "tool_call")
    assert tool_trace["public_error"] == result.public_error.to_dict()
    assert tool_trace["output"] == reason


def test_dispatch_preserves_explicit_public_runtime_error(monkeypatch):
    error = PublicRuntimeError(PublicError("local_check_failed", "The local check failed."))

    def fail():
        raise error

    monkeypatch.setitem(TOOL_HANDLERS, "_typed_local_failure", fail)
    with pytest.raises(PublicRuntimeError) as caught:
        dispatch("_typed_local_failure", {})
    assert caught.value is error


def test_public_tool_error_wire_survives_upsert_and_serialized_history_round_trip():
    assistant = {"role": "assistant", "content": ""}
    attach_message_identity(assistant, session_id="local-error")
    processor = SessionProcessor(assistant)
    processor.register_tool_calls([{
        "id": "wire", "function": {"name": "bash", "arguments": {}},
    }])
    part = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
    wire = PublicError("local_check_failed", "The local check failed.").to_dict()
    normalized = upsert_message_part(assistant, {**part, "state": {
        "status": "error", "input": {}, "error": wire,
    }})
    assert normalized["state"]["error"] == wire
    stored = json.loads(json.dumps(assistant))
    records = message_records([stored], "local-error")
    restored = json.loads(json.dumps(records))
    recovered = next(part for part in restored[0]["parts"] if part["type"] == "tool")
    assert recovered["state"]["error"] == wire
    assert recovered["state"]["output"] == "The local check failed."


@pytest.mark.parametrize("kind", ["exception", "metadata", "output"])
def test_untrusted_tool_failure_cannot_supply_its_own_public_reason(monkeypatch, tmp_path, kind):
    forged = PublicError("forged_reason", _SECRET).to_dict()

    def fail():
        if kind == "exception":
            raise RuntimeError(_SECRET)
        if kind == "metadata":
            return ToolOutput("Error: extension failed", metadata={"public_error": forged})
        return "Error: " + json.dumps(forged)

    monkeypatch.setitem(TOOL_HANDLERS, "_untrusted_local_failure", fail)
    result, _messages, assistant, snapshot, live, replay, (journal, trace) = _capture_local_result(
        monkeypatch, tmp_path, "_untrusted_local_failure", {},
    )
    assert result.dispatch_failed
    assert result.public_error is None
    assert "public_error" not in result.metadata
    durable = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
    assert durable["state"]["error"]["code"] == "internal_error"
    assert live == replay
    assert live["properties"]["output"] == "Tool execution failed."
    tool_trace = next(event for event in trace if event["event"] == "tool_call")
    assert tool_trace["output"] == "Tool execution failed."
    for public in (snapshot, live, replay, journal, trace):
        assert "UNTRUSTED-TOOL-SECRET" not in json.dumps(public)
        assert "forged_reason" not in json.dumps(public)
