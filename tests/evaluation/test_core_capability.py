"""Deterministic contracts for the core coding-capability benchmark."""
from __future__ import annotations

import json


def test_manifest_covers_required_a_to_h_scenarios() -> None:
    from nz_coder.evaluation.core_capability import benchmark_manifest

    manifest = benchmark_manifest()

    assert [case.case_id for case in manifest] == list("ABCDEFGH")
    assert {case.capability for case in manifest} == {
        "unknown-localization", "cross-file-impact", "large-repo-navigation",
        "large-tool-catalog", "huge-tool-output", "long-horizon",
        "verification-recovery", "multi-agent",
    }
    assert all(case.production_api for case in manifest)


def test_trajectory_metrics_detect_waste_and_verification_recovery(tmp_path) -> None:
    from nz_coder.evaluation.core_capability import AgentTrajectoryMetrics

    trace = tmp_path / "trace.jsonl"
    events = [
        {"event": "model_call", "input_tokens": 100, "output_tokens": 20, "cost": 0.1},
        {"event": "tool_result", "tool_name": "read_file", "path": "a.py", "tokens": 30},
        {"event": "tool_result", "tool_name": "read_file", "path": "a.py", "tokens": 30},
        {"event": "tool_result", "tool_name": "grep_search", "query": "needle", "tokens": 10},
        {"event": "tool_result", "tool_name": "grep_search", "query": "needle", "tokens": 10},
        {"event": "verification", "success": False},
        {"event": "verification", "success": True},
        {"event": "compaction"},
        {"event": "child_session", "conflicts": 2},
    ]
    trace.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

    metrics = AgentTrajectoryMetrics.from_jsonl(trace)

    assert metrics.model_calls == 1
    assert metrics.tool_calls == 4
    assert metrics.repo_intelligence_calls == 0
    assert metrics.total_tokens == 120
    assert metrics.duplicate_reads == 1
    assert metrics.duplicate_searches == 1
    assert metrics.verification_attempts == 2
    assert metrics.verification_recoveries == 1
    assert metrics.compactions == 1
    assert metrics.child_sessions == 1 and metrics.conflicts == 2


def test_trajectory_metrics_counts_repository_intelligence_calls() -> None:
    from nz_coder.evaluation.core_capability import AgentTrajectoryMetrics

    metrics = AgentTrajectoryMetrics.from_events([
        {"event": "tool_result", "tool_name": "repo_context", "tokens": 12},
        {"event": "tool_result", "tool_name": "repo_map", "tokens": 8},
        {"event": "tool_result", "tool_name": "symbol_context", "tokens": 4},
    ])

    assert metrics.tool_calls == 3
    assert metrics.repo_intelligence_calls == 3


def test_report_separates_three_score_dimensions_and_unknown_behavior() -> None:
    from nz_coder.evaluation.core_capability import build_capability_report

    report = build_capability_report(outcomes=None)

    assert report["feature_coverage"]["score"] >= 0
    assert report["implementation_depth"]["structural_probe_pass_rate"] >= 0
    assert report["behavioral_effectiveness"]["score"] == "unknown"
    assert report["behavioral_effectiveness"]["reason"] == "benchmark not run"


def test_contract_results_are_not_reported_as_behavioral_effectiveness() -> None:
    from nz_coder.evaluation.core_capability import build_capability_report

    report = build_capability_report({
        "suite_type": "core-capability-contract",
        "cases": {"A": {"passed": True}, "B": {"passed": True}},
    })

    assert report["behavioral_effectiveness"]["score"] == "unknown"
    assert report["behavioral_effectiveness"]["contract_pass_rate"] == 100.0


def test_controlled_agent_results_are_not_reported_as_measured_effectiveness() -> None:
    from nz_coder.evaluation.core_capability import build_capability_report

    report = build_capability_report({
        "suite_type": "agent-behavior-controlled-matrix",
        "evidence_kind": "controlled",
        "runs": [{"score": {"success": True}}],
    })

    assert report["behavioral_effectiveness"]["score"] == "unknown"
    assert report["behavioral_effectiveness"]["controlled_success_rate"] == 100.0


