"""Tests for generated-project verification."""


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
