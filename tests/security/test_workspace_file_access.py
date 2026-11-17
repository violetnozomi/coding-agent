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


def test_expected_content_hash_blocks_same_size_restored_mtime_write(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError
    path = tmp_path / 'data'
    path.write_bytes(b'AAAA')
    access = WorkspaceFileAccess(tmp_path)
    _, identity = access.read_bytes_with_identity('data')
    original = path.stat()
    path.write_bytes(b'BBBB')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    with pytest.raises(PublicInputError):
        access.write_text('data', 'CCCC', expected=identity)
    assert path.read_bytes() == b'BBBB'


def test_write_bytes_preserves_binary_mode_and_published_identity(tmp_path):
    import hashlib
    import stat
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    data = b'\x00\xff\r\n'
    identity = WorkspaceFileAccess(tmp_path).write_bytes('binary', data, mode=0o640)
    path = tmp_path / 'binary'
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert identity.content_hash == hashlib.sha256(data).hexdigest()


def test_parent_fsync_failure_then_external_same_inode_edit_blocks_rollback(tmp_path, monkeypatch):
    import stat
    import nz_coder.foundation.workspace_file_access as module
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager

    path = tmp_path / 'data'
    path.write_bytes(b'old!')
    original_fsync = module.os.fsync
    def fail_parent(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError('injected parent fsync failure')
        original_fsync(fd)
    with scoped_workdir(tmp_path):
        transaction = TransactionManager()
        transaction.begin()
        monkeypatch.setattr(module.os, 'fsync', fail_parent)
        with pytest.raises(OSError, match='injected'):
            WorkspaceFileAccess(tmp_path).write_bytes('data', b'ours', transaction=transaction)
        monkeypatch.setattr(module.os, 'fsync', original_fsync)
        published = path.stat()
        path.write_bytes(b'user')
        os.utime(path, ns=(published.st_atime_ns, published.st_mtime_ns))
        transaction.rollback()
        assert path.read_bytes() == b'user'
        assert transaction.active
        path.write_bytes(b'ours')
        transaction.rollback()
        assert not transaction.active
        assert path.read_bytes() == b'old!'


def test_rollback_retains_owner_when_published_content_identity_is_unknown(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.transaction import TransactionManager
    path = tmp_path / 'data'
    path.write_bytes(b'old!')
    with scoped_workdir(tmp_path):
        transaction = TransactionManager()
        transaction.begin()
        WorkspaceFileAccess(tmp_path).write_bytes('data', b'ours', transaction=transaction)
        receipt = next(iter(transaction._backups.values())).mutation
        saved = receipt.published_version
        receipt.published_version = None
        transaction.rollback()
        assert path.read_bytes() == b'ours'
        assert transaction.active
        receipt.published_version = saved
        transaction.rollback()
        assert path.read_bytes() == b'old!'
        assert not transaction.active


def test_bounded_read_limits_io_when_opened_file_grows(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError
    path = tmp_path / 'data'
    path.write_bytes(b'a')
    real_read = os.read
    consumed = []
    def grow_then_read(fd, size):
        if not consumed:
            path.write_bytes(b'x' * 4096)
        result = real_read(fd, size)
        consumed.append(len(result))
        return result
    monkeypatch.setattr(os, 'read', grow_then_read)
    with pytest.raises(PublicInputError):
        WorkspaceFileAccess(tmp_path).read_bytes_with_identity('data', maximum=8)
    assert sum(consumed) <= 9


def test_expected_mode_uses_same_handle_as_content_identity(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError
    path = tmp_path / 'data'
    path.write_bytes(b'old!')
    path.chmod(0o640)
    access = WorkspaceFileAccess(tmp_path)
    _, identity = access.read_bytes_with_identity('data')
    original = access._identity_at_posix
    def change_mode(parent, name):
        path.chmod(0o600)
        return original(parent, name)
    monkeypatch.setattr(access, '_identity_at_posix', change_mode)
    with pytest.raises(PublicInputError):
        access.write_bytes('data', b'new!', expected=identity, expected_mode=0o640)
    assert path.read_bytes() == b'old!'


def test_expected_hash_blocks_delete_after_external_change(tmp_path):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError
    path = tmp_path / 'data'
    path.write_bytes(b'AAAA')
    access = WorkspaceFileAccess(tmp_path)
    _, identity = access.read_bytes_with_identity('data')
    info = path.stat()
    path.write_bytes(b'BBBB')
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(PublicInputError):
        access.delete('data', expected=identity)
    assert path.read_bytes() == b'BBBB'


def test_model_edit_rejects_same_size_external_write_with_restored_mtime(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import edit_file
    path = tmp_path / 'data'
    path.write_text('AAAA')
    write = WorkspaceFileAccess.write_text
    def external_change(self, path_arg, content, **kwargs):
        info = path.stat()
        path.write_text('BBBB')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        return write(self, path_arg, content, **kwargs)
    monkeypatch.setattr(WorkspaceFileAccess, 'write_text', external_change)
    with scoped_workdir(tmp_path):
        result = edit_file('data', 'AAAA', 'CCCC')
    assert result.startswith('Error:')
    assert path.read_text() == 'BBBB'

def test_windows_growth_after_handle_info_cannot_return_truncated_identity(tmp_path):
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    from nz_coder.foundation import project_control
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError
    target = tmp_path / "config.py"
    target.write_bytes(b"x")
    access = WorkspaceFileAccess(tmp_path)
    def open_fd(_handle, _flags):
        target.write_bytes(b"x" * 150)
        return os.open(target, os.O_RDONLY)
    with (
        patch.object(project_control, "_windows_open", side_effect=[101, 102]),
        patch.object(project_control, "_windows_handle_info", return_value=(0, 1, 2, 1)),
        patch.object(project_control, "_windows_close"),
        patch.dict(sys.modules, {"msvcrt": SimpleNamespace(open_osfhandle=open_fd)}),
        patch.object(os, "O_BINARY", 0, create=True),
        pytest.raises(PublicInputError),
    ):
        access._read_windows_with_identity_and_mode(Path("config.py"), maximum=100)


def test_windows_source_read_does_not_prefetch_beyond_the_byte_limit(tmp_path):
    import io
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    from nz_coder.foundation import project_control
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.protocol.public_error import PublicInputError
    target = tmp_path / "config.py"
    target.write_bytes(b"x" * 90)
    consumed = []
    class CountingFile(io.FileIO):
        def readinto(self, buffer):
            target.write_bytes(b"x" * 150)
            count = super().readinto(buffer)
            consumed.append(count)
            return count
        def read(self, size=-1):
            target.write_bytes(b"x" * 150)
            data = super().read(size)
            consumed.append(len(data))
            return data
    def fdopen(descriptor, mode, *, closefd=True, buffering=-1):
        raw = CountingFile(descriptor, mode, closefd=closefd)
        return raw if buffering == 0 else io.BufferedReader(raw)
    with (
        patch.object(project_control, "_windows_open", side_effect=[101, 102]),
        patch.object(project_control, "_windows_handle_info", return_value=(0, 1, 2, 90)),
        patch.object(project_control, "_windows_close"),
        patch.dict(sys.modules, {"msvcrt": SimpleNamespace(open_osfhandle=lambda *_: os.open(target, os.O_RDONLY))}),
        patch.object(os, "O_BINARY", 0, create=True),
        patch.object(os, "fdopen", side_effect=fdopen),
        pytest.raises(PublicInputError),
    ):
        WorkspaceFileAccess(tmp_path)._read_windows_with_identity_and_mode(Path("config.py"), maximum=100)
    assert sum(consumed) <= 100


@pytest.mark.parametrize("root_target", ["outside", "missing"])
def test_subdirectory_listing_ignores_root_same_name_symlink(tmp_path, root_target):
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import list_directory

    workspace = tmp_path / "workspace"
    (workspace / "sub" / "nested").mkdir(parents=True)
    (workspace / "sub" / "safe.py").write_text("VALUE = 1\n")
    (workspace / "sub" / "nested" / "safe.py").write_text("VALUE = 2\n")
    if root_target == "outside":
        (tmp_path / root_target).write_text("outside")
    (workspace / "safe.py").symlink_to(tmp_path / root_target)
    entries = WorkspaceFileAccess(workspace).walk_directory("sub", max_depth=2)
    assert {item.path for item in entries} == {"safe.py", "nested", "nested/safe.py"}
    with scoped_workdir(workspace):
        assert "safe.py" in list_directory("sub")
