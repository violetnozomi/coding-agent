"""Tests for context-local runtime settings and verification guards."""
from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier


from nz_coder.foundation import config
from nz_coder.runtime.core.execution_context import (
    agent_timeout_seconds,
    max_agent_turns,
    max_parallel_tasks,
    nominal_agent_turns,
    scoped_runtime_overrides,
)
from nz_coder.runtime.process.workdir import current_workdir, scoped_workdir


class _ContextProbeAgent:
    def __init__(self, *args, **kwargs):
        self.observed = {
            "workdir": str(current_workdir()),
            "max_agent_turns": max_agent_turns(),
            "nominal_agent_turns": nominal_agent_turns(),
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


def test_nominal_turn_budget_is_context_local_and_bounded_by_hard_cap():
    from nz_coder.runtime.core import execution_context

    nominal_agent_turns = getattr(execution_context, "nominal_agent_turns", None)
    assert callable(nominal_agent_turns)
    default = nominal_agent_turns()

    with scoped_runtime_overrides(
        max_agent_turns=80,
        nominal_agent_turns=20,
    ):
        assert nominal_agent_turns() == 20
        with scoped_runtime_overrides(max_agent_turns=12):
            assert nominal_agent_turns() == 12
        assert nominal_agent_turns() == 20

    assert nominal_agent_turns() == default


def test_runtime_context_is_isolated_across_threads(tmp_path):
    barrier = Barrier(2)
    def worker(name, turns, parallel):
        root = tmp_path / name
        root.mkdir()
        with scoped_workdir(root), scoped_runtime_overrides(
            max_agent_turns=turns, agent_timeout_seconds=turns / 10,
            max_parallel_tasks=parallel,
        ):
            barrier.wait()
            return current_workdir(), max_agent_turns(), agent_timeout_seconds(), max_parallel_tasks()
    with scoped_runtime_overrides(max_agent_turns=31):
        with ThreadPoolExecutor(max_workers=2) as pool:
            alpha = pool.submit(worker, "alpha", 11, 2)
            beta = pool.submit(worker, "beta", 23, 5)
            assert [alpha.result(), beta.result()] == [
                (tmp_path / "alpha", 11, 1.1, 2),
                (tmp_path / "beta", 23, 2.3, 5),
            ]
        assert max_agent_turns() == 31


def test_declared_test_scope_stops_at_chinese_comma():
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    prompt = (
        "完成后必须运行 python -m pytest -q cron_engine/tests，"
        "并根据真实测试结果给出最终总结。"
    )

    assert declared_test_scopes(prompt) == ("cron_engine/tests",)


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


def test_swebench_spawn_restores_runtime_context(tmp_path):
    from nz_coder.swebench.orchestrator import _run_agent_attempt

    with (
        scoped_workdir(tmp_path),
        scoped_runtime_overrides(
            max_agent_turns=17,
            nominal_agent_turns=13,
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
        "nominal_agent_turns": 13,
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
