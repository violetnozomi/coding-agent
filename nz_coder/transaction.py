"""Transaction manager: atomic multi-file edits with backup and rollback.

Design:
  txn.begin()        — start a new transaction, clear backup state
  (file writes/edits happen via normal tool dispatch)
  txn.track(path)    — called by write_file/edit_file before mutating; snapshots original
  txn.commit()       — discard backups, transaction succeeded
  txn.rollback()     — restore all files to pre-transaction state

This gives the agent a safety net: if a multi-file edit partially fails,
all changes revert to the state before the transaction began.
"""

import hashlib
import shutil
import tempfile
from pathlib import Path

from nz_coder import config


class TransactionManager:
    def __init__(self):
        self._active = False
        self._backups = {}   # str(abs_path) -> backup_path or None (if file didn't exist)
        self._backup_dir = None

    @property
    def active(self) -> bool:
        return self._active

    def begin(self):
        """Start a new transaction. Any file tracked after this will be backed up."""
        if self._active:
            return  # nested — keep outer transaction
        self._active = True
        self._backups = {}
        self._backup_dir = Path(tempfile.mkdtemp(prefix="nzcoder_txn_"))

    def track(self, file_path: str):
        """Snapshot a file before it gets modified.

        Call this BEFORE writing/editing. If the file doesn't exist yet,
        we record that so rollback can delete it.
        """
        if not self._active:
            return
        abs_path = str((config.WORKDIR / file_path).resolve())
        if abs_path in self._backups:
            return  # already tracked in this transaction

        source = Path(abs_path)
        if source.exists():
            # FIXED: 用绝对路径的 MD5 hash 前缀 + 文件名组合生成备份名，
            # 替代手动 counter 逻辑，彻底避免 src/utils.py 与 tests/utils.py
            # 同名文件在同一备份目录中产生命名冲突。
            path_hash = hashlib.md5(abs_path.encode()).hexdigest()[:12]
            backup = self._backup_dir / f"{path_hash}_{source.name}"
            shutil.copy2(str(source), str(backup))
            self._backups[abs_path] = str(backup)
        else:
            self._backups[abs_path] = None  # file was new

    def commit(self):
        """Transaction succeeded — discard backups."""
        if not self._active:
            return
        self._cleanup_backup_dir()
        self._active = False
        self._backups = {}

    def rollback(self) -> str:
        """Transaction failed — restore all files to pre-transaction state.

        Returns a human-readable report of what was rolled back.
        """
        if not self._active:
            return ""
        report_lines = []
        for abs_path, backup_path in self._backups.items():
            target = Path(abs_path)
            try:
                rel = target.relative_to(config.WORKDIR)
            except ValueError:
                rel = target
            if backup_path is None:
                # File didn't exist before — delete it
                if target.exists():
                    target.unlink()
                    report_lines.append(f"  Deleted (new file reverted): {rel}")
            else:
                # Restore from backup
                shutil.copy2(backup_path, abs_path)
                report_lines.append(f"  Restored: {rel}")

        self._cleanup_backup_dir()
        self._active = False
        self._backups = {}

        if report_lines:
            return "Rolled back changes:\n" + "\n".join(report_lines)
        return ""

    def _cleanup_backup_dir(self):
        if self._backup_dir and self._backup_dir.exists():
            shutil.rmtree(str(self._backup_dir), ignore_errors=True)
        self._backup_dir = None
