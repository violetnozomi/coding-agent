"""Executable T1-T20 product acceptance scenarios for the terminal release."""
from __future__ import annotations

from dataclasses import dataclass
import os
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any, Callable, Sequence


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ProductScenario:
    """One independently executable product acceptance scenario."""

    scenario_id: str
    name: str
    command: tuple[str, ...]
    evidence: str = "component-contract"


def product_scenario_definitions() -> tuple[ProductScenario, ...]:
    """Return the stable final-product acceptance manifest."""
    pytest = (sys.executable, "-m", "pytest", "-q")
    return (
        ProductScenario(
            "T1",
            "Release and installed-environment contracts",
            pytest + ("-s", "tests/test_release_smoke.py"),
        ),
        ProductScenario(
            "T2",
            "First provider setup",
            pytest + (
                "-s",
                "tests/test_terminal_product_alignment.py::test_connect_flow_masks_and_saves_credential_before_discovery",
            ),
        ),
        ProductScenario(
            "T3",
            "Interactive coding task",
            pytest + (
                "-s",
                "tests/runtime/test_native_runner.py::test_native_runner_completes_model_tool_model_without_agent_loop",
            ),
        ),
        ProductScenario("T4", "Permission interaction", pytest + ("tests/test_permissions.py::test_permission_manager_supports_http_once_reject_and_scoped_always",)),
        ProductScenario("T5", "Session resume", pytest + ("tests/test_http_service.py::test_http_restart_discovers_and_lazily_restores_session",)),
        ProductScenario("T6", "Fork undo redo", pytest + ("tests/test_timeline.py::test_fork_history_keeps_complete_turn_and_returns_deep_copy", "tests/test_changes_undo.py::test_multi_level_undo_redo_restores_files_and_history")),
        ProductScenario("T7", "Background Agent and Workflow", pytest + (
            "tests/test_agent_manager.py::test_background_manager_starts_parallel_non_overlapping_tasks",
            "tests/test_http_service.py::test_http_projects_agents_and_controls_runtime_owned_workflow",
            "tests/test_http_service.py::test_http_remote_workflow_prepare_and_start_require_exact_approval",
        )),
        ProductScenario("T8", "Persistent Process", pytest + ("-s", "tests/test_terminal_product_final.py::test_process_product_metric_emits_zero_orphans")),
        ProductScenario("T9", "Daemon attach", pytest + ("-s", "tests/test_terminal_product_benchmark.py::test_phase2_terminal_product_benchmark_drives_real_daemon_and_attach",), "real-product"),
        ProductScenario("T10", "Remote reconnect", pytest + (
            "tests/test_http_service.py::test_http_client_reconnects_with_latest_complete_event_id",
            "tests/test_http_service.py::test_http_client_survives_three_delayed_disconnects_without_duplicates",
            "tests/test_http_service.py::test_resilient_client_rebaselines_after_an_explicit_gap",
            "tests/test_http_service.py::test_remote_child_running_disconnect_then_completed_reconnect",
            "tests/test_http_service.py::test_http_question_reply_validation_reject_and_abort",
            "tests/test_http_service.py::test_two_attached_clients_receive_same_events_and_one_permission_effect",
            "tests/test_daemon.py::test_daemon_restart_preserves_workspace_sessions_and_rotates_token",
        )),
        ProductScenario("T11", "Remote permission", pytest + ("tests/test_http_service.py::test_http_permission_request_reply_and_late_reply_boundary",)),
        ProductScenario("T12", "Remote process", pytest + ("tests/test_http_service.py::test_remote_session_controls_two_persistent_processes_by_identity",)),
        ProductScenario("T13", "Custom Command", pytest + (
            "-s",
            "tests/test_custom_commands.py",
            "tests/test_terminal_product_final.py::test_command_discovery_metric_uses_the_production_catalog",
        )),
        ProductScenario("T14", "Skill", pytest + ("tests/test_skill_governance.py", "tests/test_skill_loading.py")),
        ProductScenario("T15", "MCP", pytest + ("tests/test_mcp.py::test_mcp_client_initializes_lists_calls_times_out_and_closes",)),
        ProductScenario("T16", "Memory review and curation", pytest + ("tests/test_memory_control.py", "tests/test_memory_commands.py")),
        ProductScenario("T17", "Extension reload and disable", pytest + ("tests/test_extensions.py::test_skill_enable_disable_is_owned_persisted_and_runtime_effective", "tests/test_extensions.py::test_extension_reload_delegates_to_real_owners_and_reports_restart_truth", "tests/test_extensions.py::test_terminal_extension_controls_delegate_to_registry_owner")),
        ProductScenario("T18", "Terminal and large-output stress", pytest + (
            "tests/test_terminal_product_stress.py",
            "tests/test_fullscreen.py",
            "tests/test_terminal_input.py",
            "tests/tool_platform/test_result_projection.py::test_large_result_preserves_head_tail_and_durable_reference",
        )),
        ProductScenario("T19", "Cross-platform capability probe", pytest + ("tests/test_platform_capabilities.py",)),
        ProductScenario("T20", "Headless JSONL", pytest + ("tests/test_headless_cli.py::test_headless_jsonl_projects_runtime_events_then_result",)),
    )


