"""内部事务备份不能跟随工作区 TMPDIR 进入用户变更范围。"""
from __future__ import annotations

import tempfile

import pytest

from nz_coder.foundation.user_paths import user_storage_layout
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.state.transaction import TransactionManager


def test_transaction_backup_stays_private_when_tmpdir_is_in_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    local_tmp = workspace / ".tmp"
    local_tmp.mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", str(local_tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(local_tmp))
    store = WorkspaceSnapshotStore(workspace, tmp_path / "snapshots")
    before = store.verification_snapshot()
    transaction = TransactionManager()
    with scoped_workdir(workspace):
        try:
            transaction.begin()
            backup = transaction._backup_dir
            assert backup is not None
            assert backup.is_relative_to(user_storage_layout(workspace).workspace_state)
            assert not backup.is_relative_to(workspace)
            assert store.verification_snapshot() == before
        finally:
            transaction.commit()
    assert not backup.exists()
    assert store.verification_snapshot() == before


def test_backup_allocation_failure_does_not_activate_transaction(tmp_path, monkeypatch):
    def unavailable(**_kwargs):
        raise OSError("backup allocation failed")

    monkeypatch.setattr(tempfile, "mkdtemp", unavailable)
    transaction = TransactionManager()
    with scoped_workdir(tmp_path), pytest.raises(OSError, match="backup allocation failed"):
        transaction.begin()
    assert transaction.active is False
    assert transaction.state == "inactive"
    assert transaction._backup_dir is None
