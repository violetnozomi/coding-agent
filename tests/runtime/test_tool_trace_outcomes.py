"""真实执行结果决定轨迹状态；成功输出文字不能冒充执行失败。"""
from __future__ import annotations

import asyncio
import json
import shlex
import sys
from dataclasses import replace

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.providers.normalized import (
    NormalizedFunction, NormalizedToolCall, completion,
)
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.trace import TraceRecorder
from nz_coder.tools import TOOL_HANDLERS
from test_native_product_runtime import _install_runtime, _request


_PRIVATE_OUTPUT = "TRACE-PRIVATE-STDOUT-92745"


def _python_command(program):
    return shlex.join((sys.executable, "-c", program))


def _execute_and_capture(tmp_path, name, arguments):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(session_id="trace-outcome", journal_path=journal)
    subscription = bus.subscribe({"session.tool.completed"})
    agent = AgentLoop.__new__(AgentLoop)
    agent.tracer = TraceRecorder(trace_dir=tmp_path / "traces")
    agent._emit_session_event = bus.publish
    call = {"id": "trace-call", "function": {"name": name, "arguments": arguments}}
    try:
        with scoped_workdir(workspace):
            result = ToolExecutor(
                PermissionManager("auto", asker=lambda *_args: True),
            ).execute_one(call, 0)
            agent._trace_tool_result(result, result.output, tool_call_id="trace-call", index=0)
        event = subscription.get(timeout=1).properties
    finally:
        subscription.close()
        bus.close()
    rows = [json.loads(line) for line in agent.tracer.path.read_text().splitlines()]
    trace = next(row for row in rows if row["event"] == "tool_call")
    return result, trace, event, journal.read_text()


@pytest.mark.parametrize("stdout", [
    "Command exited with code 9",
    "Error: ordinary successful output",
    "Denied ordinary successful output",
    "ordinary successful output",
])
def test_successful_bash_stdout_cannot_override_typed_trace_outcome(tmp_path, stdout):
    result, trace, event, _journal = _execute_and_capture(tmp_path, "bash", {
        "command": _python_command(f"print({stdout!r})"), "timeout": 10,
    })

    assert result.executed
    assert result.metadata["exit"] == 0
    assert result.dispatch_failed is False
    assert result.command_failed is False
    assert result.output == stdout
    assert event["status"] == "ok"
    assert event["command_failed"] is False
    assert event["output"] == stdout
    assert trace["command_failed"] is False
    assert trace["status"] == "ok"
    assert trace["output"] == stdout


def test_real_bash_nonzero_exit_keeps_failure_status_and_redacts_stdout(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = workspace / "exit_nine.py"
    script.write_text(f"print({_PRIVATE_OUTPUT!r})\nraise SystemExit(9)\n")
    result, trace, event, journal = _execute_and_capture(tmp_path, "bash", {
        "command": shlex.join((sys.executable, script.name)), "timeout": 10,
    })

    assert result.executed
    assert result.metadata["exit"] == 9
    assert result.dispatch_failed is False
    assert result.command_failed is True
    assert _PRIVATE_OUTPUT in result.output
    assert trace["status"] == event["status"] == "nonzero"
    assert trace["command_failed"] is event["command_failed"] is True
    assert trace["output"] == event["output"] == "Tool execution failed."
    assert _PRIVATE_OUTPUT not in json.dumps([trace, event, journal])


def test_real_dispatch_exception_keeps_error_status_and_private_reason_hidden(monkeypatch, tmp_path):
    def fail():
        raise RuntimeError(_PRIVATE_OUTPUT)

    monkeypatch.setitem(TOOL_HANDLERS, "_trace_dispatch_failure", fail)
    result, trace, event, journal = _execute_and_capture(
        tmp_path, "_trace_dispatch_failure", {},
    )

    assert result.executed
    assert result.dispatch_failed is True
    assert result.command_failed is False
    assert result.public_error is None
    assert trace["status"] == event["status"] == "error"
    assert trace["command_failed"] is event["command_failed"] is False
    assert trace["output"] == event["output"] == "Tool execution failed."
    assert _PRIVATE_OUTPUT not in result.output
    assert _PRIVATE_OUTPUT not in json.dumps([trace, event, journal])


def test_native_sdk_real_bash_success_agrees_with_trace_and_session_event(monkeypatch, tmp_path):
    stdout = "Command exited with code 9"
    command = _python_command(f"print({stdout!r})")

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return completion(tool_calls=[NormalizedToolCall(
                    id="real-bash", function=NormalizedFunction(
                        "bash", json.dumps({"command": command, "timeout": 10}),
                    ),
                )], finish_reason="tool_calls")
            return completion(content="Command completed.", finish_reason="stop")

    _install_runtime(monkeypatch, Provider())
    request = replace(_request(tmp_path), messages=({
        "role": "user", "content": "Run the supplied local Python command once and report its output.",
    },), tool_names=("bash",))
    bus = SessionEventBus(session_id=request.session_id)
    options = RunOptions(event_bus=bus, permission_asker=lambda *_args: True)
    environment = native_sdk.build_product_run_environment(request, options)
    captured = []
    original_trace = environment._trace_tool_result

    def capture_result(result, output, **kwargs):
        captured.append(result)
        original_trace(result, output, **kwargs)

    monkeypatch.setattr(environment, "_trace_tool_result", capture_result)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        event = next(event.properties for event in bus.recent(100)
                     if event.type == "session.tool.completed")
        rows = [json.loads(line) for line in environment.tracer.path.read_text().splitlines()]
        trace = next(row for row in rows if row["event"] == "tool_call")
    finally:
        environment.close()
        bus.close()

    assert result.status.value == "completed"
    assert len(captured) == 1
    assert captured[0].metadata["exit"] == 0
    assert captured[0].dispatch_failed is False
    assert captured[0].command_failed is False
    tool_message = next(message for message in result.messages if message["role"] == "tool")
    assert tool_message["content"] == stdout
    assert event["status"] == "ok"
    assert event["output"] == stdout
    assert trace["status"] == "ok"
    assert trace["output"] == stdout
