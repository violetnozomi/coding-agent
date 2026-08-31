"""Source-parity tests for workflow host identity and launch contracts."""
from __future__ import annotations

import json
import threading
import time

def _record(runs, run_id, **extra):
    from nz_coder.runtime.workflows.workflow_run_store import WorkflowRunStore

    store = WorkflowRunStore(runs / run_id)
    store.write_terminal({
        "run_id": run_id,
        "status": "completed",
        "ended_at": 1,
        **extra,
    })


def _saved(workspace, name):
    from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule
    from nz_coder.runtime.workflows.workflow_library import save_workflow_capsule

    manifest = {
        "name": name,
        "description": "saved",
        "phases": ["inspect"],
        "read_only": True,
        "planned_agents": 1,
        "max_agents": 1,
        "max_concurrency": 1,
        "patterns": ["classify-and-act"],
    }
    capsule = create_workflow_capsule(
        manifest=manifest,
        plan={
            "manifest": manifest,
            "require_synthesis": False,
            "phases": [{
                "name": "inspect",
                "mode": "parallel",
                "tasks": [{"prompt": "inspect", "read_only": True}],
            }],
        },
    )
    return save_workflow_capsule(name, capsule, workspace=workspace)


def test_identity_resolves_run_id_and_unique_display_alias(tmp_path):
    from nz_coder.runtime.workflows.workflow_host import resolve_workflow_identity

    runs = tmp_path / "runs"
    _record(
        runs,
        "run-1",
        workflow_name="audit",
        display_name="Readable Audit",
    )

    direct = resolve_workflow_identity(
        "run-1", workspace=tmp_path, runs_root=runs
    )
    alias = resolve_workflow_identity(
        "Readable Audit", workspace=tmp_path, runs_root=runs
    )

    assert direct["kind"] == "run" and direct["run_id"] == "run-1"
    assert alias["kind"] == "run" and alias["target"] == "Readable Audit"
    assert alias["workflow_name"] == "audit"


def test_identity_fails_closed_for_duplicate_alias(tmp_path):
    from nz_coder.runtime.workflows.workflow_host import resolve_workflow_identity

    runs = tmp_path / "runs"
    _record(runs, "run-a", display_name="Same")
    _record(runs, "run-b", display_name="Same")

    result = resolve_workflow_identity(
        "Same", workspace=tmp_path, runs_root=runs
    )

    assert result == {"kind": "ambiguous", "target": "Same", "matches": ["run"]}


def test_identity_fails_closed_for_run_saved_or_builtin_collision(tmp_path):
    from nz_coder.runtime.workflows.workflow_host import resolve_workflow_identity

    runs = tmp_path / "runs"
    _record(runs, "saved-audit")
    _saved(tmp_path, "saved-audit")

    collision = resolve_workflow_identity(
        "saved-audit", workspace=tmp_path, runs_root=runs
    )
    builtin = resolve_workflow_identity(
        "parallel-investigation", workspace=tmp_path, runs_root=runs
    )

    assert collision["kind"] == "ambiguous"
    assert collision["matches"] == ["run", "saved"]
    assert builtin["kind"] == "builtin"


def test_invocation_policy_is_command_only_and_turn_consumption_is_explicit():
    from nz_coder.runtime.workflows.workflow_host import (
        workflow_invocation_decision,
        workflow_start_outcome_consumes_turn,
    )

    assert workflow_invocation_decision("command")["action"] == "suggest"
    assert workflow_invocation_decision("natural-language")["action"] == "none"
    assert workflow_start_outcome_consumes_turn("started") is True
    assert workflow_start_outcome_consumes_turn("cancelled") is True
    assert workflow_start_outcome_consumes_turn("declined") is False
    assert workflow_start_outcome_consumes_turn("failed") is False


def test_host_limits_are_min_wins_and_zero_token_budget_is_unbounded():
    from nz_coder.runtime.workflows.workflow_host import clamp_workflow_limits

    manifest = {
        "max_agents": 10,
        "max_concurrency": 8,
        "token_budget": 50_000,
    }
    assert clamp_workflow_limits(
        manifest,
        {"max_agents": 3, "max_concurrency": 2, "token_budget": 1_000},
    ) == {"max_agents": 3, "max_concurrency": 2, "token_budget": 1_000}
    assert clamp_workflow_limits(
        {"max_agents": 4, "max_concurrency": 2},
        {"token_budget": 0},
    )["token_budget"] is None
    assert clamp_workflow_limits(
        {"max_agents": 999, "max_concurrency": 999, "token_budget": 999_999},
    ) == {"max_agents": 64, "max_concurrency": 8, "token_budget": 200_000}


def test_approval_summary_reports_effective_limits_and_write_risk():
    from nz_coder.runtime.workflows.workflow_host import build_workflow_approval_summary

    summary = build_workflow_approval_summary({
        "name": "audit",
        "description": "Audit files",
        "phases": ["scan", "fix"],
        "planned_agents": 4,
        "max_agents": 8,
        "max_concurrency": 4,
        "read_only": False,
    }, {"max_agents": 3})

    assert summary["max_agents"] == 3
    assert summary["planned_agents"] == 4
    assert summary["writes_files"] is True


