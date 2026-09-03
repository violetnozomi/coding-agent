"""Small cross-platform advisory file lock for private state updates."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold an OS-released exclusive lock on one stable lock file."""
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with target.open("a+b") as handle:
        if os.name == "nt":
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

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = ["exclusive_file_lock"]
