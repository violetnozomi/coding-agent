"""Release smoke isolation contracts."""
from __future__ import annotations

from pathlib import Path

from scripts.release_smoke import _installed_environment, _measure_cli_startup


def test_installed_environment_cannot_import_from_developer_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/workspace/source")
    monkeypatch.setenv("PYTHONHOME", "/workspace/python-home")
    monkeypatch.setenv("API_KEY", "secret")

    environment = _installed_environment()

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "API_KEY" not in environment


def test_release_smoke_builds_both_wheel_and_sdist():
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "release_smoke.py"
    ).read_text(encoding="utf-8")

    assert '"--sdist"' in source
    assert "Expected one sdist" in source


def test_measure_cli_startup_times_an_installed_version_command(tmp_path):
    calls = []
    clock = iter((4.0, 4.125))

    def run(command, **kwargs):
        calls.append((command, kwargs))

    elapsed = _measure_cli_startup(
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
