"""Tests for language/task policy helpers."""
from __future__ import annotations


def test_is_test_file_handles_common_language_conventions():
    from nz_coder.task_policy import is_test_file

    assert is_test_file("tests/test_app.py")
    assert is_test_file("src/__tests__/widget.test.tsx")
    assert is_test_file("pkg/server_test.go")
    assert is_test_file("spec/models/user_spec.rb")
    assert not is_test_file("src/app.ts")


def test_broad_test_detection_is_language_aware():
    from nz_coder.task_policy import is_broad_test_command

    assert is_broad_test_command("pytest tests/")
    assert not is_broad_test_command("pytest tests/test_app.py::test_ok")
    assert is_broad_test_command("npm test")
    assert is_broad_test_command("go test ./...")
    assert not is_broad_test_command("go test ./pkg -run TestParser")


def test_detect_task_mode_distinguishes_discussion_and_creation():
    from nz_coder.task_policy import detect_task_mode, task_wants_tests

    assert detect_task_mode("How should we design this API?") == "discuss"
    assert detect_task_mode("Add a REST endpoint for users") == "feature"
    assert detect_task_mode("fix traceback in parser") == "bugfix"
    assert detect_task_mode("add unit tests for parser") == "test"
    assert task_wants_tests("please add unit tests") is True
