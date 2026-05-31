"""Tests for VerificationPlanner recommendations."""


def test_plan_verification_python_prioritizes_py_compile_and_exact_test(tmp_path):
    from nz_coder import config
    from nz_coder.verification_planner import plan_verification_commands

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("def test_bar(): assert True\n", encoding="utf-8")
        profile = {"test_roots": ["tests"], "test_commands": ["pytest"], "typecheck_commands": []}
        plan = plan_verification_commands(
            changed_files=["src/foo.py"],
            failing_tests=["tests/test_foo.py::test_bar"],
            project_profile=profile,
        )
        commands = [item["command"] for item in plan["recommended"]]
        assert "python -m py_compile src/foo.py" in commands
        assert "pytest tests/test_foo.py::test_bar" in commands
        assert "pytest" in [item["command"] for item in plan["fallback"]]
    finally:
        config.WORKDIR = old


def test_plan_verification_node_uses_typecheck_without_broad_default():
    from nz_coder.verification_planner import plan_verification_commands

    profile = {"typecheck_commands": ["pnpm typecheck"], "test_commands": ["pnpm test"]}
    plan = plan_verification_commands(changed_files=["src/app.ts"], project_profile=profile)
    assert [item["command"] for item in plan["recommended"]] == ["pnpm typecheck"]
    assert "pnpm test" in [item["command"] for item in plan["fallback"]]


def test_plan_verification_quotes_paths_with_spaces():
    from nz_coder.verification_planner import plan_verification_commands

    plan = plan_verification_commands(
        changed_files=["src/my file.py"],
        failing_tests=["tests/test my file.py::test_case"],
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )
    commands = [item["command"] for item in plan["recommended"]]
    assert "python -m py_compile 'src/my file.py'" in commands
    assert "pytest 'tests/test my file.py::test_case'" in commands
