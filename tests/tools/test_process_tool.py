from __future__ import annotations

import json
import shlex
import sys

from nz_coder.runtime.process.process_service import close_workspace_process_service
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.protocol.session_events import SessionEventBus, scoped_session_event_bus
from nz_coder.tools.process import run_process


def _payload(value: str) -> dict:
    return json.loads(str(value))


def test_process_tool_round_trip_and_session_scoped_status(tmp_path):
    script = tmp_path / "echo.py"
    script.write_text(
        "import sys\nprint('READY', flush=True)\n"
        "for line in sys.stdin:\n print('GOT:' + line.strip(), flush=True)\n",
        encoding="utf-8",
    )
    command = " ".join([
        shlex.quote(sys.executable), "-u", shlex.quote(str(script)),
    ])
    bus = SessionEventBus(session_id="tool-session", agent_id="agent-a")
    try:
        with scoped_workdir(tmp_path), scoped_session_event_bus(bus):
            started = _payload(run_process("start", command=command, tty=False))
            process_id = started["process"]["process_id"]
            first = _payload(run_process(
                "read", process_id=process_id, cursor=0, wait_seconds=2,
            ))
            assert "READY" in first["output"]
            _payload(run_process(
                "write", process_id=process_id, data="hello", append_newline=True,
            ))
            second = _payload(run_process(
                "read",
                process_id=process_id,
                cursor=first["next_cursor"],
                wait_seconds=2,
            ))
            assert "GOT:hello" in second["output"]
            status = _payload(run_process("status"))
            assert [item["process_id"] for item in status["processes"]] == [process_id]
            listed = _payload(run_process("list"))
            assert [item["process_id"] for item in listed["processes"]] == [process_id]
            killed = _payload(run_process("kill", process_id=process_id))
            assert killed["process"]["status"] == "killed"
    finally:
        close_workspace_process_service(tmp_path)
        bus.close()


def test_process_tool_rejects_workspace_escape_and_dangerous_command(tmp_path):
    with scoped_workdir(tmp_path):
        assert "escapes workspace" in run_process(
            "start", command="echo nope", workdir=str(tmp_path.parent),
        )
        assert "Dangerous command blocked" in run_process(
            "start", command="rm -rf /",
        )
    close_workspace_process_service(tmp_path)


def test_process_tool_reconnects_across_separate_run_event_scopes(tmp_path):
    script = tmp_path / "later.py"
    script.write_text(
        "import time\nprint('TURN_ONE', flush=True)\ntime.sleep(.2)\n"
        "print('TURN_TWO', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    command = " ".join([
        shlex.quote(sys.executable), "-u", shlex.quote(str(script)),
    ])
    first_bus = SessionEventBus(session_id="same-session", run_id="run-one")
    second_bus = SessionEventBus(session_id="same-session", run_id="run-two")
    try:
        with scoped_workdir(tmp_path), scoped_session_event_bus(first_bus):
            started = _payload(run_process("start", command=command, tty=False))
            process_id = started["process"]["process_id"]
            first = _payload(run_process(
                "read", process_id=process_id, cursor=0, wait_seconds=2,
            ))
        with scoped_workdir(tmp_path), scoped_session_event_bus(second_bus):
            second = _payload(run_process(
                "read",
                process_id=process_id,
                cursor=first["next_cursor"],
                wait_seconds=2,
            ))
            assert "TURN_TWO" in second["output"]
            assert "TURN_ONE" not in second["output"]
            _payload(run_process("kill", process_id=process_id))
    finally:
        close_workspace_process_service(tmp_path)
        first_bus.close()
        second_bus.close()


def test_process_permission_and_admission_are_operation_aware():
    from nz_coder.runtime.agent.admission import resolve_tool_capability
    from nz_coder.tool_platform.permissioning.checker import PermissionChecker

    checker = PermissionChecker("default")
    assert checker.check(
        "process", {"operation": "read"}, [], [], [],
    )["behavior"] == "allow"
    assert checker.check(
        "process", {"operation": "write"}, [], [], [],
    )["behavior"] == "ask"
    assert checker.check(
        "process", {"operation": "start", "command": "rm -rf /"}, [], [], [],
    )["behavior"] == "deny"
    assert resolve_tool_capability(
        "process", {"operation": "read"},
    ) == "read"
    assert resolve_tool_capability(
        "process", {"operation": "start", "command": "pytest -q"},
    ) == "bash:test"
