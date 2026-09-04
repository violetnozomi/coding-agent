"""Attack and metadata contracts for handle-anchored transaction tracking."""
from __future__ import annotations

import os
from pathlib import Path
import stat

import pytest


def _manager(workspace: Path):
    from nz_coder.state.transaction import TransactionManager

    manager = TransactionManager()
    manager.begin()
    return manager


@pytest.mark.skipif(os.name == "nt", reason="POSIX race seam")
def test_target_swap_between_track_check_and_backup_fails_closed(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "module.py"
    target.write_text("OPENED-TARGET", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-secret"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    moved = tmp_path / "module-opened.py"
    original_is_file = Path.is_file
    swapped = False

    def swap_after_check(path: Path) -> bool:
        nonlocal swapped
        result = original_is_file(path)
        if path == target and result and not swapped:
            swapped = True
            target.rename(moved)
            target.symlink_to(outside)
        return result

    monkeypatch.setattr(Path, "is_file", swap_after_check)
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("module.py")

    record = next(iter(manager._backups.values()))
    assert record.backup is not None
    assert record.backup.read_text(encoding="utf-8") == "OPENED-TARGET"
    assert "OUTSIDE-SECRET" not in record.backup.read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"


@pytest.mark.skipif(os.name == "nt", reason="POSIX race seam")
def test_parent_swap_during_track_cannot_read_outside_workspace(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "package"
    parent.mkdir()
    target = parent / "module.py"
    target.write_text("OPENED-TARGET", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside-parent"
    outside.mkdir()
    (outside / "module.py").write_text("OUTSIDE-SECRET", encoding="utf-8")
    moved = tmp_path / "package-opened"
    original_capture = TransactionManager._capture_parent_chain

    def swap_after_parent_check(root: Path, checked_parent: Path):
        result = original_capture(root, checked_parent)
        parent.rename(moved)
        parent.symlink_to(outside, target_is_directory=True)
        return result

    monkeypatch.setattr(
        TransactionManager,
        "_capture_parent_chain",
        staticmethod(swap_after_parent_check),
    )
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("package/module.py")

    record = next(iter(manager._backups.values()))
    assert record.backup is not None
    assert record.backup.read_text(encoding="utf-8") == "OPENED-TARGET"
    assert (outside / "module.py").read_text(encoding="utf-8") == "OUTSIDE-SECRET"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor semantics")
def test_track_never_follows_final_symlink_after_validation(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir

    outside = tmp_path.parent / f"{tmp_path.name}-outside-final"
    outside.write_text("OUTSIDE-SECRET", encoding="utf-8")
    (tmp_path / "module.py").symlink_to(outside)
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        with pytest.raises(ValueError, match="regular|unsafe"):
            manager.track("module.py")
    assert not manager._backups
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-SECRET"


def test_transaction_backup_matches_opened_target_identity(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "module.py"
    target.write_text("before", encoding="utf-8")
    if os.name == "nt":
        from nz_coder.foundation.project_control import (
            _windows_close,
            _windows_handle_info,
            _windows_open,
        )

        handle = _windows_open(target, directory=False)
        assert handle is not None
        try:
            _attributes, expected_device, expected_inode, _size = (
                _windows_handle_info(handle, full=True)
            )
        finally:
            _windows_close(handle)
    else:
        expected = target.stat()
        expected_device = int(expected.st_dev)
        expected_inode = int(expected.st_ino)
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("module.py")
    record = next(iter(manager._backups.values()))
    assert (record.target_device, record.target_inode) == (
        expected_device, expected_inode,
    )
    assert record.original_size == len(b"before")
    assert record.backup is not None
    assert record.backup.read_bytes() == b"before"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity contract")
def test_transaction_track_and_mutation_share_anchor(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "module.py"
    target.write_text("before", encoding="utf-8")
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        legacy_calls: list[str] = []
        monkeypatch.setattr(
            manager,
            "track",
            lambda path: legacy_calls.append(str(path)),
        )
        WorkspaceFileAccess(tmp_path).write_text(
            "module.py", "after", transaction=manager,
        )

    assert legacy_calls == []
    record = next(iter(manager._backups.values()))
    assert record.backup is not None
    assert record.backup.read_text(encoding="utf-8") == "before"
    assert target.read_text(encoding="utf-8") == "after"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor identity contract")
def test_repeated_transaction_mutation_rejects_replaced_parent(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir

    parent = tmp_path / "package"
    parent.mkdir()
    (parent / "module.py").write_text("before", encoding="utf-8")
    access = WorkspaceFileAccess(tmp_path)
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        access.write_text("package/module.py", "first", transaction=manager)
        parent.rename(tmp_path / "package-original")
        parent.mkdir()
        (parent / "module.py").write_text("replacement", encoding="utf-8")

        with pytest.raises(ValueError, match="parent identity changed"):
            access.write_text("package/module.py", "escaped", transaction=manager)

    assert (parent / "module.py").read_text(encoding="utf-8") == "replacement"


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata semantics")
def test_transaction_backup_captures_original_mode_and_mtime(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    target.chmod(0o751)
    expected_mtime = 1_700_000_000_123_456_789
    os.utime(target, ns=(expected_mtime, expected_mtime))
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("script.sh")
    record = next(iter(manager._backups.values()))
    assert stat.S_IMODE(record.original_mode) == 0o751
    assert record.original_mtime_ns == expected_mtime


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata semantics")
@pytest.mark.parametrize("mode", [0o755, 0o444])
def test_posix_rollback_restores_mode_and_mtime(tmp_path, mode):
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "script.sh"
    target.write_text("before", encoding="utf-8")
    target.chmod(mode)
    expected_mtime = 1_700_000_000_123_456_789
    os.utime(target, ns=(expected_mtime, expected_mtime))
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("script.sh")
        target.chmod(0o600)
        target.write_text("after", encoding="utf-8")
        manager.rollback()
    restored = target.stat()
    assert target.read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(restored.st_mode) == mode
    assert restored.st_mtime_ns == expected_mtime


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata semantics")
def test_metadata_restore_failure_keeps_backup_for_retry(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "script.sh"
    target.write_text("before", encoding="utf-8")
    target.chmod(0o755)
    original_fchmod = os.fchmod
    failed_once = False

    def fail_once(fd: int, mode: int) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise OSError("metadata restore failed")
        original_fchmod(fd, mode)

    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("script.sh")
        target.write_text("after", encoding="utf-8")
        monkeypatch.setattr(os, "fchmod", fail_once)
        first = manager.rollback()
        assert manager.state == "rollback_partial"
        record = next(iter(manager._backups.values()))
        assert record.backup is not None and record.backup.exists()
        assert "retry is available" in first

        manager.rollback()

    assert manager.state == "rolled_back"
    assert target.read_text(encoding="utf-8") == "before"
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle semantics")
def test_windows_track_reparse_swap_fails_closed(tmp_path, monkeypatch):
    """An opened target cannot be renamed during the backup capture."""
    import nz_coder.foundation.project_control as project_control
    from nz_coder.runtime.process.workdir import scoped_workdir

    target = tmp_path / "module.py"
    target.write_text("OPENED-TARGET", encoding="utf-8")
    moved = tmp_path / "module-opened.py"
    original_open = project_control._windows_open
    attempted = False

    def attempt_swap_after_open(path: Path, **kwargs):
        nonlocal attempted
        handle = original_open(path, **kwargs)
        if path == target and handle is not None and not attempted:
            attempted = True
            with pytest.raises(OSError):
                target.rename(moved)
        return handle

    monkeypatch.setattr(project_control, "_windows_open", attempt_swap_after_open)
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("module.py")
    record = next(iter(manager._backups.values()))
    assert attempted
    assert record.backup is not None
    assert record.backup.read_text(encoding="utf-8") == "OPENED-TARGET"


@pytest.mark.skipif(os.name != "nt", reason="Windows native handle semantics")
def test_windows_track_parent_swap_is_blocked_or_fails_closed(tmp_path, monkeypatch):
    import nz_coder.foundation.project_control as project_control
    from nz_coder.runtime.process.workdir import scoped_workdir

    parent = tmp_path / "package"
    parent.mkdir()
    (parent / "module.py").write_text("OPENED-TARGET", encoding="utf-8")
    moved = tmp_path / "package-opened"
    original_open = project_control._windows_open
    attempted = False

    def attempt_swap_after_open(path: Path, **kwargs):
        nonlocal attempted
        handle = original_open(path, **kwargs)
        if path == parent and handle is not None and not attempted:
            attempted = True
            with pytest.raises(OSError):
                parent.rename(moved)
        return handle

    monkeypatch.setattr(project_control, "_windows_open", attempt_swap_after_open)
    with scoped_workdir(tmp_path):
        manager = _manager(tmp_path)
        manager.track("package/module.py")
    record = next(iter(manager._backups.values()))
    assert attempted
    assert record.backup is not None
    assert record.backup.read_text(encoding="utf-8") == "OPENED-TARGET"
