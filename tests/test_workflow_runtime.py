"""Source-parity tests for the bounded declarative workflow runtime."""
from __future__ import annotations

import time
import json
import os
import threading


def _install_fake_child(
    monkeypatch,
    workspace,
    *,
    delays=None,
    calls=None,
    verifier_verdicts=None,
):
    import nz_coder.runtime.subagent as subagent

    delay_map = delays or {}
    observed = calls if calls is not None else []
    verdicts = verifier_verdicts if verifier_verdicts is not None else []

    def fake_run(prompt, *, session_id, cancel_event=None, **_kwargs):
        observed.append(prompt)
        if "ABORT_CHILD" in prompt:
            assert cancel_event is not None
            cancel_event.wait(2)
        for marker, delay in delay_map.items():
            if marker in prompt:
                time.sleep(delay)
        state = subagent._load_subagent_state("parent", session_id, workspace)
        state["status"] = (
            "cancelled"
            if "ABORT_CHILD" in prompt
            else "error" if "FAIL_CHILD" in prompt else "completed"
        )
        state["tokens"] = {"output": 7, "total": 7}
        if prompt.startswith("You are an independent sidecar verifier"):
            verdict = verdicts.pop(0) if verdicts else "accept"
            state["structured_output"] = {
                "verdict": verdict,
                "reason": f"verifier says {verdict}",
                "suggested_fix": "correct unsupported claims",
            }
        subagent._save_subagent_state("parent", state, workspace)
        if "ABORT_CHILD" in prompt:
            return "cancelled"
        if "FAIL_CHILD" in prompt:
            raise RuntimeError("ordinary child failure")
        if prompt.startswith("You are the final synthesis owner"):
            return "SYNTHESIZED RESULT"
        return f"RESULT: {prompt}"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    return observed


def _manager(tmp_path, monkeypatch, *, max_tasks=20, concurrency=4):
    from nz_coder import config
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_TASKS", max_tasks)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", concurrency)
    return BackgroundAgentManager(tmp_path, "parent")


def test_gated_synthesis_counts_as_agent_and_consumes_full_results(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime, lint_workflow_plan

    calls = _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=3, concurrency=2)
    plan = {
        "phases": [
            {
                "name": "inspect",
                "mode": "parallel",
                "tasks": [
                    {"name": "a", "prompt": "inspect A", "read_only": True},
                    {"name": "b", "prompt": "inspect B", "read_only": True},
                ],
            },
            {
                "name": "synthesize",
                "mode": "synthesize",
                "from_phases": ["inspect"],
                "rubric": "Merge evidence and uncertainty.",
            },
        ],
    }
    assert lint_workflow_plan(plan, remaining_agents=3) == []

    outcome = WorkflowRuntime(manager, run_id="run-a154").execute(plan)

    assert outcome["result"]["final_text"] == "SYNTHESIZED RESULT"
    assert manager.spawned_count() == 3
    synthesis_prompt = next(call for call in calls if call.startswith("You are the final synthesis owner"))
    assert "RESULT: inspect A" in synthesis_prompt
    assert "RESULT: inspect B" in synthesis_prompt
    assert "Merge evidence and uncertainty" in synthesis_prompt


