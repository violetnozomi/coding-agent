"""跨平台验证工作区文件删除、快照回滚与句柄释放。"""
from __future__ import annotations

import os

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.protocol.public_error import PublicInputError
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.state.transaction import TransactionManager


def test_expected_hash_blocks_delete_after_external_change(tmp_path):
    path = tmp_path / "data"
    path.write_bytes(b"AAAA")
    access = WorkspaceFileAccess(tmp_path)
    _, identity = access.read_bytes_with_identity("data")
    original = path.stat()
    path.write_bytes(b"BBBB")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert path.stat().st_mtime_ns == original.st_mtime_ns

    with pytest.raises(PublicInputError, match="File changed after it was read"):
        access.delete("data", expected=identity)
    assert path.read_bytes() == b"BBBB"
    path.rename(tmp_path / "kept")


def test_verified_delete_releases_handles_and_allows_transaction_rollback(tmp_path):
    workspace = tmp_path / "project"
    directory = workspace / "docs"
    directory.mkdir(parents=True)
    file = directory / "notes.md"
    file.write_bytes(b"original content")
    with scoped_workdir(workspace):
        access = WorkspaceFileAccess(workspace)
        _, version = access.read_bytes_with_identity("docs/notes.md")
        transaction = TransactionManager()
        transaction.begin()
        access.delete("docs/notes.md", expected=version, transaction=transaction)
        assert not file.exists()
        transaction.rollback()
        assert not transaction.active
        assert file.read_bytes() == b"original content"

    # 正常删除后，同一进程应能立即复用文件名并移动其父目录。
    _, restored = access.read_bytes_with_identity("docs/notes.md")
    access.delete("docs/notes.md", expected=restored)
    file.write_bytes(b"new content")
    moved = directory.rename(workspace / "moved")
    assert (moved / "notes.md").read_bytes() == b"new content"


def test_file_delete_rejects_directory_and_releases_parent(tmp_path):
    workspace = tmp_path / "project"
    directory = workspace / "docs"
    directory.mkdir(parents=True)
    (directory / "notes.md").write_bytes(b"keep")
    with pytest.raises((ValueError, OSError)):
        WorkspaceFileAccess(workspace).delete("docs")
    moved = workspace.rename(tmp_path / "moved")
    assert (moved / "docs" / "notes.md").read_bytes() == b"keep"


def test_snapshot_deletes_command_file_using_persisted_mode(tmp_path):
    store = WorkspaceSnapshotStore(tmp_path, tmp_path / ".nz-coder" / "snapshots")
    empty = store.track()
    command = tmp_path / "build.cmd"
    command.write_bytes(b"@echo off\r\n")
    occupied = store.track()
    if os.name == "nt":
        # Windows 的路径 stat 会给 .cmd 加执行位，句柄 stat 不使用文件后缀。
        assert command.stat().st_mode & 0o111

    restored = store.transition(occupied, empty)

    assert restored.files == ("build.cmd",)
    assert not command.exists()
