"""Tests for VerificationPlanner recommendations."""

import pytest


def test_plan_verification_python_prioritizes_py_compile_and_exact_test(tmp_path):
    from nz_coder import config
    from nz_coder.verification_planner import format_verification_plan, plan_verification_commands

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
        assert [stage["name"] for stage in plan["stages"]] == [
            "static", "targeted", "regression",
        ]
        static, targeted, regression = plan["stages"]
        assert static["required"] is True
        exact = next(item for item in targeted["commands"] if "::test_bar" in item["command"])
        related = next(item for item in targeted["commands"] if item["command"] == "pytest tests/test_foo.py")
        assert exact["required"] is True
        assert related["required"] is False
        assert regression["required"] is False
        formatted = format_verification_plan(plan)
        assert "pytest tests/test_foo.py::test_bar (required)" in formatted
        assert "pytest tests/test_foo.py (optional)" in formatted
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


def test_plan_verification_rejects_changed_file_outside_workspace(tmp_path):
    from nz_coder import config
    from nz_coder.verification_planner import plan_verification_commands

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with pytest.raises(ValueError, match="Path escapes workspace"):
            plan_verification_commands(
                changed_files=["../outside.py"],
                project_profile={"test_roots": [], "test_commands": []},
            )
    finally:
        config.WORKDIR = old


def test_include_broad_changes_legacy_bucket_but_not_gate_requirement():
    from nz_coder.verification_planner import plan_verification_commands

    profile = {"test_roots": ["tests"], "test_commands": ["pytest"], "typecheck_commands": []}
    plan = plan_verification_commands(
        changed_files=["src/foo.py"],
        project_profile=profile,
        include_broad=True,
    )

    assert "pytest" in [item["command"] for item in plan["recommended"]]
    regression = next(stage for stage in plan["stages"] if stage["name"] == "regression")
    assert regression["required"] is False
    assert regression["commands"][0]["required"] is False


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python3 -m py_compile src/app.py", "static"),
        ("ruff check src tests", "static"),
        ("go test ./pkg -run '^$'", "static"),
        ("pytest -q tests/test_app.py::test_case", "targeted"),
        ("python -m pytest tests/test_app.py", "targeted"),
        ("cargo test test_parser", "targeted"),
        ("pytest -q", "regression"),
        ("go test ./...", "regression"),
        ("npm test", "regression"),
        ("ruff format src", None),
        ("ruff check --fix src", None),
        ("python -m py_compile --help src/app.py", None),
        ("pytest --collect-only tests/test_app.py::test_case", None),
        ("pytest --co tests/test_app.py", None),
        ("pytest --version", None),
        ("cargo test --no-run test_parser", None),
        ("cargo test test_parser -- --list", None),
        ("cargo clippy --fix", None),
        ("npm run lint -- --fix", None),
        ("eslint --fix src/app.ts", None),
        ("biome check --write src", None),
        ("tsc", None),
        ("tsc --noEmit", "static"),
        ("tsc --noEmit false", None),
        ("python -c 'import os'", None),
        ("python -c 'assert 1 + 1 == 2'", "static"),
        ("echo pytest", None),
        ("printf 'pytest'", None),
        ("rg pytest nz_coder", None),
    ],
)
def test_classify_verification_command_uses_actual_executable(command, expected):
    from nz_coder.verification_planner import classify_verification_command

    assert classify_verification_command(command) == expected


def test_classify_compound_command_uses_highest_verification_stage():
    from nz_coder.verification_planner import classify_verification_command

    command = "cd repo && python -m py_compile src/app.py && pytest tests/test_app.py"
    assert classify_verification_command(command) == "targeted"


def test_planner_exact_match_cannot_override_non_execution_filter():
    from nz_coder.verification_planner import classify_verification_command

    command = "pytest --collect-only tests/test_app.py::test_case"
    plan = {
        "stages": [{
            "name": "targeted",
            "commands": [{"command": command, "required": True}],
        }],
    }

    assert classify_verification_command(command, plan) is None


