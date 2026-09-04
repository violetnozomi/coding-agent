"""Race and rollback contracts for model-reachable workspace file access."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX swap harness")


def _swap_parent_to_outside(workspace: Path, outside: Path) -> None:
    parent = workspace / "src"
    moved = workspace / "src-original"
    parent.rename(moved)
    parent.symlink_to(outside, target_is_directory=True)


def test_read_parent_swap_after_validation_cannot_escape(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    (workspace / "src" / "secret.txt").write_text("inside", encoding="utf-8")
    (outside / "secret.txt").write_text("OUTSIDE-SENTINEL", encoding="utf-8")
    original = WorkspacePathPolicy.validate_model_read

    def validate_then_swap(policy, path):
        result = original(policy, path)
        _swap_parent_to_outside(workspace, outside)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_read", validate_then_swap)
    with scoped_workdir(workspace):
        result = read_file("src/secret.txt")

    assert "OUTSIDE-SENTINEL" not in result


def test_write_parent_swap_after_validation_cannot_escape(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import write_file

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    sentinel = outside / "target.txt"
    sentinel.write_text("OUTSIDE-SENTINEL", encoding="utf-8")
    original = WorkspacePathPolicy.validate_model_write

    def validate_then_swap(policy, path):
        result = original(policy, path)
        _swap_parent_to_outside(workspace, outside)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_write", validate_then_swap)
    with scoped_workdir(workspace):
        result = write_file("src/target.txt", "changed")

    assert sentinel.read_text(encoding="utf-8") == "OUTSIDE-SENTINEL"
    assert result.startswith("Error:") or (workspace / "src-original" / "target.txt").read_text() == "changed"


def test_delete_parent_swap_after_validation_cannot_escape(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    (workspace / "src" / "target.txt").write_text("inside", encoding="utf-8")
    sentinel = outside / "target.txt"
    sentinel.write_text("OUTSIDE-SENTINEL", encoding="utf-8")
    original = WorkspacePathPolicy.validate_model_write

    def validate_then_swap(policy, path):
        result = original(policy, path)
        _swap_parent_to_outside(workspace, outside)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_write", validate_then_swap)
    access = WorkspaceFileAccess(workspace)
    try:
        access.delete("src/target.txt")
    except (OSError, ValueError):
        pass

    assert sentinel.read_text(encoding="utf-8") == "OUTSIDE-SENTINEL"


def test_direct_apply_patch_failure_rolls_back_all_files(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import apply_patch

    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("before-one", encoding="utf-8")
    second.write_text("before-two", encoding="utf-8")
    original = WorkspaceFileAccess.write_text
    calls = 0

    def fail_second(self, path, content, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected write failure")
        return original(self, path, content, **kwargs)

    monkeypatch.setattr(WorkspaceFileAccess, "write_text", fail_second)
    with scoped_workdir(tmp_path):
        result = apply_patch([
            {"path": "first.txt", "old_text": "before-one", "new_text": "after-one"},
            {"path": "second.txt", "old_text": "before-two", "new_text": "after-two"},
        ])

    assert result.startswith("Error:")
    assert first.read_text(encoding="utf-8") == "before-one"
    assert second.read_text(encoding="utf-8") == "before-two"
