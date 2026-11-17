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
            from nz_coder.permissions import PermissionManager
            from nz_coder.runtime.execution.tool_executor import ToolExecutor

            denied = run_process("write", process_id=process_id, data="unapproved\n")
            assert "authorization" in denied.lower()
            write = ToolExecutor(PermissionManager("auto")).execute_one({
                "id": "authorized-stdin", "function": {"name": "process", "arguments": {
                    "operation": "write", "process_id": process_id,
                    "data": "hello", "append_newline": True,
                }},
            }, 0)
            assert not write.dispatch_failed, write.output
            _payload(write.output)
            second = _payload(run_process(
                "read",
                process_id=process_id,
                cursor=first["next_cursor"],
                wait_seconds=2,
            ))
            assert "GOT:hello" in second["output"]
            assert "unapproved" not in second["output"]
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
        assert "outside workspace" in run_process(
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


def test_process_kill_output_keeps_running_until_cleanup_done(tmp_path, monkeypatch):
    from nz_coder.runtime.process import process_service as processes
    from tests.fixtures.process_lifecycle import ControlledBackend

    backend = ControlledBackend()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    bus = SessionEventBus(session_id="tool-pending")
    try:
        with scoped_workdir(tmp_path), scoped_session_event_bus(bus):
            service = processes.workspace_process_service(tmp_path)
            service._kill_grace_override = 0
            started = _payload(run_process("start", command="owned"))
            process_id = started["process"]["process_id"]
            killed = _payload(run_process("kill", process_id=process_id))
            assert set(killed) == {"operation", "process"}
            assert killed["process"]["status"] == "running"
            assert killed["process"]["exit_code"] is None
            assert not any(
                e.type in {"process.killed", "process.exited"} for e in bus.recent(20)
            )
            backend.recover()
            assert service._records[process_id].cleanup_done.wait(2)
            assert (
                _payload(run_process("status", process_id=process_id))["process"][
                    "status"
                ]
                == "killed"
            )
    finally:
        backend.recover()
        close_workspace_process_service(tmp_path)
        bus.close()


def test_process_local_uses_service_root_even_when_thread_workspace_differs(tmp_path):
    from types import SimpleNamespace
    from nz_coder.tools.process import execute_process_local
    root = tmp_path / 'owner'
    root.mkdir()
    (root / 'sub').mkdir()
    other = tmp_path / 'thread'
    other.mkdir()
    def start(*args, **kwargs):
        raise AssertionError('rejected command reached spawn')
    service = SimpleNamespace(workspace=root, start=start)
    with scoped_workdir(other):
        result = execute_process_local({'operation':'start', 'command':'cat ../../secret', 'workdir':'sub'},
            service=service, owner_session_id='owner', owner_agent_id='a', event_bus=None)
    assert 'outside workspace' in result


def test_shell_admission_requests_approval_for_unknown_curl(tmp_path):
    from nz_coder.tool_platform.permissioning.checker import PermissionChecker
    checker = PermissionChecker('auto', workspace_trusted=True)
    with scoped_workdir(tmp_path):
        for name, arguments in [('bash', {}), ('process', {'operation': 'start'})]:
            result = checker.check(name, {**arguments, 'command':'curl -K config http://127.0.0.1:8123'}, [], [], [])
            assert result['behavior'] == 'ask'


def test_unknown_curl_capture_cannot_spawn_without_authorization(tmp_path):
    from types import SimpleNamespace
    from nz_coder.tools.process import execute_process_local
    from nz_coder.tool_platform.permissioning.checker import PermissionChecker
    command='''B=http://127.0.0.1:8123; curl -o '#1' "$B/{../escaped,safe}"'''
    def spawn(*args,**kwargs): raise AssertionError('must not spawn')
    with scoped_workdir(tmp_path):
        checker=PermissionChecker('auto')
        for tool,arguments in [('bash',{}),('process',{'operation':'start'})]:
            assert checker.check(tool,{**arguments,'command':command},[],[],[])['behavior']=='ask'
        output=execute_process_local({'operation':'start','command':command},
            service=SimpleNamespace(workspace=tmp_path,start=spawn),owner_session_id='s',owner_agent_id='a',event_bus=None)
        assert 'authorization' in output.lower()