def test_classify_verification_segments_returns_each_real_stage():
    from nz_coder.verification_planner import classify_verification_segments

    command = "python -m py_compile src/app.py && python -m pytest -q tests/test_app.py"
    assert [stage for stage, _segment in classify_verification_segments(command)] == [
        "static", "targeted",
    ]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python -m py_compile src/app.py && pytest tests/test_app.py", True),
        ("cargo check || true", False),
        ("pytest; echo done", False),
        ("pytest | tee output.log", False),
        ("pytest\ntrue", False),
        ("bash -lc 'pytest || true'", False),
        ("pytest 2>&1", True),
    ],
)
def test_verification_success_reliability_preserves_shell_flow(command, expected):
    from nz_coder.verification_planner import verification_success_is_reliable

    assert verification_success_is_reliable(command) is expected


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("1 failed, 3 passed", True),
        ("--- FAIL: TestParser (0.01s)", True),
        ("test result: FAILED. 1 passed; 1 failed", True),
        ("Django setup failed while loading settings", True),
        ("0 failed, 4 passed", False),
        ("FAILURE_THRESHOLD = 3", False),
        ("4 passed", False),
    ],
)
def test_shared_verification_failure_signals(output, expected):
    from nz_coder.verification_planner import verification_output_failed

    assert verification_output_failed(output) is expected


def test_python_pth_startup_warning_does_not_invalidate_successful_test_output():
    from nz_coder.verification_planner import verification_output_failed

    warning = """Error processing line 1 of /tmp/broken-nspkg.pth:

  Traceback (most recent call last):
    File \"<frozen site>\", line 207, in addpackage
  AttributeError: 'NoneType' object has no attribute 'loader'

Remainder of file ignored
.                                                                        [100%]
1 passed in 0.01s
"""

    assert verification_output_failed(warning) is False
    assert verification_output_failed(warning + "\nTraceback (most recent call last):\nFAIL: real") is True


def test_verification_command_key_normalizes_runner_wrapper_and_quiet_flag():
    from nz_coder.verification_planner import verification_command_key

    direct = "pytest tests/test_app.py::test_case"
    wrapped = "poetry run python -m pytest -q tests/test_app.py::test_case"
    assert verification_command_key(direct) == verification_command_key(wrapped)


def test_deleted_python_file_has_no_required_compile_command(monkeypatch):
    import nz_coder.verification_planner as planner

    monkeypatch.setattr(
        planner,
        "_git_deleted_files",
        lambda: (_ for _ in ()).throw(AssertionError("explicit paths must not query Git")),
    )
    plan = planner.plan_verification_commands(
        changed_files=["src/deleted.py"],
        deleted_files=["src/deleted.py"],
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )

    static = next(stage for stage in plan["stages"] if stage["name"] == "static")
    assert static["required"] is False
    assert static["commands"] == []
    assert any("Deleted files" in note for note in plan["notes"])


@pytest.mark.parametrize(
    ("changed_file", "metadata", "note_fragment"),
    [
        ("pkg/server.go", None, "no root go.mod or go.work"),
        ("src/lib.rs", None, "no root Cargo.toml"),
    ],
)
def test_plan_avoids_required_build_without_root_metadata(
    tmp_path, changed_file, metadata, note_fragment,
):
    from nz_coder import config
    from nz_coder.verification_planner import plan_verification_commands

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        plan = plan_verification_commands(
            changed_files=[changed_file],
            project_profile={"test_roots": [], "test_commands": [], "typecheck_commands": []},
        )
    finally:
        config.WORKDIR = old

    static = next(stage for stage in plan["stages"] if stage["name"] == "static")
    assert static["required"] is False
    assert static["commands"] == []
    assert any(note_fragment in note for note in plan["notes"])


@pytest.mark.parametrize(
    ("changed_file", "metadata", "expected_command"),
    [
        ("pkg/server.go", "go.mod", "go test ./pkg -run '^$'"),
        ("src/lib.rs", "Cargo.toml", "cargo check"),
    ],
)
def test_plan_requires_build_when_root_metadata_exists(
    tmp_path, changed_file, metadata, expected_command,
):
    from nz_coder import config
    from nz_coder.verification_planner import plan_verification_commands

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        (tmp_path / metadata).write_text("module demo\n", encoding="utf-8")
        plan = plan_verification_commands(
            changed_files=[changed_file],
            project_profile={"test_roots": [], "test_commands": [], "typecheck_commands": []},
        )
    finally:
        config.WORKDIR = old

    static = next(stage for stage in plan["stages"] if stage["name"] == "static")
    assert static["required"] is True
    assert expected_command in [item["command"] for item in static["commands"]]
