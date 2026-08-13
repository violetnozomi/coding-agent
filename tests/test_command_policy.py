"""Focused tests for shell command safety classification."""
from __future__ import annotations

from nz_coder.command_policy import classify_bash, is_known_read_only_command


def test_command_policy_recognizes_read_only_git():
    assert is_known_read_only_command("git status")


def test_command_policy_does_not_mark_rg_copy_as_mutating():
    assert not classify_bash("rg copy")["mutating"]


def test_command_policy_marks_shell_redirection_as_mutating():
    assert classify_bash("echo hi > x.txt")["mutating"]


def test_command_policy_ignores_dev_null_redirection():
    assert not classify_bash("python -m pytest >/dev/null")["dangerous"]


def test_command_policy_detects_package_install_and_dangerous_commands():
    assert classify_bash("python3 -m pip install legacy-cgi")["reason"] == "package install"
    assert classify_bash("sudo rm -rf /")["dangerous"]
