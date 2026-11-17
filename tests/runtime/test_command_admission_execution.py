"""普通开发命令应实际执行，授权和 Shell 写入观察不能相互矛盾。"""
from __future__ import annotations

import shutil
import shlex
import sys

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools.bash import execute_bash_local


def test_compile_workspace_glob_starts_real_python(tmp_path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "module.py").write_text("value = 1\n")
    with scoped_workdir(tmp_path):
        output = execute_bash_local(f"{shlex.quote(sys.executable)} -m py_compile pkg/*.py", timeout=5)
    assert output.metadata["exit"] == 0
    assert list((package / "__pycache__").glob("module.*.pyc"))


def test_exit_status_is_data_and_retains_actual_shell_result(tmp_path):
    with scoped_workdir(tmp_path):
        output = execute_bash_local('false; printf "status=%s\\n" "$?"', timeout=5)
    assert str(output) == "status=1"
    assert output.metadata["exit"] == 0


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("sed") is None, reason="Linux sed scenarios")
@pytest.mark.parametrize("flags,files,exit_code", [
    ("-i", "first.txt", 0),
    ("-i.bak", "first.txt", 0),
    ("-i", "first.txt second.txt", 0),
    ("-i", "first.txt missing.txt", 2),
])
def test_sed_preserves_native_write_backup_and_partial_failure(tmp_path, flags, files, exit_code):
    (tmp_path / "first.txt").write_text("old\n")
    (tmp_path / "second.txt").write_text("old\n")
    with scoped_workdir(tmp_path):
        output = execute_bash_local(f"sed {flags} 's/old/new/g' {files}", timeout=5)
    assert (output.metadata["exit"] == 0) is (exit_code == 0)
    assert (tmp_path / "first.txt").read_text() == "new\n"
    if flags == "-i.bak":
        assert (tmp_path / "first.txt.bak").read_text() == "old\n"
    if "second.txt" in files:
        assert (tmp_path / "second.txt").read_text() == "new\n"


def test_unreviewed_complex_shell_has_no_side_effect(tmp_path):
    with scoped_workdir(tmp_path):
        output = execute_bash_local('for item in one; do printf x > result.txt; done', timeout=5)
    assert "authorization" in str(output).lower()
    assert output.metadata["command_not_executed"]
    assert not (tmp_path / "result.txt").exists()


def test_approved_other_action_does_not_authorize_supported_write(tmp_path):
    from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization

    with scoped_workdir(tmp_path), scoped_command_authorization("bash", {"command": "printf approved", "timeout": 5}):
        output = execute_bash_local("printf unexpected > result.txt", timeout=5)
    assert "authorization" in str(output).lower()
    assert output.metadata["permission_denied"]
    assert not (tmp_path / "result.txt").exists()


def test_host_approval_does_not_override_private_file_restriction(tmp_path):
    from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization

    secret = tmp_path / ".env"
    secret.write_text("DO_NOT_READ=private\n")
    args = {"command": "cat .env", "timeout": 5}
    with scoped_workdir(tmp_path), scoped_command_authorization("bash", args):
        output = execute_bash_local(**args)
    assert str(output).startswith("Error:")
    assert "DO_NOT_READ=private" not in str(output)


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("sed") is None, reason="Linux sed scenarios")
def test_sed_actual_host_observation_invalidates_previous_verification(tmp_path):
    from test_verification_interfaces import _source
    from tests.runtime.test_workspace_scope_execution import _scope_host, _execute
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor

    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        host.executor = ToolExecutor(PermissionManager("auto"))
        compiled = _execute(host, "python -m py_compile pkg/module.py")
        assert not compiled.dispatch_failed and not compiled.command_failed
        assert host.verification_runtime.status()["verification_needed"] is False
        generation = host.runtime_state.mutation_generation
        result = _execute(host, "sed -i 's/1/2/g' pkg/module.py")
        assert not result.dispatch_failed
        assert result.metadata["exit"] == 0
        assert (tmp_path / "pkg/module.py").read_text() == "def value(): return 2\n"
        assert host.runtime_state.mutation_generation == generation + 1
        assert host.runtime_state.last_mutation_paths == ("pkg/module.py",)
        assert host.verification_runtime.status()["verification_needed"] is True


