"""Completeness contracts for W/U/R release manifests."""
from __future__ import annotations

from pathlib import Path
import subprocess

from nz_coder.evaluation.windows_product_scenarios import (
    acceptance_manifest,
    run_acceptance_suite,
    tui_scenarios,
)


def test_windows_manifest_has_w1_through_w15_and_executable_owners():
    manifest = acceptance_manifest()
    scenarios = manifest["windows"]
    assert [item["scenario_id"] for item in scenarios] == [f"W{i}" for i in range(1, 16)]
    assert all(item["command"] and item["native_platform"] == "windows" for item in scenarios)
    by_id = {item["scenario_id"]: item for item in scenarios}
    assert "job_object_binding" in " ".join(by_id["W6"]["command"])
    assert "test_daemon_start_status_stop_owns_pid_and_private_token" in " ".join(
        by_id["W10"]["command"]
    )


def test_tui_manifest_has_u1_through_u14_and_executable_owners():
    scenarios = acceptance_manifest()["tui"]
    assert [item["scenario_id"] for item in scenarios] == [f"U{i}" for i in range(1, 15)]
    assert all(item["command"] for item in scenarios)


def test_release_manifests_have_r1_through_r12_per_platform():
    release = acceptance_manifest()["release"]
    for platform in ("windows", "linux"):
        assert [item["scenario_id"] for item in release[platform]] == [f"R{i}" for i in range(1, 13)]
        assert all(item["native_platform"] == platform for item in release[platform])


def test_windows_workflow_installs_all_required_lsp_families():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "windows-product-rc.yml"
    ).read_text(encoding="utf-8")

    assert "basedpyright" in workflow
    assert "typescript-language-server" in workflow
    assert "golang.org/x/tools/gopls" in workflow
    assert "tree_sitter" in workflow


def test_acceptance_runner_records_release_evidence_fields_without_short_circuiting():
    calls = 0

    def execute(command, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command,
            1 if calls == 2 else 0,
            stdout="ok" if calls != 2 else "",
            stderr="broken" if calls == 2 else "",
        )

    report = run_acceptance_suite(
        "TUI U1-U14",
        tui_scenarios()[:3],
        executor=execute,
    )

    assert calls == 3
    assert report["summary"] == {"passed": 2, "failed": 1, "total": 3}
    assert set(report["environment"]) >= {
        "platform", "python_version", "package_version", "machine",
    }
    assert report["scenarios"][1]["scenario"] == "U2"
    assert report["scenarios"][1]["result"] == "failed"
    assert report["scenarios"][1]["failure"] == "broken"
    assert report["scenarios"][1]["duration_ms"] >= 0


def test_windows_workflow_persists_structured_acceptance_artifacts():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "windows-product-rc.yml"
    ).read_text(encoding="utf-8")

    assert "run_rc_acceptance.py --suite windows" in workflow
    assert "benchmark_terminal_product_final.py" in workflow
    assert "actions/upload-artifact" in workflow
    assert "windows-rc-evidence" in workflow
