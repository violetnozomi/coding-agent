"""Tests for Bash running metadata and structured final results."""
from __future__ import annotations

import shlex
import sys
import json

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import ToolOutput, scoped_tool_metadata_reporter
from nz_coder.tools.bash import run_bash


def test_bash_rejects_nonfinite_timeout_without_raising(tmp_path):
    """Tool handlers retain their Error-string contract for hostile numbers."""
    with scoped_workdir(tmp_path):
        result = run_bash("echo unreachable", timeout=float("inf"))

    assert result == "Error: timeout must be an integer"


def test_bash_reports_live_output_and_returns_final_metadata(tmp_path):
    updates: list[tuple[str, dict]] = []
    script = (
        "import time; "
        "print('first', flush=True); "
        "time.sleep(0.15); "
        "print('second', flush=True)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    with scoped_workdir(tmp_path), scoped_tool_metadata_reporter(
        lambda title, metadata: updates.append((title, metadata)),
    ):
        result = run_bash(command, timeout=2)

    assert isinstance(result, ToolOutput)
    assert str(result) == "first\nsecond"
    assert result.metadata["exit"] == 0
    assert result.metadata["output"] == "first\nsecond"
    assert result.metadata["workdir"] == str(tmp_path)
    assert result.metadata["truncated"] is False
    assert updates[0][1]["output"] == ""
    assert any("first" in update[1]["output"] for update in updates[1:])


def test_tool_executor_keeps_full_bash_output_for_unified_projection(
    tmp_path, monkeypatch,
):
    """Only progress metadata may truncate before result projection."""
    from nz_coder.foundation import config

    payload = "HEAD-" + "x" * 80 + "-TAIL"
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(f'print({payload!r})')}"
    )
    updates: list[tuple[str, dict]] = []
    monkeypatch.setattr(config, "CONTEXT_TRUNCATE_CHARS", 32)

    with scoped_workdir(tmp_path), scoped_tool_metadata_reporter(
        lambda title, metadata: updates.append((title, metadata)),
    ):
        result = ToolExecutor(PermissionManager("auto")).execute_one({
            "id": "full-bash-result",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": command, "timeout": 2}),
            },
        }, 0)

    assert result.output == payload
    assert result.metadata["truncated"] is True
    assert result.metadata["output"] != payload
    assert "characters omitted" in result.metadata["output"]
    assert all(update[1]["output"] != payload for update in updates)


def test_bash_timeout_keeps_error_contract_and_reports_initial_state(tmp_path):
    updates: list[tuple[str, dict]] = []
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(2)')}"

    with scoped_workdir(tmp_path), scoped_tool_metadata_reporter(
        lambda title, metadata: updates.append((title, metadata)),
    ):
        result = run_bash(command, timeout=1)

    assert result == "Error: Command timed out (1s)"
    assert updates
    assert updates[0][1]["workdir"] == str(tmp_path)


def test_bash_workdir_runs_in_workspace_subdirectory(tmp_path):
    child = tmp_path / "pkg"
    child.mkdir()

    with scoped_workdir(tmp_path):
        result = run_bash("pwd", workdir="pkg")

    assert str(result) == str(child)
    assert result.metadata["workdir"] == str(child)


def test_bash_workdir_rejects_workspace_escape(tmp_path):
    with scoped_workdir(tmp_path):
        result = run_bash("pwd", workdir="../outside")

    assert result.startswith("Error: ")
    assert "workdir escapes workspace" in result


def test_bash_decodes_configured_windows_codepage_from_raw_bytes(tmp_path, monkeypatch):
    from nz_coder.foundation import config

    payload = "中文错误 日本語".encode("gbk", errors="replace")
    script = f"import os; os.write(1, {payload!r})"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    monkeypatch.setattr(config, "PROCESS_OUTPUT_ENCODING", "cp936", raising=False)

    with scoped_workdir(tmp_path):
        result = run_bash(command, timeout=2)

    assert str(result) == payload.decode("cp936")


def test_bash_pipeline_preserves_upstream_nonzero_exit(tmp_path):
    """G4: Bash pipefail keeps a failed producer visible through tail."""
    command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote('raise SystemExit(3)')} | tail -1"
    )

    with scoped_workdir(tmp_path):
        result = run_bash(command, timeout=2)

    assert isinstance(result, ToolOutput)
    assert result.metadata["exit"] == 3


def test_sh_rejects_verification_pipeline_without_pipefail(tmp_path, monkeypatch):
    from nz_coder.runtime.process.platform_runtime import ShellKind, ShellSpec
    import nz_coder.tools.bash as bash_module

    monkeypatch.setattr(
        bash_module,
        "select_shell",
        lambda: ShellSpec(ShellKind.SH, "/bin/sh"),
    )
    with scoped_workdir(tmp_path):
        result = run_bash("python -m pytest -q | tail -1", timeout=2)

    assert str(result).startswith("Error: ")
    assert "pipefail" in str(result)
    assert "directly" in str(result)


def test_tool_executor_marks_real_failed_pipelines_as_command_failed(tmp_path):
    """G4 end-to-end: shell metadata reaches the canonical executor result."""
    import json

    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor

    commands = (
        (
            f"{shlex.quote(sys.executable)} -c "
            f"{shlex.quote('raise SystemExit(3)')} | tail -1"
        ),
        f"{shlex.quote(sys.executable)} -m pytest -q missing_test.py | tail -1",
    )
    with scoped_workdir(tmp_path):
        results = [
            ToolExecutor(PermissionManager("auto")).execute_one({
                "id": f"pipeline-{index}",
                "function": {
                    "name": "bash",
                    "arguments": json.dumps({"command": command, "timeout": 10}),
                },
            }, 0)
            for index, command in enumerate(commands)
        ]

    assert [result.command_failed for result in results] == [True, True]
    assert [result.metadata["exit"] != 0 for result in results] == [True, True]
