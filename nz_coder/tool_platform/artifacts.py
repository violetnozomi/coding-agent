"""Opaque, quota-bounded, session-owned model-readable artifacts."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.workspace_paths import WorkspacePathPolicy


_ARTIFACT_ID = re.compile(r"^artifact_[a-f0-9]{32}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_ALLOWED_MODEL_KINDS = frozenset({"tool-result"})
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
    ) -> None:
        self.workspace = Path(workspace).resolve(strict=True)
        self.session_id = _validated_session_id(session_id)
        self.max_result_bytes = _positive(max_result_bytes, "max_result_bytes")
        self.max_session_bytes = _positive(max_session_bytes, "max_session_bytes")
        self.max_session_files = _positive(max_session_files, "max_session_files")
        self.max_workspace_bytes = _positive(max_workspace_bytes, "max_workspace_bytes")
        self.max_workspace_files = _positive(max_workspace_files, "max_workspace_files")
        self.max_read_bytes = _positive(max_read_bytes, "max_read_bytes")
        self.directory = (
            self.workspace
            / ".nz-coder"
            / "sessions"
            / "_artifacts"
            / self.session_id
            / "runtime"
            / "tool-results"
        )
        WorkspacePathPolicy(self.workspace).validate_internal_access(self.directory)
        self.manifest_path = self.directory / "manifest.json"

    def put(self, text: str, *, kind: str) -> str:
        """Atomically persist one allowed model artifact and return an opaque ID."""
        if kind not in _ALLOWED_MODEL_KINDS:
            raise ArtifactAccessError("Model-readable artifact type is not allowed")
        payload = str(text).encode("utf-8")
        if len(payload) > self.max_result_bytes:
            raise ArtifactQuotaError("Artifact exceeds the per-result byte quota")
        with _LOCK:
            manifest = self._load_manifest()
            entries = manifest["entries"]
            session_bytes = sum(int(item.get("size", 0)) for item in entries.values())
            if len(entries) >= self.max_session_files:
                raise ArtifactQuotaError("Artifact Session file quota exceeded")
            if session_bytes + len(payload) > self.max_session_bytes:
                raise ArtifactQuotaError("Artifact Session byte quota exceeded")
            workspace_files, workspace_bytes = self._workspace_usage()
            if workspace_files >= self.max_workspace_files:
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
                "created_at": time.time(),
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
        with _LOCK:
            manifest = self._load_manifest()
            record = manifest["entries"].get(safe_id)
            if not isinstance(record, dict):
                raise ArtifactAccessError("Artifact is not owned by the current Session")
            if record.get("kind") not in _ALLOWED_MODEL_KINDS:
                raise ArtifactAccessError("Model-readable artifact type is not allowed")
            filename = str(record.get("filename") or "")
            if filename != f"{safe_id}.txt":
                raise ArtifactAccessError("Artifact manifest entry is invalid")
            path = WorkspacePathPolicy(self.workspace).validate_internal_access(
                self.directory / filename
            )
            try:
                info = path.lstat()
            except OSError as exc:
                raise ArtifactAccessError("Artifact content is unavailable") from exc
            if path.is_symlink() or not path.is_file():
                raise ArtifactAccessError("Artifact content is not a regular file")
            total = int(info.st_size)
            if offset > total:
                raise ArtifactAccessError("Artifact offset exceeds content length")
            with path.open("rb") as handle:
                handle.seek(offset)
                payload = handle.read(limit)
            next_offset = offset + len(payload)
            return ArtifactChunk(
                payload.decode("utf-8", errors="replace"),
                next_offset,
                next_offset < total,
                total,
            )

    def delete_all(self) -> None:
        """Delete only this Session's known artifact files and manifest."""
        with _LOCK:
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
        root = self.workspace / ".nz-coder" / "sessions" / "_artifacts"
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
                files += len(entries)
                total += sum(
                    max(0, int(item.get("size", 0)))
                    for item in entries.values()
                    if isinstance(item, dict)
                )
        except OSError:
            pass
        return files, total


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
