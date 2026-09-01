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


def test_windows_workflow_runs_doctor_with_non_secret_ci_credential():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "windows-product-rc.yml"
    ).read_text(encoding="utf-8")

    doctor_step = workflow.split("- name: Platform and doctor evidence", 1)[1]
    doctor_step = doctor_step.split("- name:", 1)[0]
    assert "API_KEY: nz-coder-ci-doctor-placeholder" in doctor_step


def test_product_workflow_installs_backend_for_non_isolated_release_builds():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "windows-product-rc.yml"
    ).read_text(encoding="utf-8")

    install_commands = [
        line.strip()
        for line in workflow.splitlines()
        if "pip install" in line and '".[dev]"' in line
    ]
    assert len(install_commands) == 2
    assert all('"setuptools>=68"' in command for command in install_commands)
    assert all(" wheel" in command for command in install_commands)


def test_windows_workflow_disables_go_cache_without_a_go_module():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "windows-product-rc.yml"
    ).read_text(encoding="utf-8")

    go_setup = workflow.split("- uses: actions/setup-go@v6", 1)[1]
    go_setup = go_setup.split("- name:", 1)[0]
    assert "cache: false" in go_setup


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

    assert "tests/test_windows_platform_runtime.py" in workflow
    assert "tests/test_http_service.py" in workflow
    assert "python -m build --wheel --sdist" in workflow
    assert "installed-platform.json" in workflow
    assert "actions/upload-artifact" in workflow
    assert "windows-rc-evidence" in workflow
