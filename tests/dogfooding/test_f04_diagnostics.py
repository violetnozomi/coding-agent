"""F04: real shell capture, public facts, and local/remote diagnostic boundaries."""
from __future__ import annotations

from io import StringIO
import json
import shlex
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

from nz_coder.interface.remote import _feed_snapshot_events
from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.protocol.message_schema import project_public_tool_part
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import scoped_tool_metadata_reporter
from nz_coder.tools.bash import run_bash


@pytest.fixture
def failed_output(tmp_path, monkeypatch):
    # The secret is runtime output, never an argument in the public tool call.
    monkeypatch.setenv("NZ_PROCESS_BUFFER_BYTES", "4096")
    (tmp_path / "failure.py").write_text(
        "import sys, traceback\n"
        "print('Authorization: Bearer F04_PRIVATE_SENTINEL', flush=True)\n"
        "for i in range(3000): print('synthetic 中文 ' + 'x' * 80)\n"
        "sys.stdout.flush()\n"
        "try: compile('\\n' * 72 + 'def broken(:', 'fixture.py', 'exec')\n"
        "except SyntaxError as exc: traceback.print_exception(exc)\n"
        "print('Cookie: F04_PRIVATE_SENTINEL\\x1b[2J', file=sys.stderr)\n"
        "sys.exit(7)\n", encoding="utf-8",
    )
    updates = []
    with scoped_workdir(tmp_path), scoped_tool_metadata_reporter(
        lambda title, metadata: updates.append(metadata),
    ):
        result = run_bash(f"{shlex.quote(sys.executable)} failure.py", timeout=5)
    return result, updates


def event_for(result, *, status="nonzero"):
    bus = SessionEventBus(session_id="f04-session")
    try:
        return bus.publish("session.tool.completed", {
            "name": "bash", "tool_call_id": "f04-call", "status": status,
            "category": "execute", "output": str(result),
            "metadata": getattr(result, "metadata", {}),
        })
    finally:
        bus.close()


def render(event, *, remote=False):
    output = StringIO()
    view = TerminalRunRenderer(Console(file=output, width=100), SimpleNamespace())
    if remote:
        _feed_snapshot_events(view, {"events": [event.to_dict()]})
    else:
        view.feed(event)
    return output.getvalue()


def test_failed_shell_card_shows_exit_code(failed_output):
    result, _ = failed_output
    text = render(event_for(result))
    assert "exit code 7" in text
    assert "Command failed" in text


def test_truncated_output_has_explicit_notice(failed_output):
    result, _ = failed_output
    assert result.metadata["truncated"] is True
    assert "Output truncated" in render(event_for(result))


def test_untruncated_output_does_not_claim_truncation(tmp_path):
    with scoped_workdir(tmp_path):
        result = run_bash(f'{shlex.quote(sys.executable)} -c "raise SystemExit(2)"')
    text = render(event_for(result))
    assert "Output not truncated" in text
    assert "Output truncated" not in text


def test_safe_error_tail_reaches_model_and_renderer(failed_output):
    result, _ = failed_output
    expected = "SyntaxError (line 73): invalid syntax"
    assert expected in str(result)
    assert expected in event_for(result).properties["output"]
    assert expected in render(event_for(result))


def test_remote_replay_preserves_output_status(failed_output):
    result, _ = failed_output
    event = event_for(result)
    assert render(event) == render(event, remote=True)
    assert event.properties["metadata"]["exit"] == 7
    assert event.properties["metadata"]["truncated"] is True
    assert "exit code 7" in render(event, remote=True)


def test_shell_error_projection_redacts_secret_on_every_output_path(failed_output):
    result, updates = failed_output
    part = {"type": "tool", "tool": "bash", "state": {
        "status": "completed", "output": str(result), "metadata": result.metadata,
    }}
    projected = project_public_tool_part(part)
    surfaces = [str(result), json.dumps(result.metadata), json.dumps(updates),
                json.dumps(projected), json.dumps(event_for(result).to_dict()),
                render(event_for(result))]
    assert all("F04_PRIVATE_SENTINEL" not in text for text in surfaces)
    assert all("\x1b[2J" not in text for text in surfaces)


def test_unknown_output_facts_are_not_zero_or_false():
    text = render(event_for("private exception", status="error"))
    assert "Tool infrastructure failure" in text
    assert "exit code unknown" in text
    assert "Truncation unknown" in text
    assert "private exception" not in text


def test_unrecognized_command_diagnostic_is_explicitly_hidden(tmp_path):
    (tmp_path / "unknown.py").write_text("print('private diagnostic'); raise SystemExit(9)")
    with scoped_workdir(tmp_path):
        result = run_bash(f"{shlex.quote(sys.executable)} unknown.py")
    assert "Diagnostic hidden" in str(result)
    assert "private diagnostic" not in str(result)


def test_large_failure_output_remains_bounded(failed_output):
    result, updates = failed_output
    assert result.metadata["total_output_bytes"] > 250_000
    assert len(str(result)) < 2000
    assert len(render(event_for(result))) < 3000
    assert all(len(str(m.get("output", ""))) < 2000 for m in updates)


