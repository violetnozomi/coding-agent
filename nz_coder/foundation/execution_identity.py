"""Stable executable and payload identities shared by MCP and LSP launches."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Iterable

from nz_coder.foundation.secret_values import secret_str


class UnsafeExecutionIdentity(ValueError):
    """Raised when a command cannot be identified safely and deterministically."""


_MAX_SINGLE_PAYLOAD_BYTES = 8 * 1024 * 1024
_MAX_PAYLOAD_FILES = 512
_MAX_TOTAL_PAYLOAD_BYTES = 32 * 1024 * 1024
_MAX_PAYLOAD_DEPTH = 16


@dataclass(frozen=True)
class ExecutionIdentity:
    """Content-bound identity for an interpreter, payload, cwd and argv."""

    command: tuple[str, ...] = field(repr=False)
    executable_path: Path
    executable_hash: str
    entrypoint_kind: str
    entrypoint_path: Path | None
    entrypoint_module: str
    entrypoint_hash: str
    cwd: Path
    cwd_identity: str
    argv_semantics: tuple[str, ...] = field(repr=False)
    config_source: str
    environment_profile: str
    workspace: Path
    workspace_controlled: bool
    fingerprint: str

    def __post_init__(self) -> None:
        """Keep argv executable while redacting generic diagnostic projections."""
        object.__setattr__(self, "command", tuple(secret_str(value) for value in self.command))
        object.__setattr__(
            self,
            "argv_semantics",
            tuple(secret_str(value) for value in self.argv_semantics),
        )

    def __repr__(self) -> str:
        return (
            "ExecutionIdentity("
            f"executable={self.executable_path.name!r}, "
            f"entrypoint_kind={self.entrypoint_kind!r}, "
            f"workspace_controlled={self.workspace_controlled!r}, "
            f"fingerprint={self.fingerprint[:12]!r})"
        )

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
    executable_hash = _hash_path(executable, allow_symlink=True)
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
        kind, entrypoint, semantics = _dotnet_entrypoint(argv, run_cwd)

    payloads = _explicit_payloads(argv, run_cwd, stem)
    if entrypoint is not None and entrypoint not in payloads:
        payloads.append(entrypoint)
    payload_hashes = tuple(
        (str(path), _hash_module_payload(path, module if path == entrypoint else "", run_cwd))
        for path in payloads
    )
    entrypoint_hash = hashlib.sha256(_canonical(payload_hashes)).hexdigest()
    cwd_identity = _directory_identity(run_cwd)
    workspace_controlled = any(
        _inside(candidate, root)
        for candidate in (executable, entrypoint, *payloads)
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
        "explicit_payloads": payload_hashes,
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
    index = 0
    while index < len(args):
        value = args[index]
        if value == "-c":
            raise UnsafeExecutionIdentity("inline Python commands require explicit advanced trust")
        if value == "-m":
            if index + 1 >= len(args) or not args[index + 1].strip():
                raise UnsafeExecutionIdentity("Python module command has no module")
            module = args[index + 1]
            path = _resolve_python_module(module, cwd)
            if path is None:
                raise UnsafeExecutionIdentity("Python module entrypoint cannot be located")
            return "python-module", path, module, ("module", module, *args[index + 2:])
        if value in {"-W", "-X"}:
            index = _consume_option(args, index, value)
            continue
        if value.startswith(("-W", "-X")) and len(value) > 2:
            index += 1
            continue
        if value in {"-B", "-d", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v", "-V"}:
            index += 1
            continue
        if value == "--":
            index += 1
            break
        if value.startswith("-"):
            raise UnsafeExecutionIdentity(f"unsupported Python option: {value}")
        break
    if index >= len(args):
        raise UnsafeExecutionIdentity("interpreter command has no stable entrypoint")
    path = _resolve_payload_path(args[index], cwd)
    return "python-script", path, "", ("script", str(path), *args[index + 1:])


def _script_entrypoint(
    argv: tuple[str, ...], cwd: Path, *, kind: str, inline_flags: set[str],
) -> tuple[str, Path | None, tuple[str, ...]]:
    args = argv[1:]
    if not args:
        return "executable", None, ("direct",)
    if any(flag in args for flag in inline_flags):
        raise UnsafeExecutionIdentity("inline script commands require explicit advanced trust")
    index = 0
    option_arity = {"-O": 1, "-o": 1} if kind == "shell-script" else {}
    while index < len(args):
        value = args[index]
        if value in inline_flags:
            raise UnsafeExecutionIdentity("inline script commands require explicit advanced trust")
        if value in option_arity:
            index = _consume_option(args, index, value)
            continue
        if kind == "node-script" and value in {"--require", "-r", "--loader", "--import"}:
            index = _consume_option(args, index, value)
            continue
        if kind == "node-script" and value.startswith(("--require=", "--loader=", "--import=")):
            index += 1
            continue
        if kind == "shell-script" and value.startswith(("-O", "-o")) and len(value) > 2:
            index += 1
            continue
        if value == "--":
            index += 1
            break
        if value.startswith("-"):
            if kind == "node-script" and value in {
                "--no-warnings", "--enable-source-maps", "--experimental-modules",
            }:
                index += 1
                continue
            if kind == "shell-script" and all(ch in "abefhkmnptuvxBCEHPT" for ch in value[1:]):
                index += 1
                continue
            raise UnsafeExecutionIdentity(f"unsupported interpreter option: {value}")
        break
    if index >= len(args):
        raise UnsafeExecutionIdentity("interpreter command has no stable entrypoint")
    path = _resolve_payload_path(args[index], cwd)
    return kind, path, ("script", str(path), *args[index + 1:])


def _pwsh_entrypoint(
    argv: tuple[str, ...], cwd: Path,
) -> tuple[str, Path, tuple[str, ...]]:
    lowered = tuple(value.lower() for value in argv[1:])
    if any(value in {"-command", "-c"} for value in lowered):
        raise UnsafeExecutionIdentity("inline PowerShell commands require explicit advanced trust")
    args = argv[1:]
    index = 0
    while index < len(args):
        value = lowered[index]
        if value in {"-command", "-c"}:
            raise UnsafeExecutionIdentity("inline PowerShell commands require explicit advanced trust")
        if value in {"-executionpolicy", "-ep", "-inputformat", "-outputformat", "-windowstyle"}:
            index = _consume_option(args, index, args[index])
            continue
        if value in {"-nologo", "-noprofile", "-noninteractive", "-sta", "-mta"}:
            index += 1
            continue
        if value in {"-file", "-f"}:
            if index + 1 >= len(args):
                raise UnsafeExecutionIdentity("PowerShell -File has no payload")
            path = _resolve_payload_path(args[index + 1], cwd)
            return "powershell-script", path, ("file", str(path), *args[index + 2:])
        if value.startswith("-"):
            raise UnsafeExecutionIdentity(f"unsupported PowerShell option: {args[index]}")
        path = _resolve_payload_path(args[index], cwd)
        return "powershell-script", path, ("file", str(path), *args[index + 1:])
    raise UnsafeExecutionIdentity("PowerShell command has no stable entrypoint")


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
    path = _resolve_payload_path(args[index + 1], cwd)
    return kind, path, (flag, str(path), *args[index + 2:])


def _dotnet_entrypoint(
    argv: tuple[str, ...], cwd: Path,
) -> tuple[str, Path, tuple[str, ...]]:
    args = argv[1:]
    index = 1 if args and args[0] == "exec" else 0
    value_options = {
        "--additional-deps", "--additionalprobingpath", "--depsfile",
        "--fx-version", "--roll-forward", "--runtimeconfig",
    }
    flag_options = {"--disable-diagnostic-port", "--additionalprobingpath"}
    while index < len(args):
        value = args[index]
        if value in value_options:
            index = _consume_option(args, index, value)
            continue
        if any(value.startswith(f"{option}=") for option in value_options):
            index += 1
            continue
        if value in flag_options:
            index += 1
            continue
        if value.startswith("-"):
            raise UnsafeExecutionIdentity(f"unsupported dotnet option: {value}")
        path = _resolve_payload_path(value, cwd)
        if path.suffix.lower() != ".dll":
            raise UnsafeExecutionIdentity("dotnet command has no stable assembly payload")
        return "dotnet-assembly", path, ("assembly", str(path), *args[index + 1:])
    raise UnsafeExecutionIdentity("dotnet command has no stable assembly payload")


def _consume_option(args: tuple[str, ...], index: int, option: str) -> int:
    if index + 1 >= len(args):
        raise UnsafeExecutionIdentity(f"{option} requires an argument")
    return index + 2


def _explicit_payloads(argv: tuple[str, ...], cwd: Path, stem: str) -> list[Path]:
    """Return every explicit code-bearing hook in addition to the main entrypoint."""
    if stem not in {"node", "nodejs"}:
        return []
    args = argv[1:]
    result: list[Path] = []
    index = 0
    while index < len(args):
        value = args[index]
        if value in {"-e", "--eval"}:
            raise UnsafeExecutionIdentity("inline script commands require explicit advanced trust")
        matched = next(
            (flag for flag in ("--require", "-r", "--loader", "--import") if value == flag),
            None,
        )
        if matched:
            if index + 1 >= len(args):
                raise UnsafeExecutionIdentity(f"{matched} requires a payload")
            result.append(_resolve_payload_path(args[index + 1], cwd))
            index += 2
            continue
        inline = next(
            (flag for flag in ("--require=", "--loader=", "--import=") if value.startswith(flag)),
            None,
        )
        if inline:
            result.append(_resolve_payload_path(value[len(inline):], cwd))
            index += 1
            continue
        if value.startswith("-"):
            if value in {"--no-warnings", "--enable-source-maps", "--experimental-modules"}:
                index += 1
                continue
            raise UnsafeExecutionIdentity(f"unsupported Node option: {value}")
        result.append(_resolve_payload_path(value, cwd))
        break
    if not result:
        raise UnsafeExecutionIdentity("interpreter command has no stable entrypoint")
    return result


def _resolve_python_module(module: str, cwd: Path) -> Path | None:
    relative = Path(*module.split("."))
    candidates = (cwd / relative.with_suffix(".py"), cwd / relative / "__main__.py")
    for candidate in candidates:
        if candidate.is_file():
            return _resolve_payload_path(str(candidate), cwd)
    for raw in sys.path:
        if not raw:
            continue
        base = Path(raw)
        candidates = (base / relative.with_suffix(".py"), base / relative / "__main__.py")
        for candidate in candidates:
            if candidate.is_file():
                return _resolve_payload_path(str(candidate), cwd)
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


def _resolve_payload_path(value: str, cwd: Path) -> Path:
    candidate = Path(value).expanduser()
    lexical = candidate if candidate.is_absolute() else cwd / candidate
    try:
        cursor = lexical
        boundary = cwd.resolve()
        while cursor != cursor.parent:
            if cursor.is_symlink():
                raise UnsafeExecutionIdentity("execution payload symlink is not allowed")
            if cursor == boundary:
                break
            cursor = cursor.parent
        resolved = lexical.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UnsafeExecutionIdentity("execution payload cannot be located") from exc
    return resolved


def _hash_module_payload(path: Path | None, module: str, cwd: Path) -> str:
    if path is None:
        return ""
    if module and path.name == "__main__.py":
        package = path.parent
        digest = hashlib.sha256()
        count = 0
        total = 0
        stack = [(package, 0)]
        records: list[tuple[str, str]] = []
        while stack:
            directory, depth = stack.pop()
            if depth > _MAX_PAYLOAD_DEPTH:
                raise UnsafeExecutionIdentity("execution payload traversal depth exceeded")
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        raise UnsafeExecutionIdentity("execution payload symlink is not allowed")
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), depth + 1))
                    elif entry.is_file(follow_symlinks=False) and entry.name.endswith(".py"):
                        count += 1
                        if count > _MAX_PAYLOAD_FILES:
                            raise UnsafeExecutionIdentity("execution payload file budget exceeded")
                        item = Path(entry.path)
                        size = item.stat().st_size
                        total += size
                        if total > _MAX_TOTAL_PAYLOAD_BYTES:
                            raise UnsafeExecutionIdentity("execution payload byte budget exceeded")
                        records.append((item.relative_to(package).as_posix(), _hash_path(item)))
        for relative, item_hash in sorted(records):
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(item_hash))
        return digest.hexdigest()
    return _hash_path(path)


def _hash_path(path: Path, *, allow_symlink: bool = False) -> str:
    try:
        if path.is_symlink() and not allow_symlink:
            raise UnsafeExecutionIdentity("execution payload symlink is not allowed")
        if not path.is_file():
            return ""
        if not allow_symlink and path.stat().st_size > _MAX_SINGLE_PAYLOAD_BYTES:
            raise UnsafeExecutionIdentity("execution payload file budget exceeded")
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