def test_pipeline_streams_items_without_stage_barrier_and_preserves_order(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    calls = _install_fake_child(
        monkeypatch,
        tmp_path,
        delays={'"slow"-map': 0.10, '"fast"-map': 0.01},
    )
    manager = _manager(tmp_path, monkeypatch, max_tasks=4, concurrency=2)
    runtime = WorkflowRuntime(manager, run_id="run-a155-pipeline")
    stages = [
        {"prompt": "{item}-map", "read_only": True},
        {"prompt": "reduce {item} using {previous}", "read_only": True},
    ]

    results = runtime.pipeline(["slow", "fast"], stages, phase="pipeline")

    assert [item["status"] for item in results] == ["completed", "completed"]
    fast_reduce = next(index for index, call in enumerate(calls) if call.startswith("reduce \"fast\""))
    slow_reduce = next(index for index, call in enumerate(calls) if call.startswith("reduce \"slow\""))
    assert fast_reduce < slow_reduce


def test_map_reduce_is_failure_isolated_and_runs_final_fold(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=4, concurrency=2)
    plan = {
        "phases": [{
            "name": "review",
            "mode": "map_reduce",
            "items": ["good", "FAIL_CHILD", "also-good"],
            "map": {"prompt": "review {item}", "read_only": True},
            "rubric": "Return confirmed findings only.",
        }],
    }

    outcome = WorkflowRuntime(manager, run_id="run-a155-map-reduce").execute(plan)

    assert outcome["result"]["final_text"] == "SYNTHESIZED RESULT"
    assert manager.spawned_count() == 4
    snapshot = outcome["workflow_snapshot"]
    assert snapshot["counts"]["failed"] == 1
    assert snapshot["counts"]["completed"] == 3


def test_quality_preflight_rejects_before_any_child_is_published(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import lint_workflow_plan

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2)
    bad = {
        "phases": [{
            "name": "fanout",
            "mode": "parallel",
            "concurrency": 0,
            "tasks": [
                {"prompt": "write without scope"},
                {"prompt": "write a", "target_paths": ["src"]},
                {"prompt": "write b", "target_paths": ["src/api.py"]},
            ],
        }],
    }

    findings = lint_workflow_plan(
        bad,
        remaining_agents=manager.agent_cap,
        workspace=tmp_path,
    )
    codes = {finding.code for finding in findings}

    assert {
        "invalid-concurrency",
        "write-task-without-scope",
        "literal-fanout-exceeds-max-agents",
        "missing-final-synthesis",
        "overlapping-parallel-write-scopes",
    } <= codes
    assert manager.spawned_count() == 0


def test_workflow_tool_runs_through_bound_session_manager(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import scoped_background_agent_manager
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.runtime.workflow_runtime import workflow_run

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    plan = {
        "phases": [{
            "name": "answer",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Return the final answer.",
        }],
    }

    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        result = workflow_run(plan)

    assert str(result) == "SYNTHESIZED RESULT"
    assert result.metadata["workflow_result"]["status"] == "completed"
    assert manager.spawned_count() == 1


def test_workflow_children_cannot_recursively_start_another_workflow():
    from nz_coder.runtime.subagent import _subagent_tools

    names = {
        spec["function"]["name"]
        for spec in _subagent_tools("explore")
    }

    assert "workflow_run" not in names
    assert "agent_manager" not in names


def test_resume_cache_replays_success_but_reruns_synthesis(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    calls = _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=5, concurrency=2)
    plan = {
        "phases": [
            {
                "name": "inspect",
                "mode": "parallel",
                "tasks": [{"prompt": "stable inspection", "read_only": True}],
            },
            {
                "name": "synthesize",
                "mode": "synthesize",
                "from_phases": ["inspect"],
                "rubric": "Merge.",
            },
        ],
    }
    first = WorkflowRuntime(manager, run_id="cache-first").execute(plan)
    before = list(calls)
    second = WorkflowRuntime(
        manager,
        run_id="cache-second",
        resume_from="cache-first",
    ).execute(plan)

    assert first["replayed_agents"] == 0
    assert second["replayed_agents"] == 1
    assert calls.count("stable inspection") == 1
    assert sum(call.startswith("You are the final synthesis owner") for call in calls) == 2
    assert len(calls) == len(before) + 1
    assert list((manager._workflow.root / "runs" / "cache-second" / "results").glob("*.json"))


def test_result_cache_is_private_and_treats_corruption_or_failure_as_miss(tmp_path):
    from nz_coder.runtime.child_result import ChildAgentResult
    from nz_coder.runtime.workflow_runtime import WorkflowResultCache

    cache = WorkflowResultCache(tmp_path / "runs", "current")
    completed = ChildAgentResult(
        task_id="task-1",
        name="inspect",
        status="completed",
        final_text="done",
    ).to_dict()
    cache.set("abc#0", completed)
    files = list(cache.results_dir.glob("*.json"))

    assert len(files) == 1
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert cache.get("abc#0")["final_text"] == "done"
    files[0].write_text("{broken", encoding="utf-8")
    assert cache.get("abc#0") is None
    failed = dict(completed, status="error")
    cache.set("failed#0", failed)
    assert cache.get("failed#0") is None


def test_workflow_events_bridge_to_session_bus_and_sse(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime
    from nz_coder.session_events import SessionEventBus, encode_sse

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2)
    bus = SessionEventBus(session_id="parent")
    manager.bind_event_bus(bus)
    plan = {
        "phases": [{
            "name": "final",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Return a bounded final answer.",
        }],
    }

    WorkflowRuntime(manager, run_id="run-a158").execute(plan)
    events = [event for event in bus.recent(100) if event.type.startswith("workflow.")]
    event_types = [event.type for event in events]

    assert "workflow.phase.started" in event_types
    assert "workflow.task.queued" in event_types
    assert "workflow.task.started" in event_types
    assert "workflow.task.terminal" in event_types
    assert "workflow.synthesis.completed" in event_types
    assert "workflow.phase.finished" in event_types
    terminal = next(event for event in events if event.type == "workflow.task.terminal")
    assert terminal.properties["workflow_snapshot"]["items"][0]["phase"] == "final"
    frame = encode_sse(events[-1])
    assert frame.startswith("id: ")
    assert '"workflow_snapshot"' in frame


def test_sidecar_verifier_accepts_in_fresh_child_context(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    calls = _install_fake_child(monkeypatch, tmp_path, verifier_verdicts=["accept"])
    manager = _manager(tmp_path, monkeypatch, max_tasks=2)
    plan = {
        "verification": {"enabled": True, "max_revisions": 1},
        "phases": [{
            "name": "final",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Only supported conclusions.",
        }],
    }

    outcome = WorkflowRuntime(manager, run_id="run-a159").execute(plan)

    assert outcome["result"]["sidecar_verification"]["verdict"] == "accept"
    assert manager.spawned_count() == 2
    assert sum(call.startswith("You are an independent sidecar verifier") for call in calls) == 1


def test_sidecar_revise_reanimates_synthesis_once_then_accepts(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    calls = _install_fake_child(
        monkeypatch,
        tmp_path,
        verifier_verdicts=["revise", "accept"],
    )
    manager = _manager(tmp_path, monkeypatch, max_tasks=4)
    plan = {
        "verification": {"enabled": True, "max_revisions": 1},
        "phases": [{
            "name": "final",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Merge.",
        }],
    }

    outcome = WorkflowRuntime(manager, run_id="run-a159-revise").execute(plan)

    assert outcome["result"]["sidecar_verification"]["verdict"] == "accept"
    assert manager.spawned_count() == 4
    assert sum(call.startswith("You are the final synthesis owner") for call in calls) == 2


def test_token_budget_stops_before_next_agent_spawn(tmp_path, monkeypatch):
    import pytest

    from nz_coder.runtime.workflow_runtime import WorkflowBudgetError, WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=3, concurrency=1)
    plan = {
        "token_budget": 7,
        "phases": [
            {
                "name": "inspect",
                "mode": "parallel",
                "tasks": [{"prompt": "one", "read_only": True}],
            },
            {
                "name": "final",
                "mode": "synthesize",
                "from_phases": ["inspect"],
                "rubric": "Merge.",
            },
        ],
    }

    with pytest.raises(WorkflowBudgetError, match="tokenBudget cap"):
        WorkflowRuntime(
            manager,
            run_id="run-a160-budget",
            token_budget=7,
        ).execute(plan)

    assert manager.spawned_count() == 1
    assert manager.events().metadata["workflow_events"][-1]["type"] == "workflow_run_failed"


def test_workflow_abort_stops_active_child_and_emits_unique_terminal(tmp_path, monkeypatch):
    import threading

    from nz_coder.runtime.workflow_runtime import WorkflowAbortError, WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    cancelled = threading.Event()
    runtime = WorkflowRuntime(
        manager,
        run_id="run-a161-abort",
        cancel_event=cancelled,
    )
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "work",
            "mode": "parallel",
            "tasks": [{"prompt": "ABORT_CHILD", "read_only": True}],
        }],
    }
    errors = []

    def execute():
        try:
            runtime.execute(plan)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=execute)
    thread.start()
    deadline = time.monotonic() + 1
    while manager.spawned_count() == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    cancelled.set()
    thread.join(timeout=3)

    assert len(errors) == 1
    assert isinstance(errors[0], WorkflowAbortError)
    state = manager._states()[0]
    assert state["status"] == "cancelled"
    events = manager.events().metadata["workflow_events"]
    assert sum(event["type"] == "task_terminal" for event in events) == 1
    assert events[-1]["type"] == "workflow_run_stopped"