def test_local_runner_is_reproducible_and_exercises_scale_cases(tmp_path) -> None:
    from nz_coder.evaluation.core_capability import run_local_benchmark

    first = run_local_benchmark(tmp_path / "one")
    second = run_local_benchmark(tmp_path / "two")

    assert first["manifest_hash"] == second["manifest_hash"]
    assert first["cases"]["D"]["catalog_sizes"] == [20, 50, 100, 200]
    assert first["cases"]["D"]["low_pressure_visible"] == [21, 51, 101, 201]
    assert first["cases"]["D"]["low_pressure_hinted"] == [0, 50, 100, 200]
    assert first["cases"]["D"]["schema_budget_enforced"] is True
    assert first["cases"]["D"]["discovery_recall_cases"] == 8
    assert first["cases"]["D"]["discovery_recall_hits"] == 8
    assert first["cases"]["D"]["worst_target_rank"] <= 2
    assert first["cases"]["D"]["next_turn_unlocks"] == 8
    assert first["cases"]["D"]["two_turn_token_savings_min_pct"] > 0
    assert first["cases"]["C"]["module_count"] >= 250
    assert first["cases"]["E"]["aggregate_budget_respected"] is True
    assert first["cases"]["G"]["verification_recovered"] is True
    assert first["cases"]["H"]["conflict_accounted"] is True
    assert first["cases"]["F"]["production_projection_calls"] == 40
    assert first["cases"]["F"]["agent_runner_model_calls"] == 41
    assert first["cases"]["F"]["agent_runner_tool_results"] == 40
    assert first["cases"]["F"]["nominal_sla_enforced"] is False
    assert first["cases"]["F"]["nominal_sla_advisory"] is True
    assert first["cases"]["F"]["agent_runner_result"]["status"] == "completed"
    assert first["cases"]["F"]["passed"] is True
    assert first["trajectory_metrics"]["model_calls"] == 41
    assert first["trajectory_metrics"]["tool_calls"] == 40
    assert first["trajectory_metrics"]["success"] is True
    assert first["trajectory_metrics"]["patch_valid"] is True
    assert first["trajectory_metrics"]["wall_time_ms"] > 0
    assert first["cases"]["G"]["first_exit_code"] != 0
    assert first["cases"]["G"]["second_exit_code"] == 0
    assert "parent changed since child snapshot" in first["cases"]["H"]["conflict_detail"]
    assert (tmp_path / "one" / "trajectory.jsonl").is_file()


def test_trajectory_diagnostics_detects_selection_compaction_and_verification_loops() -> None:
    from nz_coder.evaluation.core_capability import diagnose_trajectory

    events = [
        {"event": "model_call", "input_tokens": 1000, "context_window": 10000},
        {"event": "compaction"},
        *[
            {"event": "tool_result", "tool_name": "grep_search", "query": "missing",
             "failed": True, "output": "No matches found"}
            for _ in range(3)
        ],
        *[
            {"event": "verification", "command": "pytest -q", "success": False}
            for _ in range(3)
        ],
        {"event": "backtrack"},
    ]

    diagnostics = diagnose_trajectory(events)

    assert diagnostics.premature_compactions == 1
    assert diagnostics.tool_selection_errors == 3
    assert diagnostics.verification_loops == 1
    assert diagnostics.backtracks == 1
    assert diagnostics.recommendations


def test_local_runner_normalizes_relative_output_path(tmp_path, monkeypatch) -> None:
    from nz_coder.evaluation.core_capability import run_local_benchmark

    monkeypatch.chdir(tmp_path)
    result = run_local_benchmark("relative-evidence")

    assert result["cases"]["G"]["first_exit_code"] != 0
    assert result["cases"]["G"]["second_exit_code"] == 0
    assert result["cases"]["G"]["verification_recovered"] is True


def test_local_runner_is_idempotent_in_same_evidence_directory(tmp_path) -> None:
    from nz_coder.evaluation.core_capability import run_local_benchmark

    target = tmp_path / "same"
    first = run_local_benchmark(target)
    second = run_local_benchmark(target)

    assert first["cases"]["C"] == second["cases"]["C"] == {
        "passed": True, "module_count": 300,
    }
