"""Source-parity tests for inert reusable workflow capsules and lifecycle."""
from __future__ import annotations

import os
import json
from pathlib import Path

import pytest


def _manifest(name: str = "saved-review") -> dict:
    return {
        "name": name,
        "description": "Produce one evidence-grounded review.",
        "phases": ["final"],
        "read_only": True,
        "planned_agents": 1,
        "max_agents": 1,
        "max_concurrency": 1,
        "patterns": ["fan-out-and-synthesize"],
    }


def _plan() -> dict:
    return {
        "phases": [{
            "name": "final",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Return confirmed evidence only.",
            "artifact": "final-review",
        }],
    }


def _capsule(**overrides) -> dict:
    from nz_coder.runtime.workflow_capsule import create_workflow_capsule

    values = {
        "manifest": _manifest(),
        "plan": _plan(),
        "min_nzcoder_version": "0.1.0",
        "intent": {
            "task_class": "review",
            "patterns": ["fan-out-and-synthesize"],
            "reusable_for": ["bounded reviews"],
        },
        "provenance": {
            "created_at": "2026-08-09T00:00:00Z",
            "nzcoder_version": "0.1.0",
        },
    }
    values.update(overrides)
    return create_workflow_capsule(**values)


def test_capsule_is_json_only_versioned_and_rejects_source():
    from nz_coder.runtime.workflow_capsule import validate_workflow_capsule

    capsule = _capsule()
    assert capsule["format"] == "nzcoder.workflow"
    assert capsule["workflow_api_version"] == 1
    assert capsule["plan"]["manifest"] == capsule["manifest"]

    with pytest.raises(ValueError, match="unsupported field: source"):
        validate_workflow_capsule({**capsule, "source": "os.system('bad')"})


def test_capsule_preflight_reports_version_environment_and_inventory():
    from nz_coder.runtime.workflow_capsule import (
        create_workflow_capsule,
        preflight_workflow_capsule,
    )

    capsule = create_workflow_capsule(
        manifest=_manifest(),
        plan=_plan(),
        min_nzcoder_version="9.0.0",
        requires={
            "environment": ["git-repo", "worktree-capable"],
            "tools": ["read_file", "missing_tool"],
            "mcp": ["issues"],
            "skills": ["review"],
            "model_tiers": ["deep"],
        },
    )
    result = preflight_workflow_capsule(capsule, {
        "nzcoder_version": "0.1.0",
        "is_git_repo": False,
        "worktree_capable": False,
        "available_tools": ["read_file"],
        "available_mcp": [],
        "available_skills": [],
        "available_model_tiers": ["fast", "balanced", "deep"],
    })
    requirements = {item["requirement"] for item in result["issues"]}

    assert result["ok"] is False
    assert {
        "nzcoder:min-version",
        "environment:git-repo",
        "environment:worktree-capable",
        "tools:missing_tool",
        "mcp:issues",
        "skills:review",
    } <= requirements


def test_capsule_discovery_project_overrides_personal_and_ignores_symlink(tmp_path):
    from nz_coder.runtime.workflow_library import (
        discover_workflow_capsules,
        load_workflow_capsule,
        save_workflow_capsule,
    )

    workspace = tmp_path / "workspace"
    personal = tmp_path / "personal"
    workspace.mkdir()
    personal.mkdir()
    personal_ref = save_workflow_capsule(
        "review", _capsule(), scope="personal",
        workspace=workspace, personal_dir=personal,
    )
    project_ref = save_workflow_capsule(
        "review", _capsule(), scope="project",
        workspace=workspace, personal_dir=personal,
    )
    project_dir = Path(project_ref["path"]).parent
    (project_dir / "linked.workflow.json").symlink_to(personal_ref["path"])

    refs = discover_workflow_capsules(workspace, personal)
    capsule, ref = load_workflow_capsule(
        "review", workspace=workspace, personal_dir=personal
    )

    assert refs == [project_ref]
    assert ref["source"] == "project"
    assert capsule["manifest"]["name"] == "saved-review"
    assert os.stat(project_ref["path"]).st_mode & 0o777 == 0o600


def test_saved_capsule_preflights_and_executes_through_real_runtime(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_library import save_workflow_capsule
    from nz_coder.runtime.workflow_runtime import workflow_run
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    save_workflow_capsule("review", _capsule(), workspace=tmp_path)

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(capsule_name="review")

    outcome = result.metadata["workflow_result"]
    assert outcome["result"]["final_text"] == "SYNTHESIZED RESULT"
    assert outcome["capsule_ref"]["source"] == "project"
    assert outcome["capsule_preflight"]["ok"] is True
    assert manager.spawned_count() == 1
    record_path = manager._workflow.root / "runs" / outcome["run_id"] / "run.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["capsule_ref"] == outcome["capsule_ref"]


def test_persisted_run_and_artifact_read_then_recoverable_archive(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_lifecycle import (
        workflow_run_archive,
        workflow_runs,
    )
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    WorkflowRuntime(manager, run_id="run-capsule-history").execute(_plan())

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        history = workflow_runs("list")
        artifact = workflow_runs(
            "artifact",
            run_id="run-capsule-history",
            artifact="final-review",
        )
        archived = workflow_run_archive(
            ["run-capsule-history"],
            confirm=True,
        )

    assert history.metadata["workflow_runs"][0]["run_id"] == "run-capsule-history"
    assert artifact.metadata["workflow_artifact"]["final_text"] == "SYNTHESIZED RESULT"
    archived_item = archived.metadata["archived_workflow_runs"][0]
    assert archived_item["run_id"] == "run-capsule-history"
    assert Path(archived_item["trash_path"]).is_dir()
    assert "recoverable trash" in archived


def test_archive_preflights_all_ids_before_moving_any_run(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_lifecycle import workflow_run_archive
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    WorkflowRuntime(manager, run_id="run-stays").execute(_plan())
    run_dir = manager._workflow.root / "runs" / "run-stays"

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run_archive(
            ["run-stays", "missing-run"],
            confirm=True,
        )

    assert result == "Error: workflow run records not found: missing-run"
    assert run_dir.is_dir()
