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
    from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule

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


def _trust_project_control(workspace: Path, monkeypatch) -> None:
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    trust_path = workspace.parent / f"{workspace.name}-workflow-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace, "workspace-control", snapshot.control_fingerprint
    )


def test_capsule_is_json_only_versioned_and_rejects_source():
    from nz_coder.runtime.workflows.workflow_capsule import validate_workflow_capsule

    capsule = _capsule()
    assert capsule["format"] == "nzcoder.workflow"
    assert capsule["workflow_api_version"] == 1
    assert capsule["plan"]["manifest"] == capsule["manifest"]

    with pytest.raises(ValueError, match="unsupported field: source"):
        validate_workflow_capsule({**capsule, "source": "os.system('bad')"})


def test_capsule_preflight_reports_version_environment_and_inventory():
    from nz_coder.runtime.workflows.workflow_capsule import (
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


def test_capsule_discovery_project_overrides_personal_when_trusted(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows.workflow_library import (
        discover_workflow_capsules,
        load_workflow_capsule,
        save_workflow_capsule,
    )

    workspace = tmp_path / "workspace"
    personal = tmp_path / "personal"
    workspace.mkdir()
    personal.mkdir()
    save_workflow_capsule(
        "review", _capsule(), scope="personal",
        workspace=workspace, personal_dir=personal,
    )
    project_ref = save_workflow_capsule(
        "review", _capsule(), scope="project",
        workspace=workspace, personal_dir=personal,
    )
    _trust_project_control(workspace, monkeypatch)

    refs = discover_workflow_capsules(workspace, personal)
    capsule, ref = load_workflow_capsule(
        "review", workspace=workspace, personal_dir=personal
    )

    assert refs == [project_ref]
    assert ref["source"] == "project"
    assert capsule["manifest"]["name"] == "saved-review"
    assert os.stat(project_ref["path"]).st_mode & 0o777 == 0o600


def test_untrusted_project_workflow_is_not_discovered_or_loaded(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows.workflow_library import (
        discover_workflow_capsules,
        load_workflow_capsule,
        save_workflow_capsule,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    save_workflow_capsule("repo-only", _capsule(), workspace=workspace)
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "empty-trust.json")
    )

    assert discover_workflow_capsules(workspace) == []
    with pytest.raises(ValueError, match="not trusted"):
        load_workflow_capsule("repo-only", workspace=workspace, source="project")


def test_untrusted_project_workflow_cannot_override_personal(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows.workflow_library import (
        load_workflow_capsule,
        save_workflow_capsule,
    )

    workspace = tmp_path / "workspace"
    personal = tmp_path / "personal"
    workspace.mkdir()
    personal.mkdir()
    personal_manifest = _manifest()
    personal_manifest["description"] = "personal"
    personal_capsule = _capsule(manifest=personal_manifest)
    project_manifest = _manifest()
    project_manifest["description"] = "project"
    project_capsule = _capsule(manifest=project_manifest)
    save_workflow_capsule(
        "review", personal_capsule, scope="personal",
        workspace=workspace, personal_dir=personal,
    )
    save_workflow_capsule(
        "review", project_capsule, scope="project",
        workspace=workspace, personal_dir=personal,
    )
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "empty-trust.json")
    )

    capsule, ref = load_workflow_capsule(
        "review", workspace=workspace, personal_dir=personal
    )
    assert ref["source"] == "personal"
    assert capsule["manifest"]["description"] == "personal"


def test_trusted_project_workflow_loads_until_exact_control_change(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.workflows.workflow_library import (
        load_workflow_capsule,
        save_workflow_capsule,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ref = save_workflow_capsule("review", _capsule(), workspace=workspace)
    _trust_project_control(workspace, monkeypatch)

    _capsule_value, trusted_ref = load_workflow_capsule("review", workspace=workspace)
    assert trusted_ref["source"] == "project"

    Path(ref["path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="not found"):
        load_workflow_capsule("review", workspace=workspace)


def test_workflow_project_library_rejects_workspace_symlink_escape(tmp_path):
    from nz_coder.runtime.workflows.workflow_library import save_workflow_capsule

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes workspace"):
        save_workflow_capsule("review", _capsule(), workspace=workspace)
    assert not (outside / "workflows").exists()


def test_workflow_capsule_rejects_nonstandard_json_numbers(tmp_path):
    from nz_coder.runtime.workflows.workflow_library import (
        load_workflow_capsule,
        save_workflow_capsule,
    )

    capsule = _capsule()
    capsule["plan"]["phases"][0]["score"] = float("nan")
    with pytest.raises(ValueError, match="strict JSON"):
        save_workflow_capsule("review", capsule, workspace=tmp_path)

    personal = tmp_path / "personal"
    target = personal / "review.workflow.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_capsule()).replace(
            '"read_only": true',
            '"read_only": true, "score": NaN',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="invalid workflow capsule"):
        load_workflow_capsule(
            "review", workspace=tmp_path, personal_dir=personal, source="personal"
        )


def test_workflow_exact_load_does_not_scan_the_library(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows import workflow_library

    workflow_library.save_workflow_capsule(
        "review",
        _capsule(),
        workspace=tmp_path,
    )
    _trust_project_control(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workflow_library,
        "discover_workflow_capsules",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("exact load must not scan")
        ),
    )

    capsule, ref = workflow_library.load_workflow_capsule(
        "review",
        workspace=tmp_path,
    )
    assert capsule["manifest"]["name"] == "saved-review"
    assert ref["source"] == "project"


def test_saved_capsule_preflights_and_executes_through_real_runtime(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_library import save_workflow_capsule
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    save_workflow_capsule("review", _capsule(), workspace=tmp_path)
    _trust_project_control(tmp_path, monkeypatch)

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
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_lifecycle import (
        workflow_run_archive,
        workflow_runs,
    )
    from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime
    from nz_coder.runtime.process.workdir import scoped_workdir
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
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_lifecycle import workflow_run_archive
    from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime
    from nz_coder.runtime.process.workdir import scoped_workdir
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
