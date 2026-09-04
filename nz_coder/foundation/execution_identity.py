"""Stable executable and payload identities shared by MCP and LSP launches."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Iterable


class UnsafeExecutionIdentity(ValueError):
    """Raised when a command cannot be identified safely and deterministically."""


@dataclass(frozen=True)
class ExecutionIdentity:
    """Content-bound identity for an interpreter, payload, cwd and argv."""

    command: tuple[str, ...]
    executable_path: Path
    executable_hash: str
    entrypoint_kind: str
    entrypoint_path: Path | None
    entrypoint_module: str
    entrypoint_hash: str
    cwd: Path
    cwd_identity: str
    argv_semantics: tuple[str, ...]
    config_source: str
    environment_profile: str
    workspace: Path
    workspace_controlled: bool
    fingerprint: str

    def public(self) -> dict[str, object]:
        """Return identity metadata without arguments or environment values."""
        return {
            "executable": str(self.executable_path),
            "entrypoint_kind": self.entrypoint_kind,
            "entrypoint_path": str(self.entrypoint_path or ""),
            "entrypoint_module": self.entrypoint_module,
            "cwd": str(self.cwd),
            "config_source": self.config_source,
            "environment_profile": self.environment_profile,
            "workspace_controlled": self.workspace_controlled,
            "fingerprint": self.fingerprint,
        }


def resolve_execution_identity(
    command: Iterable[str],
    *,
    cwd: Path,
    workspace: Path,
    config_source: str,
    environment_profile: str,
) -> ExecutionIdentity:
    """Resolve a supported command into a content-bound execution identity."""
    argv = tuple(str(part) for part in command)
    if not argv or not argv[0]:
        raise UnsafeExecutionIdentity("execution command is empty")
    root = Path(workspace).expanduser().resolve()
    run_cwd = Path(cwd).expanduser().resolve()
    executable = _resolve_path(argv[0], run_cwd, use_path=True)
    executable_hash = _hash_path(executable)
    name = executable.name.lower()
    stem = name.removesuffix(".exe")
    kind = "executable"
    entrypoint: Path | None = None
    module = ""
    semantics: tuple[str, ...] = ("direct", *argv[1:])

    if stem.startswith(("python", "pypy")):
        kind, entrypoint, module, semantics = _python_entrypoint(argv, run_cwd)
    elif stem in {"node", "nodejs"}:
        kind, entrypoint, semantics = _script_entrypoint(
            argv, run_cwd, kind="node-script", inline_flags={"-e", "--eval"},
        )
    elif stem in {"bash", "sh", "dash", "zsh", "ksh"}:
        kind, entrypoint, semantics = _script_entrypoint(
            argv, run_cwd, kind="shell-script", inline_flags={"-c"},
        )
    elif stem in {"pwsh", "powershell"}:
        kind, entrypoint, semantics = _pwsh_entrypoint(argv, run_cwd)
    elif stem == "java":
        kind, entrypoint, semantics = _flagged_entrypoint(
            argv, run_cwd, flag="-jar", kind="java-jar",
        )
    elif stem == "dotnet":
        kind, entrypoint, semantics = _script_entrypoint(
            argv, run_cwd, kind="dotnet-assembly", inline_flags=set(),
        )

    entrypoint_hash = _hash_module_payload(entrypoint, module, run_cwd)
    cwd_identity = _directory_identity(run_cwd)
    workspace_controlled = any(
        _inside(candidate, root)
        for candidate in (executable, entrypoint)
        if candidate is not None
    )
    payload = {
        "command": list(argv),
        "executable_path": str(executable),
        "executable_hash": executable_hash,
        "entrypoint_kind": kind,
        "entrypoint_path": str(entrypoint or ""),
        "entrypoint_module": module,
        "entrypoint_hash": entrypoint_hash,
        "cwd": str(run_cwd),
        "cwd_identity": cwd_identity,
        "argv_semantics": list(semantics),
        "config_source": str(config_source),
        "environment_profile": str(environment_profile),
        "workspace": str(root),
        "workspace_controlled": workspace_controlled,
    }
    fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
    return ExecutionIdentity(
        command=argv,
        executable_path=executable,
        executable_hash=executable_hash,
        entrypoint_kind=kind,
        entrypoint_path=entrypoint,
        entrypoint_module=module,
        entrypoint_hash=entrypoint_hash,
        cwd=run_cwd,
        cwd_identity=cwd_identity,
        argv_semantics=semantics,
        config_source=str(config_source),
        environment_profile=str(environment_profile),
        workspace=root,
        workspace_controlled=workspace_controlled,
        fingerprint=fingerprint,
    )


def verify_execution_identity(
    expected: ExecutionIdentity,
    *,
    workspace: Path | None = None,
) -> ExecutionIdentity:
    """Re-resolve immediately before launch and fail closed on any change.

    This closes ordinary edit/swap races between approval and launch.  It does
    not claim to remove the final OS path-to-exec TOCTOU window.
    """
    actual = resolve_execution_identity(
        expected.command,
        cwd=expected.cwd,
        workspace=workspace or expected.workspace,
        config_source=expected.config_source,
        environment_profile=expected.environment_profile,
    )
    if actual.fingerprint != expected.fingerprint:
        raise UnsafeExecutionIdentity("execution payload changed after trust decision")
    return actual


def _python_entrypoint(
    argv: tuple[str, ...], cwd: Path,
) -> tuple[str, Path | None, str, tuple[str, ...]]:
    args = argv[1:]
    if not args:
        return "executable", None, "", ("direct",)
    if "-c" in args:
        raise UnsafeExecutionIdentity("inline Python commands require explicit advanced trust")
    if "-m" in args:
        index = args.index("-m")
        if index + 1 >= len(args) or not args[index + 1].strip():
            raise UnsafeExecutionIdentity("Python module command has no module")
        module = args[index + 1]
        path = _resolve_python_module(module, cwd)
        if path is None:
            raise UnsafeExecutionIdentity("Python module entrypoint cannot be located")
        return "python-module", path, module, ("module", module, *args[index + 2:])
    path = _first_payload(args, cwd)
    return "python-script", path, "", ("script", str(path), *args[1:])


def _script_entrypoint(
    argv: tuple[str, ...], cwd: Path, *, kind: str, inline_flags: set[str],
) -> tuple[str, Path | None, tuple[str, ...]]:
    args = argv[1:]
    if not args:
        return "executable", None, ("direct",)
    if any(flag in args for flag in inline_flags):
        raise UnsafeExecutionIdentity("inline script commands require explicit advanced trust")
    path = _first_payload(args, cwd)
    return kind, path, ("script", str(path), *args[1:])


def _pwsh_entrypoint(
    argv: tuple[str, ...], cwd: Path,
) -> tuple[str, Path, tuple[str, ...]]:
    lowered = tuple(value.lower() for value in argv[1:])
    if any(value in {"-command", "-c"} for value in lowered):
        raise UnsafeExecutionIdentity("inline PowerShell commands require explicit advanced trust")
    for index, value in enumerate(lowered):
        if value in {"-file", "-f"} and index + 2 <= len(argv[1:]):
            raw = argv[1:][index + 1]
            path = _resolve_path(raw, cwd)
            return "powershell-script", path, ("file", str(path), *argv[index + 3:])
    return _script_entrypoint(
        argv, cwd, kind="powershell-script", inline_flags=set(),
    )


def _flagged_entrypoint(
    argv: tuple[str, ...], cwd: Path, *, flag: str, kind: str,
) -> tuple[str, Path | None, tuple[str, ...]]:
    args = argv[1:]
    if not args:
        return "executable", None, ("direct",)
    if flag not in args:
        raise UnsafeExecutionIdentity(f"{Path(argv[0]).name} command has no stable payload")
    index = args.index(flag)
    if index + 1 >= len(args):
        raise UnsafeExecutionIdentity(f"{flag} has no payload")
    path = _resolve_path(args[index + 1], cwd)
    return kind, path, (flag, str(path), *args[index + 2:])


def _first_payload(args: tuple[str, ...], cwd: Path) -> Path:
    for value in args:
        if not value.startswith("-"):
            return _resolve_path(value, cwd)
    raise UnsafeExecutionIdentity("interpreter command has no stable entrypoint")


def _resolve_python_module(module: str, cwd: Path) -> Path | None:
    relative = Path(*module.split("."))
    candidates = (cwd / relative.with_suffix(".py"), cwd / relative / "__main__.py")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    for raw in sys.path:
        if not raw:
            continue
        base = Path(raw)
        candidates = (base / relative.with_suffix(".py"), base / relative / "__main__.py")
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
    return None


def _resolve_path(value: str, cwd: Path, *, use_path: bool = False) -> Path:
    candidate = Path(value).expanduser()
    if (
        candidate.is_absolute()
        or candidate.parent != Path(".")
        or value.startswith(".")
        or "/" in value
        or "\\" in value
    ):
        return (candidate if candidate.is_absolute() else cwd / candidate).resolve()
    if use_path:
        found = shutil.which(value)
        if found:
            return Path(found).resolve()
        return candidate
    return (cwd / candidate).resolve()


def _hash_module_payload(path: Path | None, module: str, cwd: Path) -> str:
    if path is None:
        return ""
    if module and path.name == "__main__.py":
        package = path.parent
        digest = hashlib.sha256()
        for item in sorted(package.rglob("*.py")):
            if item.is_file() and not item.is_symlink():
                digest.update(item.relative_to(package).as_posix().encode("utf-8"))
                digest.update(b"\0")
                digest.update(bytes.fromhex(_hash_path(item)))
        return digest.hexdigest()
    return _hash_path(path)


def _hash_path(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise UnsafeExecutionIdentity("execution payload cannot be read") from exc


def _directory_identity(path: Path) -> str:
    try:
        metadata = path.stat()
    except OSError as exc:
        raise UnsafeExecutionIdentity("execution cwd cannot be identified") from exc
    return hashlib.sha256(_canonical({
        "path": os.path.normcase(os.path.normpath(str(path))),
        "device": getattr(metadata, "st_dev", None),
        "inode": getattr(metadata, "st_ino", None),
    })).hexdigest()


def _inside(path: Path, root: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


__all__ = [
    "ExecutionIdentity", "UnsafeExecutionIdentity",
    "resolve_execution_identity", "verify_execution_identity",
]
