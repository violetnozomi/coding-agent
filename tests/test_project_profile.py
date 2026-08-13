"""Tests for ProjectProfile detection and tool output."""


def test_project_profile_detects_python_pytest(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.project_profile import build_project_profile, compact_profile_summary, project_profile

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths=["tests"]\n', encoding="utf-8")
        (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("def test_x(): assert True\n", encoding="utf-8")

        profile = build_project_profile(save=True)
        assert "python" in profile["languages"]
        assert "pip" in profile["package_managers"]
        assert "pytest" in profile["test_commands"]
        assert "src" in profile["source_roots"]
        assert "tests" in profile["test_roots"]
        assert (tmp_path / ".nz-coder" / "project_profile.json").exists()
        assert "ProjectProfile:" in compact_profile_summary(profile)
        assert "tests=pytest" in project_profile(save=False)
    finally:
        config.WORKDIR = old


def test_project_profile_detects_node_scripts(tmp_path):
    from nz_coder import config
    from nz_coder.project_profile import build_project_profile

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "package.json").write_text(
            '{"scripts":{"test":"vitest","typecheck":"tsc --noEmit","lint":"eslint .","build":"vite build"},"devDependencies":{"typescript":"latest"}}',
            encoding="utf-8",
        )
        (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
        (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")
        profile = build_project_profile(save=False)
        assert "typescript" in profile["languages"]
        assert "pnpm" in profile["package_managers"]
        assert "pnpm test" in profile["test_commands"]
        assert "pnpm typecheck" in profile["typecheck_commands"]
        assert "pnpm lint" in profile["lint_commands"]
    finally:
        config.WORKDIR = old
