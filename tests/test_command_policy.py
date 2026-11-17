"""Focused tests for shell command safety classification."""
from __future__ import annotations

from nz_coder.tool_platform.command_policy import (
    classify_bash,
    external_workspace_path,
    is_known_read_only_command,
)


def test_command_policy_recognizes_read_only_git():
    assert is_known_read_only_command("git status")


def test_command_policy_does_not_mark_rg_copy_as_mutating():
    assert not classify_bash("rg copy")["mutating"]


def test_command_policy_marks_shell_redirection_as_mutating():
    assert classify_bash("echo hi > x.txt")["mutating"]


def test_command_policy_does_not_treat_fd_duplication_as_workspace_mutation():
    """Redirecting stderr to stdout must not fabricate a source edit."""
    for command in (
        "python3 -m pytest tests/test_app.py 2>&1 | tail -20",
        "python3 -m pytest tests/test_app.py 1>&2",
    ):
        assert classify_bash(command)["mutating"] is False


def test_command_policy_ignores_dev_null_redirection():
    assert not classify_bash("python -m pytest >/dev/null")["dangerous"]


def test_command_policy_detects_package_install_and_dangerous_commands():
    assert classify_bash("python3 -m pip install legacy-cgi")["reason"] == "package install"
    assert classify_bash("sudo rm -rf /")["dangerous"]


def test_read_only_shell_cannot_hide_command_substitution():
    for command in (
        "echo $(touch injected.txt)",
        "echo `rm victim.txt`",
        "cat <(python mutate.py)",
    ):
        assert is_known_read_only_command(command) is False
        assert classify_bash(command)["mutating"] is True


def test_read_only_shell_splits_newline_command_segments():
    for command in (
        "cat README.md\nrm generated.txt",
        "cat README.md & rm generated.txt",
    ):
        assert is_known_read_only_command(command) is False
        assert classify_bash(command)["mutating"] is True


def test_private_home_expansion_is_forbidden_without_assuming_its_value(tmp_path):
    from nz_coder.tool_platform.policies.command_policy import assess_shell_command
    assessment = assess_shell_command("cat $HOME/.ssh/id_rsa", tmp_path, command_cwd=tmp_path)
    assert assessment.paths.kind == "forbidden"
    assert assessment.rejection
    windows = assess_shell_command("type %USERPROFILE%\\.ssh\\id_rsa", tmp_path, command_cwd=tmp_path)
    assert windows.paths.kind == "outside" and windows.rejection


def test_external_path_gate_resolves_workspace_symlinks(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "inside-link"
    link.symlink_to(outside)

    assert external_workspace_path("cat inside-link", tmp_path) == "inside-link"
