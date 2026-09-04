"""Opaque, quota-bounded, session-owned model-readable artifacts."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
import uuid
from typing import Callable

from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import session_dir, session_tool_results_dir


_ARTIFACT_ID = re.compile(r"^artifact_[a-f0-9]{32}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_ALLOWED_MODEL_KINDS = frozenset({"tool-result", "user-input"})
_LOCK = threading.RLock()


class ArtifactError(OSError):
    """Base artifact failure whose message never includes stored content."""


class ArtifactAccessError(ArtifactError):
    """Artifact ownership, type, or identifier validation failed."""


class ArtifactQuotaError(ArtifactError):
    """Artifact storage or read quota was exceeded."""


@dataclass(frozen=True)
class ArtifactChunk:
    """One bounded UTF-8 projection of an opaque artifact."""

    text: str
    next_offset: int
    has_more: bool
    total_bytes: int


class ArtifactStore:
    """Persist and read only model-safe artifacts owned by one Session."""

    def __init__(
        self,
        workspace: Path,
        session_id: str,
        *,
        max_result_bytes: int = 4 * 1024 * 1024,
        max_session_bytes: int = 64 * 1024 * 1024,
        max_session_files: int = 256,
        max_workspace_bytes: int = 256 * 1024 * 1024,
        max_workspace_files: int = 2048,
        max_read_bytes: int = 64 * 1024,
        ttl_seconds: int = 7 * 24 * 60 * 60,
        clock: Callable[[], float] = time.time,
        on_cleanup: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.session_id = _validated_session_id(session_id)
        self.max_result_bytes = _positive(max_result_bytes, "max_result_bytes")
        self.max_session_bytes = _positive(max_session_bytes, "max_session_bytes")
        self.max_session_files = _positive(max_session_files, "max_session_files")
        self.max_workspace_bytes = _positive(max_workspace_bytes, "max_workspace_bytes")
        self.max_workspace_files = _positive(max_workspace_files, "max_workspace_files")
        self.max_read_bytes = _positive(max_read_bytes, "max_read_bytes")
        self.ttl_seconds = _positive(ttl_seconds, "ttl_seconds")
        if not callable(clock):
            raise ValueError("artifact clock must be callable")
        self._clock = clock
        self._on_cleanup = on_cleanup
        self.last_cleanup: tuple[dict[str, object], ...] = ()
        with scoped_workdir(self.workspace):
            self.sessions_root = session_dir().absolute()
            self.directory = session_tool_results_dir(self.session_id).absolute()
        self.artifact_root = self.sessions_root / "_artifacts"
        try:
            self.directory.relative_to(self.workspace)
        except ValueError:
            self.directory.relative_to(self.artifact_root)
        else:
            raise ArtifactAccessError("Artifact directory must be outside the workspace")
        self.manifest_path = self.directory / "manifest.json"
        self.lock_path = self.artifact_root / ".artifact.lock"

    def put(self, text: str, *, kind: str) -> str:
        """Atomically persist one allowed model artifact and return an opaque ID."""
        if kind not in _ALLOWED_MODEL_KINDS:
            raise ArtifactAccessError("Model-readable artifact type is not allowed")
        payload = str(text).encode("utf-8")
        if len(payload) > self.max_result_bytes:
            raise ArtifactQuotaError("Artifact exceeds the per-result byte quota")
        with self._exclusive_lock():
            manifest = self._load_manifest()
            entries = manifest["entries"]
            session_files, session_bytes = self._manifest_usage(
                manifest,
                self.directory,
            )
            if session_files >= self.max_session_files:
                raise ArtifactQuotaError("Artifact Session file quota exceeded")
            if session_bytes + len(payload) > self.max_session_bytes:
                raise ArtifactQuotaError("Artifact Session byte quota exceeded")
            cleanup = self._cleanup_workspace(
                required_bytes=len(payload),
                required_files=1,
            )
            self.last_cleanup = tuple(cleanup)
            workspace_files, workspace_bytes = self._workspace_usage()
            if workspace_files + 1 > self.max_workspace_files:
                raise ArtifactQuotaError("Artifact workspace file quota exceeded")
            if workspace_bytes + len(payload) > self.max_workspace_bytes:
                raise ArtifactQuotaError("Artifact workspace byte quota exceeded")
            artifact_id = f"artifact_{uuid.uuid4().hex}"
            filename = f"{artifact_id}.txt"
            self._ensure_directory()
            _atomic_write(self.directory / filename, payload)
            entries[artifact_id] = {
                "filename": filename,
                "kind": kind,
                "size": len(payload),
                "created_at": self._clock(),
            }
            try:
                self._write_manifest(manifest)
            except Exception:
                (self.directory / filename).unlink(missing_ok=True)
                raise
            return artifact_id

    def read(self, artifact_id: str) -> str:
        chunk = self.read_chunk(
            artifact_id,
            offset=0,
            max_bytes=self.max_result_bytes,
        )
        if chunk.has_more:
            raise ArtifactQuotaError("Artifact exceeds the complete-read quota")
        return chunk.text

    def read_chunk(
        self,
        artifact_id: str,
        *,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> ArtifactChunk:
        safe_id = _validated_artifact_id(artifact_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ArtifactAccessError("Artifact offset must be a non-negative integer")
        requested = self.max_read_bytes if max_bytes is None else max_bytes
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
            raise ArtifactAccessError("Artifact max_bytes must be a positive integer")
        limit = min(requested, self.max_read_bytes)
        with self._exclusive_lock():
            manifest = self._load_manifest()
            record = manifest["entries"].get(safe_id)
            if not isinstance(record, dict):
                raise ArtifactAccessError("Artifact is not owned by the current Session")
            if record.get("kind") not in _ALLOWED_MODEL_KINDS:
                raise ArtifactAccessError("Model-readable artifact type is not allowed")
            filename = str(record.get("filename") or "")
            if filename != f"{safe_id}.txt":
                raise ArtifactAccessError("Artifact manifest entry is invalid")
            path = self._validated_artifact_path(self.directory / filename)
            try:
                descriptor = os.open(
                    path,
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
            except OSError as exc:
                raise ArtifactAccessError("Artifact content is unavailable") from exc
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    raise ArtifactAccessError("Artifact content is not a regular file")
                total = int(info.st_size)
                if offset > total:
                    raise ArtifactAccessError("Artifact offset exceeds content length")
                os.lseek(descriptor, offset, os.SEEK_SET)
                payload = os.read(descriptor, limit)
            finally:
                os.close(descriptor)
            next_offset = offset + len(payload)
            return ArtifactChunk(
                payload.decode("utf-8", errors="replace"),
                next_offset,
                next_offset < total,
                total,
            )

    def delete_all(self) -> None:
        """Delete only this Session's known artifact files and manifest."""
        with self._exclusive_lock():
            manifest = self._load_manifest()
            for artifact_id, record in manifest["entries"].items():
                if not _ARTIFACT_ID.fullmatch(str(artifact_id)) or not isinstance(record, dict):
                    continue
                filename = str(record.get("filename") or "")
                if filename == f"{artifact_id}.txt":
                    (self.directory / filename).unlink(missing_ok=True)
            self.manifest_path.unlink(missing_ok=True)

    def _ensure_directory(self) -> None:
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        harden_private_path(self.directory)

    def _load_manifest(self) -> dict[str, object]:
        try:
            if self.manifest_path.is_symlink():
                raise ArtifactAccessError("Artifact manifest must not be a symbolic link")
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"version": 1, "session_id": self.session_id, "entries": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ArtifactAccessError("Artifact manifest is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("version") != 1
            or payload.get("session_id") != self.session_id
            or not isinstance(payload.get("entries"), dict)
        ):
            raise ArtifactAccessError("Artifact manifest schema or ownership is invalid")
        return payload

    def _write_manifest(self, manifest: dict[str, object]) -> None:
        self._ensure_directory()
        _atomic_write(
            self.manifest_path,
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def _workspace_usage(self) -> tuple[int, int]:
        root = self.artifact_root
        files = 0
        total = 0
        try:
            manifests = root.glob("*/runtime/tool-results/manifest.json")
            for manifest in manifests:
                try:
                    payload = json.loads(manifest.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError):
                    continue
                entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
                if not isinstance(entries, dict):
                    continue
                entry_files, entry_bytes = self._manifest_usage(
                    {"entries": entries},
                    manifest.parent,
                )
                files += entry_files
                total += entry_bytes
        except OSError:
            pass
        return files, total

    def _cleanup_workspace(
        self,
        *,
        required_bytes: int,
        required_files: int,
    ) -> list[dict[str, object]]:
        """Remove expired/LRU entries from other Sessions until quotas fit."""
        root = self.artifact_root
        now = float(self._clock())
        durable_references = self._durable_artifact_references()
        candidates: list[tuple[float, Path, str, dict[str, object]]] = []
        for manifest_path in root.glob("*/runtime/tool-results/manifest.json"):
            owner = manifest_path.parents[2].name
            if owner == self.session_id:
                continue
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
            if not isinstance(entries, dict):
                continue
            for artifact_id, record in entries.items():
                if not _ARTIFACT_ID.fullmatch(str(artifact_id)) or not isinstance(record, dict):
                    continue
                try:
                    created = float(record.get("created_at", 0.0))
                except (TypeError, ValueError):
                    created = 0.0
                candidates.append((created, manifest_path, str(artifact_id), record))
        candidates.sort(key=lambda item: (item[0], item[2]))
        events: list[dict[str, object]] = []
        for created, manifest_path, artifact_id, record in candidates:
            files, size = self._workspace_usage()
            over_quota = (
                files + required_files > self.max_workspace_files
                or size + required_bytes > self.max_workspace_bytes
            )
            expired = now - created >= self.ttl_seconds
            if not over_quota and not expired:
                continue
            if artifact_id in durable_references:
                continue
            event = self._remove_workspace_entry(
                manifest_path,
                artifact_id,
                record,
                reason="ttl" if expired else "lru-quota",
            )
            if event is not None:
                events.append(event)
                if self._on_cleanup is not None:
                    try:
                        self._on_cleanup(dict(event))
                    except Exception as exc:
                        event["observer_error"] = type(exc).__name__
        return events

    def _remove_workspace_entry(
        self,
        manifest_path: Path,
        artifact_id: str,
        record: dict[str, object],
        *,
        reason: str,
    ) -> dict[str, object] | None:
        filename = str(record.get("filename") or "")
        if filename != f"{artifact_id}.txt":
            return None
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        entries = payload.get("entries", {}) if isinstance(payload, dict) else {}
        if not isinstance(entries, dict) or artifact_id not in entries:
            return None
        target = self._validated_artifact_path(manifest_path.parent / filename)
        try:
            actual_size = target.lstat().st_size
        except FileNotFoundError:
            actual_size = 0
        target.unlink(missing_ok=True)
        entries.pop(artifact_id, None)
        _atomic_write(
            manifest_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        return {
            "event": "artifact.cleaned",
            "artifact_id": artifact_id,
            "reason": reason,
            "size": max(0, int(actual_size)),
        }

    def _manifest_usage(
        self,
        manifest: dict[str, object],
        directory: Path,
    ) -> tuple[int, int]:
        entries = manifest.get("entries", {})
        if not isinstance(entries, dict):
            return 0, 0
        files = 0
        total = 0
        for artifact_id, record in entries.items():
            if not _ARTIFACT_ID.fullmatch(str(artifact_id)) or not isinstance(record, dict):
                continue
            filename = str(record.get("filename") or "")
            if filename != f"{artifact_id}.txt":
                continue
            target = self._validated_artifact_path(directory / filename)
            try:
                info = target.lstat()
            except OSError:
                continue
            if target.is_symlink() or not target.is_file():
                continue
            files += 1
            total += max(0, int(info.st_size))
        return files, total

    def _durable_artifact_references(self) -> set[str]:
        """Collect opaque handles retained by saved Session transcripts."""
        references: set[str] = set()
        candidates = list(self.sessions_root.glob("*.json"))
        candidates.extend(self.artifact_root.glob("*/runtime/transcripts/*"))
        remaining = 16 * 1024 * 1024
        for path in sorted(candidates, key=lambda item: item.as_posix()):
            if remaining <= 0:
                break
            try:
                path.lstat()
                if path.is_symlink() or not path.is_file():
                    continue
                payload = path.read_bytes()[:remaining]
            except OSError:
                continue
            remaining -= len(payload)
            references.update(
                match.decode("ascii")
                for match in re.findall(rb"artifact_[a-f0-9]{32}", payload)
            )
        return references

    def _validated_artifact_path(self, path: Path) -> Path:
        target = path.absolute()
        try:
            target.relative_to(self.artifact_root)
        except ValueError as exc:
            raise ArtifactAccessError("Artifact path escapes its private root") from exc
        return target

    @contextmanager
    def _exclusive_lock(self):
        """Serialize manifest/quota updates across threads and processes."""
        with _LOCK:
            self.artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            harden_private_path(self.artifact_root)
            with exclusive_file_lock(self.lock_path):
                yield


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        harden_private_path(path)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)


def _validated_session_id(value: str) -> str:
    selected = str(value or "")
    if not _SESSION_ID.fullmatch(selected) or selected in {".", ".."}:
        raise ArtifactAccessError("Invalid artifact Session identity")
    return selected


def _validated_artifact_id(value: str) -> str:
    selected = str(value or "")
    if not _ARTIFACT_ID.fullmatch(selected):
        raise ArtifactAccessError("invalid artifact id")
    return selected


def _positive(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


__all__ = [
    "ArtifactAccessError",
    "ArtifactChunk",
    "ArtifactError",
    "ArtifactQuotaError",
    "ArtifactStore",
]
