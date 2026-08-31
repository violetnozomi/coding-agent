"""Tests for language/task policy helpers."""
from __future__ import annotations


def test_is_test_file_handles_common_language_conventions():
    from nz_coder.runtime.agent.task_policy import is_test_file

    assert is_test_file("tests/test_app.py")
    assert is_test_file("src/__tests__/widget.test.tsx")
    assert is_test_file("pkg/server_test.go")
    assert is_test_file("spec/models/user_spec.rb")
    assert is_test_file("testing/python/fixtures.py")
    assert not is_test_file("src/app.ts")


def test_broad_test_detection_is_language_aware():
    from nz_coder.runtime.agent.task_policy import is_broad_test_command

    assert is_broad_test_command("pytest tests/")
    assert not is_broad_test_command("pytest tests/test_app.py::test_ok")
    assert is_broad_test_command("npm test")
    assert is_broad_test_command("go test ./...")
    assert not is_broad_test_command("go test ./pkg -run TestParser")


def test_exact_test_detection_requires_a_test_runner():
    """A source filename alone must not turn shell reads/writes into test runs."""
    from nz_coder.runtime.agent.task_policy import is_exact_test_command

    assert is_exact_test_command("pytest -q tests/test_app.py")
    assert is_exact_test_command("go test ./pkg -run TestParser")
    assert not is_exact_test_command("touch src/generated.py")
    assert not is_exact_test_command("cat src/app.py")


def test_declared_test_scopes_extract_directory_suite_from_user_request():
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    assert declared_test_scopes(
        "实现别名，然后运行 `python -m pytest -q cron_engine/tests`。"
    ) == ("cron_engine/tests",)


def test_test_command_scope_must_not_expand_past_user_request():
    from nz_coder.runtime.agent.task_policy import test_command_within_scopes

    scopes = ("cron_engine/tests",)
    assert test_command_within_scopes(
        "python -m pytest -q cron_engine/tests 2>&1 | tail -4",
        scopes,
    )
    assert test_command_within_scopes(
        "pytest cron_engine/tests/test_cli.py cron_engine/tests/test_parser.py",
        scopes,
    )
    assert not test_command_within_scopes("pytest", scopes)
    assert not test_command_within_scopes("pytest other_package/tests", scopes)


def test_detect_task_mode_distinguishes_discussion_and_creation():
    from nz_coder.runtime.agent.task_policy import (
        detect_task_mode,
        task_forbids_test_changes,
        task_wants_tests,
    )

    assert detect_task_mode("How should we design this API?") == "discuss"
    assert detect_task_mode("Add a REST endpoint for users") == "feature"
    assert detect_task_mode("fix traceback in parser") == "bugfix"
    assert detect_task_mode("add unit tests for parser") == "test"
    assert detect_task_mode("帮我创建一个 FastAPI todo API 项目") == "project_creation"
    assert task_wants_tests("please add unit tests") is True
    assert task_wants_tests(
        "Fix parser.py, do not modify tests, then run pytest -q tests/test_parser.py"
    ) is False
    assert task_wants_tests(
        "修复 parser.py，不修改测试，完成后运行 pytest -q tests/test_parser.py"
    ) is False
    assert task_forbids_test_changes("do not modify the tests") is True
    assert task_forbids_test_changes("不修改测试文件") is True
    assert detect_task_mode(
        "Fix parser.py, do not modify tests, then run pytest -q tests/test_parser.py"
    ) == "bugfix"
