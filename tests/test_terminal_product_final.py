"""Contracts for the final T1-T20 terminal product scenario runner."""
from __future__ import annotations

import subprocess
import time

from nz_coder.evaluation.product_scenarios import (
    product_scenario_definitions,
    run_product_scenario_suite,
)




def test_product_scenario_manifest_covers_t1_through_t20_once():
    scenarios = product_scenario_definitions()

    assert [item.scenario_id for item in scenarios] == [
        f"T{index}" for index in range(1, 21)
    ]
    assert len({item.name for item in scenarios}) == 20
    assert all(item.command for item in scenarios)
    assert all(item.evidence in {"real-product", "component-contract"} for item in scenarios)
    assert all(item.evidence == "component-contract" for item in scenarios[:3])
    assert "test_connect_flow_masks" in scenarios[1].command[-1]
    assert "test_native_runner_completes_model_tool_model" in scenarios[2].command[-1]


def test_product_scenario_suite_records_each_result_and_metrics():
    calls: list[tuple[str, ...]] = []

    def execute(command, **_kwargs):
        calls.append(tuple(command))
        output = "1 passed"
        if any(item.endswith("tests/test_release_smoke.py") for item in command):
            output += '\nNZ_PRODUCT_METRICS {"startup_time_ms":125.0}'
        if any("test_connect_flow_masks" in item for item in command):
            output += '\nNZ_PRODUCT_METRICS {"provider_setup":"passed"}'
        if any("test_native_runner_completes_model_tool_model" in item for item in command):
            output += '\nNZ_PRODUCT_METRICS {"interactive_coding":"passed"}'
        if any("test_phase2_terminal_product_benchmark_drives_real_daemon_and_attach" in item for item in command):
            output += "\nNZ_PRODUCT_METRICS " + (
                '{"attach_latency_ms":{"median":12.5},'
                '"reconnect_latency_ms":{"median":15.5}}'
            )
        if any("test_process_product_metric_emits_zero_orphans" in item for item in command):
            output += '\nNZ_PRODUCT_METRICS {"orphan_process_count":0}'
        if any("test_command_discovery_metric" in item for item in command):
            output += '\n.......NZ_PRODUCT_METRICS {"command_discovery_latency_ms":2.5}'
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

    report = run_product_scenario_suite(executor=execute)

    assert report["success"] is True
    assert report["summary"] == {"passed": 20, "failed": 0, "total": 20}
    assert len(calls) == 20
    assert report["scenarios"][0]["id"] == "T1"
    assert report["scenarios"][-1]["id"] == "T20"
    assert report["metrics"]["render_errors"] == 0
    assert report["metrics"]["scenario_duration_ms"]["max"] >= 0
    assert report["metrics"]["attach_latency_ms"]["median"] == 12.5
    assert report["metrics"]["reconnect_latency_ms"]["median"] == 15.5
    assert report["metrics"]["orphan_process_count"] == 0
    assert report["metrics"]["startup_time_ms"] == 125.0
    assert report["metrics"]["command_discovery_latency_ms"] == 2.5
    assert report["metrics"]["provider_setup"] == "passed"
    assert report["metrics"]["interactive_coding"] == "passed"


def test_product_scenario_failure_is_attributed_without_stopping_later_checks():
    attempt = 0

    def execute(command, **_kwargs):
        nonlocal attempt
        attempt += 1
        if attempt == 4:
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="Traceback: permission failed"
            )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    report = run_product_scenario_suite(executor=execute)

    assert report["success"] is False
    assert report["summary"] == {"passed": 19, "failed": 1, "total": 20}
    assert report["scenarios"][3]["id"] == "T4"
    assert report["scenarios"][3]["status"] == "failed"
    assert report["metrics"]["render_errors"] == 1
    assert report["scenarios"][-1]["status"] == "passed"


def test_product_metrics_follow_the_scenario_that_proves_each_claim():
    attempt = 0

    def execute(command, **_kwargs):
        nonlocal attempt
        attempt += 1
        return subprocess.CompletedProcess(
            command,
            1 if attempt in {1, 10} else 0,
            stdout="",
            stderr="failed" if attempt in {1, 10} else "",
        )

    report = run_product_scenario_suite(executor=execute)

    assert report["metrics"]["doctor_accuracy"] == "failed"
    assert report["metrics"]["interaction_recovery"] is False


def test_process_product_metric_emits_zero_orphans(tmp_path):
    import json

    from nz_coder.evaluation.process_capability import (
        run_persistent_process_capability_benchmark,
    )

    report = run_persistent_process_capability_benchmark(tmp_path)

    assert report["structural_failures"] == 0
    assert report["orphan_process_count"] == 0
    print("NZ_PRODUCT_METRICS " + json.dumps({
        "orphan_process_count": report["orphan_process_count"],
    }, separators=(",", ":")))


def test_command_discovery_metric_uses_the_production_catalog(tmp_path):
    import json
    from dataclasses import replace

    from nz_coder.foundation.project_control import capture_project_control_snapshot
    from nz_coder.interface.custom_commands import CommandCatalog

    command_dir = tmp_path / ".nz-coder" / "commands"
    command_dir.mkdir(parents=True)
    for index in range(200):
        (command_dir / f"command-{index:03d}.md").write_text(
            f"---\ndescription: Command {index}\n---\nRun $ARGUMENTS\n",
            encoding="utf-8",
        )

    started = time.perf_counter()
    snapshot = replace(capture_project_control_snapshot(tmp_path), trusted=True)
    catalog = CommandCatalog.discover(
        project_dir=command_dir,
        project_trusted=True,
        project_control_snapshot=snapshot,
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 3)

    assert len(catalog.list()) == 200
    assert catalog.errors == ()
    print("NZ_PRODUCT_METRICS " + json.dumps({
        "command_discovery_latency_ms": latency_ms,
    }, separators=(",", ":")))