def test_workflow_records_bounded_idempotent_lineage_outcome(tmp_path, monkeypatch):
    from nz_coder.runtime.lineage import SessionLineage
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    lineage = SessionLineage(tmp_path / "lineage.jsonl", "parent")
    manager.bind_lineage(lineage)
    plan = {
        "phases": [{
            "name": "final",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Return a bounded result.",
        }],
    }

    outcome = WorkflowRuntime(manager, run_id="run-a162").execute(plan)
    duplicate = manager.record_workflow_outcome(outcome)
    entries = [
        entry for entry in lineage.entries()
        if entry["type"] == "memory_outcome_digest"
    ]

    assert duplicate is None
    assert len(entries) == 1
    assert entries[0]["payload"]["unique_key"] == "workflow:run-a162"
    assert entries[0]["payload"]["phase_names"] == ["final"]
    assert "final_text" not in entries[0]["payload"]["result"]
    events = manager.events().metadata["workflow_events"]
    assert sum(event["type"] == "memory_outcome_recorded" for event in events) == 1


def test_manifest_preflight_enforces_declared_phases_caps_and_read_only():
    from nz_coder.runtime.workflow_runtime import lint_workflow_plan

    plan = {
        "manifest": {
            "name": "review",
            "description": "Review two areas.",
            "phases": ["wrong"],
            "read_only": True,
            "planned_agents": 1,
            "max_agents": 1,
            "max_concurrency": 1,
            "patterns": ["fan-out-and-synthesize"],
        },
        "require_synthesis": False,
        "phases": [{
            "name": "inspect",
            "mode": "parallel",
            "tasks": [{"prompt": "write", "target_paths": ["app.py"]}],
        }],
    }

    codes = {
        finding.code for finding in lint_workflow_plan(
            plan,
            remaining_agents=4,
            concurrency_cap=2,
        )
    }

    assert "manifest-phase-mismatch" in codes
    assert "manifest-read-only-violation" in codes


