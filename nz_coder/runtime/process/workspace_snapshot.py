"""Git-independent, content-addressed workspace snapshots for Agent steps.

Snapshots intentionally exclude NZ-Coder state, VCS metadata, dependency
caches, symlinks, and oversized files.  A transition validates every affected
path before writing, so a later user or concurrent-agent edit cannot be
silently overwritten by message revert.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path


_EXCLUDED_NAMES = {
    ".git", ".hg", ".svn", ".nz-coder", ".nz-coder-runs",
    "node_modules", "vendor",
    ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "dist", "build", "target",
}
_MAX_PATCH_BYTES = 256 * 1024


def _is_excluded_name(name: str) -> bool:
    return name in _EXCLUDED_NAMES


class SnapshotError(RuntimeError):
    """Raised when a snapshot cannot be read or safely applied."""


@dataclass(frozen=True)
class SnapshotTransition:
    """Result of one atomic, conflict-checked workspace transition."""

    source: str
    destination: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotFileDiff:
    """One bounded, content-addressed file diff between two snapshots."""

    file: str
    patch: str
    additions: int
    deletions: int
    status: str


class WorkspaceSnapshotStore:
    """Persist bounded file manifests and deduplicated blobs for one workspace."""

    def __init__(
        self,
        workspace: Path,
        root: Path,
        *,
        max_file_bytes: int = 8 * 1024 * 1024,
        max_files: int = 50_000,
        max_total_bytes: int = 256 * 1024 * 1024,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = Path(root)
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_files = max(1, int(max_files))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self._lock = threading.RLock()
        self._file_cache: dict[str, tuple[int, int, int, str]] = {}

    def track(self, cancel_event: threading.Event | None = None) -> str:
        """Capture a bounded workspace manifest and return its stable ID."""
        with self._lock:
            entries: dict[str, dict] = {}
            total = 0
            for path in self._iter_files():
                if cancel_event is not None and cancel_event.is_set():
                    raise SnapshotError("snapshot capture cancelled")
                try:
                    info = path.stat(follow_symlinks=False)
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_file_bytes:
                    continue
                if len(entries) >= self.max_files:
                    raise SnapshotError("workspace snapshot file limit exceeded")
                if total + info.st_size > self.max_total_bytes:
                    raise SnapshotError("workspace snapshot byte limit exceeded")
                rel = path.relative_to(self.workspace).as_posix()
                mode = stat.S_IMODE(info.st_mode)
                cached = self._file_cache.get(rel)
                if cached is not None and cached[:3] == (info.st_mtime_ns, info.st_size, mode):
                    digest = cached[3]
                else:
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    self._write_blob(digest, data)
                    self._file_cache[rel] = (info.st_mtime_ns, info.st_size, mode, digest)
                entries[rel] = {
                    "blob": digest,
                    "size": info.st_size,
                    "mode": mode,
                }
                total += info.st_size

            canonical = json.dumps(entries, sort_keys=True, separators=(",", ":"))
            snapshot_id = "snap-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            manifest = {
                "version": 1,
                "snapshot": snapshot_id,
                "workspace": str(self.workspace),
                "files": entries,
            }
            self._atomic_json(self._manifest_path(snapshot_id), manifest)
            return snapshot_id

    def changed_files(self, source: str, destination: str) -> list[str]:
        """Return paths whose existence, blob, or executable mode differs."""
        left = self._load(source)["files"]
        right = self._load(destination)["files"]
        return sorted(
            path
            for path in set(left) | set(right)
            if _entry_identity(left.get(path)) != _entry_identity(right.get(path))
        )

    def diff_full(self, source: str, destination: str) -> list[SnapshotFileDiff]:
        """Return InfCode-shaped file diffs without consulting the user VCS."""
        with self._lock:
            left = self._load(source)["files"]
            right = self._load(destination)["files"]
            result: list[SnapshotFileDiff] = []
            for rel in self.changed_files(source, destination):
                before_entry = left.get(rel)
                after_entry = right.get(rel)
                status = (
                    "added" if before_entry is None
                    else "deleted" if after_entry is None
                    else "modified"
                )
                before = self._entry_bytes(before_entry)
                after = self._entry_bytes(after_entry)
                patch, additions, deletions = _text_diff(rel, before, after)
                if len(patch.encode("utf-8")) > _MAX_PATCH_BYTES:
                    patch = ""
                result.append(SnapshotFileDiff(
                    file=rel,
                    patch=patch,
                    additions=additions,
                    deletions=deletions,
                    status=status,
                ))
            return result

    def transition(
        self,
        source: str,
        destination: str,
        *,
        paths: list[str] | tuple[str, ...] | None = None,
    ) -> SnapshotTransition:
        """Atomically replace changed paths from source state with destination.

        Only paths that differ between the two manifests are inspected or
        changed.  Every such path must still match ``source`` before any write.
        """
        with self._lock:
            source_files = self._load(source)["files"]
            destination_files = self._load(destination)["files"]
            available = self.changed_files(source, destination)
            if paths is None:
                changed = available
            else:
                requested = set(paths)
                if any(not isinstance(path, str) for path in paths):
                    raise SnapshotError("invalid transition path list")
                changed = [path for path in available if path in requested]
                if requested != set(changed):
                    raise SnapshotError("transition path is absent from snapshot diff")
            conflicts = [
                path for path in changed
                if not self._matches(path, source_files.get(path))
            ]
            if conflicts:
                joined = ", ".join(conflicts[:20])
                if len(conflicts) > 20:
                    joined += f", ... ({len(conflicts) - 20} more)"
                raise SnapshotError(
                    "workspace changed after the recorded snapshot: " + joined
                )

            backup_root = Path(tempfile.mkdtemp(prefix="nz-snapshot-"))
            applied: list[tuple[str, bool, int]] = []
            try:
                for rel in changed:
                    target = self._safe_path(rel)
                    existed = target.is_file() and not target.is_symlink()
                    mode = stat.S_IMODE(target.stat().st_mode) if existed else 0
                    if existed:
                        backup = backup_root / rel
                        backup.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(target, backup)
                    self._apply(target, destination_files.get(rel))
                    applied.append((rel, existed, mode))
            except Exception as exc:
                for rel, existed, mode in reversed(applied):
                    target = self._safe_path(rel)
                    try:
                        if existed:
                            backup = backup_root / rel
                            self._atomic_bytes(target, backup.read_bytes(), mode)
                        elif target.exists() and target.is_file() and not target.is_symlink():
                            target.unlink()
                    except OSError:
                        pass
                raise SnapshotError(f"snapshot transition rolled back: {exc}") from exc
            finally:
                shutil.rmtree(backup_root, ignore_errors=True)
            return SnapshotTransition(source, destination, tuple(changed))

    def _iter_files(self):
        for root, dirs, files in os.walk(self.workspace, topdown=True, followlinks=False):
            root_path = Path(root)
            dirs[:] = sorted(
                name for name in dirs
                if not _is_excluded_name(name)
                and not (root_path / name).is_symlink()
            )
            for name in sorted(files):
                path = root_path / name
                if _is_excluded_name(name) or path.is_symlink():
                    continue
                yield path

    def _matches(self, rel: str, entry: dict | None) -> bool:
        target = self._safe_path(rel)
        if entry is None:
            return not target.exists()
        if target.is_symlink() or not target.is_file():
            return False
        try:
            data = target.read_bytes()
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            return False
        return (
            hashlib.sha256(data).hexdigest() == entry.get("blob")
            and mode == int(entry.get("mode", mode))
        )

    def _entry_bytes(self, entry: dict | None) -> bytes:
        if entry is None:
            return b""
        blob = str(entry.get("blob") or "")
        data = self._blob_path(blob).read_bytes()
        if hashlib.sha256(data).hexdigest() != blob:
            raise SnapshotError(f"corrupt snapshot blob: {blob}")
        return data

    def _apply(self, target: Path, entry: dict | None) -> None:
        if entry is None:
            if target.exists():
                if target.is_symlink() or not target.is_file():
                    raise SnapshotError(f"unsafe deletion target: {target}")
                target.unlink()
            return
        blob = str(entry.get("blob") or "")
        data = self._blob_path(blob).read_bytes()
        if hashlib.sha256(data).hexdigest() != blob:
            raise SnapshotError(f"corrupt snapshot blob: {blob}")
        self._atomic_bytes(target, data, int(entry.get("mode", 0o644)))

    def _safe_path(self, rel: str) -> Path:
        raw = Path(rel)
        if raw.is_absolute() or ".." in raw.parts:
            raise SnapshotError(f"unsafe snapshot path: {rel}")
        target = (self.workspace / raw).resolve(strict=False)
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise SnapshotError(f"snapshot path escapes workspace: {rel}") from exc
        return target

    def _load(self, snapshot_id: str) -> dict:
        if not _valid_snapshot_id(snapshot_id):
            raise SnapshotError("invalid snapshot ID")
        try:
            payload = json.loads(self._manifest_path(snapshot_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"snapshot not found: {snapshot_id}") from exc
        if payload.get("snapshot") != snapshot_id or payload.get("workspace") != str(self.workspace):
            raise SnapshotError("snapshot workspace or identity mismatch")
        if not isinstance(payload.get("files"), dict):
            raise SnapshotError("invalid snapshot manifest")
        files = payload["files"]
        for rel, entry in files.items():
            if not isinstance(rel, str) or not isinstance(entry, dict):
                raise SnapshotError("invalid snapshot manifest entry")
            self._safe_path(rel)
            blob = entry.get("blob")
            if not _valid_digest(blob):
                raise SnapshotError("invalid snapshot blob identity")
            size = entry.get("size")
            mode = entry.get("mode")
            if (
                not isinstance(size, int) or isinstance(size, bool) or size < 0
                or not isinstance(mode, int) or isinstance(mode, bool) or mode < 0
            ):
                raise SnapshotError("invalid snapshot file metadata")
        canonical = json.dumps(files, sort_keys=True, separators=(",", ":"))
        expected = "snap-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if expected != snapshot_id:
            raise SnapshotError("snapshot manifest integrity mismatch")
        return payload

    def _write_blob(self, digest: str, data: bytes) -> None:
        path = self._blob_path(digest)
        if path.exists():
            return
        # The manifest is the commit point and blobs are integrity-checked on
        # read. Avoid one fsync per source file during initial repository scan;
        # a crash can at worst make that snapshot unusable, never unsafe.
        self._atomic_bytes(path, data, 0o600, sync=False)

    def _manifest_path(self, snapshot_id: str) -> Path:
        return self.root / "manifests" / f"{snapshot_id}.json"

    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest[2:]

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes, mode: int, *, sync: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                if sync:
                    os.fsync(handle.fileno())
            os.chmod(temp, stat.S_IMODE(mode))
            temp.replace(path)
        except Exception:
            temp.unlink(missing_ok=True)
            raise

    @classmethod
    def _atomic_json(cls, path: Path, payload: dict) -> None:
        data = (json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        cls._atomic_bytes(path, data, 0o600)


def _entry_identity(entry: dict | None) -> tuple[str, int] | None:
    if not isinstance(entry, dict):
        return None
    return str(entry.get("blob") or ""), int(entry.get("mode", 0))


def _text_diff(rel: str, before: bytes, after: bytes) -> tuple[str, int, int]:
    """Build a bounded unified text diff and git-numstat-like line counts."""
    if b"\0" in before or b"\0" in after:
        return "", 0, 0
    try:
        before_text = before.decode("utf-8")
        after_text = after.decode("utf-8")
    except UnicodeDecodeError:
        return "", 0, 0
    old_lines = before_text.splitlines(keepends=True)
    new_lines = after_text.splitlines(keepends=True)
    additions = 0
    deletions = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in {"replace", "delete"}:
            deletions += old_end - old_start
        if tag in {"replace", "insert"}:
            additions += new_end - new_start
    patch = "".join(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        n=3,
    ))
    return patch, additions, deletions


def _valid_snapshot_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("snap-")
        and len(value) == 69
        and all(char in "0123456789abcdef" for char in value[5:])
    )


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )
