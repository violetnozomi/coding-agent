"""Advanced source-parity tests for nested, built-in, review, and sweep workflows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _trust_project_control(workspace, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    trust_path = workspace.parent / f"{workspace.name}-advanced-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace, "workspace-control", snapshot.control_fingerprint
    )


def test_builtin_resolver_precedes_shadowing_saved_capsule(tmp_path):
    from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule
    from nz_coder.runtime.workflows.workflow_library import save_workflow_capsule
    from nz_coder.runtime.workflows.workflow_resolver import resolve_workflow_capsule

    manifest = {
        "name": "shadow",
        "description": "Saved shadow.",
        "phases": ["saved"],
        "read_only": True,
        "planned_agents": 1,
        "max_agents": 1,
        "max_concurrency": 1,
        "patterns": ["fan-out-and-synthesize"],
    }
    saved = create_workflow_capsule(
        manifest=manifest,
        plan={
            "manifest": manifest,
            "phases": [{
                "name": "saved", "mode": "synthesize",
                "from_phases": [], "rubric": "saved",
            }],
        },
    )
    save_workflow_capsule(
        "parallel-investigation", saved, workspace=tmp_path
    )

    resolved = resolve_workflow_capsule(
        "parallel-investigation",
        {"question": "Where is routing?", "targets": ["routing"]},
        workspace=tmp_path,
    )

    assert resolved["ref"]["source"] == "builtin"
    assert resolved["capsule"]["manifest"]["name"] == "parallel-investigation"


def test_one_level_nested_workflow_shares_parent_runtime_and_phase_events(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2, concurrency=1)
    plan = {
        "phases": [{
            "name": "nested-investigation",
            "mode": "workflow",
            "workflow": "parallel-investigation",
            "args": {"question": "Find routing", "targets": ["routing"]},
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan=plan)

    outcome = result.metadata["workflow_result"]
    assert outcome["result"]["final_text"] == "SYNTHESIZED RESULT"
    assert outcome["budget"]["spent"] == 14
    assert manager.spawned_count() == 2
    events = manager.events().metadata["workflow_events"]
    phase_names = [
        event["data"].get("name") for event in events
        if event["type"] == "phase_started"
    ]
    assert "nested-investigation/investigate" in phase_names
    assert "nested-investigation/synthesize" in phase_names


def test_nested_workflow_shares_token_budget_and_blocks_inner_synthesis(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2, concurrency=1)
    plan = {
        "token_budget": 7,
        "phases": [{
            "name": "nested",
            "mode": "workflow",
            "workflow": "parallel-investigation",
            "args": {"question": "Inspect", "targets": ["one"]},
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan=plan)

    assert result.startswith("Error: tokenBudget cap")
    assert manager.spawned_count() == 1


def test_nested_workflow_depth_is_rejected_before_spawn(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule
    from nz_coder.runtime.workflows.workflow_library import save_workflow_capsule
    from nz_coder.runtime.workflows.workflow_resolver import resolve_nested_workflows
    from tests.test_workflow_runtime import _manager

    manifest = {
        "name": "outer", "description": "nested outer", "phases": ["inner"],
        "read_only": True, "planned_agents": 2, "max_agents": 2,
        "max_concurrency": 1, "patterns": ["fan-out-and-synthesize"],
    }
    capsule = create_workflow_capsule(
        manifest=manifest,
        plan={
            "manifest": manifest,
            "phases": [{
                "name": "inner", "mode": "workflow",
                "workflow": "parallel-investigation",
                "args": {"question": "inner", "targets": ["one"]},
            }],
        },
    )
    save_workflow_capsule("outer", capsule, workspace=tmp_path)
    _trust_project_control(tmp_path, monkeypatch)
    manager = _manager(tmp_path, monkeypatch, max_tasks=4)
    plan = {"phases": [{
        "name": "outer", "mode": "workflow", "workflow": "outer",
    }]}

    with pytest.raises(ValueError, match="limited to one level"):
        resolve_nested_workflows(plan, workspace=tmp_path)
    assert manager.spawned_count() == 0


def test_parallel_investigation_builtin_is_bounded_and_structured():
    from nz_coder.runtime.workflows.workflow_builtins import get_builtin_workflow

    capsule = get_builtin_workflow(
        "parallel-investigation",
        {"question": "Inspect", "targets": ["a", "b", "c"], "max_agents": 3},
    )

    assert capsule["manifest"]["max_agents"] == 3
    tasks = capsule["plan"]["phases"][0]["tasks"]
    assert len(tasks) == 2
    assert all(task["read_only"] for task in tasks)
    assert all(task["output_schema"]["required"] == ["finding"] for task in tasks)


def test_builtin_input_bounds_reject_zero_investigators_and_excess_reviewers():
    from nz_coder.runtime.workflows.workflow_builtins import get_builtin_workflow

    with pytest.raises(ValueError, match="targets must be non-empty"):
        get_builtin_workflow(
            "parallel-investigation",
            {"question": "Inspect", "targets": []},
        )
    with pytest.raises(ValueError, match="at most 9 packets"):
        get_builtin_workflow(
            "scoped-review",
            {"packets": [{"packet_path": f"packet-{index}"} for index in range(10)]},
        )


def test_review_packets_partition_hash_and_preserve_unicode(tmp_path):
    from nz_coder.runtime.workflows.workflow_review import write_review_packets

    diff = (
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1 +1 @@\n-old\n+新内容\n"
        "diff --git a/docs/readme.md b/docs/readme.md\n--- a/docs/readme.md\n+++ b/docs/readme.md\n"
        "@@ -1 +1 @@\n-old\n+说明\n"
    )
    packets = write_review_packets(
        workspace=tmp_path,
        session_id="session",
        label="review",
        diff=diff,
        requirements=["Preserve Unicode"],
        routing_risk="high",
        chunk_bytes=4096,
    )

    assert len(packets) == 2
    assert {item["partition_key"] for item in packets} == {"src/source", "docs/docs"}
    assert all(item["risk_flags"] == ["routing-high"] for item in packets)
    contents = "".join(
        Path(chunk["path"]).read_text(encoding="utf-8")
        for packet in packets for chunk in packet["evidence_chunks"]
    )
    assert "新内容" in contents and "说明" in contents
    assert all(Path(item["packet_path"]).is_file() for item in packets)
    assert (Path(packets[0]["packet_path"]).parent.stat().st_mode & 0o777) == 0o700


def test_quality_gate_preserves_confirmed_and_unresolved_but_drops_refuted():
    from nz_coder.runtime.workflows.workflow_review import review_quality_gate

    result = review_quality_gate([{
        "structured": {
            "findings": [
                {"id": "a", "disposition": "confirmed"},
                {"id": "b", "disposition": "refuted"},
                {"id": "c", "disposition": "unresolved"},
            ],
            "unverified_requirements": ["R1"],
        },
    }])

    assert [item["id"] for item in result["actionable_findings"]] == ["a", "c"]
    assert [item["id"] for item in result["unresolved_findings"]] == ["c"]
    assert result["unqualified_approval_allowed"] is False


def test_quality_gate_never_infers_approval_from_missing_structured_output():
    from nz_coder.runtime.workflows.workflow_review import review_quality_gate

    result = review_quality_gate([{"final_text": "looks fine"}])

    assert result["unqualified_approval_allowed"] is False
    assert result["unverified_requirements"] == ["review output unavailable"]


def test_scoped_review_builtin_runs_review_gate_artifact_and_synthesis(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=3, concurrency=2)
    packet = {
        "packet_path": str(tmp_path / "packet.json"),
        "content_hash": "abc",
        "partition_key": "src/source",
        "scope_paths": ["src/app.py"],
        "risk_flags": [],
        "evidence_chunks": [],
    }
    (tmp_path / "packet.json").write_text("{}", encoding="utf-8")

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(
            capsule_name="scoped-review",
            capsule_args={"packets": [packet]},
        )

    outcome = result.metadata["workflow_result"]
    assert manager.spawned_count() == 3
    assert outcome["result"]["final_text"] == "SYNTHESIZED RESULT"
    audit = next(item for item in outcome["artifacts"] if item["name"] == "scoped-review-audit")
    stored = json.loads(
        (manager._workflow.root / "runs" / outcome["run_id"] / audit["path"]).read_text(encoding="utf-8")
    )
    assert stored["unqualified_approval_allowed"] is False


def test_worktree_sweep_removes_clean_read_only_and_retains_changed(tmp_path):
    from nz_coder.runtime.workflows.workflow_sweep import sweep_workflow_worktrees

    from nz_coder.runtime.worktree import WorktreeManager

    worktrees = WorktreeManager(tmp_path).worktree_dir
    clean = worktrees / "clean"
    changed = worktrees / "changed"
    clean.mkdir(parents=True)
    changed.mkdir(parents=True)
    states = [
        {
            "session_id": "clean", "workflow_run_id": "run", "status": "completed",
            "read_only": True, "changed_files": [],
            "worktree": {"id": "clean", "path": str(clean), "mode": "copy"},
        },
        {
            "session_id": "changed", "workflow_run_id": "run", "status": "completed",
            "read_only": True, "changed_files": ["app.py"],
            "worktree": {"id": "changed", "path": str(changed), "mode": "copy"},
        },
    ]

    result = sweep_workflow_worktrees(states, tmp_path, run_id="run")

    assert str(clean) in result["removed"]
    assert not clean.exists()
    assert changed.exists()
    assert any("reported changed files" in item for item in result["warnings"])


@pytest.mark.parametrize(
    "pattern",
    [
        "classify-and-act", "fan-out-and-synthesize",
        "adversarial-verification", "generate-and-filter",
        "tournament", "loop-until-done",
    ],
)
def test_json_generator_covers_all_declared_patterns_without_source(pattern):
    from nz_coder.runtime.workflows.workflow_builtins import generate_pattern_workflow
    from nz_coder.runtime.workflows.workflow_capsule import validate_workflow_capsule

    capsule = generate_pattern_workflow(pattern, "Inspect the repository")
    validated = validate_workflow_capsule(capsule)

    assert validated["manifest"]["patterns"] == [pattern]
    assert "source" not in validated
    assert validated["manifest"]["max_agents"] <= 9
