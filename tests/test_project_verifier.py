"""Tests for generated-project verification."""

import subprocess

import pytest


def test_verify_project_build_runs_py_compile_and_pytest(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.project_creation.verifier import verify_project_build

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "tests").mkdir()
        (tmp_path / "app.py").write_text("def main():\n    return 0\n", encoding="utf-8")
        (tmp_path / "tests" / "test_smoke.py").write_text(
            "def test_smoke():\n    assert True\n",
            encoding="utf-8",
        )
        result = verify_project_build(
            ".",
            ["python -m py_compile app.py", "python -m pytest tests/test_smoke.py"],
        )
        assert result.startswith("OK:")
    finally:
        config.WORKDIR = old


def test_verify_project_build_warns_on_missing_dependency(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.project_creation import verifier

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        calls = []

        def fake_run(*args, **kwargs):
            calls.append(args[0])
            raise FileNotFoundError("pnpm not found")

        monkeypatch.setattr(verifier.subprocess, "run", fake_run)
        result = verifier.verify_project_build(".", ["pnpm test"])
        assert result.startswith("WARN:")
        assert "dependencies" in result.lower()
    finally:
        config.WORKDIR = old


@pytest.mark.parametrize(('command', 'expected'), [
    ("python3 -m pytest -q 'tests/a file.py::test_b[x]'", ['python3', '-m', 'pytest', '-q', 'tests/a file.py::test_b[x]']),
    ('python3 -m py_compile app.py', ['python3', '-m', 'py_compile', 'app.py']),
    ('go test ./... -run ^$', ['go', 'test', './...', '-run', '^$']),
    ('uvicorn app:app --help', ['uvicorn', 'app:app', '--help']),
    ('uvicorn --help', ['uvicorn', '--help']),
    ('python3 -m pytest_extra -q', None),
    ('python -m pytest_extra', None),
    ('pytest-other -q', None),
    ('npm run build-other', None),
    ('uvicorn app:app --help-extra', None),
    ('uvicorn --app-dir --help app:app', None),
    ('python3 -m pytest -q | tail -2', None),
    ('pytest -q; echo done', None),
    ('pytest "$TARGET"', None),
    ('PYTHONPATH=src python3 -m ledger --help', None),
])
def test_project_command_runs_only_the_allowed_literal_argv(tmp_path, monkeypatch, command, expected):
    from nz_coder.project_creation import verifier

    calls = []

    def run(argv, **options):
        calls.append((list(argv), options['cwd'], options.get('shell', False)))
        return subprocess.CompletedProcess(argv, 0, '1 passed in 0.01s', '')

    monkeypatch.setattr(verifier.subprocess, 'run', run)
    observed = verifier._run_one(command, tmp_path)
    assert calls == ([(expected, tmp_path, False)] if expected else [])
    assert observed['status'] == ('passed' if expected else 'blocked')
