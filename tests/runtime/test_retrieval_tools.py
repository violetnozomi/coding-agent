"""原工具真实结果经显式执行者保持；远程用例随后复用同一装配。"""

from dataclasses import replace
import os
import subprocess
import sys
import threading
import time

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools import ToolOutput, scoped_tool_cancellation
from tests.fixtures.process_lifecycle import make_auxiliary_runner


def executable(path, script):
    path.write_text("#!" + sys.executable + "\n" + script, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.parametrize("code", [0, 1, 2])
@pytest.mark.parametrize("mode", ["content", "count", "files_with_matches"])
def test_local_search_runner_preserves_results_and_metadata(
    tmp_path, monkeypatch, code, mode
):
    from nz_coder.tools import search

    binary = tmp_path / "bin"
    binary.mkdir()
    names = ["old.py", "空 格\n新.py"]
    for i, name in enumerate(names):
        (tmp_path / name).write_text("before\nneedle\nafter\n", encoding="utf-8")
        os.utime(tmp_path / name, (10 + i, 10 + i))
    rows = [
        {
            "type": "match",
            "data": {
                "path": {"text": name},
                "lines": {"text": "needle\n"},
                "line_number": 2,
                "absolute_offset": 7,
                "submatches": [],
            },
        }
        for name in names
    ]
    executable(
        binary / "rg",
        "import json,sys\nrows="
        + repr(rows)
        + "\nfor row in rows: print(json.dumps(row))\nraise SystemExit("
        + str(code)
        + ")\n",
    )
    monkeypatch.setenv("PATH", str(binary))
    runner = make_auxiliary_runner(tmp_path)
    kwargs = dict(pattern="needle", output_mode=mode, offset=0, head_limit=1, context=1)
    try:
        with scoped_workdir(tmp_path):
            expected = search.grep_search(**kwargs)
            actual = search.execute_grep_search_local(**kwargs, runner=runner)
        assert isinstance(actual, ToolOutput)
        assert (
            str(actual) == str(expected)
            and actual.metadata == expected.metadata
            and actual.title == expected.title
        )
        if code == 1:
            assert actual.metadata["matches"] == 0
        else:
            assert actual.metadata["matches"] == 2 and actual.metadata["truncated"]
            assert ("inaccessible" in actual) == (code == 2)
            assert str(tmp_path / names[1]) in actual
    finally:
        assert runner.close(deadline=time.monotonic() + 2)


def test_captured_path_and_no_rg_fallback(tmp_path, monkeypatch):
    from nz_coder.tools import search

    a, b = tmp_path / "bin-a", tmp_path / "bin-b"
    a.mkdir()
    b.mkdir()
    (tmp_path / "actual.py").write_text("needle\n")
    executable(a / "rg", 'print("actual.py")\n')
    executable(b / "rg", 'raise RuntimeError("wrong captured PATH")\n')
    monkeypatch.setenv("PATH", str(a))
    runner = make_auxiliary_runner(tmp_path)
    monkeypatch.setenv("PATH", str(b))
    try:
        with scoped_workdir(tmp_path):
            result = search.execute_glob_search_local("*.py", runner=runner)
        assert result.metadata["count"] == 1 and "actual.py" in result
    finally:
        assert runner.close(deadline=time.monotonic() + 2)
    monkeypatch.setenv("PATH", str(tmp_path / "missing-bin"))
    cancel = threading.Event()
    runner = make_auxiliary_runner(tmp_path, cancel_event=cancel)
    try:
        with scoped_workdir(tmp_path), scoped_tool_cancellation(cancel):
            assert "actual.py" in search.execute_grep_search_local(
                "needle", runner=runner
            )
            assert "actual.py" in search.execute_glob_search_local(
                "*.py", runner=runner
            )
            cancel.set()
            assert "cancelled" in search.execute_grep_search_local(
                "needle", runner=runner
            )
    finally:
        assert runner.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("git", [True, False])
def test_local_diff_runner_keeps_git_and_child_change_tracker(tmp_path, git):
    from nz_coder.tools import repo_intel
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session, session_change_dir
    from nz_coder.state.changes import ChangeTracker
    from nz_coder.tools.files import bind_tool_state

    if git:
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="demo"\n')
    runner = make_auxiliary_runner(tmp_path)
    try:
        with scoped_workdir(tmp_path), scoped_session("child"):
            if not git:
                tracker = ChangeTracker(
                    run_id="child-change", change_dir=session_change_dir()
                )
                tracker.record_before(
                    "pyproject.toml", True, '[project]\nname="demo"\n'
                )
                (tmp_path / "pyproject.toml").write_text('[project]\nname="changed"\n')
                tracker.record_after(
                    "pyproject.toml", True, '[project]\nname="changed"\n'
                )
            with bind_tool_state(change_tracker=tracker if not git else None):
                expected = repo_intel.diff_status()
                actual = repo_intel.execute_diff_status_local(runner=runner)
                assert actual == expected and "pyproject.toml" in actual
        with scoped_workdir(tmp_path), scoped_session("parent"):
            if not git:
                assert "pyproject.toml" not in repo_intel.execute_diff_status_local(
                    runner=runner
                )
    finally:
        assert runner.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("tool", ["grep_search", "glob_search", "diff_status"])
@pytest.mark.parametrize("outcome", ["success", "lost", "limit"])
def test_retrieval_rpc_preserves_public_result_and_never_falls_back(
    tmp_path, monkeypatch, tool, outcome
):
    from nz_coder.tools import search, repo_intel
    from nz_coder.runtime.process.command_channel import (
        ChildCommandClient,
        CommandChannelLimits,
        scoped_command_client,
    )
    from tests.fixtures.process_lifecycle import make_command_scope

    scope = make_command_scope(tmp_path, ready=False)
    scope.authority = replace(scope.authority, allowed_tools=frozenset({tool}))
    if outcome == "limit":
        limits = replace(
            CommandChannelLimits.from_settings(scope.authority.run_settings),
            max_message_bytes=2048,
        )
        monkeypatch.setattr(
            CommandChannelLimits,
            "from_settings",
            classmethod(lambda cls, settings: limits),
        )
    client = ChildCommandClient(
        scope.start(),
        limits=CommandChannelLimits.from_settings(scope.authority.run_settings),
        deadline=time.monotonic() + 5,
    )
    client.set_fact_receiver(lambda _fact: None)
    calls = []
    from nz_coder.protocol.attachments import make_image_attachment
    expected = ToolOutput(
        "body" if outcome != "limit" else "x" * 3_000_000,
        title="测试",
        metadata={"count": 2, "truncated": False},
        attachments=[make_image_attachment(b"\x89PNG\r\n\x1a\n", "image/png")],
    )
    module = repo_intel if tool == "diff_status" else search

    def local(*args, runner, **kwargs):
        assert threading.current_thread() is not threading.main_thread(), (
            "子方不允许本地重试"
        )
        assert runner is not None
        calls.append(threading.get_ident())
        return expected

    monkeypatch.setattr(module, "execute_" + tool + "_local", local)
    try:
        assert client.policy_ready(
            scope.authority.execution_policy, deadline=time.monotonic() + 1
        )
        if outcome == "lost":
            assert client.close(deadline=time.monotonic() + 2)
        with scoped_command_client(client):
            result = getattr(module, tool)(
                **({} if tool == "diff_status" else {"pattern": "needle"})
            )
        if outcome == "success":
            assert str(result) == str(expected)
            assert (
                result.title == expected.title
                and result.metadata == expected.metadata
                and result.attachments == expected.attachments
            )
        else:
            assert result.startswith("Error:")
        assert len(calls) == (0 if outcome == "lost" else 1)
    finally:
        client.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)
