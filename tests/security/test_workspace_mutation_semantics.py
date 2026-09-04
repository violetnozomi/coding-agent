"""Regression tests for optimistic and atomic workspace mutations."""
from __future__ import annotations

import os
import stat

from nz_coder.runtime.process.workdir import scoped_workdir


def test_successful_edit_preserves_executable_mode(tmp_path):
    from nz_coder.tools.files import edit_file

    target = tmp_path / "run.sh"
    target.write_text("echo old\n", encoding="utf-8")
    target.chmod(0o755)
    with scoped_workdir(tmp_path):
        result = edit_file("run.sh", "old", "new")

    assert not result.startswith("Error:")
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_edit_rejects_file_changed_after_read(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.tools.files import edit_file

    target = tmp_path / "value.txt"
    target.write_text("old\n", encoding="utf-8")
    original = WorkspaceFileAccess.write_text

    def race(self, path, content, **kwargs):
        target.write_text("concurrent\n", encoding="utf-8")
        return original(self, path, content, **kwargs)

    monkeypatch.setattr(WorkspaceFileAccess, "write_text", race)
    with scoped_workdir(tmp_path):
        result = edit_file("value.txt", "old", "agent")

    assert result == "Error: File changed after it was read; re-read before editing."
    assert target.read_text(encoding="utf-8") == "concurrent\n"


def test_batch_overwrite_false_is_atomic_under_create_race(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.tools.files import write_files_batch

    target = tmp_path / "new.txt"
    original = WorkspaceFileAccess.write_text

    def race(self, path, content, **kwargs):
        target.write_text("concurrent\n", encoding="utf-8")
        return original(self, path, content, **kwargs)

    monkeypatch.setattr(WorkspaceFileAccess, "write_text", race)
    with scoped_workdir(tmp_path):
        result = write_files_batch(
            [{"path": "new.txt", "content": "agent\n"}], overwrite=False,
        )

    assert result.startswith("Error:")
    assert target.read_text(encoding="utf-8") == "concurrent\n"


def test_apply_patch_rejects_changed_member_and_rolls_back_all(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.tools.files import apply_patch

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("one\n", encoding="utf-8")
    second.write_text("two\n", encoding="utf-8")
    original = WorkspaceFileAccess.write_text
    calls = 0

    def race(self, path, content, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            second.write_text("concurrent\n", encoding="utf-8")
        return original(self, path, content, **kwargs)

    monkeypatch.setattr(WorkspaceFileAccess, "write_text", race)
    with scoped_workdir(tmp_path):
        result = apply_patch([
            {"path": "first.txt", "old_text": "one", "new_text": "agent-one"},
            {"path": "second.txt", "old_text": "two", "new_text": "agent-two"},
        ])

    assert result == "Error: File changed after it was read; re-read before editing."
    assert first.read_text(encoding="utf-8") == "one\n"
    assert second.read_text(encoding="utf-8") == "concurrent\n"


def test_new_file_uses_secure_default_mode(tmp_path):
    from nz_coder.tools.files import write_file

    previous = os.umask(0)
    try:
        with scoped_workdir(tmp_path):
            result = write_file("new.txt", "value\n")
    finally:
        os.umask(previous)

    assert not result.startswith("Error:")
    assert stat.S_IMODE((tmp_path / "new.txt").stat().st_mode) == 0o600
