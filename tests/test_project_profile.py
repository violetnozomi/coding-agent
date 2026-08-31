"""Tests for ProjectProfile detection and tool output."""


def test_project_profile_detects_python_pytest(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import build_project_profile, compact_profile_summary, project_profile

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


def test_project_profile_prefers_repository_native_python_test_runner(
    tmp_path,
    monkeypatch,
):
    """A custom runtests.py is stronger evidence than a bare tests directory."""
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import (
        build_project_profile,
        compact_profile_summary,
    )

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "runtests.py").write_text("# native runner\n", encoding="utf-8")
    (tests / "test_app.py").write_text(
        "def test_app(): pass\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    profile = build_project_profile(save=False)

    assert profile["test_commands"] == ["python tests/runtests.py"]
    assert "tests=python tests/runtests.py" in compact_profile_summary(profile)


def test_project_profile_detects_node_scripts(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import build_project_profile

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


def test_project_profile_keeps_unmanaged_product_prefixed_sources(
    tmp_path,
    monkeypatch,
):
    """A user directory is not internal state merely because it starts with .product-."""
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import build_project_profile

    source = tmp_path / ".product-catalog"
    source.mkdir()
    (source / "main.go").write_text("package main\n", encoding="utf-8")
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    profile = build_project_profile(save=False)

    assert "go" in profile["languages"]


def test_project_execution_facts_expose_python_module_cwd(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import build_project_execution_facts

    (tmp_path / "cron_engine").mkdir()
    (tmp_path / "cron_engine" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "cron_engine" / "__main__.py").write_text("", encoding="utf-8")
    (tmp_path / "cron_engine" / "tests").mkdir()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    facts = build_project_execution_facts()

    assert facts["workspace_root"] == str(tmp_path.resolve())
    assert facts["project_root"] == str(tmp_path.resolve())
    assert facts["python_packages"] == [{
        "module_name": "cron_engine",
        "package_path": "cron_engine",
        "module_cwd": str(tmp_path.resolve()),
    }]
    assert facts["entrypoints"] == ["cron_engine/__main__.py"]


def test_project_execution_facts_handle_src_layout(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import build_project_execution_facts

    (tmp_path / "src" / "sample").mkdir(parents=True)
    (tmp_path / "src" / "sample" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    facts = build_project_execution_facts()

    assert facts["python_packages"] == [{
        "module_name": "sample",
        "package_path": "src/sample",
        "module_cwd": str((tmp_path / "src").resolve()),
    }]
    assert facts["source_roots"] == ["src"]
    assert facts["test_roots"] == ["tests"]


def test_project_execution_facts_detect_single_nested_python_project(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.intelligence.project_profile import build_project_execution_facts

    project = tmp_path / "cron_engine"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths=["tests"]\n',
        encoding="utf-8",
    )
    (project / "__init__.py").write_text("", encoding="utf-8")
    (project / "__main__.py").write_text("", encoding="utf-8")
    (project / "tests").mkdir()
    (project / "tests" / "test_parser.py").write_text(
        "def test_parser(): assert True\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    facts = build_project_execution_facts()

    assert facts["project_root"] == str(project.resolve())
    assert facts["test_roots"] == ["cron_engine/tests"]
    assert facts["test_commands"] == [
        "python -m pytest -q cron_engine/tests",
    ]
    assert facts["python_packages"] == [{
        "module_name": "cron_engine",
        "package_path": "cron_engine",
        "module_cwd": str(tmp_path.resolve()),
    }]