def test_legacy_nonzero_snapshot_cannot_restore_raw_failure():
    part = {"type": "tool", "tool": "bash", "state": {
        "status": "completed", "output": "PRIVATE_RAW", "metadata": {
            "exit": 7, "truncated": True, "output": "PRIVATE_RAW",
        },
    }}
    public = project_public_tool_part(part)
    assert "PRIVATE_RAW" not in json.dumps(public)
    assert "exit code 7" in public["state"]["output"]


def test_forged_safe_diagnostic_cannot_bypass_projection():
    from nz_coder.tools import ToolOutput
    result = ToolOutput("PRIVATE_RAW", metadata={"exit": 7, "truncated": True,
        "diagnostic": {"status": "available", "text": "PRIVATE_RAW", "items": [
            {"kind": "PRIVATE_RAW", "line": "PRIVATE_RAW"},
        ]}})
    event = event_for(result)
    assert "PRIVATE_RAW" not in json.dumps(event.to_dict())


def test_completed_snapshot_drops_legacy_error_field():
    part = {"type": "tool", "tool": "bash", "state": {
        "status": "completed", "error": "PRIVATE_RAW", "output": "PRIVATE_RAW",
        "metadata": {"exit": 7, "truncated": True},
    }}
    assert "PRIVATE_RAW" not in json.dumps(project_public_tool_part(part))


def test_snapshot_without_replay_renders_failure_once(failed_output):
    result, _ = failed_output
    output = StringIO()
    view = TerminalRunRenderer(Console(file=output, width=100), SimpleNamespace())
    part = {"id": "part", "type": "tool", "tool": "bash", "call_id": "call",
            "state": {"status": "completed", "metadata": result.metadata, "output": str(result)}}
    snapshot = {"events": [], "run": {"interaction_run_id": "run", "parts": [part]}}
    _feed_snapshot_events(view, snapshot)
    _feed_snapshot_events(view, snapshot)
    assert output.getvalue().count("exit code 7") == 1
    assert "Output truncated" in output.getvalue()


def test_actual_unittest_failure_has_safe_counts(tmp_path):
    (tmp_path / "test_failure.py").write_text(
        "import unittest\nclass Test(unittest.TestCase):\n"
        " def test_bad(self): self.assertEqual(1, 2)\n"
        "if __name__ == '__main__': unittest.main()\n"
    )
    with scoped_workdir(tmp_path):
        result = run_bash(f"{shlex.quote(sys.executable)} test_failure.py")
    assert "unittest: 1 failures, 0 errors" in str(result)


def test_f04_evidence_rejects_command_and_previous_request(tmp_path, monkeypatch):
    # Driver imports reuse the old scripts; no product imports are monkeypatched.
    monkeypatch.syspath_prepend(str(__import__("pathlib").Path(__file__).parent))
    from f04_followup import measure

    identity = {"session_id": "sid", "interaction_run_id": "current", "tool_call_id": "call", "attempt_id": "attempt"}
    result = measure(identity=identity, marker="marker", command="echo marker",
        model_replies=[{"attempt_id": "previous", "tool_replies": [{"tool_call_id": "call", "content": "marker"}]}],
        snapshot={"run": {"interaction_run_id": "previous", "parts": []}}, terminal_text="echo marker")
    assert not any(result.values())


def test_interrupted_event_cannot_publish_raw_diagnostic():
    from nz_coder.tools import ToolOutput
    event = event_for(ToolOutput("PRIVATE_RAW", metadata={"output": "PRIVATE_RAW"}), status="interrupted")
    assert "PRIVATE_RAW" not in json.dumps(event.to_dict())


def test_timeout_keeps_known_capture_facts(tmp_path):
    (tmp_path / "timeout.py").write_text("import time\nprint('x'*100000, flush=True)\ntime.sleep(10)")
    with scoped_workdir(tmp_path):
        result = run_bash(f"{shlex.quote(sys.executable)} timeout.py", timeout=1)
    text = render(event_for(result, status="error"))
    assert "Command timed out" in text
    assert "Tool infrastructure failure" not in text
    assert "Output truncated" in text


def test_local_gap_restores_snapshot_only_failure(failed_output):
    result, _ = failed_output
    output = StringIO()
    view = TerminalRunRenderer(Console(file=output, width=100), SimpleNamespace())
    bus = SessionEventBus(session_id="sid")
    part = {"id": "part-f04", "message_id": "msg-f04", "type": "tool", "tool": "bash", "call_id": "call",
            "state": {"status": "completed", "input": {}, "metadata": result.metadata, "output": str(result)}}
    messages = [{"role": "assistant", "content": "", "_nz_message_id": "msg-f04", "_nz_session_id": "sid",
                 "_nz_parts": [part]}]
    agent = SimpleNamespace(event_bus=bus, session_id="sid", _active_processor_messages=messages,
                            active_run_context=SimpleNamespace(interaction_run_id="run"))
    try:
        view.begin(agent)
        assert view._rebase_local_after_gap(view._subscription)
        view.drain()
        assert "exit code 7" in output.getvalue()
    finally:
        view.close()
        bus.close()


