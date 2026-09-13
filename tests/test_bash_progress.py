"""Tests for Bash running metadata and structured final results."""
from __future__ import annotations

import shlex
import sys
import json
import threading
from dataclasses import replace

from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import scoped_session
from nz_coder.tools import ToolOutput, scoped_tool_metadata_reporter
from nz_coder.tools.bash import run_bash
from nz_coder.tool_platform.artifacts import ArtifactStore
from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector


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


def test_long_failed_bash_preserves_middle_diagnostic_for_artifact_recovery(tmp_path):
    """A child failure keeps middle diagnostics in a recoverable artifact."""
    marker = "PYTEST_FAILURE_MIDDLE::test_unique_case::assert 7 == 9"
    script = (
        "print('START-OUTPUT'); "
        "print('x' * 2500); "
        f"print({marker!r}); "
        "print('x' * 2500); "
        "print('END-OUTPUT'); raise SystemExit(1)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    baseline = RunSettings.from_legacy_globals()
    small = replace(
        baseline,
        process_buffer_bytes=1024,
        bash_output_hard_limit_bytes=64 * 1024,
    )
    saved: list[str] = []
    with scoped_workdir(tmp_path), scoped_session("long-failed-evidence"), scoped_run_settings(baseline):
        complete = run_bash(command, timeout=2)
    assert marker in str(complete)

    with scoped_workdir(tmp_path), scoped_session("long-failed-evidence"), scoped_run_settings(small):
        bounded = ToolExecutor(PermissionManager("auto")).execute_one({
            "id": "failed-test",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": command, "timeout": 2}),
            },
        }, 0)
        projected = ToolResultProjector(
            budget=ToolResultBudget(max_tokens=120),
            artifact_writer=lambda _call_id, output: (
                saved.append(output) or ".nz-coder/artifacts/failed-test.txt"
            ),
        ).project_batch([(
            "failed-test",
            bounded.name,
            bounded.output,
            bounded.metadata.get("raw_artifact_id"),
            bounded.metadata.get("raw_artifact_complete", True),
        )], max_tokens=120)[0]

    assert bounded.metadata["truncated"] is True
    assert marker not in bounded.output
    assert projected.metadata["truncated"] is True
    artifact_id = bounded.metadata["raw_artifact_id"]
    assert projected.artifact_path == artifact_id
    assert marker in ArtifactStore(tmp_path, "long-failed-evidence").read(artifact_id)
    from nz_coder.tools import artifacts as _artifact_registration  # noqa: F401

    with scoped_workdir(tmp_path), scoped_session("long-failed-evidence"):
        recovered = ToolExecutor(PermissionManager("auto")).execute_one({
            "id": "read-failed-evidence",
            "function": {
                "name": "read_tool_result",
                "arguments": json.dumps({"artifact_id": artifact_id, "max_bytes": 64 * 1024}),
            },
        }, 1)
    assert recovered.executed is True
    assert recovered.dispatch_failed is False
    assert marker in recovered.output
    assert saved == []

    messages: list[dict] = []
    ProductionToolResultProjector(projector=ToolResultProjector(
        budget=ToolResultBudget(max_tokens=120),
    )).consume(
        ToolProjectionContext(
            signal_from_metadata=lambda _metadata: None,
            record_result=lambda _result: False,
            trace_result=lambda *_args, **_kwargs: None,
            stall_orchestrator=None,
            after_result=lambda _messages, _result, _output: None,
        ),
        [(0, {
            "id": "failed-test",
            "function": {"name": "bash", "arguments": {}},
        }, bounded)],
        messages,
    )
    assert artifact_id in messages[0]["content"]


def test_long_bash_marks_recovery_incomplete_when_artifact_save_fails(tmp_path, monkeypatch):
    from nz_coder.tools import bash as bash_module

    class FailingArtifactStore:
        def __init__(self, *_args, **_kwargs):
            pass

        def put(self, _output, *, kind):
            assert kind == "tool-result"
            raise OSError("artifact quota exhausted")

    script = "print('x' * 5000); raise SystemExit(1)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    baseline = RunSettings.from_legacy_globals()
    small = replace(
        baseline,
        process_buffer_bytes=1024,
        bash_output_hard_limit_bytes=64 * 1024,
    )
    monkeypatch.setattr(bash_module, "ArtifactStore", FailingArtifactStore)

    with scoped_workdir(tmp_path), scoped_run_settings(small):
        result = run_bash(command, timeout=2)

    assert result.metadata["output_incomplete"] is True
    assert result.metadata["raw_artifact_complete"] is False
    assert result.metadata["raw_artifact_error"] == "Artifact persistence failed"


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


def test_bash_large_output_retains_head_tail_with_bounded_progress(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config

    script = "import sys; sys.stdout.write('HEAD-' + 'x' * 200000 + '-TAIL')"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    updates = []
    monkeypatch.setattr(config, "PROCESS_BUFFER_BYTES", 4096)
    monkeypatch.setattr(config, "BASH_OUTPUT_HARD_LIMIT_BYTES", 400000)

    with scoped_workdir(tmp_path), scoped_tool_metadata_reporter(
        lambda _title, metadata: updates.append(metadata),
    ):
        result = run_bash(command, timeout=5)

    assert str(result).startswith("HEAD-")
    assert str(result).endswith("-TAIL")
    assert "bytes omitted" in str(result)
    assert result.metadata["total_output_bytes"] > 200000
    assert result.metadata["retained_output_bytes"] <= 5000
    assert max(len(item.get("output", "")) for item in updates) <= 5000


def test_bash_output_hard_limit_terminates_producer_without_deadlock(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config

    script = "import os\nwhile True: os.write(1, b'z' * 8192)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    monkeypatch.setattr(config, "PROCESS_BUFFER_BYTES", 2048)
    monkeypatch.setattr(config, "BASH_OUTPUT_HARD_LIMIT_BYTES", 32768)

    with scoped_workdir(tmp_path):
        result = run_bash(command, timeout=5)

    assert "output limit exceeded" in str(result).lower()
    assert result.metadata["output_limit_exceeded"] is True
    assert result.metadata["total_output_bytes"] <= 65536
    assert not any(thread.name == "nz-bash-output" for thread in threading.enumerate())
