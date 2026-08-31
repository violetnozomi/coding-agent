"""Tests for VerificationPlanner recommendations."""

import pytest


def test_plan_verification_python_prioritizes_py_compile_and_exact_test(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.intelligence.verification_planner import format_verification_plan, plan_verification_commands

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
        assert exact["automation_provenance"] == "failure_evidence"
        assert related["required"] is False
        assert "automation_provenance" not in related
        assert regression["required"] is False
        formatted = format_verification_plan(plan)
        assert "pytest tests/test_foo.py::test_bar (required)" in formatted
        assert "pytest tests/test_foo.py (optional)" in formatted
    finally:
        config.WORKDIR = old


def test_strict_plan_keeps_inferred_related_test_advisory(tmp_path):
    """Strict mode must not turn a filename guess into required evidence."""
    from nz_coder.foundation import config
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    old = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        source = tmp_path / "lib" / "package" / "widget.py"
        test = tmp_path / "lib" / "package" / "tests" / "test_widget.py"
        source.parent.mkdir(parents=True)
        test.parent.mkdir(parents=True)
        source.write_text("def widget(): return 1\n", encoding="utf-8")
        test.write_text("def test_widget(): assert True\n", encoding="utf-8")

        plan = plan_verification_commands(
            changed_files=["lib/package/widget.py"],
            project_profile={
                "test_roots": ["tests"],
                "test_commands": ["pytest"],
                "typecheck_commands": [],
            },
            require_targeted=True,
            use_repo_intelligence=False,
        )
    finally:
        config.WORKDIR = old

    targeted = next(stage for stage in plan["stages"] if stage["name"] == "targeted")
    assert targeted["required"] is True
    assert targeted["evidence_required"] is True
    assert targeted["commands"] == [{
        "command": "pytest lib/package/tests/test_widget.py",
        "reason": "related test candidate for lib/package/widget.py",
        "required": False,
    }]


def test_strict_plan_ranks_path_affine_test_without_requiring_it(
    tmp_path,
):
    """A generic graph edge must not become the required SWE behavior check."""
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    source = tmp_path / "src" / "_pytest" / "assertion" / "rewrite.py"
    relevant = tmp_path / "testing" / "test_assertrewrite.py"
    generic = tmp_path / "doc" / "en" / "example" / "assertion" / "test_setup_flow_example.py"
    unrelated = tmp_path / "testing" / "code" / "test_code.py"
    for path in (source, relevant, generic, unrelated):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_placeholder(): pass\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=["src/_pytest/assertion/rewrite.py"],
            related_tests=[
                "doc/en/example/assertion/test_setup_flow_example.py",
                "testing/code/test_code.py",
            ],
            project_profile={
                "test_roots": ["testing", "doc"],
                "test_commands": ["pytest"],
                "typecheck_commands": [],
            },
            require_targeted=True,
            use_repo_intelligence=False,
        )

    targeted = next(stage for stage in plan["stages"] if stage["name"] == "targeted")
    commands = targeted["commands"]
    assert commands[0]["command"] == "pytest testing/test_assertrewrite.py"
    assert all(item["required"] is False for item in commands)


