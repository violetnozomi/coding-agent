"""Shared byte-object layout and atomic publication, without runtime ownership."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile


class SnapshotError(RuntimeError):
    """A file snapshot or recovery transition cannot safely be applied."""


class ContentObjectStore:
    """Raw objects shared by step snapshots and the authoritative tool ledger."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest[2:]

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes, mode: int, *, sync: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temporary = Path(name)
        try:
            handle = os.fdopen(fd, "wb")
            fd = -1  # Ownership transferred; never close a possibly reused fd.
            with handle:
                handle.write(data)
                handle.flush()
                os.chmod(temporary, stat.S_IMODE(mode))
                if sync:
                    os.fsync(handle.fileno())
            temporary.replace(path)
        except Exception:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            temporary.unlink(missing_ok=True)
            raise