def test_manifest_rejects_unsupported_pattern_before_spawn():
    from nz_coder.runtime.workflow_runtime import lint_workflow_plan

    plan = {
        "manifest": {
            "name": "bad",
            "description": "Bad declaration.",
            "phases": ["inspect"],
            "read_only": True,
            "max_agents": 1,
            "max_concurrency": 1,
            "patterns": ["arbitrary-python"],
        },
        "require_synthesis": False,
        "phases": [{
            "name": "inspect",
            "mode": "parallel",
            "tasks": [{"prompt": "inspect", "read_only": True}],
        }],
    }

    findings = lint_workflow_plan(plan, remaining_agents=2)

    assert any(item.code == "invalid-manifest" for item in findings)


def test_managed_run_pause_gates_next_spawn_then_resume(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    runtime = WorkflowRuntime(manager, run_id="run-a165-pause")
    runtime._ensure_managed_run("pause-test")
    assert manager.pause_workflow_run(runtime.run_id)
    results = []
    worker = threading.Thread(target=lambda: results.append(runtime.run_agent({
        "prompt": "after resume",
        "read_only": True,
    })))
    worker.start()
    time.sleep(0.1)

    assert manager.spawned_count() == 0
    assert manager.resume_workflow_run(runtime.run_id)
    worker.join(timeout=2)
    assert results[0]["status"] == "completed"
    assert manager.spawned_count() == 1


def test_managed_run_stop_releases_paused_spawn_as_abort(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowAbortError, WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    runtime = WorkflowRuntime(manager, run_id="run-a165-stop")
    runtime._ensure_managed_run("stop-test")
    manager.pause_workflow_run(runtime.run_id)
    errors = []

    def run():
        try:
            runtime.run_agent({"prompt": "never spawned", "read_only": True})
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.05)
    assert manager.stop_workflow_run(runtime.run_id, "operator stop")
    worker.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], WorkflowAbortError)
    assert manager.spawned_count() == 0
    snapshot = manager.workflow_run_snapshots()[0]
    assert snapshot["status"] == "stopped"


def test_managed_run_stop_active_child_cannot_complete_workflow(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowAbortError, WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    runtime = WorkflowRuntime(manager, run_id="run-a165-active-stop")
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "work",
            "mode": "parallel",
            "tasks": [{"prompt": "ABORT_CHILD", "read_only": True}],
        }],
    }
    errors = []
    worker = threading.Thread(target=lambda: _capture_error(runtime, plan, errors))
    worker.start()
    deadline = time.monotonic() + 1
    while manager.spawned_count() == 0:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert manager.stop_workflow_run(runtime.run_id, "operator stop")
    worker.join(timeout=3)

    assert len(errors) == 1
    assert isinstance(errors[0], WorkflowAbortError)
    assert manager.workflow_run_snapshots()[0]["status"] == "stopped"
    assert manager.events().metadata["workflow_events"][-1]["type"] == "workflow_run_stopped"


def _capture_error(runtime, plan, errors):
    try:
        runtime.execute(plan)
    except Exception as exc:
        errors.append(exc)


def test_artifact_log_cost_report_and_terminal_run_record(tmp_path, monkeypatch):
    from nz_coder.runtime.workflow_runtime import WorkflowRuntime

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=1)
    plan = {
        "phases": [{
            "name": "final",
            "mode": "synthesize",
            "from_phases": [],
            "rubric": "Return evidence.",
            "artifact": "final/review",
            "log": "Final review persisted.",
        }],
    }

    outcome = WorkflowRuntime(manager, run_id="run-a168-record").execute(plan)
    run_dir = manager._workflow.root / "runs" / "run-a168-record"
    artifact = run_dir / outcome["artifacts"][0]["path"]
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    assert artifact.name == "final_review.json"
    assert artifact.is_file()
    assert os.stat(artifact).st_mode & 0o777 == 0o600
    assert record["status"] == "completed"
    assert record["efficiency_report"]["agent_starts"] == 1
    assert record["efficiency_report"]["model_tokens"]["total"] == 7
    assert record["efficiency_report"]["token_coverage"]["ok"] is True
    assert outcome["efficiency_report"] == record["efficiency_report"]
    events = manager.events().metadata["workflow_events"]
    assert sum(event["type"] == "artifact_written" for event in events) == 1
    assert sum(event["type"] == "workflow_log" for event in events) == 1