def test_plan_uses_repository_native_python_runner_for_related_tests(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    source = tmp_path / "django" / "forms" / "widgets.py"
    test = tmp_path / "tests" / "forms_tests" / "tests" / "test_media.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("class Media: pass\n", encoding="utf-8")
    test.write_text("def test_merge(): pass\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=["django/forms/widgets.py"],
            project_profile={
                "test_roots": ["tests"],
                "test_commands": ["python tests/runtests.py"],
                "typecheck_commands": [],
            },
            use_repo_intelligence=False,
        )

    targeted = next(
        stage for stage in plan["stages"] if stage["name"] == "targeted"
    )
    assert targeted["commands"][0] == {
        "command": (
            "python tests/runtests.py "
            "tests/forms_tests/tests/test_media.py"
        ),
        "reason": "related test candidate for django/forms/widgets.py",
        "required": False,
    }
    assert [item["command"] for item in plan["fallback"]] == [
        "python tests/runtests.py",
    ]
    assert plan.get("native_runner_kind", "") == ""


def test_plan_marks_verified_django_native_runner(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    source = tmp_path / "django" / "forms" / "widgets.py"
    django_init = tmp_path / "django" / "__init__.py"
    runner = tmp_path / "tests" / "runtests.py"
    related = tmp_path / "tests" / "forms_tests" / "test_widgets.py"
    for path in (source, django_init, runner, related):
        path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("class Widget: pass\n", encoding="utf-8")
    django_init.write_text("VERSION = (2, 2)\n", encoding="utf-8")
    runner.write_text("import django\n", encoding="utf-8")
    related.write_text("def test_widget(): pass\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=["django/forms/widgets.py"],
            project_profile={
                "test_roots": ["tests"],
                "test_commands": ["python tests/runtests.py"],
                "typecheck_commands": [],
            },
            use_repo_intelligence=False,
        )

    assert plan["native_runner_kind"] == "django"
    targeted = next(
        stage for stage in plan["stages"] if stage["name"] == "targeted"
    )
    assert targeted["commands"][0]["command"] == (
        "python tests/runtests.py forms_tests.test_widgets"
    )


def test_plan_preserves_paths_for_untrusted_vendor_runtests(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    source = tmp_path / "django" / "forms" / "widgets.py"
    test = tmp_path / "tests" / "forms_tests" / "tests" / "test_media.py"
    runner = tmp_path / "vendor" / "tests" / "runtests.py"
    for path in (source, test, runner):
        path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("class Media: pass\n", encoding="utf-8")
    test.write_text("def test_merge(): pass\n", encoding="utf-8")
    runner.write_text("print('custom runner')\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        plan = plan_verification_commands(
            changed_files=["django/forms/widgets.py"],
            project_profile={
                "test_roots": ["tests"],
                "test_commands": ["python vendor/tests/runtests.py"],
                "typecheck_commands": [],
            },
            use_repo_intelligence=False,
        )

    targeted = next(
        stage for stage in plan["stages"] if stage["name"] == "targeted"
    )
    assert plan["native_runner_kind"] == ""
    assert targeted["commands"][0]["command"] == (
        "python vendor/tests/runtests.py "
        "tests/forms_tests/tests/test_media.py"
    )


def test_plan_verification_node_uses_typecheck_without_broad_default():
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    profile = {"typecheck_commands": ["pnpm typecheck"], "test_commands": ["pnpm test"]}
    plan = plan_verification_commands(changed_files=["src/app.ts"], project_profile=profile)
    assert [item["command"] for item in plan["recommended"]] == ["pnpm typecheck"]
    assert "pnpm test" in [item["command"] for item in plan["fallback"]]


def test_plan_verification_quotes_paths_with_spaces():
    from nz_coder.intelligence.verification_planner import plan_verification_commands

    plan = plan_verification_commands(
        changed_files=["src/my file.py"],
        failing_tests=["tests/test my file.py::test_case"],
        project_profile={"test_roots": ["tests"], "test_commands": ["pytest"]},
    )
    commands = [item["command"] for item in plan["recommended"]]
    assert "python -m py_compile 'src/my file.py'" in commands
    assert "pytest 'tests/test my file.py::test_case'" in commands


def test_plan_verification_rejects_changed_file_outside_workspace(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.intelligence.verification_planner import plan_verification_commands

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
    from nz_coder.intelligence.verification_planner import plan_verification_commands

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
        (
            "PYTHONPATH=. python3 tests/runtests.py "
            "auth_tests.test_migrations --parallel 1 -v1",
            "targeted",
        ),
        (
            "python3 tests/runtests.py --parallel 1 "
            "auth_tests.test_migrations -v1",
            "targeted",
        ),
        (
            "python3 tests/runtests.py --settings=test_sqlite "
            "auth_tests.test_migrations",
            "targeted",
        ),
        ("python3 tests/runtests.py --parallel 1", "regression"),
        ("python3 tests/runtests.py --settings=test_sqlite", "regression"),
        (
            "python3 tests/runtests.py --liveserver localhost:8081",
            "regression",
        ),
        ("python tests/runtests.py", "regression"),
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
    from nz_coder.intelligence.verification_planner import classify_verification_command

    assert classify_verification_command(command) == expected


def test_classify_compound_command_uses_highest_verification_stage():
    from nz_coder.intelligence.verification_planner import classify_verification_command

    command = "cd repo && python -m py_compile src/app.py && pytest tests/test_app.py"
    assert classify_verification_command(command) == "targeted"


def test_planner_exact_match_cannot_override_non_execution_filter():
    from nz_coder.intelligence.verification_planner import classify_verification_command

    command = "pytest --collect-only tests/test_app.py::test_case"
    plan = {
        "stages": [{
            "name": "targeted",
            "commands": [{"command": command, "required": True}],
        }],
    }

    assert classify_verification_command(command, plan) is None


def test_classify_verification_segments_returns_each_real_stage():
    from nz_coder.intelligence.verification_planner import classify_verification_segments

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
    from nz_coder.intelligence.verification_planner import verification_success_is_reliable

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
    from nz_coder.intelligence.verification_planner import verification_output_failed

    assert verification_output_failed(output) is expected


@pytest.mark.parametrize(
    "output",
    [
        "no tests ran in 0.01s",
        "collected 0 items",
        "Ran 0 tests in 0.000s",
        "Found 0 test(s).",
        "? example/pkg [no test files]",
        "ok example/pkg 0.002s [no tests to run]",
        "No tests found, exiting with code 0",
        "Tests run: 0, Failures: 0, Errors: 0, Skipped: 0",
        "running 0 tests",
        "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured",
    ],
)
def test_verification_output_has_no_tests(output):
    from nz_coder.intelligence.verification_planner import verification_output_has_no_tests

    assert verification_output_has_no_tests(output)


@pytest.mark.parametrize(
    "output",
    [
        "1 passed in 0.01s",
        "Ran 2 tests in 0.002s",
        "Tests run: 3, Failures: 0, Errors: 0, Skipped: 0",
        "running 4 tests",
        "test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured",
        "? example/empty [no test files]\nok example/real 0.002s",
    ],
)
def test_verification_output_with_positive_evidence_is_not_empty(output):
    from nz_coder.intelligence.verification_planner import verification_output_has_no_tests

    assert not verification_output_has_no_tests(output)


def test_python_pth_startup_traceback_invalidates_verification_output():
    from nz_coder.intelligence.verification_planner import verification_output_failed

    warning = """Error processing line 1 of /tmp/broken-nspkg.pth:

  Traceback (most recent call last):
    File \"<frozen site>\", line 207, in addpackage
  AttributeError: 'NoneType' object has no attribute 'loader'

Remainder of file ignored
.                                                                        [100%]
1 passed in 0.01s
"""

    assert verification_output_failed(warning) is True
    assert verification_output_failed(warning + "\nTraceback (most recent call last):\nFAIL: real") is True


def test_verification_command_key_normalizes_runner_wrapper_and_quiet_flag():
    from nz_coder.intelligence.verification_planner import verification_command_key

    direct = "pytest tests/test_app.py::test_case"
    wrapped = "poetry run python -m pytest -q tests/test_app.py::test_case"
    assert verification_command_key(direct) == verification_command_key(wrapped)


def test_deleted_python_file_has_no_required_compile_command(monkeypatch):
    import nz_coder.intelligence.verification_planner as planner

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
    from nz_coder.foundation import config
    from nz_coder.intelligence.verification_planner import plan_verification_commands

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
    from nz_coder.foundation import config
    from nz_coder.intelligence.verification_planner import plan_verification_commands

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