def run_product_scenario_suite(
    *,
    executor: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Run all scenarios independently so one failure never hides later evidence."""
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (str(ROOT), existing) if value
    )
    results: list[dict[str, Any]] = []
    durations: list[float] = []
    render_errors = 0
    for scenario in product_scenario_definitions():
        started = time.perf_counter()
        try:
            completed = executor(
                list(scenario.command),
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=max(1.0, float(timeout_seconds)),
                check=False,
            )
            returncode = int(completed.returncode)
            stdout = str(completed.stdout or "")
            stderr = str(completed.stderr or "")
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = str(exc.stdout or "")
            stderr = f"scenario timed out after {timeout_seconds:.1f}s"
        duration = (time.perf_counter() - started) * 1000
        durations.append(duration)
        combined = f"{stdout}\n{stderr}"
        tracebacks = combined.count("Traceback")
        render_errors += tracebacks
        results.append({
            "id": scenario.scenario_id,
            "name": scenario.name,
            "status": "passed" if returncode == 0 else "failed",
            "evidence": scenario.evidence,
            "returncode": returncode,
            "duration_ms": round(duration, 3),
            "command": list(scenario.command),
            "output_excerpt": _bounded_excerpt(combined),
        })
    passed = sum(item["status"] == "passed" for item in results)
    emitted_metrics: dict[str, Any] = {}
    for item in results:
        for line in str(item.get("output_excerpt") or "").splitlines():
            marker = "NZ_PRODUCT_METRICS "
            marker_index = line.find(marker)
            if marker_index < 0:
                continue
            try:
                value = json.loads(line[marker_index + len(marker):])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                emitted_metrics.update(value)
    return {
        "schema_version": 1,
        "suite": "NZCoder ProductScenarioSuite T1-T20",
        "success": passed == len(results),
        "summary": {"passed": passed, "failed": len(results) - passed, "total": len(results)},
        "scenarios": results,
        "metrics": {
            "scenario_duration_ms": _distribution(durations),
            "startup_smoke": results[0]["status"],
            "session_resume_success": results[4]["status"] == "passed",
            "interaction_recovery": all(
                results[index]["status"] == "passed" for index in (9, 10)
            ),
            "event_duplication": 0 if results[9]["status"] == "passed" else None,
            "render_errors": render_errors,
            "command_discovery": results[12]["status"],
            "command_discovery_latency_ms": emitted_metrics.get(
                "command_discovery_latency_ms"
            ),
            "memory_review_correctness": results[15]["status"],
            "extension_reload_result": results[16]["status"],
            "doctor_accuracy": results[0]["status"],
            "install_smoke_result": results[0]["status"],
            "startup_time_ms": emitted_metrics.get("startup_time_ms"),
            "provider_setup": emitted_metrics.get("provider_setup"),
            "interactive_coding": emitted_metrics.get("interactive_coding"),
            "attach_latency_ms": emitted_metrics.get("attach_latency_ms"),
            "reconnect_latency_ms": emitted_metrics.get("reconnect_latency_ms"),
            "orphan_process_count": emitted_metrics.get("orphan_process_count"),
        },
    }


def _bounded_excerpt(value: str, limit: int = 2_000) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... output truncated ...\n{text[-half:]}"


def _distribution(values: Sequence[float]) -> dict[str, float]:
    ordered = sorted(float(value) for value in values) or [0.0]
    p95_index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * 0.95)))
    return {
        "min": round(ordered[0], 3),
        "median": round(float(statistics.median(ordered)), 3),
        "p95": round(ordered[p95_index], 3),
        "max": round(ordered[-1], 3),
    }


__all__ = [
    "ProductScenario",
    "product_scenario_definitions",
    "run_product_scenario_suite",
]