def test_scout_then_author_prompt_requires_concrete_investigation():
    from nz_coder.runtime.workflows.workflow_host import build_scout_then_author_prompt

    prompt = build_scout_then_author_prompt("Review routing")

    assert "First investigate" in prompt
    assert "exact paths" in prompt
    assert prompt.endswith("Review routing")


def test_host_policy_rejects_literal_fanout_before_spawn(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=4)
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "inspect",
            "mode": "parallel",
            "tasks": [
                {"prompt": "one", "read_only": True},
                {"prompt": "two", "read_only": True},
            ],
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan=plan, host_policy={"max_agents": 1})

    assert "host-agent-cap-exceeded" in str(result)
    assert manager.spawned_count() == 0


def test_host_concurrency_ceiling_clamps_runtime_pool(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime
    from tests.test_workflow_runtime import _manager

    manager = _manager(tmp_path, monkeypatch, max_tasks=4, concurrency=4)
    runtime = WorkflowRuntime(
        manager,
        run_id="host-concurrency",
        max_concurrency=1,
    )
    lock = threading.Lock()
    active = 0
    peak = 0

    def work():
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return "ok"

    assert runtime.parallel([work, work, work], concurrency=4) == ["ok"] * 3
    assert peak == 1


def test_host_token_ceiling_stops_before_next_spawn(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2)
    plan = {
        "phases": [
            {
                "name": "inspect", "mode": "parallel",
                "tasks": [{"prompt": "one", "read_only": True}],
            },
            {
                "name": "final", "mode": "synthesize",
                "from_phases": ["inspect"], "rubric": "Merge.",
            },
        ],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan=plan, host_policy={"token_budget": 7})

    assert str(result).startswith("Error: tokenBudget cap")
    assert manager.spawned_count() == 1


def test_display_name_persists_and_unique_alias_can_seed_resume(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2)
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "inspect",
            "mode": "parallel",
            "tasks": [{"prompt": "one", "read_only": True}],
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        first = workflow_run(plan=plan, display_name="Readable Audit")
        second = workflow_run(plan=plan, resume_target="Readable Audit")

    first_outcome = first.metadata["workflow_result"]
    second_outcome = second.metadata["workflow_result"]
    record = json.loads(
        (manager._workflow.root / "runs" / first_outcome["run_id"] / "run.json")
        .read_text(encoding="utf-8")
    )
    assert record["display_name"] == "Readable Audit"
    assert record["workflow_name"] == "workflow"
    assert second_outcome["replayed_agents"] == 1
    assert manager.spawned_count() == 1


def test_resume_target_rejects_saved_or_ambiguous_identity(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _manager

    _saved(tmp_path, "saved-audit")
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "inspect", "mode": "parallel",
            "tasks": [{"prompt": "one", "read_only": True}],
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan=plan, resume_target="saved-audit")

    assert "must resolve uniquely to a run; got saved" in str(result)
    assert manager.spawned_count() == 0


def test_workflow_host_tool_exposes_contracts_without_starting_run():
    from nz_coder.runtime.workflows.workflow_features import workflow_host

    invocation = workflow_host("invocation", source="natural-language")
    prompt = workflow_host("author-prompt", request="Inspect tests")

    assert invocation.metadata["workflow_host"]["action"] == "none"
    assert "First investigate" in prompt.metadata["workflow_host"]["prompt"]


def test_approval_digest_binds_effective_summary_and_rejects_stale_decision():
    from nz_coder.runtime.workflows.workflow_host import (
        evaluate_workflow_approval,
        workflow_approval_digest,
    )

    summary = {"name": "audit", "max_agents": 2, "writes_files": False}
    digest = workflow_approval_digest(summary)

    assert workflow_approval_digest(dict(summary)) == digest
    assert workflow_approval_digest({**summary, "max_agents": 3}) != digest
    stale = evaluate_workflow_approval(
        summary,
        decision="approve",
        expected_digest="0" * 64,
    )
    assert stale["outcome"] == "failed"
    assert stale["reason"] == "stale approval summary"


def test_approval_gate_distinguishes_headless_denial_and_cancellation():
    from nz_coder.runtime.workflows.workflow_host import evaluate_workflow_approval

    summary = {"name": "audit"}

    assert evaluate_workflow_approval(summary, headless=True)["mode"] == "headless-auto"
    assert evaluate_workflow_approval(summary, decision="deny")["outcome"] == "declined"
    cancelled = evaluate_workflow_approval(summary, decision="cancel")
    assert cancelled["outcome"] == "cancelled"
    assert cancelled["consumes_turn"] is True


def test_explicit_denial_returns_without_run_or_child(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.test_workflow_runtime import _manager

    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "inspect", "mode": "parallel",
            "tasks": [{"prompt": "one", "read_only": True}],
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan=plan, approval_decision="deny")

    assert result.metadata["workflow_approval"]["outcome"] == "declined"
    assert manager.spawned_count() == 0
    assert manager.workflow_run_snapshots() == []


def test_headless_approval_receipt_is_persisted(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workflows.workflow_runtime import workflow_run
    from nz_coder.runtime.process.workdir import scoped_workdir
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
        result = workflow_run(plan=plan)

    outcome = result.metadata["workflow_result"]
    record = json.loads(
        (manager._workflow.root / "runs" / outcome["run_id"] / "run.json")
        .read_text(encoding="utf-8")
    )
    assert outcome["approval_receipt"]["mode"] == "headless-auto"
    assert record["approval_receipt"]["outcome"] == "started"
