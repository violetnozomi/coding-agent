"""Unified host/model filesystem capability boundary for one workspace."""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import re
import shlex

from nz_coder.protocol.public_error import PublicInputError

_PRIVATE_DIRECTORIES = frozenset({
    ".git",
    ".hg",
    ".nz-coder",
    ".nz-coder-runs",
    ".ssh",
    ".aws",
    ".azure",
    ".gcloud",
    ".kube",
})
_PRIVATE_FILENAMES = frozenset({
    ".npmrc",
    ".pypirc",
    ".netrc",
    "known_hosts",
    "credentials",
    "credentials.json",
    "kubeconfig",
    "service-account.json",
    "service_account.json",
    "application_default_credentials.json",
})
_PRIVATE_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_PUBLIC_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[/\\]")


class WorkspacePathError(PublicInputError):
    """A requested filesystem capability is outside the allowed boundary."""


class WorkspacePathPolicy:
    """Resolve workspace paths and keep private host state away from models."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise WorkspacePathError("Workspace root must be a directory")

    def validate_model_read(self, path: str | os.PathLike[str]) -> Path:
        return self._validate_model(path, operation="read")

    def validate_model_list(self, path: str | os.PathLike[str]) -> Path:
        return self._validate_model(path, operation="list")

    def validate_model_write(self, path: str | os.PathLike[str]) -> Path:
        return self._validate_model(path, operation="write")

    def validate_model_execute(self, path: str | os.PathLike[str]) -> Path:
        return self._validate_model(path, operation="execute")

    def validate_internal_access(self, path: str | os.PathLike[str]) -> Path:
        """Validate host-owned access; this method is never exposed as a tool flag."""
        return self._resolve_inside(path)

    def is_model_visible(self, path: str | os.PathLike[str]) -> bool:
        try:
            self.validate_model_read(path)
        except (OSError, WorkspacePathError, ValueError):
            return False
        return True

    def _validate_model(self, path: str | os.PathLike[str], *, operation: str) -> Path:
        requested = _portable_path(path)
        reason = _private_path_reason(requested)
        if reason:
            raise WorkspacePathError(
                f"Model access blocked for {requested or '.'}: {reason}"
            )
        resolved = self._resolve_inside(requested)
        # A symlink may hide a private basename in the resolved target inside
        # the workspace. Check the canonical relative form as well.
        relative = resolved.relative_to(self.root).as_posix()
        reason = _private_path_reason(relative)
        if reason:
            raise WorkspacePathError(
                f"Model access blocked for {requested or '.'}: {reason}"
            )
        return resolved

    def _resolve_inside(self, path: str | os.PathLike[str]) -> Path:
        requested = _portable_path(path)
        if _WINDOWS_ABSOLUTE.match(requested) and os.name != "nt":
            raise WorkspacePathError(f"Path escapes workspace: {requested}")
        raw = Path(requested or ".").expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise WorkspacePathError(f"Path escapes workspace: {requested}") from exc

        # ``resolve(strict=False)`` follows every existing ancestor. Recheck
        # the nearest existing ancestor to make the nonexistent-write contract
        # explicit and portable across pathlib implementations.
        ancestor = candidate
        while not ancestor.exists() and ancestor != ancestor.parent:
            ancestor = ancestor.parent
        try:
            ancestor.resolve(strict=True).relative_to(self.root)
        except (OSError, ValueError) as exc:
            raise WorkspacePathError(f"Path escapes workspace: {requested}") from exc
        return resolved


def _portable_path(path: str | os.PathLike[str]) -> str:
    value = os.fspath(path)
    if not isinstance(value, str):
        value = os.fsdecode(value)
    value = value.strip()
    if "\x00" in value:
        raise WorkspacePathError("Path contains a null byte")
    # Treat alternate separators as separators on every platform so a policy
    # decision cannot change when a request is replayed on Windows.
    return value.replace("\\", "/")


def _private_path_reason(path: str) -> str:
    normalized = str(path or ".").replace("\\", "/")
    parts = tuple(part.lower() for part in PurePosixPath(normalized).parts if part not in {"", "/", "."})
    if any(part in _PRIVATE_DIRECTORIES for part in parts):
        return "private workspace metadata is host-only"
    name = parts[-1] if parts else ""
    if name.startswith(".env") and name not in _PUBLIC_ENV_TEMPLATES:
        return "environment and credential files are host-only"
    if name in _PRIVATE_FILENAMES or name.startswith("id_rsa") or name.startswith("id_dsa") or name.startswith("id_ed25519"):
        return "credential-like files are host-only"
    if name.endswith(_PRIVATE_SUFFIXES):
        return "private-key-like files are host-only"
    if "credential" in name or "service-account" in name or "service_account" in name:
        return "credential-like files are host-only"
    return ""


def model_command_private_path(command: str, workspace: Path) -> str | None:
    """Return the first explicit private path referenced by a shell command.

    This is a defense-in-depth lexical gate, not an OS sandbox. It prevents
    known read commands and obvious interpreter snippets from naming host-only
    workspace state while the permission layer still governs shell execution.
    """
    text = str(command or "")
    policy = WorkspacePathPolicy(workspace)
    candidates: list[str] = []
    for segment in re.split(r"(?:&&|\|\||[;|&\n])", text):
        try:
            tokens = shlex.split(segment, posix=os.name != "nt")
        except ValueError:
            tokens = segment.split()
        for token in tokens[1:]:
            value = token.strip("\"'()[]{};,`")
            if "=" in value and not value.startswith(("/", "\\")):
                value = value.split("=", 1)[1]
            if value and not value.startswith("-") and "://" not in value:
                candidates.append(value)
    # Also catch direct path literals embedded in interpreter expressions.
    candidates.extend(re.findall(
        r"(?:[A-Za-z]:)?(?:^|[\"'=(\s])((?:\.{0,2}[/\\])?(?:\.env(?:\.[\w-]+)?|\.git|\.nz-coder|\.ssh|\.aws|\.azure|\.gcloud|\.kube)(?:[/\\][^\"')\s;&|]*)?)",
        text,
        flags=re.IGNORECASE,
    ))
    for candidate in candidates:
        try:
            policy.validate_model_read(candidate)
        except WorkspacePathError as exc:
            if "Model access blocked" in str(exc):
                return candidate
    return None


__all__ = [
    "WorkspacePathError",
    "WorkspacePathPolicy",
    "model_command_private_path",
]
