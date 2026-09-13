"""Race and rollback contracts for model-reachable workspace file access."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _assert_directory_scan_stops_at_limit(tmp_path, monkeypatch):
    import nz_coder.foundation.workspace_file_access as workspace_access
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError

    for index in range(20):
        (tmp_path / f"file-{index:02}.txt").write_text("x", encoding="utf-8")
    real_scandir = workspace_access.os.scandir
    consumed = 0

    class CountingScandir:
        def __init__(self, target):
            self._iterator = real_scandir(target)

        def __enter__(self):
            self._iterator.__enter__()
            return self

        def __exit__(self, *args):
            return self._iterator.__exit__(*args)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            item = next(self._iterator)
            consumed += 1
            return item

    monkeypatch.setattr(workspace_access.os, "scandir", CountingScandir)
    with pytest.raises(PublicInputError, match="entry limit"):
        WorkspaceFileAccess(tmp_path).walk_directory(
            ".", max_depth=1, maximum_entries=2,
        )
    assert consumed == 3


def test_directory_limit_stops_enumeration_early(tmp_path, monkeypatch):
    _assert_directory_scan_stops_at_limit(tmp_path, monkeypatch)


def test_directory_listing_does_not_materialize_unbounded_entries(
    tmp_path, monkeypatch,
):
    _assert_directory_scan_stops_at_limit(tmp_path, monkeypatch)


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


def test_read_directory_parent_swap_cannot_enumerate_outside(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    (workspace / "src" / "inside.txt").write_text("inside", encoding="utf-8")
    (outside / "OUTSIDE-FILENAME-SENTINEL").write_text("outside", encoding="utf-8")
    original = WorkspacePathPolicy.validate_model_read
    swapped = False

    def validate_then_swap(policy, path):
        nonlocal swapped
        result = original(policy, path)
        if not swapped:
            swapped = True
            _swap_parent_to_outside(workspace, outside)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_read", validate_then_swap)
    with scoped_workdir(workspace):
        result = read_file("src")

    assert str(result).startswith("Error:")
    assert "OUTSIDE-FILENAME-SENTINEL" not in str(result)


def test_list_directory_parent_swap_cannot_enumerate_outside(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import list_directory

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    (workspace / "src" / "inside.txt").write_text("inside", encoding="utf-8")
    (outside / "OUTSIDE-FILENAME-SENTINEL").write_text("outside", encoding="utf-8")
    original = WorkspacePathPolicy.validate_model_list
    swapped = False

    def validate_then_swap(policy, path):
        nonlocal swapped
        result = original(policy, path)
        if not swapped:
            swapped = True
            _swap_parent_to_outside(workspace, outside)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_list", validate_then_swap)
    with scoped_workdir(workspace):
        result = list_directory("src", depth=2)

    assert str(result).startswith("Error:")
    assert "OUTSIDE-FILENAME-SENTINEL" not in str(result)


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


def test_document_parent_swap_after_validation_cannot_escape(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "docs").mkdir(parents=True)
    outside.mkdir()
    (workspace / "docs" / "input.pdf").write_bytes(b"%PDF-safe")
    (outside / "input.pdf").write_bytes(b"%PDF-OUTSIDE-SENTINEL")
    original = WorkspacePathPolicy.validate_model_read
    swapped = False

    def validate_then_swap(policy, path):
        nonlocal swapped
        result = original(policy, path)
        if not swapped:
            swapped = True
            (workspace / "docs").rename(workspace / "docs-original")
            (workspace / "docs").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_read", validate_then_swap)
    with scoped_workdir(workspace):
        result = read_file("docs/input.pdf")

    assert "OUTSIDE-SENTINEL" not in str(result)


def test_apply_patch_parent_swap_cannot_escape(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import apply_patch

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    (workspace / "src").mkdir(parents=True)
    outside.mkdir()
    (workspace / "src" / "target.txt").write_text("inside", encoding="utf-8")
    sentinel = outside / "target.txt"
    sentinel.write_text("OUTSIDE-SENTINEL", encoding="utf-8")
    original = WorkspacePathPolicy.validate_model_write
    swapped = False

    def validate_then_swap(policy, path):
        nonlocal swapped
        result = original(policy, path)
        if not swapped:
            swapped = True
            (workspace / "src").rename(workspace / "src-original")
            (workspace / "src").symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(WorkspacePathPolicy, "validate_model_write", validate_then_swap)
    with scoped_workdir(workspace):
        result = apply_patch([{
            "path": "src/target.txt",
            "old_text": "inside",
            "new_text": "changed",
        }])

    assert str(result).startswith("Error:")
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
