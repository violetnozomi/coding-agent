"""Handle-anchored model file operations confined to one workspace."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import locale
import stat
import tempfile
import uuid

from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.protocol.public_error import PublicInputError


@dataclass(frozen=True)
class WorkspaceFileStat:
    """Public metadata captured from the same opened object used for I/O."""

    size: int
    mode: int
    mtime_ns: int


@dataclass(frozen=True)
class WorkspaceDirectoryEntry:
    """One entry enumerated from a held workspace directory handle."""

    path: str
    is_directory: bool
    depth: int


class WorkspaceFileAccess:
    """Perform model reads and mutations through workspace-anchored handles."""

    def __init__(self, root: Path | str):
        self.root = Path(root).expanduser().resolve(strict=True)
        self.policy = WorkspacePathPolicy(self.root)

    def display_path(self, path: str, *, write: bool = False) -> Path:
        """Return a validated path for display and non-authoritative metadata."""
        return (
            self.policy.validate_model_write(path)
            if write else self.policy.validate_model_read(path)
        )

    def read_bytes(self, path: str, *, maximum: int | None = None) -> bytes:
        """Read one regular file from the opened workspace directory chain."""
        relative = self._relative(path, write=False)
        if os.name == "nt":
            return self._read_windows(relative, maximum=maximum)
        parent, name = self._open_parent_posix(relative, create=False)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(name, flags, dir_fd=parent)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError("Workspace target is not a regular file")
                if maximum is not None and info.st_size > maximum:
                    raise PublicInputError("Workspace file exceeds the allowed size")
                chunks: list[bytes] = []
                total = 0
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if maximum is not None and total > maximum:
                        raise PublicInputError("Workspace file exceeds the allowed size")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            os.close(parent)

    def read_text(
        self,
        path: str,
        *,
        encoding: str = "utf-8",
        errors: str = "strict",
        maximum: int | None = None,
    ) -> str:
        return self.read_bytes(path, maximum=maximum).decode(encoding, errors=errors)

    def stat(self, path: str) -> WorkspaceFileStat:
        """Stat the exact regular file reached through the anchored parent."""
        relative = self._relative(path, write=False)
        if os.name == "nt":
            data = self._read_windows(relative, maximum=None)
            info = (self.root / relative).stat()
            return WorkspaceFileStat(len(data), int(info.st_mode), int(info.st_mtime_ns))
        parent, name = self._open_parent_posix(relative, create=False)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Workspace target is not a regular file")
            return WorkspaceFileStat(int(info.st_size), int(info.st_mode), int(info.st_mtime_ns))
        finally:
            os.close(parent)

    def kind(self, path: str, *, operation: str = "read") -> str:
        """Return ``file``, ``directory`` or ``missing`` from anchored metadata."""
        relative = self._relative_directory(path, operation=operation)
        if os.name == "nt":
            return self._kind_windows(relative)
        if not relative.parts:
            return "directory"
        try:
            parent, name = self._open_parent_posix(relative, create=False)
        except FileNotFoundError:
            return "missing"
        try:
            try:
                info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                return "missing"
            if stat.S_ISREG(info.st_mode):
                return "file"
            if stat.S_ISDIR(info.st_mode):
                return "directory"
            raise ValueError("Workspace target has an unsafe type")
        finally:
            os.close(parent)

    def walk_directory(
        self,
        path: str = ".",
        *,
        max_depth: int = 1,
        maximum_entries: int = 10_000,
        include_hidden_root: bool = False,
        directories_first: bool = True,
    ) -> tuple[WorkspaceDirectoryEntry, ...]:
        """Enumerate a directory tree while every traversed parent stays open."""
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise PublicInputError("Directory depth must be a non-negative integer")
        if maximum_entries < 1:
            raise ValueError("Directory entry limit must be positive")
        relative = self._relative_directory(path, operation="list")
        if os.name == "nt":
            return self._walk_windows(
                relative,
                max_depth,
                maximum_entries,
                include_hidden_root,
                directories_first,
            )
        descriptor = self._open_directory_posix(relative)
        records: list[WorkspaceDirectoryEntry] = []
        try:
            self._walk_posix(
                descriptor,
                Path(),
                0,
                max_depth,
                maximum_entries,
                records,
                include_hidden_root,
                directories_first,
            )
        finally:
            os.close(descriptor)
        return tuple(records)

    def exists(self, path: str) -> bool:
        return self.kind(path) != "missing"

    def _exists_windows(self, relative: Path) -> bool:
        """Probe an optional file while retaining the verified Windows parent chain."""
        from nz_coder.foundation.project_control import (
            UnsafeProjectControl,
            _windows_close,
            _windows_handle_info,
            _windows_open,
        )

        handles: list[int] = []
        try:
            parent = _windows_open(self.root, directory=True)
            assert parent is not None
            handles.append(parent)
            cursor = self.root
            for part in relative.parts[:-1]:
                cursor = cursor / part
                child = _windows_open(cursor, directory=True, parent=parent)
                assert child is not None
                handles.append(child)
                parent = child
            target = _windows_open(
                cursor / relative.name,
                directory=False,
                missing_ok=True,
                parent=parent,
            )
            if target is None:
                return False
            handles.append(target)
            attributes, _device, _inode, _size = _windows_handle_info(
                target, full=True,
            )
            return not bool(attributes & 0x00000010)
        except UnsafeProjectControl as exc:
            raise ValueError("Workspace file boundary is unsafe") from exc
        finally:
            for handle in reversed(handles):
                _windows_close(handle)

    def write_text(
        self,
        path: str,
        content: str,
        *,
        transaction=None,
    ) -> None:
        """Atomically replace a file beneath a verified, held parent handle."""
        relative = self._relative(path, write=True)
        if os.name == "nt":
            self._write_windows(relative, content.encode("utf-8"), transaction)
            return
        parent, name = self._open_parent_posix(relative, create=True)
        temporary = f".nzcoder-{uuid.uuid4().hex}.tmp"
        try:
            if transaction is not None and getattr(transaction, "active", False):
                transaction.track_anchored(
                    relative.as_posix(), parent_fd=parent,
                )
            flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            )
            descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)
            try:
                data = content.encode("utf-8")
                view = memoryview(data)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("workspace write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def delete(self, path: str, *, transaction=None) -> None:
        """Delete relative to a verified, held workspace parent handle."""
        relative = self._relative(path, write=True)
        if os.name == "nt":
            self._delete_windows(relative, transaction)
            return
        parent, name = self._open_parent_posix(relative, create=False)
        try:
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("Workspace target is not a regular file")
            if transaction is not None and getattr(transaction, "active", False):
                transaction.track_anchored(
                    relative.as_posix(), parent_fd=parent,
                )
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)

    def _relative(self, path: str, *, write: bool) -> Path:
        validated = self.display_path(path, write=write)
        try:
            relative = validated.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path escapes workspace") from exc
        if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Workspace file path is invalid")
        return relative

    def _relative_directory(self, path: str, *, operation: str) -> Path:
        if operation == "list":
            validated = self.policy.validate_model_list(path)
        else:
            validated = self.policy.validate_model_read(path)
        try:
            relative = validated.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("Path escapes workspace") from exc
        if relative == Path("."):
            return Path()
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Workspace directory path is invalid")
        return relative

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        )

    def _open_directory_posix(self, relative: Path) -> int:
        flags = self._directory_flags()
        descriptor = os.open(self.root, flags)
        try:
            for part in relative.parts:
                child = os.open(part, flags, dir_fd=descriptor)
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    os.close(child)
                    raise ValueError("Workspace directory path is unsafe")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _walk_posix(
        self,
        descriptor: int,
        prefix: Path,
        level: int,
        max_depth: int,
        maximum_entries: int,
        records: list[WorkspaceDirectoryEntry],
        include_hidden_root: bool,
        directories_first: bool,
    ) -> None:
        if level >= max_depth:
            return
        candidates: list[tuple[str, bool]] = []
        for name in os.listdir(descriptor):
            if name in {".", ".."}:
                continue
            try:
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISDIR(info.st_mode):
                candidates.append((name, True))
            elif stat.S_ISREG(info.st_mode):
                candidates.append((name, False))
        if directories_first:
            candidates.sort(key=lambda item: (not item[1], item[0].lower()))
        else:
            candidates.sort(key=lambda item: locale.strxfrm(item[0]))
        flags = self._directory_flags()
        for name, is_directory in candidates:
            relative = prefix / name
            if level == 0 and name.startswith(".") and not include_hidden_root:
                continue
            if not self.policy.is_model_visible(self.root / relative):
                continue
            if len(records) >= maximum_entries:
                raise PublicInputError("Directory listing exceeds the allowed entry limit")
            records.append(WorkspaceDirectoryEntry(relative.as_posix(), is_directory, level))
            if not is_directory or level + 1 >= max_depth:
                continue
            try:
                child = os.open(name, flags, dir_fd=descriptor)
            except (FileNotFoundError, NotADirectoryError):
                continue
            try:
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("Workspace directory entry changed type")
                self._walk_posix(
                    child,
                    relative,
                    level + 1,
                    max_depth,
                    maximum_entries,
                    records,
                    include_hidden_root,
                    directories_first,
                )
            finally:
                os.close(child)

    def _open_parent_posix(self, relative: Path, *, create: bool) -> tuple[int, str]:
        flags = self._directory_flags()
        descriptor = os.open(self.root, flags)
        try:
            root_info = os.fstat(descriptor)
            if not stat.S_ISDIR(root_info.st_mode):
                raise ValueError("Workspace root is not a directory")
            for part in relative.parts[:-1]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o755, dir_fd=descriptor)
                    child = os.open(part, flags, dir_fd=descriptor)
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    os.close(child)
                    raise ValueError("Workspace parent is unsafe")
                os.close(descriptor)
                descriptor = child
            return descriptor, relative.name
        except Exception:
            os.close(descriptor)
            raise

    def _kind_windows(self, relative: Path) -> str:
        from nz_coder.foundation.project_control import (
            UnsafeProjectControl,
            _windows_close,
            _windows_open,
        )

        target = self.root / relative
        if not relative.parts:
            return "directory"
        try:
            info = target.lstat()
        except FileNotFoundError:
            return "missing"
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Workspace target has an unsafe type")
        expected_directory = stat.S_ISDIR(info.st_mode)
        if not expected_directory and not stat.S_ISREG(info.st_mode):
            raise ValueError("Workspace target has an unsafe type")
        handles: list[int] = []
        try:
            parent = _windows_open(self.root, directory=True)
            assert parent is not None
            handles.append(parent)
            cursor = self.root
            for part in relative.parts[:-1]:
                cursor /= part
                child = _windows_open(cursor, directory=True, parent=parent)
                assert child is not None
                handles.append(child)
                parent = child
            opened = _windows_open(
                cursor / relative.name,
                directory=expected_directory,
                parent=parent,
            )
            assert opened is not None
            handles.append(opened)
            return "directory" if expected_directory else "file"
        except UnsafeProjectControl as exc:
            raise ValueError("Workspace file boundary is unsafe") from exc
        finally:
            for handle in reversed(handles):
                _windows_close(handle)

    def _walk_windows(
        self,
        relative: Path,
        max_depth: int,
        maximum_entries: int,
        include_hidden_root: bool,
        directories_first: bool,
    ) -> tuple[WorkspaceDirectoryEntry, ...]:
        from nz_coder.foundation.project_control import (
            UnsafeProjectControl,
            _windows_close,
            _windows_final_path,
            _windows_open,
        )

        handles: list[int] = []
        try:
            directory = _windows_open(self.root, directory=True)
            assert directory is not None
            handles.append(directory)
            cursor = self.root
            for part in relative.parts:
                cursor /= part
                child = _windows_open(cursor, directory=True, parent=directory)
                assert child is not None
                handles.append(child)
                directory = child
            records: list[WorkspaceDirectoryEntry] = []
            self._walk_windows_handle(
                directory,
                Path(_windows_final_path(directory)),
                Path(),
                0,
                max_depth,
                maximum_entries,
                records,
                include_hidden_root,
                directories_first,
            )
            return tuple(records)
        except UnsafeProjectControl as exc:
            raise ValueError("Workspace directory boundary is unsafe") from exc
        finally:
            for handle in reversed(handles):
                _windows_close(handle)

    def _walk_windows_handle(
        self,
        handle: int,
        directory_path: Path,
        prefix: Path,
        level: int,
        max_depth: int,
        maximum_entries: int,
        records: list[WorkspaceDirectoryEntry],
        include_hidden_root: bool,
        directories_first: bool,
    ) -> None:
        if level >= max_depth:
            return
        from nz_coder.foundation.project_control import (
            UnsafeProjectControl,
            _windows_close,
            _windows_final_path,
            _windows_open,
        )

        before = os.path.normcase(os.path.normpath(_windows_final_path(handle)))
        candidates: list[tuple[str, bool]] = []
        with os.scandir(directory_path) as iterator:
            for entry in iterator:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        candidates.append((entry.name, True))
                    elif entry.is_file(follow_symlinks=False):
                        candidates.append((entry.name, False))
                except FileNotFoundError:
                    continue
        if os.path.normcase(os.path.normpath(_windows_final_path(handle))) != before:
            raise UnsafeProjectControl("workspace directory identity changed")
        if directories_first:
            candidates.sort(key=lambda item: (not item[1], item[0].lower()))
        else:
            candidates.sort(key=lambda item: locale.strxfrm(item[0]))
        for name, is_directory in candidates:
            relative = prefix / name
            if level == 0 and name.startswith(".") and not include_hidden_root:
                continue
            if not self.policy.is_model_visible(self.root / relative):
                continue
            try:
                child = _windows_open(
                    directory_path / name,
                    directory=is_directory,
                    missing_ok=True,
                    parent=handle,
                )
            except UnsafeProjectControl:
                continue
            if child is None:
                continue
            try:
                if len(records) >= maximum_entries:
                    raise PublicInputError("Directory listing exceeds the allowed entry limit")
                records.append(WorkspaceDirectoryEntry(relative.as_posix(), is_directory, level))
                if is_directory and level + 1 < max_depth:
                    self._walk_windows_handle(
                        child,
                        Path(_windows_final_path(child)),
                        relative,
                        level + 1,
                        max_depth,
                        maximum_entries,
                        records,
                        include_hidden_root,
                        directories_first,
                    )
            finally:
                _windows_close(child)

    def _read_windows(self, relative: Path, *, maximum: int | None) -> bytes:
        from nz_coder.foundation.project_control import (
            UnsafeProjectControl,
            _windows_close,
            _windows_handle_info,
            _windows_open,
        )

        handles: list[int] = []
        try:
            parent = _windows_open(self.root, directory=True)
            assert parent is not None
            handles.append(parent)
            cursor = self.root
            for part in relative.parts[:-1]:
                cursor = cursor / part
                child = _windows_open(cursor, directory=True, parent=parent)
                assert child is not None
                handles.append(child)
                parent = child
            target = _windows_open(cursor / relative.name, directory=False, parent=parent)
            assert target is not None
            handles.append(target)
            _attrs, _device, _inode, size = _windows_handle_info(target, full=True)
            if maximum is not None and size > maximum:
                raise PublicInputError("Workspace file exceeds the allowed size")
            import msvcrt

            source_fd = msvcrt.open_osfhandle(target, os.O_RDONLY)
            handles.pop()
            with os.fdopen(source_fd, "rb", closefd=True) as stream:
                data = stream.read(maximum + 1 if maximum is not None else -1)
            if maximum is not None and len(data) > maximum:
                raise PublicInputError("Workspace file exceeds the allowed size")
            return data
        except UnsafeProjectControl as exc:
            raise ValueError("Workspace file boundary is unsafe") from exc
        finally:
            for handle in reversed(handles):
                _windows_close(handle)

    def _write_windows(self, relative: Path, data: bytes, transaction) -> None:
        # Windows directory handles are retained and verified by the project
        # control helper; replacement still has a documented final path/rename
        # TOCTOU window because Python exposes no handle-relative ReplaceFile.
        from nz_coder.foundation.project_control import _windows_close, _windows_final_path, _windows_open

        parent_path = self.root / relative.parent
        parent_path.mkdir(parents=True, exist_ok=True)
        handle = _windows_open(parent_path, directory=True)
        assert handle is not None
        temporary: Path | None = None
        try:
            if Path(_windows_final_path(handle)) != parent_path.resolve():
                raise ValueError("Workspace parent identity changed")
            if transaction is not None and getattr(transaction, "active", False):
                transaction.track_anchored(
                    relative.as_posix(), windows_parent_handle=handle,
                )
            descriptor, raw = tempfile.mkstemp(prefix=".nzcoder-", suffix=".tmp", dir=parent_path)
            temporary = Path(raw)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if Path(_windows_final_path(handle)) != parent_path.resolve():
                raise ValueError("Workspace parent identity changed")
            os.replace(temporary, parent_path / relative.name)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            _windows_close(handle)

    def _delete_windows(self, relative: Path, transaction) -> None:
        from nz_coder.foundation.project_control import _windows_close, _windows_final_path, _windows_open

        parent_path = self.root / relative.parent
        handle = _windows_open(parent_path, directory=True)
        assert handle is not None
        try:
            if Path(_windows_final_path(handle)) != parent_path.resolve():
                raise ValueError("Workspace parent identity changed")
            if transaction is not None and getattr(transaction, "active", False):
                transaction.track_anchored(
                    relative.as_posix(), windows_parent_handle=handle,
                )
            target = parent_path / relative.name
            if target.is_symlink() or not target.is_file():
                raise ValueError("Workspace target is not a regular file")
            target.unlink()
        finally:
            _windows_close(handle)


__all__ = [
    "WorkspaceDirectoryEntry",
    "WorkspaceFileAccess",
    "WorkspaceFileStat",
]
