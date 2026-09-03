"""Recoverable multi-file transaction contracts."""
from __future__ import annotations

from pathlib import Path
import os
import shutil

import pytest


def test_partial_rollback_continues_and_can_retry(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    first = tmp_path / "one.txt"
    second = tmp_path / "two.txt"
    third = tmp_path / "three.txt"
    for path in (first, second, third):
        path.write_text("before", encoding="utf-8")
    transaction = TransactionManager()
    with scoped_workdir(tmp_path):
        transaction.begin()
        for path in (first, second, third):
            transaction.track(path.name)
            path.write_text("after", encoding="utf-8")
        original = transaction._restore_backup
        failed_once = False

        def fail_second(record, backup: Path, parent):
            nonlocal failed_once
            if record.target == second and not failed_once:
                failed_once = True
                raise OSError("simulated restore failure")
            return original(record, backup, parent)

        monkeypatch.setattr(transaction, "_restore_backup", fail_second)
        report = transaction.rollback()

        assert first.read_text(encoding="utf-8") == "before"
        assert second.read_text(encoding="utf-8") == "after"
        assert third.read_text(encoding="utf-8") == "before"
        assert transaction.state == "rollback_partial"
        assert transaction.active is True
        assert "simulated restore failure" not in report
        assert "1 rollback operation(s) failed" in report

        retry = transaction.rollback()

    assert second.read_text(encoding="utf-8") == "before"
    assert transaction.state == "rolled_back"
    assert transaction.active is False
    assert "Restored: two.txt" in retry


def test_track_rejects_external_and_symlink_escape(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    outside = tmp_path.parent / "outside-transaction.txt"
    outside.write_text("outside", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    transaction = TransactionManager()
    with scoped_workdir(tmp_path):
        transaction.begin()
        with pytest.raises(ValueError, match="workspace"):
            transaction.track(str(outside))
        with pytest.raises(ValueError, match="workspace"):
            transaction.track("link.txt")

    assert outside.read_text(encoding="utf-8") == "outside"


def test_rollback_handles_new_file_and_is_idempotent(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    transaction = TransactionManager()
    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("new-target")
        target = tmp_path / "new-target"
        target.write_text("generated", encoding="utf-8")
        report = transaction.rollback()
        second = transaction.rollback()

    assert target.exists() is False
    assert "Deleted" in report
    assert second == ""
    assert transaction.state == "rolled_back"


def test_original_exception_remains_primary_when_rollback_is_partial(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    target = tmp_path / "app.py"
    target.write_text("before", encoding="utf-8")
    transaction = TransactionManager()
    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("app.py")
        target.write_text("after", encoding="utf-8")
        monkeypatch.setattr(
            transaction,
            "_restore_backup",
            lambda *_args: (_ for _ in ()).throw(OSError("rollback-secret")),
        )
        try:
            raise RuntimeError("business failure")
        except RuntimeError:
            transaction.rollback()
            with pytest.raises(RuntimeError, match="business failure"):
                raise


def test_parent_symlink_swap_after_track_is_rejected_and_retryable(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "package"
    parent.mkdir()
    target = parent / "module.py"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    outside_target = outside / "module.py"
    outside_target.write_text("outside", encoding="utf-8")
    original_parent = tmp_path / "package-original"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("package/module.py")
        target.write_text("after", encoding="utf-8")
        parent.rename(original_parent)
        try:
            parent.symlink_to(outside, target_is_directory=True)
        except OSError:
            original_parent.rename(parent)
            pytest.skip("directory symlinks are unavailable")

        report = transaction.rollback()

        assert transaction.state == "rollback_partial"
        assert "retry is available" in report
        assert outside_target.read_text(encoding="utf-8") == "outside"
        assert next(iter(transaction._backups.values())).backup.exists()

        parent.unlink()
        original_parent.rename(parent)
        retry = transaction.rollback()

    assert target.read_text(encoding="utf-8") == "before"
    assert transaction.state == "rolled_back"
    assert "Restored: package/module.py" in retry


def test_rollback_detects_parent_identity_change(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "package"
    parent.mkdir()
    target = parent / "module.py"
    target.write_text("before", encoding="utf-8")
    moved = tmp_path / "package-original"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("package/module.py")
        target.write_text("after", encoding="utf-8")
        parent.rename(moved)
        parent.mkdir()
        (parent / "module.py").write_text("replacement", encoding="utf-8")

        transaction.rollback()

    assert transaction.state == "rollback_partial"
    assert (parent / "module.py").read_text(encoding="utf-8") == "replacement"
    assert next(iter(transaction._backups.values())).backup.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_parent_junction_swap_after_track_is_rejected(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "package"
    parent.mkdir()
    target = parent / "module.py"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-junction-outside"
    outside.mkdir()
    outside_target = outside / "module.py"
    outside_target.write_text("outside", encoding="utf-8")
    original_parent = tmp_path / "package-original"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("package/module.py")
        target.write_text("after", encoding="utf-8")
        parent.rename(original_parent)
        os.system(f'mklink /J "{parent}" "{outside}" >NUL')
        if not parent.exists():
            original_parent.rename(parent)
            pytest.skip("junction creation is unavailable")

        transaction.rollback()

    assert transaction.state == "rollback_partial"
    assert outside_target.read_text(encoding="utf-8") == "outside"
    shutil.rmtree(parent, ignore_errors=True)
    original_parent.rename(parent)


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor semantics")
def test_parent_swap_after_validation_before_restore_cannot_escape(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "package"
    parent.mkdir()
    target = parent / "module.py"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-restore-outside"
    outside.mkdir()
    sentinel = outside / "module.py"
    sentinel.write_text("sentinel", encoding="utf-8")
    moved = tmp_path / "package-opened"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("package/module.py")
        target.write_text("after", encoding="utf-8")
        original = transaction._restore_backup_posix

        def swap_after_validation(record, backup, *args):
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            return original(record, backup, *args)

        monkeypatch.setattr(transaction, "_restore_backup_posix", swap_after_validation)
        transaction.rollback()

    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert (moved / "module.py").read_text(encoding="utf-8") == "before"


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor semantics")
def test_parent_swap_after_validation_before_delete_cannot_escape(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "generated"
    parent.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-delete-outside"
    outside.mkdir()
    sentinel = outside / "new.py"
    sentinel.write_text("sentinel", encoding="utf-8")
    moved = tmp_path / "generated-opened"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("generated/new.py")
        target = parent / "new.py"
        target.write_text("generated", encoding="utf-8")
        original = transaction._validate_recovery_target

        def swap_after_validation(record):
            guard = original(record)
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
            return guard

        monkeypatch.setattr(transaction, "_validate_recovery_target", swap_after_validation)
        transaction.rollback()

    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert not (moved / "new.py").exists()


def test_unsafe_new_directory_delete_fails_closed_and_preserves_retry(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    transaction = TransactionManager()
    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("generated")
        target = tmp_path / "generated"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep", encoding="utf-8")

        report = transaction.rollback()

    assert transaction.state == "rollback_partial"
    assert target.joinpath("sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert "retry is available" in report
    assert transaction._backup_dir is not None and transaction._backup_dir.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor semantics")
def test_intermediate_parent_swap_is_handle_anchored(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    intermediate = tmp_path / "src"
    parent = intermediate / "pkg"
    parent.mkdir(parents=True)
    target = parent / "app.py"
    target.write_text("before", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-intermediate-outside"
    (outside / "pkg").mkdir(parents=True)
    sentinel = outside / "pkg" / "app.py"
    sentinel.write_text("sentinel", encoding="utf-8")
    moved = tmp_path / "src-opened"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("src/pkg/app.py")
        target.write_text("after", encoding="utf-8")
        original = transaction._restore_backup_posix

        def swap_after_open(record, backup, parent_guard):
            intermediate.rename(moved)
            intermediate.symlink_to(outside, target_is_directory=True)
            return original(record, backup, parent_guard)

        monkeypatch.setattr(transaction, "_restore_backup_posix", swap_after_open)
        transaction.rollback()

    assert sentinel.read_text(encoding="utf-8") == "sentinel"
    assert (moved / "pkg" / "app.py").read_text(encoding="utf-8") == "before"


def test_nonexistent_parent_created_after_track_cannot_escape(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    outside = tmp_path.parent / f"{tmp_path.name}-missing-parent-outside"
    outside.mkdir()
    sentinel = outside / "new.py"
    sentinel.write_text("sentinel", encoding="utf-8")
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("missing/new.py")
        (tmp_path / "missing").symlink_to(outside, target_is_directory=True)
        report = transaction.rollback()

    assert transaction.state == "rollback_partial"
    assert "retry is available" in report
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_windows_parent_swap_during_recovery_is_blocked_or_fails_closed(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    parent = tmp_path / "package"
    parent.mkdir()
    target = parent / "module.py"
    target.write_text("before", encoding="utf-8")
    moved = tmp_path / "package-moved"
    transaction = TransactionManager()

    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track("package/module.py")
        target.write_text("after", encoding="utf-8")
        original = transaction._restore_backup

        def attempt_swap(record, backup, parent_guard):
            try:
                parent.rename(moved)
            except OSError:
                pass
            return original(record, backup, parent_guard)

        monkeypatch.setattr(transaction, "_restore_backup", attempt_swap)
        transaction.rollback()

    assert not moved.exists()
    assert target.read_text(encoding="utf-8") == "before"
