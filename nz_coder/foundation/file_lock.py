"""Small cross-platform advisory file lock for private state updates."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
from typing import Iterator


class UnsafeFileLock(OSError):
    """Raised when a private lock path can be redirected or is not regular."""


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an OS-released exclusive lock on one stable lock file."""
    target = Path(path).absolute()
    if os.name == "nt":
        _prepare_windows_parent(target.parent)
        if target.exists() and (
            target.is_symlink()
            or _is_windows_reparse_point(target)
            or not target.is_file()
        ):
            raise UnsafeFileLock("private lock is a symbolic link, reparse point, or non-regular file")
        with target.open("a+b") as handle:
            if target.is_symlink() or _is_windows_reparse_point(target):
                raise UnsafeFileLock("private lock is a symbolic link or reparse point")
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    parent_fd = _open_posix_parent(target.parent)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target.name, flags, 0o600, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            raise UnsafeFileLock("private lock is not a regular file")
        handle = os.fdopen(fd, "a+b", closefd=True)
    except OSError as exc:
        os.close(parent_fd)
        if isinstance(exc, UnsafeFileLock):
            raise
        raise UnsafeFileLock("private lock is a symbolic link or unsafe path") from exc
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
        os.close(parent_fd)


def _open_posix_parent(parent: Path) -> int:
    """Open/create an absolute directory chain without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    current = os.open(parent.anchor or os.sep, flags)
    try:
        for part in parent.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=current)
                child = os.open(part, flags, dir_fd=current)
            except OSError as exc:
                raise UnsafeFileLock("private lock parent is a symbolic link or unsafe directory") from exc
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _prepare_windows_parent(parent: Path) -> None:
    pending: list[Path] = []
    current = parent
    while not current.exists():
        pending.append(current)
        if current == current.parent:
            break
        current = current.parent
    for candidate in [current, *reversed(pending)]:
        if candidate.exists():
            if candidate.is_symlink() or _is_windows_reparse_point(candidate) or not candidate.is_dir():
                raise UnsafeFileLock("private lock parent is a symbolic link or reparse point")
        else:
            candidate.mkdir(mode=0o700)


def _is_windows_reparse_point(path: Path) -> bool:
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, OSError):
        return False


__all__ = ["UnsafeFileLock", "exclusive_file_lock"]