@pytest.mark.skipif(sys.platform != "linux" or shutil.which("sed") is None, reason="Linux sed scenarios")
def test_sed_partial_failure_still_invalidates_previous_verification(tmp_path):
    from test_verification_interfaces import _source
    from tests.runtime.test_workspace_scope_execution import _scope_host, _execute
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor

    _source(tmp_path)
    with scoped_workdir(tmp_path):
        host = _scope_host(tmp_path)
        host.executor = ToolExecutor(PermissionManager("auto"))
        compiled = _execute(host, "python -m py_compile pkg/module.py")
        assert not compiled.dispatch_failed and not compiled.command_failed
        assert not host.verification_runtime.status()["verification_needed"]
        result = _execute(host, "sed -i 's/1/2/g' pkg/module.py missing.py")
        assert result.command_failed and not result.dispatch_failed
        assert (tmp_path / "pkg/module.py").read_text() == "def value(): return 2\n"
        assert host.runtime_state.last_mutation_paths == ("pkg/module.py",)
        assert host.verification_runtime.status()["verification_needed"]


def test_default_permission_can_authorize_real_loop_once(tmp_path):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor

    asked = []
    args = {"command": "for item in one; do printf ok > result.txt; done", "timeout": 5}
    with scoped_workdir(tmp_path):
        permissions = PermissionManager("default", asker=lambda name, args: asked.append((name, args)) or "once")
        result = ToolExecutor(permissions).execute_one({"id": "approved-loop", "function": {"name": "bash", "arguments": args}}, 0)
    assert not result.dispatch_failed, result.output
    assert result.metadata["exit"] == 0
    assert (tmp_path / "result.txt").read_text() == "ok"
    assert asked == [("bash", args)]


def test_process_unknown_requires_authorization_then_reports_real_exit(tmp_path):
    import json
    from nz_coder.runtime.process.process_service import close_workspace_process_service
    from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization
    from nz_coder.tools.process import run_process
    from tests.fixtures.process_lifecycle import wait_until

    args = {"operation": "start", "command": "for item in one; do printf process-ok; done", "tty": False}
    try:
        with scoped_workdir(tmp_path):
            denied = run_process(**args)
            assert "authorization" in str(denied).lower()
            with scoped_command_authorization("process", args):
                started = json.loads(str(run_process(**args)))
            process_id = started["process"]["process_id"]
            observed = json.loads(str(run_process("read", process_id=process_id, cursor=0, wait_seconds=2)))
            assert "process-ok" in observed["output"]
            wait_until(lambda: json.loads(str(run_process("status", process_id=process_id)))["process"]["exit_code"] is not None)
            status = json.loads(str(run_process("status", process_id=process_id)))
            assert status["process"]["exit_code"] == 0
    finally:
        close_workspace_process_service(tmp_path)


@pytest.mark.parametrize("operation", ["start", "write"])
@pytest.mark.parametrize("mismatch", ["workspace", "session"])
def test_process_authorization_matches_actual_service_and_owner(tmp_path, operation, mismatch):
    from types import SimpleNamespace
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization
    from nz_coder.tools.process import execute_process_local

    approved = tmp_path / "approved"
    actual = tmp_path / "actual"
    approved.mkdir()
    actual.mkdir()
    args = ({"operation": "start", "command": "for item in one; do printf wrong > marker.txt; done", "tty": False}
            if operation == "start" else {"operation": "write", "process_id": "owned-process", "data": "wrong"})

    def unexpected_operation(*_args, **_kwargs):
        pytest.fail("A different service or owner must not receive this authorized action")

    service = SimpleNamespace(workspace=actual if mismatch == "workspace" else approved,
                              start=unexpected_operation, write=unexpected_operation)
    with scoped_workdir(approved), scoped_session("approved-session"), scoped_command_authorization("process", args):
        output = execute_process_local(args, service=service,
            owner_session_id="other-session" if mismatch == "session" else "approved-session",
            owner_agent_id="agent", event_bus=None)
    assert "authorization" in str(output).lower()
    assert output.metadata["command_not_executed"]
    assert not (actual / "marker.txt").exists()


@pytest.mark.skipif(shutil.which("curl") is None, reason="requires real curl")
def test_local_http_probe_can_be_composed_with_other_commands(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"healthy\n")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        with scoped_workdir(tmp_path):
            result = execute_bash_local(f"curl -sS http://127.0.0.1:{server.server_port}/health && printf done", timeout=5)
        assert result.metadata["exit"] == 0
        assert str(result) == "healthy\ndone"
    finally:
        server.shutdown()
        server.server_close()
        serving.join(2)
