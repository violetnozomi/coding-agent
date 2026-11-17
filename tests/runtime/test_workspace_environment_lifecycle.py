"""正式子任务入口必须等待准备，并让只读 direct 保持轻量。"""

import json
import sys
import time
import pytest

from nz_coder.foundation.workspace_trust import (
    load_config_snapshot,
    scoped_config_snapshot,
)
from nz_coder.runtime.process.workdir import scoped_workdir


def test_sync_task_failed_check_never_constructs_provider(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent

    (tmp_path / "app.py").write_text("VALUE=1")
    snapshot = load_config_snapshot(
        tmp_path,
        environ={
            "NZ_SUBAGENT_WORKSPACE_JSON": json.dumps(
                {"checks": [[sys.executable, "-c", "raise SystemExit(7)"]]}
            )
        },
    )
    monkeypatch.setattr(subagent, "_resolve_subagent_route", lambda *_: ("test", {}))

    def provider(**_):
        raise AssertionError("Provider constructed before preparation passed")

    monkeypatch.setattr(subagent, "create_provider", provider)
    with (
        scoped_workdir(tmp_path),
        scoped_config_snapshot(snapshot),
        subagent.scoped_parent_context(session_id="parent", config_snapshot=snapshot),
    ):
        result = subagent.run_subagent(
            "edit app.py", agent_type="general-purpose", target_paths=["app.py"]
        )
    assert "checks failed" in str(result)
    assert result.metadata["child_status"] == "error"


def test_readonly_direct_skips_project_setup_and_checks(tmp_path):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings
    from tests.fixtures.process_lifecycle import make_preparation_scope

    snapshot = load_config_snapshot(
        tmp_path,
        environ={
            "NZ_SUBAGENT_WORKSPACE_JSON": json.dumps(
                {
                    "read_only_mode": "direct",
                    "setup": [
                        [sys.executable, "-c", 'raise RuntimeError("MUST NOT RUN")']
                    ],
                    "checks": [
                        [sys.executable, "-c", 'raise RuntimeError("MUST NOT RUN")']
                    ],
                }
            )
        },
    )
    state = subagent._new_subagent_state("parent", "explore", None)
    scope = make_preparation_scope(tmp_path, child=state["session_id"])
    try:
        prepared, _ = subagent.prepare_subagent_command_context(
            "inspect",
            state,
            workspace=tmp_path,
            config_snapshot=snapshot,
            settings=RunSettings.from_snapshot(snapshot),
            settlement_id="test",
            preparation=scope,
        )
        assert prepared.execution_root == tmp_path
        assert state["environment_preparation"]["level"] == "workspace"
        assert not (tmp_path / ".venv").exists()
    finally:
        scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("isolation", ["thread", "process"])
def test_background_prepare_failure_prevents_execution(
    tmp_path, monkeypatch, isolation
):
    from nz_coder.runtime.agent import agent_manager, subagent

    (tmp_path / "app.py").write_text("VALUE=1")
    snapshot = load_config_snapshot(
        tmp_path,
        environ={
            "NZ_SUBAGENT_WORKSPACE_JSON": json.dumps(
                {
                    "checks": [[sys.executable, "-c", "raise SystemExit(7)"]],
                }
            )
        },
    )
    calls = []
    monkeypatch.setattr(
        subagent, "run_subagent", lambda *a, **kw: calls.append("thread")
    )
    monkeypatch.setattr(
        agent_manager.SubagentProcessSupervisor,
        "start",
        lambda *a, **kw: calls.append("process"),
    )
    manager = agent_manager.BackgroundAgentManager(tmp_path, "parent")
    try:
        with scoped_config_snapshot(snapshot), scoped_workdir(tmp_path):
            result = manager.start(
                [
                    {
                        "prompt": "edit app.py",
                        "isolation": isolation,
                        "target_paths": ["app.py"],
                    }
                ]
            )
        task = result.metadata["task_ids"][0]
        assert manager._jobs[task].done_event.wait(5)
        state = manager._load_raw(task)
        assert calls == []
        assert state["status"] == "error"
        assert state["environment_preparation"]["status"] == "failed"
        assert state["environment_preparation"]["exit_code"] == 7
        assert state.get("source_copy_manifest")
    finally:
        manager.close(timeout=3)


def test_submodule_write_request_is_rejected_before_workspace_creation(tmp_path):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings
    from tests.fixtures.process_lifecycle import make_preparation_scope

    snapshot = load_config_snapshot(
        tmp_path,
        environ={
            "NZ_SUBAGENT_WORKSPACE_JSON": json.dumps({"submodules": ["vendor/lib"]})
        },
    )
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state["claimed_paths"] = ["vendor/lib/source.cpp"]
    scope = make_preparation_scope(tmp_path, child=state["session_id"])
    try:
        with pytest.raises(Exception, match="submodule"):
            subagent.prepare_subagent_workspace(
                state,
                workspace=tmp_path,
                config_snapshot=snapshot,
                settings=RunSettings.from_snapshot(snapshot),
                preparation=scope,
            )
        assert not state.get("worktree")
    finally:
        scope.close(deadline=time.monotonic() + 2)