@pytest.mark.parametrize("name", ["read_file", "bash"])
def test_success_completion_does_not_erase_part_metadata(name):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.tool_platform.execution import ToolExecutionResult
    from nz_coder.protocol.run_view_reducer import RunViewReducer
    bus = SessionEventBus(session_id="sid")
    host = SimpleNamespace(tracer=SimpleNamespace(log=lambda *a, **k: None),
                           _emit_session_event=bus.publish)
    result = ToolExecutionResult(name, {}, "ok", True, False, False, False,
                                 metadata={"encoding": "utf-8", "exit": 0, "output": "ok"})
    reducer = RunViewReducer()
    try:
        reducer.apply_event(bus.publish("message.part.completed", {"part": {
            "id": "part", "call_id": "call", "type": "tool", "tool": name,
            "state": {"status": "completed", "metadata": result.metadata},
        }}))
        AgentLoop._trace_tool_result(host, result, "ok", "call", 0)
        reducer.apply_event(bus.recent()[-1])
        assert next(iter(reducer.state.tool_parts.values()))["state"]["metadata"]["encoding"] == "utf-8"
    finally:
        bus.close()


def test_timeout_facts_survive_message_record_normalization():
    from nz_coder.protocol.message_schema import message_records
    part = {"id": "part-timeout", "message_id": "msg-timeout", "type": "tool", "tool": "bash", "call_id": "call",
            "state": {"status": "error", "input": {}, "error": "private timeout details",
                      "metadata": {"exit": -9, "truncated": True, "termination": "timeout"}}}
    records = message_records([{"role": "assistant", "content": "", "_nz_message_id": "msg-timeout",
        "_nz_session_id": "sid", "_nz_parts": [part]}], "sid")
    state = records[0]["parts"][0]["state"]
    assert state["metadata"]["truncated"] is True
    assert "Command timed out" in state["output"]
    assert "private timeout details" not in json.dumps(records)


def test_actual_timeout_settlement_keeps_snapshot_facts(tmp_path):
    from nz_coder.runtime.session.session_processor import SessionProcessor
    from nz_coder.protocol.message_schema import message_records

    message = {"role": "assistant", "content": "", "_nz_message_id": "msg-settle", "_nz_session_id": "sid"}
    processor = SessionProcessor(message)
    calls = [{"id": "call", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]
    processor.register_tool_calls(calls)
    processor.start_tools(calls)
    (tmp_path / "wait.py").write_text("import time\nprint('x'*100000, flush=True)\ntime.sleep(10)")
    with scoped_workdir(tmp_path):
        result = run_bash(f"{shlex.quote(sys.executable)} wait.py", timeout=1)
    processor.settle_tool("call", str(result), failed=True, metadata=result.metadata)
    records = message_records([message], "sid")
    state = next(p for p in records[0]["parts"] if p["type"] == "tool")["state"]
    assert state["metadata"]["truncated"] is True
    assert state["metadata"]["termination"] == "timeout"
    assert "Command timed out" in state["output"]


def test_actual_attach_entry_restores_settled_failure(tmp_path, monkeypatch):
    """Exercise real HTTP + Native Runner + attach entry; only transport/input are offline."""
    import argparse
    import asyncio
    from pathlib import Path
    import threading
    import time
    from nz_coder.http_service.client import NZCoderClient
    from nz_coder.http_service.server import SessionHTTPService
    from nz_coder.interface import remote

    monkeypatch.syspath_prepend(str(Path(__file__).parent))
    from test_http_settlement import offline_transport
    offline_transport(monkeypatch)
    with scoped_workdir(tmp_path):
        service = SessionHTTPService(port=0, restore_saved_sessions=False)
    thread = threading.Thread(target=service.serve_forever)
    thread.start()

    class EndInput:
        def __init__(self, **kwargs):
            pass

        async def read_async(self):
            raise EOFError

        async def close_async(self):
            pass

    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        sid = client.create_session("acceptEdits")["id"]
        client.run(sid, "R1:F04 controlled shell failure")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for permission in client.pending_permissions(sid):
                client.reply_permission(sid, permission["id"], "once")
            snapshot = client.attach_snapshot(sid)
            if snapshot["settled"]:
                break
            time.sleep(.02)
        assert snapshot["settled"] and snapshot["session"]["status"] == "completed"
        monkeypatch.setattr(remote, "TerminalInput", EndInput)
        # Use the real token-file connection path, not a fake backend/snapshot.
        token = tmp_path / "attach-token"
        token.write_text(service.token)
        args = argparse.Namespace(url=service.base_url, token_file=str(token),
                                  session_id=sid, new=False, workspace_id=None,
                                  profile="default", state_root=str(tmp_path / "client-state"))
        output = StringIO()
        assert asyncio.run(remote._attach(args, Console(file=output, width=100))) == 0
        # The old scripted final response echoes result text into the history.
        # Only a real tool-card heading proves that attach restored the card.
        assert output.getvalue().count("Bash · bash") == 1
        assert "Output truncated" in output.getvalue()
    finally:
        service.shutdown()
        thread.join(timeout=5)
