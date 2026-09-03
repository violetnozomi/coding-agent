"""Recoverable, workspace-confined multi-file edit transactions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import uuid

from nz_coder.state.workdir import current_workdir


@dataclass(frozen=True)
class _PathIdentity:
    """Identity of one directory in the recovery path."""

    path: Path
    device: int
    inode: int


@dataclass(frozen=True)
class _Backup:
    """Immutable recovery information for one canonical workspace target."""

    target: Path
    relative: str
    backup: Path | None
    parent_chain: tuple[_PathIdentity, ...]


class TransactionManager:
    """Track edits and retain failed rollback entries for a later retry."""

    def __init__(self):
        self._active = False
        self._state = "inactive"
        self._backups: dict[str, _Backup] = {}
        self._backup_dir: Path | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def state(self) -> str:
        return self._state

    def begin(self) -> None:
        """Start a transaction; nested callers share the active transaction."""
        if self._active:
            return
        self._active = True
        self._state = "active"
        self._backups = {}
        self._backup_dir = Path(tempfile.mkdtemp(prefix="nzcoder_txn_"))

    def track(self, file_path: str | os.PathLike[str]) -> None:
        """Snapshot one canonical target after independently checking confinement."""
        if not self._active:
            return
        root = current_workdir().resolve(strict=True)
        raw = Path(file_path)
        lexical = raw if raw.is_absolute() else root / raw
        target = lexical.resolve(strict=False)
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError("Transaction target escapes workspace") from exc
        ancestor = lexical
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        try:
            ancestor.resolve(strict=True).relative_to(root)
        except (OSError, ValueError) as exc:
            raise ValueError("Transaction target escapes workspace") from exc
        key = str(target)
        if key in self._backups:
            return
        if self._backup_dir is None:
            raise RuntimeError("Transaction backup directory is unavailable")
        parent_chain = self._capture_parent_chain(root, lexical.parent)
        if target.exists():
            if target.is_symlink() or not target.is_file():
                raise ValueError("Transaction can only track regular workspace files")
            path_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
            backup = self._backup_dir / f"{path_hash}_{target.name}"
            shutil.copy2(target, backup)
            self._backups[key] = _Backup(target, relative, backup, parent_chain)
        else:
            self._backups[key] = _Backup(target, relative, None, parent_chain)

    def commit(self) -> None:
        """Commit and discard every recovery snapshot."""
        if not self._active:
            return
        self._cleanup_backup_dir()
        self._active = False
        self._state = "committed"
        self._backups = {}

    def rollback(self) -> str:
        """Attempt every recovery operation and retain only failures for retry."""
        if not self._active:
            return ""
        restored: list[str] = []
        deleted: list[str] = []
        failed: dict[str, _Backup] = {}
        for key, record in tuple(self._backups.items()):
            try:
                self._validate_recovery_target(record)
                if record.backup is None:
                    self._delete_new_target(record.target)
                    deleted.append(record.relative)
                else:
                    self._restore_backup(record.target, record.backup)
                    restored.append(record.relative)
            except (OSError, ValueError, RuntimeError):
                failed[key] = record
        self._backups = failed
        lines = [f"  Restored: {path}" for path in restored]
        lines.extend(f"  Deleted (new target reverted): {path}" for path in deleted)
        if failed:
            self._state = "rollback_partial"
            self._active = True
            lines.append(
                f"  Warning: {len(failed)} rollback operation(s) failed; retry is available."
            )
        else:
            self._cleanup_backup_dir()
            self._state = "rolled_back"
            self._active = False
        return "Rolled back changes:\n" + "\n".join(lines) if lines else ""

    def _validate_recovery_target(self, record: _Backup) -> None:
        root = current_workdir().resolve(strict=True)
        try:
            record.target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Transaction recovery target escapes workspace") from exc
        for identity in record.parent_chain:
            try:
                info = identity.path.lstat()
            except OSError as exc:
                raise ValueError("Transaction recovery parent identity changed") from exc
            if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Transaction recovery parent is unsafe")
            if (int(info.st_dev), int(info.st_ino)) != (
                identity.device,
                identity.inode,
            ):
                raise ValueError("Transaction recovery parent identity changed")
        last = record.parent_chain[-1].path
        relative_parent = record.target.parent.relative_to(last)
        cursor = last
        for part in relative_parent.parts:
            cursor = cursor / part
            try:
                info = cursor.lstat()
            except OSError as exc:
                raise ValueError("Transaction recovery parent is unavailable") from exc
            if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Transaction recovery parent is unsafe")

    @classmethod
    def _capture_parent_chain(
        cls,
        root: Path,
        parent: Path,
    ) -> tuple[_PathIdentity, ...]:
        chain: list[_PathIdentity] = []
        cursor = root
        candidates = [root]
        try:
            relative = parent.relative_to(root)
        except ValueError as exc:
            raise ValueError("Transaction target escapes workspace") from exc
        for part in relative.parts:
            cursor = cursor / part
            candidates.append(cursor)
        for candidate in candidates:
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                break
            if cls._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Transaction target parent is unsafe")
            chain.append(_PathIdentity(candidate, int(info.st_dev), int(info.st_ino)))
        if not chain:
            raise ValueError("Transaction workspace identity is unavailable")
        return tuple(chain)

    @staticmethod
    def _is_link_or_reparse(info: os.stat_result) -> bool:
        attributes = int(getattr(info, "st_file_attributes", 0))
        reparse = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)

    def _restore_backup(self, target: Path, backup: Path) -> None:
        """Restore through a same-directory fsynced temporary and atomic replace."""
        record = self._backups.get(str(target))
        if record is None:
            raise RuntimeError("Transaction recovery metadata is unavailable")
        self._validate_recovery_target(record)
        if os.name != "nt":
            self._restore_backup_posix(record, backup)
            return
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".rollback",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output, backup.open("rb") as source:
                descriptor = -1
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            shutil.copystat(backup, temporary, follow_symlinks=False)
            os.replace(temporary, target)
            self._fsync_directory(target.parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def _restore_backup_posix(self, record: _Backup, backup: Path) -> None:
        """Restore relative to a verified directory descriptor on POSIX."""
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        parent_fd = os.open(record.target.parent, flags)
        temporary_name = f".{record.target.name}.{uuid.uuid4().hex}.rollback"
        descriptor = -1
        try:
            info = os.fstat(parent_fd)
            if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Transaction recovery parent is unsafe")
            if record.parent_chain[-1].path == record.target.parent:
                expected = record.parent_chain[-1]
                if (int(info.st_dev), int(info.st_ino)) != (expected.device, expected.inode):
                    raise ValueError("Transaction recovery parent identity changed")
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            with os.fdopen(descriptor, "wb") as output, backup.open("rb") as source:
                descriptor = -1
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                temporary_name,
                record.target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.fsync(parent_fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)

    def _delete_new_target(self, target: Path) -> None:
        try:
            target.lstat()
        except FileNotFoundError:
            return
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        self._fsync_directory(target.parent)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _cleanup_backup_dir(self) -> None:
        if self._backup_dir and self._backup_dir.exists():
            shutil.rmtree(self._backup_dir, ignore_errors=True)
        self._backup_dir = None


__all__ = ["TransactionManager"]
