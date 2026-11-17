"""Real thread/spawn children inherit the admitted parent model consistently."""
from __future__ import annotations

from contextlib import contextmanager
import json

import pytest

from tests.fixtures.process_lifecycle import wait_until
from tests.fixtures.process_model_capture import (
    capture_model_boundary, spawned_model_capture,
)


@pytest.fixture
def model_case(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import (
        load_config_snapshot, scoped_config_snapshot,
    )
    from nz_coder.runtime.agent import process_supervisor, subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text('"""Read-only routing fixture."""\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("MODEL_ID", "configured-default")
    monkeypatch.setenv("SUBAGENT_EXPLORE_MODEL", "")
    monkeypatch.setenv("SUBAGENT_DEEP_MODEL", "deep-tier")
    monkeypatch.setenv("SUBAGENT_MAX_TURNS", "2")
    monkeypatch.setenv("SUBAGENT_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("SUBAGENT_BACKGROUND_MAX_CONCURRENT", "1")
    monkeypatch.setenv("NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED", "1")
    monkeypatch.setattr(
        process_supervisor, "_run_subagent_process", spawned_model_capture,
    )
    managers = []

    @contextmanager
    def run_context(*, parent_model=None, explore_model=""):
        monkeypatch.setenv("SUBAGENT_EXPLORE_MODEL", explore_model)
        snapshot = load_config_snapshot(workspace)
        manager = BackgroundAgentManager(workspace, "model-parent")
        managers.append(manager)
        with (
            scoped_workdir(workspace),
            scoped_config_snapshot(snapshot),
            subagent.scoped_parent_context(
                session_id="model-parent", model_id=parent_model or "",
                config_snapshot=snapshot,
            ),
            capture_model_boundary(workspace),
        ):
            yield workspace, manager

    try:
        yield run_context
    finally:
        directory = workspace / ".nz-coder"
        if directory.exists():
            (directory / "model-blocker-release").touch()
        for manager in managers:
            jobs = list(manager._jobs.values())
            manager.close(timeout=10)
            for job in jobs:
                job.thread.join(3)
                assert not job.thread.is_alive()
                if job.supervisor is not None:
                    assert job.supervisor.exited


def start(manager, *, isolation, label="subject", hint=None):
    task = {
        "name": label,
        "prompt": f"MODEL_CAPTURE:{label} Inspect app.py read-only; report the model used.",
        "target_paths": [], "read_only": True, "isolation": isolation,
    }
    if hint is not None:
        task["model_hint"] = hint
    output = manager.start([task])
    assert not str(output).startswith("Error:"), output
    return output.metadata["task_ids"][0]


def assert_model(workspace, manager, task_id, expected, *, source="parent"):
    result = manager.wait_for_result(task_id)
    assert result.status == "completed", result
    state = manager._load_raw(task_id)
    label = state["display_name"]
    captured = json.loads(
        (workspace / ".nz-coder" / f"model-capture-{label}.json").read_text(),
    )
    assert captured == {"selection_model": expected, "request_model": expected}
    assert state["model_id"] == expected
    assert result.model == expected
    assert state["child_result"]["model"] == expected
    assert state["route_facts"]["model_source"] == source
    assert state["route_facts"]["initial_model"] == expected
    assert state["route_facts"]["final_model"] == expected
    assert result.route_facts["model_source"] == source
    assert f"Captured model {expected}." in result.final_text


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_child_uses_explicit_parent_model_instead_of_config_default(model_case, isolation):
    with model_case(parent_model="request-parent") as (workspace, manager):
        task_id = start(manager, isolation=isolation)
        assert_model(workspace, manager, task_id, "request-parent")


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_child_without_explicit_parent_model_keeps_config_default(model_case, isolation):
    with model_case() as (workspace, manager):
        task_id = start(manager, isolation=isolation)
        assert_model(workspace, manager, task_id, "configured-default")


@pytest.mark.parametrize("isolation", ["thread", "process"])
@pytest.mark.parametrize("hint,expected", [("fast", "fast-tier"), ("deep", "deep-tier")])
def test_explicit_child_tier_still_overrides_parent(model_case, isolation, hint, expected):
    with model_case(parent_model="request-parent", explore_model="fast-tier") as (workspace, manager):
        task_id = start(manager, isolation=isolation, hint=hint)
        assert_model(workspace, manager, task_id, expected, source="tier")


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_explore_override_without_hint_remains_effective(model_case, isolation):
    with model_case(parent_model="request-parent", explore_model="fast-tier") as (workspace, manager):
        task_id = start(manager, isolation=isolation)
        assert_model(workspace, manager, task_id, "fast-tier")


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_admitted_parent_model_is_frozen_while_child_waits_for_capacity(model_case, isolation):
    from nz_coder.runtime.agent import subagent

    with model_case(parent_model="request-parent") as (workspace, manager):
        blocker = start(manager, isolation="thread", label="blocker")
        ready = workspace / ".nz-coder" / "model-blocker-ready"
        release = workspace / ".nz-coder" / "model-blocker-release"
        try:
            wait_until(ready.exists, timeout=5)
            task_id = start(manager, isolation=isolation)
            assert manager._load_raw(task_id)["status"] == "queued"
            # 接收任务后的父方切换不能影响已在队列中的任务来源。
            with subagent.scoped_parent_context(model_id="changed-after-admission"):
                release.touch()
                assert_model(workspace, manager, task_id, "request-parent")
                assert manager.wait_for_result(blocker).status == "completed"
        finally:
            release.touch()
