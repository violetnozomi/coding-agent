"""Release smoke isolation contracts."""
from __future__ import annotations

from pathlib import Path
import re

from nz_coder.evaluation.release_contracts import (
    installed_environment,
    measure_cli_startup,
)


def test_installed_environment_cannot_import_from_developer_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/workspace/source")
    monkeypatch.setenv("PYTHONHOME", "/workspace/python-home")
    monkeypatch.setenv("API_KEY", "secret")

    environment = installed_environment()

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "API_KEY" not in environment


def test_release_smoke_builds_both_wheel_and_sdist():
    source = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "core-runtime.yml"
    ).read_text(encoding="utf-8")

    assert "python -m build --wheel --sdist" in source
    assert "python -m nz_coder --help" in source


def test_core_runtime_exercises_declared_python_floor():
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "core-runtime.yml"
    ).read_text(encoding="utf-8")

    assert 'python-version: "3.9"' in workflow
    assert "python-compatibility" in workflow
    assert "tests/runtime/test_native_runner.py" in workflow
    assert "python -m build --wheel --sdist" in workflow


def test_declared_dev_dependencies_are_sufficient_for_full_suite():
    project = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert '"pytest>=7,<9"' in project
    assert '"ruff==0.15.10"' in project
    assert not re.search(r'^\s*"httpx(?:[<>=;\[]|\")', project, re.MULTILINE)


def test_openai_dependency_declares_supported_major_range():
    project = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert '"openai>=1.30.0,<4"' in project


def test_measure_cli_startup_times_an_installed_version_command(tmp_path):
    calls = []
    clock = iter((4.0, 4.125))

    def run(command, **kwargs):
        calls.append((command, kwargs))

    elapsed = measure_cli_startup(
        tmp_path / "python",
        tmp_path,
        {"PATH": "/bin"},
        run=run,
        clock=lambda: next(clock),
    )

    assert elapsed == 125.0
    assert calls == [([
        str(tmp_path / "python"), "-m", "nz_coder", "--version",
    ], {"cwd": tmp_path, "env": {"PATH": "/bin"}})]
