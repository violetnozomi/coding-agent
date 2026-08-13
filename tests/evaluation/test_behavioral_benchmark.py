"""Agent-owned behavioral benchmark and A/B evidence contracts."""
from __future__ import annotations

from dataclasses import asdict
import json


def test_behavior_manifest_has_distinct_agent_executed_a_to_i_cases() -> None:
    from nz_coder.evaluation.behavioral import behavior_manifest

    case_ids = [case_id for case_id, _name in behavior_manifest()]
    assert case_ids[:9] == list("ABCDEFGHI")
    assert case_ids[9:] == ["I2", "I3", "I4", "IS"]
    assert len({name for _case_id, name in behavior_manifest()}) == len(case_ids)


def test_controlled_repo_intelligence_ab_runs_through_agent_runner(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import run_repo_intelligence_ab

    result = run_repo_intelligence_ab(tmp_path)
    comparison = result["comparison"]

    assert comparison["off"]["success"] is True
    assert comparison["v3"]["success"] is True
    assert comparison["lookup"]["success"] is True
    assert comparison["v3"]["reads"] < comparison["off"]["reads"]
    assert comparison["v3"]["wrong_file_reads"] < comparison["off"]["wrong_file_reads"]
    assert comparison["v3"]["repo_intelligence_calls"] >= 1
    assert (tmp_path / "repo-intelligence-ab.json").is_file()


def test_behavior_scorer_does_not_repair_failed_agent_patch(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import (
        AgentBehaviorBenchmark, BehaviorBenchmarkConfig,
        BehaviorObservation, CallableBehaviorDriver,
    )

    driver = CallableBehaviorDriver(
        lambda _task, _workspace, _config: BehaviorObservation(
            "I did not make the requested change.", events=({"event": "model_call"},),
        )
    )
    benchmark = AgentBehaviorBenchmark(tmp_path, driver)

    result = benchmark.run_case("F", BehaviorBenchmarkConfig(model="mock"))

    assert result["score"]["success"] is False
    assert result["score"]["verification"]["passed"] is False
    assert result["evidence_kind"] == "controlled"
    assert result["trace"]["events"]
    assert result["trace"]["failure_category"] == "edit failure"
    workspace = next((tmp_path / "workspaces").iterdir())
    assert "return total / count" in (workspace / "calc/service.py").read_text(encoding="utf-8")


def test_behavior_failure_classifies_verification_hook_stop() -> None:
    from nz_coder.evaluation.behavioral import _failure_category

    assert _failure_category({
        "success": False,
        "error": "agent run ended with status=error raw_status=stopped_by_hook",
    }) == "verification policy failure"


def test_controlled_model_completes_full_agent_owned_a_to_h_suite(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import run_controlled_behavior_suite

    result = run_controlled_behavior_suite(tmp_path)

    assert result["success_rate"] == 1.0
    scores = {run["task"]["case_id"]: run["score"] for run in result["runs"]}
    assert scores["E"]["metrics"]["turns"] >= 15
    assert scores["E"]["metrics"]["compactions"] >= 1
    assert scores["F"]["metrics"]["verification_recoveries"] == 1


def test_web_search_manifest_and_tool_exposure_are_capability_aware() -> None:
    from nz_coder.evaluation.behavioral import (
        BehaviorBenchmarkConfig, BehaviorTask, ProductionAgentBehaviorDriver,
        web_search_behavior_manifest,
    )

    assert [case_id for case_id, _ in web_search_behavior_manifest()] == [
        "W1", "W2", "W3", "W4", "W5",
    ]
    task = BehaviorTask("W1", "web-knowledge", "research")
    disabled = ProductionAgentBehaviorDriver._tool_names(
        task, BehaviorBenchmarkConfig(model="mock", web_search_enabled=False), [],
    )
    enabled = ProductionAgentBehaviorDriver._tool_names(
        task, BehaviorBenchmarkConfig(model="mock", web_search_enabled=True), [],
    )
    assert "webfetch" in disabled
    assert "web_search" not in disabled
    assert "web_search" in enabled


def test_process_manifest_and_tool_exposure_include_persistent_process() -> None:
    from nz_coder.evaluation.behavioral import (
        BehaviorBenchmarkConfig,
        BehaviorTask,
        ProductionAgentBehaviorDriver,
        process_behavior_manifest,
    )

    assert [case_id for case_id, _ in process_behavior_manifest()] == [
        "P1", "P2", "P3", "P4", "P5", "P6",
    ]
    names = ProductionAgentBehaviorDriver._tool_names(
        BehaviorTask("P5", "persistent-process", "inspect crash"),
        BehaviorBenchmarkConfig(model="mock"),
        [],
    )
    assert "process" in names


def test_controlled_multi_agent_and_tool_scale_matrices_are_observable(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import (
        AgentBehaviorBenchmark, BehaviorBenchmarkConfig, ControlledBehaviorDriver,
    )

    benchmark = AgentBehaviorBenchmark(tmp_path, ControlledBehaviorDriver())
    two = benchmark.run_case("G", BehaviorBenchmarkConfig(model="controlled", child_agents=2))
    four = benchmark.run_case("G", BehaviorBenchmarkConfig(model="controlled", child_agents=4))
    all_tools = benchmark.run_case("H", BehaviorBenchmarkConfig(
        model="controlled", tool_catalog_size=200, progressive_exposure=False,
    ))
    progressive = benchmark.run_case("H", BehaviorBenchmarkConfig(
        model="controlled", tool_catalog_size=200, progressive_exposure=True,
    ))

    assert two["score"]["metrics"]["child_sessions"] == 1
    assert four["score"]["metrics"]["child_sessions"] == 3
    assert all("__pycache__" not in path for path in four["score"]["changed_files"])
    assert progressive["score"]["metrics"]["schema_tokens"] < all_tools["score"]["metrics"]["schema_tokens"]


def test_repo_intelligence_behavior_ab_covers_localization_and_impact(tmp_path) -> None:
    from nz_coder.evaluation.behavioral import run_repo_intelligence_behavior_ab

    result = run_repo_intelligence_behavior_ab(tmp_path)
    comparison = result["comparison"]

    assert len(result["runs"]) == 16
    assert comparison["v3"]["reads"] < comparison["current"]["reads"]
    assert comparison["v3"]["wrong_file_reads"] < comparison["off"]["wrong_file_reads"]
    assert comparison["v3"]["success_rate"] == 1.0
    assert comparison["lookup"]["success_rate"] == 1.0


def test_production_retrieval_matrix_resumes_only_exact_config_matches(
    tmp_path, monkeypatch,
) -> None:
    from nz_coder.evaluation import behavioral
    from nz_coder.evaluation.behavioral import BehaviorBenchmarkConfig

    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    matching = BehaviorBenchmarkConfig(
        provider="fixture-provider", model="fixture-model", reasoning="fixture",
        max_turns=2, repo_intelligence="lookup", retrieval_strategy="tool-only",
        repetition=1,
    )

    def run_payload(config):
        return {
            "task": {"case_id": "A"}, "config": asdict(config),
            "score": {"success": True, "metrics": {}}, "trace": {},
        }

    (report_dir / "matching.json").write_text(
        json.dumps(run_payload(matching)), encoding="utf-8",
    )
    calls = []

    def run_case(_self, case_id, config):
        calls.append((case_id, config.repetition, config.retrieval_strategy))
        return run_payload(config)

    monkeypatch.setattr(behavioral.AgentBehaviorBenchmark, "run_case", run_case)
    result = behavioral.run_production_retrieval_matrix(
        tmp_path, provider="fixture-provider", model="fixture-model",
        reasoning="fixture", max_turns=2, repetitions=3, case_ids="A",
        resume=True,
    )

    assert len(result["runs"]) == 12
    assert result["reused_runs"] == 1
    assert len(calls) == 11
