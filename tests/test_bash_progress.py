"""Tests for Bash running metadata and structured final results."""
from __future__ import annotations

import shlex
import sys

from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.tools import ToolOutput, scoped_tool_metadata_reporter
from nz_coder.tools.bash import run_bash


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
    from nz_coder import config

    payload = "中文错误 日本語".encode("gbk", errors="replace")
    script = f"import os; os.write(1, {payload!r})"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    monkeypatch.setattr(config, "PROCESS_OUTPUT_ENCODING", "cp936", raising=False)

    with scoped_workdir(tmp_path):
        result = run_bash(command, timeout=2)

    assert str(result) == payload.decode("cp936")
