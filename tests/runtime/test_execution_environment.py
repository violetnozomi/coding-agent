"""准备环境必须在每个实际命令中生效，不污染宿主。"""

import hashlib
import os
import time

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir


def test_bash_uses_selected_environment_without_host_mutation(tmp_path):
    from nz_coder.runtime.process.execution_environment import (
        ExecutionEnvironment,
        scoped_execution_environment,
    )
    from nz_coder.tools.bash import execute_bash_local

    env = ExecutionEnvironment.capture({"NZ_WORKSPACE_SENTINEL": "child-only"})
    with scoped_workdir(tmp_path), scoped_execution_environment(env):
        out = execute_bash_local(
            "python -c 'import os; print(os.environ[\"NZ_WORKSPACE_SENTINEL\"])'"
        )
    assert "child-only" in out
    assert "NZ_WORKSPACE_SENTINEL" not in os.environ


def test_changed_input_stops_command_before_execution(tmp_path):
    from nz_coder.runtime.process.execution_environment import (
        ExecutionEnvironment,
        scoped_execution_environment,
    )
    from nz_coder.tools.bash import execute_bash_local

    lock = tmp_path / "lock.txt"
    lock.write_text("old")
    env = ExecutionEnvironment.capture(
        {}, inputs=((str(lock), hashlib.sha256(lock.read_bytes()).hexdigest()),)
    )
    lock.write_text("new")
    with scoped_workdir(tmp_path), scoped_execution_environment(env):
        out = execute_bash_local("touch launched")
    assert "environment_inputs_changed" in str(out) or "inputs changed" in str(out)
    assert not (tmp_path / "launched").exists()


@pytest.mark.parametrize("tty", [False, True])
def test_process_backend_uses_same_environment(tmp_path, tty):
    from nz_coder.runtime.process.execution_environment import (
        ExecutionEnvironment,
        scoped_execution_environment,
    )
    from nz_coder.runtime.process.process_service import ProcessService

    env = ExecutionEnvironment.capture({"NZ_WORKSPACE_SENTINEL": "process-child"})
    service = ProcessService(tmp_path)
    try:
        with scoped_execution_environment(env):
            handle = service.start(
                "python -c 'import os; print(os.environ[\"NZ_WORKSPACE_SENTINEL\"])'",
                cwd=tmp_path,
                owner_session_id="child",
                tty=tty,
            )
        until = time.monotonic() + 5
        while (
            service.get(handle.process_id).exit_code is None
            and time.monotonic() < until
        ):
            time.sleep(0.01)
        result = service.read(handle.process_id)
        assert "process-child" in result.output
    finally:
        service.close()


def test_selected_shell_is_not_login():
    from nz_coder.runtime.process.platform_runtime import ShellSpec, ShellKind

    assert "-lc" not in ShellSpec(ShellKind.BASH, "/bin/bash").argv(
        "echo ok", login=False
    )


def test_parent_broker_uses_prepared_environment_and_reports_stale_inputs(tmp_path):
    from dataclasses import replace
    from nz_coder.runtime.process.command_broker import ParentCommandScope
    from nz_coder.runtime.process.execution_environment import ExecutionEnvironment
    from tests.fixtures.process_lifecycle import make_command_authority
    from tests.runtime.test_command_broker import call

    lock = tmp_path / "lock"
    lock.write_text("v1")
    env = ExecutionEnvironment.capture(
        {"NZ_CHILD_SENTINEL": "from-parent-broker"},
        inputs=((str(lock), hashlib.sha256(lock.read_bytes()).hexdigest()),),
    )
    authority = replace(make_command_authority(tmp_path), environment=env)
    errors = []
    scope = ParentCommandScope(
        authority,
        deadline=time.monotonic() + 5,
        child_publisher=None,
        on_channel_error=errors.append,
    )
    scope.policy_ready(authority.execution_policy.fingerprint)
    try:
        result = call(
            scope,
            1,
            "bash",
            {
                "command": "python -c 'import os; print(os.environ[\"NZ_CHILD_SENTINEL\"])'"
            },
        )
        assert "from-parent-broker" in result
        lock.write_text("v2")
        result = call(scope, 2, "bash", {"command": "touch should-not-run"})
        assert "inputs changed" in str(result)
        assert not (tmp_path / "should-not-run").exists()
        assert errors == []
    finally:
        scope.close(deadline=time.monotonic() + 2)
