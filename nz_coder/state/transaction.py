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


@dataclass
class _RecoveryParent:
    """Stable parent authority held across validation and recovery I/O."""

    fd: int | None = None
    windows_handles: tuple[int, ...] = ()

    @property
    def windows_parent_handle(self) -> int | None:
        """Return the final opened directory handle on Windows."""
        return self.windows_handles[-1] if self.windows_handles else None

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.windows_handles:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            for handle in reversed(self.windows_handles):
                close_handle(wintypes.HANDLE(handle))
            self.windows_handles = ()


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
            parent: _RecoveryParent | None = None
            try:
                parent = self._validate_recovery_target(record)
                if record.backup is None:
                    self._delete_new_target(record, parent)
                    deleted.append(record.relative)
                else:
                    self._restore_backup(record, record.backup, parent)
                    restored.append(record.relative)
            except (OSError, ValueError, RuntimeError):
                failed[key] = record
            finally:
                if parent is not None:
                    parent.close()
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

    def _validate_recovery_target(self, record: _Backup) -> _RecoveryParent:
        root = current_workdir().resolve(strict=True)
        try:
            record.target.relative_to(root)
        except ValueError as exc:
            raise ValueError("Transaction recovery target escapes workspace") from exc
        if os.name != "nt":
            return self._open_recovery_parent_posix(root, record)
        for identity in record.parent_chain:
            try:
                info = identity.path.lstat()
            except OSError as exc:
                raise ValueError("Transaction recovery parent identity changed") from exc
            if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ValueError("Transaction recovery parent is unsafe")
        return self._open_recovery_parent_windows(record)

    def _open_recovery_parent_posix(
        self,
        root: Path,
        record: _Backup,
    ) -> _RecoveryParent:
        """Traverse from the verified root and retain the final parent fd."""
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(root, flags)
        try:
            self._verify_opened_directory(descriptor, record.parent_chain[0])
            relative = record.target.parent.relative_to(root)
            for index, part in enumerate(relative.parts, start=1):
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
                if index < len(record.parent_chain):
                    self._verify_opened_directory(
                        descriptor, record.parent_chain[index]
                    )
                else:
                    info = os.fstat(descriptor)
                    if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                        raise ValueError("Transaction recovery parent is unsafe")
            return _RecoveryParent(fd=descriptor)
        except Exception:
            os.close(descriptor)
            raise

    def _open_recovery_parent_windows(self, record: _Backup) -> _RecoveryParent:
        """Lock each existing parent against rename/delete for the I/O window."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        handles: list[int] = []
        share_read_write = 0x00000001 | 0x00000002
        flags = 0x02000000 | 0x00200000
        invalid = ctypes.c_void_p(-1).value
        paths = [identity.path for identity in record.parent_chain]
        last = record.parent_chain[-1].path
        cursor = last
        for part in record.target.parent.relative_to(last).parts:
            cursor = cursor / part
            paths.append(cursor)
        try:
            for index, path in enumerate(paths):
                handle = create_file(
                    str(path), 0x00000080, share_read_write, None, 3, flags, None
                )
                value = (
                    int(getattr(handle, "value", handle))
                    if handle is not None else invalid
                )
                if value == invalid:
                    raise OSError(ctypes.get_last_error(), "cannot lock recovery parent")
                handles.append(value)
                info = path.lstat()
                if self._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("Transaction recovery parent is unsafe")
                if index < len(record.parent_chain):
                    expected = record.parent_chain[index]
                    if (int(info.st_dev), int(info.st_ino)) != (
                        expected.device, expected.inode,
                    ):
                        raise ValueError("Transaction recovery parent identity changed")
            return _RecoveryParent(windows_handles=tuple(handles))
        except Exception:
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            for handle in reversed(handles):
                close_handle(wintypes.HANDLE(handle))
            raise

    @classmethod
    def _verify_opened_directory(cls, descriptor: int, expected: _PathIdentity) -> None:
        info = os.fstat(descriptor)
        if cls._is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("Transaction recovery parent is unsafe")
        if (int(info.st_dev), int(info.st_ino)) != (expected.device, expected.inode):
            raise ValueError("Transaction recovery parent identity changed")

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

    def _restore_backup(
        self,
        record: _Backup,
        backup: Path,
        parent: _RecoveryParent,
    ) -> None:
        """Restore through a same-directory fsynced temporary and atomic replace."""
        if os.name != "nt":
            self._restore_backup_posix(record, backup, parent)
            return
        self._restore_backup_windows(record, backup, parent)

    @staticmethod
    def _restore_backup_windows(
        record: _Backup,
        backup: Path,
        parent: _RecoveryParent,
    ) -> None:
        """Atomically move the backup relative to the verified parent handle."""
        import ctypes
        from ctypes import wintypes

        parent_handle = parent.windows_parent_handle
        if parent_handle is None:
            raise RuntimeError("Transaction recovery parent handle is unavailable")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        desired_access = 0x80000000 | 0x00010000
        flags = 0x00000080 | 0x00200000
        handle = create_file(str(backup), desired_access, 0x00000001, None, 3, flags, None)
        value = int(getattr(handle, "value", handle)) if handle is not None else -1
        if value == ctypes.c_void_p(-1).value:
            raise OSError(ctypes.get_last_error(), "cannot open transaction backup")

        class _FileRenameInfoHeader(ctypes.Structure):
            _fields_ = (
                ("replace_if_exists", wintypes.BOOLEAN),
                ("root_directory", wintypes.HANDLE),
                ("file_name_length", wintypes.DWORD),
                ("file_name", wintypes.WCHAR * 1),
            )

        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        try:
            file_name = record.target.name.encode("utf-16-le")
            buffer_size = ctypes.sizeof(_FileRenameInfoHeader) + len(file_name)
            buffer = ctypes.create_string_buffer(buffer_size)
            info = _FileRenameInfoHeader.from_buffer(buffer)
            info.replace_if_exists = True
            info.root_directory = wintypes.HANDLE(parent_handle)
            info.file_name_length = len(file_name)
            ctypes.memmove(
                ctypes.addressof(buffer) + _FileRenameInfoHeader.file_name.offset,
                file_name,
                len(file_name),
            )
            class _IoStatusBlock(ctypes.Structure):
                _fields_ = (
                    ("status", ctypes.c_void_p),
                    ("information", ctypes.c_size_t),
                )

            ntdll = ctypes.WinDLL("ntdll")
            rename = ntdll.NtSetInformationFile
            rename.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_IoStatusBlock),
                wintypes.LPVOID,
                wintypes.ULONG,
                ctypes.c_int,
            )
            rename.restype = ctypes.c_long
            io_status = _IoStatusBlock()
            status = rename(
                wintypes.HANDLE(value), ctypes.byref(io_status), buffer,
                buffer_size, 10,
            )
            if status != 0:
                convert_error = ntdll.RtlNtStatusToDosError
                convert_error.argtypes = (ctypes.c_long,)
                convert_error.restype = wintypes.ULONG
                error = int(convert_error(status))
                raise OSError(error, "cannot restore transaction backup")
        finally:
            close_handle(wintypes.HANDLE(value))

    def _restore_backup_posix(
        self,
        record: _Backup,
        backup: Path,
        parent: _RecoveryParent,
    ) -> None:
        """Restore relative to a verified directory descriptor on POSIX."""
        parent_fd = parent.fd
        if parent_fd is None:
            raise RuntimeError("Transaction recovery parent handle is unavailable")
        temporary_name = f".{record.target.name}.{uuid.uuid4().hex}.rollback"
        descriptor = -1
        try:
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

    def _delete_new_target(
        self,
        record: _Backup,
        parent: _RecoveryParent,
    ) -> None:
        target = record.target
        if os.name != "nt":
            if parent.fd is None:
                raise RuntimeError("Transaction recovery parent handle is unavailable")
            try:
                info = os.stat(target.name, dir_fd=parent.fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(info.st_mode):
                raise ValueError("Transaction cannot safely remove a new directory")
            os.unlink(target.name, dir_fd=parent.fd)
            os.fsync(parent.fd)
            return
        self._delete_new_target_windows(record, parent)

    @classmethod
    def _delete_new_target_windows(
        cls,
        record: _Backup,
        parent: _RecoveryParent,
    ) -> None:
        """Delete a regular file by handle after binding it to the opened parent."""
        import ctypes
        from ctypes import wintypes
        import ntpath

        parent_handle = parent.windows_parent_handle
        if parent_handle is None:
            raise RuntimeError("Transaction recovery parent handle is unavailable")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        desired_access = 0x00010000 | 0x00000080
        flags = 0x00000080 | 0x00200000
        handle = create_file(
            str(record.target), desired_access, 0x00000001 | 0x00000002,
            None, 3, flags, None,
        )
        value = int(getattr(handle, "value", handle)) if handle is not None else -1
        invalid = ctypes.c_void_p(-1).value
        if value == invalid:
            error = ctypes.get_last_error()
            if error in {2, 3}:
                return
            raise OSError(error, "cannot open transaction target")
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        try:
            target_path = cls._windows_final_path(value)
            parent_path = cls._windows_final_path(parent_handle)
            if ntpath.normcase(ntpath.dirname(target_path)) != ntpath.normcase(parent_path):
                raise ValueError("Transaction recovery parent identity changed")

            class _FileDispositionInfo(ctypes.Structure):
                _fields_ = (("delete_file", wintypes.BOOLEAN),)

            disposition = _FileDispositionInfo(True)
            delete = kernel32.SetFileInformationByHandle
            delete.argtypes = (
                wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            )
            delete.restype = wintypes.BOOL
            if not delete(
                wintypes.HANDLE(value), 4, ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                raise OSError(ctypes.get_last_error(), "cannot remove transaction target")
        finally:
            close_handle(wintypes.HANDLE(value))

    @staticmethod
    def _windows_final_path(handle: int) -> str:
        """Return the normalized DOS path represented by one Windows handle."""
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        final_path = kernel32.GetFinalPathNameByHandleW
        final_path.argtypes = (
            wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        )
        final_path.restype = wintypes.DWORD
        required = final_path(wintypes.HANDLE(handle), None, 0, 0)
        if not required:
            raise OSError(ctypes.get_last_error(), "cannot resolve recovery handle")
        buffer = ctypes.create_unicode_buffer(required + 1)
        written = final_path(wintypes.HANDLE(handle), buffer, len(buffer), 0)
        if not written or written >= len(buffer):
            raise OSError(ctypes.get_last_error(), "cannot resolve recovery handle")
        value = buffer.value
        return value[4:] if value.startswith("\\\\?\\") else value

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
