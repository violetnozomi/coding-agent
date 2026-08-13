"""Tests for context-local runtime settings and verification guards."""
from __future__ import annotations

import ast
import io
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from nz_coder import config
from nz_coder.runtime.execution_context import (
    agent_timeout_seconds,
    broad_tests_blocked,
    max_agent_turns,
    max_parallel_tasks,
    scoped_broad_test_guard,
    scoped_runtime_overrides,
    set_broad_tests_blocked,
)
from nz_coder.runtime.workdir import current_workdir, scoped_workdir


class _ContextProbeAgent:
    def __init__(self, *args, **kwargs):
        self.observed = {
            "workdir": str(current_workdir()),
            "max_agent_turns": max_agent_turns(),
            "agent_timeout_seconds": agent_timeout_seconds(),
            "max_parallel_tasks": max_parallel_tasks(),
        }

    async def run(self, *args, **kwargs):
        return dict(self.observed)


def test_runtime_overrides_are_nested_and_restore_defaults():
    defaults = (
        max_agent_turns(),
        agent_timeout_seconds(),
        max_parallel_tasks(),
    )

    with scoped_runtime_overrides(
        max_agent_turns=7,
        agent_timeout_seconds=1.5,
        max_parallel_tasks=2,
    ):
        assert (max_agent_turns(), agent_timeout_seconds(), max_parallel_tasks()) == (
            7,
            1.5,
            2,
        )
        with scoped_runtime_overrides(max_parallel_tasks=3):
            assert (max_agent_turns(), agent_timeout_seconds(), max_parallel_tasks()) == (
                7,
                1.5,
                3,
            )
        assert max_parallel_tasks() == 2

    assert (max_agent_turns(), agent_timeout_seconds(), max_parallel_tasks()) == defaults


def test_runtime_context_is_isolated_across_threads(tmp_path):
    barrier = Barrier(2)

    def worker(name: str, turns: int, parallel: int, blocked: bool):
        root = tmp_path / name
        root.mkdir()
        with (
            scoped_workdir(root),
            scoped_runtime_overrides(
                max_agent_turns=turns,
                agent_timeout_seconds=turns / 10,
                max_parallel_tasks=parallel,
            ),
            scoped_broad_test_guard(),
        ):
            set_broad_tests_blocked(blocked)
            barrier.wait()
            return (
                current_workdir(),
                max_agent_turns(),
                agent_timeout_seconds(),
                max_parallel_tasks(),
                broad_tests_blocked(),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        alpha = pool.submit(worker, "alpha", 11, 2, True)
        beta = pool.submit(worker, "beta", 23, 5, False)
        results = [alpha.result(), beta.result()]

    assert results == [
        (tmp_path / "alpha", 11, 1.1, 2, True),
        (tmp_path / "beta", 23, 2.3, 5, False),
    ]
    assert broad_tests_blocked() is False


def test_bash_broad_test_guard_is_context_local(monkeypatch, tmp_path):
    from nz_coder.tools.bash import run_bash

    calls = []

    class FakeProcess:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))
            self.stdout = io.StringIO("ok")
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    with scoped_workdir(tmp_path), scoped_broad_test_guard(True):
        blocked = run_bash("pytest")
    with scoped_workdir(tmp_path), scoped_broad_test_guard(False):
        allowed = run_bash("pytest")

    assert blocked.startswith("Error: Broad test runner blocked")
    assert allowed == "ok"
    assert len(calls) == 1


def test_evaluation_workspace_scope_does_not_mutate_config(tmp_path):
    from nz_coder.evaluation.eval_runner import _temporary_workdir

    original_workdir = config.WORKDIR
    original_turns = config.MAX_AGENT_TURNS
    with _temporary_workdir(tmp_path, 9):
        assert current_workdir() == tmp_path.resolve()
        assert max_agent_turns() == 9
        assert config.WORKDIR is original_workdir
        assert config.MAX_AGENT_TURNS == original_turns

    assert current_workdir() == original_workdir.resolve()
    assert config.WORKDIR is original_workdir
    assert config.MAX_AGENT_TURNS == original_turns


def test_swebench_fork_inherits_runtime_context(tmp_path):
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    with (
        scoped_workdir(tmp_path),
        scoped_runtime_overrides(
            max_agent_turns=17,
            agent_timeout_seconds=4.5,
            max_parallel_tasks=3,
        ),
    ):
        result = _run_agent_attempt(
            _ContextProbeAgent,
            "system",
            None,
            [{"role": "user", "content": "probe"}],
            lambda _name, _output: None,
            timeout=2,
        )

    assert result == {
        "workdir": str(tmp_path.resolve()),
        "max_agent_turns": 17,
        "agent_timeout_seconds": 4.5,
        "max_parallel_tasks": 3,
    }


def test_production_modules_do_not_assign_runtime_config():
    project_root = Path(__file__).resolve().parents[1]
    package_root = project_root / "nz_coder"
    assignments: list[str] = []

    for path in package_root.rglob("*.py"):
        if path.name.endswith(".orig"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            elif isinstance(node, ast.AugAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in {"config", "_config"}
                ):
                    relative = path.relative_to(project_root)
                    assignments.append(f"{relative}:{node.lineno}:{target.value.id}.{target.attr}")

    assert assignments == []
