"""Advanced lifecycle parity tests for saved workflows and durable runs."""
from __future__ import annotations

import json
from pathlib import Path


def _capsule(name: str, description: str = "saved") -> dict:
    from nz_coder.runtime.workflow_capsule import create_workflow_capsule

    manifest = {
        "name": name,
        "description": description,
        "phases": ["inspect"],
        "read_only": True,
        "planned_agents": 1,
        "max_agents": 1,
        "max_concurrency": 1,
        "patterns": ["classify-and-act"],
    }
    return create_workflow_capsule(
        manifest=manifest,
        plan={
            "manifest": manifest,
            "require_synthesis": False,
            "phases": [{
                "name": "inspect", "mode": "parallel",
                "tasks": [{"prompt": "inspect", "read_only": True}],
            }],
        },
    )


def _record(runs, run_id, **extra):
    from nz_coder.runtime.workflow_run_store import WorkflowRunStore

    WorkflowRunStore(runs / run_id).write_terminal({
        "run_id": run_id,
        "status": "completed",
        "ended_at": 1,
        "artifacts": [],
        **extra,
    })


def test_saved_capsule_rename_is_exact_and_atomic(tmp_path):
    from nz_coder.runtime.workflow_library import (
        rename_workflow_capsule,
        save_workflow_capsule,
    )

    original = save_workflow_capsule("audit", _capsule("audit"), workspace=tmp_path)
    renamed = rename_workflow_capsule("audit", "review", workspace=tmp_path)

    assert not Path(original["path"]).exists()
    assert Path(renamed["path"]).is_file()


def test_saved_capsule_delete_is_recoverable_private_trash(tmp_path):
    from nz_coder.runtime.workflow_library import (
        save_workflow_capsule,
        trash_workflow_capsule,
    )

    save_workflow_capsule("audit", _capsule("audit"), workspace=tmp_path)
    result = trash_workflow_capsule("audit", workspace=tmp_path)

    path = Path(result["trash_path"])
    assert result["recoverable"] is True and path.is_file()
    assert (path.parent.stat().st_mode & 0o777) == 0o700


def test_saved_capsule_replace_preserves_prior_revision(tmp_path):
    from nz_coder.runtime.workflow_library import (
        load_workflow_capsule,
        replace_workflow_capsule,
        save_workflow_capsule,
    )

    save_workflow_capsule("audit", _capsule("audit", "old"), workspace=tmp_path)
    result = replace_workflow_capsule(
        "audit", _capsule("audit", "new"), workspace=tmp_path
    )
    current, _ref = load_workflow_capsule("audit", workspace=tmp_path)
    prior = json.loads(Path(result["previous_revision"]).read_text(encoding="utf-8"))

    assert current["manifest"]["description"] == "new"
    assert prior["manifest"]["description"] == "old"
    assert result["recoverable"] is True


def test_library_delete_tool_requires_confirmation(tmp_path):
    from nz_coder.runtime.workflow_library import (
        save_workflow_capsule,
        workflow_library_mutate,
    )
    from nz_coder.runtime.workdir import scoped_workdir

    save_workflow_capsule("audit", _capsule("audit"), workspace=tmp_path)
    with scoped_workdir(tmp_path):
        result = workflow_library_mutate("delete", "audit", confirm=False)

    assert result == "Error: confirm=true is required to trash a saved workflow"


def test_terminal_run_rename_updates_record_and_identity(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_host import resolve_workflow_identity
    from nz_coder.runtime.workflow_lifecycle import workflow_run_rename
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _manager

    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    runs = manager._workflow.root / "runs"
    _record(runs, "run-1")
    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run_rename("run-1", "Readable Audit")

    identity = resolve_workflow_identity(
        "Readable Audit", workspace=tmp_path, runs_root=runs
    )
    assert result.metadata["display_name"] == "Readable Audit"
    assert identity["run_id"] == "run-1"


def test_run_history_unions_active_and_persisted_without_duplicates(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_lifecycle import workflow_runs
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _manager

    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    _record(manager._workflow.root / "runs", "terminal")
    manager.begin_workflow_run("active", "Active Audit")
    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_runs("list")

    rows = result.metadata["workflow_runs"]
    assert [item["run_id"] for item in rows] == ["active", "terminal"]
    assert rows[0]["active"] is True


def test_archive_dry_run_needs_no_confirmation_and_moves_nothing(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_lifecycle import workflow_run_archive
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _manager

    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    runs = manager._workflow.root / "runs"
    _record(runs, "run-1")
    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run_archive(["run-1"], dry_run=True)

    assert result.metadata["workflow_archive_candidates"] == ["run-1"]
    assert (runs / "run-1").is_dir()


def test_result_summary_is_persisted_and_readable(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflow_lifecycle import workflow_runs
    from nz_coder.runtime.workflow_runtime import workflow_run
    from nz_coder.runtime.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "inspect", "mode": "parallel",
            "tasks": [{"prompt": "one", "read_only": True}],
        }],
    }
    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        executed = workflow_run(plan=plan)
        run_id = executed.metadata["workflow_result"]["run_id"]
        result = workflow_runs("result", run_id=run_id)

    assert "RESULT: one" in str(result)
    assert result.metadata["run_id"] == run_id
