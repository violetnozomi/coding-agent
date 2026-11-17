"""Unified host/model filesystem capability boundary for one workspace."""
from __future__ import annotations

import os
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
import re

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
        try:
            self.root = Path(root).expanduser().resolve(strict=True)
            if not self.root.is_dir():
                raise WorkspacePathError("Workspace root must be a directory")
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError("Cannot resolve workspace root.") from exc

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
        try:
            raw = Path(requested or ".").expanduser()
            candidate = raw if raw.is_absolute() else self.root / raw
            resolved = candidate.resolve(strict=False)
            if not resolved.is_relative_to(self.root):
                raise WorkspacePathError(f"Path escapes workspace: {requested}")
            ancestor = candidate
            while not ancestor.exists() and ancestor != ancestor.parent:
                ancestor = ancestor.parent
            if not ancestor.resolve(strict=True).is_relative_to(self.root):
                raise WorkspacePathError(f"Path escapes workspace: {requested}")
        except WorkspacePathError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            # 原异常的 filename 可能是整段程序或凭据，公开原因不能复制它。
            raise WorkspacePathError("Cannot resolve workspace path.") from exc

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
    from nz_coder.foundation.file_kinds import is_source_file

    if not is_source_file(name) and ("credential" in name or "service-account" in name or "service_account" in name):
        return "credential-like files are host-only"
    return ""


def private_program_literal(source: str) -> str | None:
    """未知解释器只保留明确私有字面量保护，不对整段源码做文件系统操作。"""
    for match in re.finditer(r"(['\"])(.*?)\1", source, flags=re.DOTALL):
        if _private_path_reason(match[2]):
            return match[2]
    return None


def model_command_private_path(command: str, workspace: Path, *, command_cwd: Path | None = None, _depth: int = 0) -> str | None:
    """Return the first explicit private path referenced by a shell command.

    This is a defense-in-depth lexical gate, not an OS sandbox. It prevents
    known read commands and obvious interpreter snippets from naming host-only
    workspace state while the permission layer still governs shell execution.
    """
    from nz_coder.foundation.shell_syntax import (
        ShellSyntaxError, file_operands, inline_source, python_effects, shell_commands,
    )
    if _depth > 8:
        raise ShellSyntaxError("shell analysis nesting limit")
    policy = WorkspacePathPolicy(workspace)
    candidates = []
    for segment in shell_commands(command):
        candidates.extend(token.value for token in file_operands(segment))
        candidates.extend(token.value for operator, token in segment.redirects
                          if token.heredoc is None and not operator.endswith("&"))
        program = inline_source(segment)
        if program:
            if program[0] == "python":
                effects = python_effects(program[1])
                candidates.extend(effects.files)
                for child in effects.commands:
                    nested = model_command_private_path(child, workspace, command_cwd=command_cwd, _depth=_depth + 1)
                    if nested:
                        return nested
            elif program[0] == "shell":
                nested = model_command_private_path(program[1], workspace, command_cwd=command_cwd, _depth=_depth + 1)
                if nested:
                    return nested
            else:
                literal = private_program_literal(program[1])
                if literal:
                    return literal
    for candidate in candidates:
        try:
            if any(part.startswith(".") and any(char in part for char in "*?[")
                   and any(fnmatchcase(private, part.lower()) for private in _PRIVATE_DIRECTORIES)
                   for part in PurePosixPath(_portable_path(candidate)).parts):
                return candidate
            resolved_candidate = (command_cwd / candidate) if command_cwd is not None else candidate
            policy.validate_model_read(resolved_candidate)
        except WorkspacePathError as exc:
            if "Model access blocked" in str(exc):
                return candidate
            if "escapes workspace" not in str(exc):
                raise

    return None


__all__ = [
    "WorkspacePathError",
    "WorkspacePathPolicy",
    "model_command_private_path",
]
