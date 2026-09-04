"""Load bounded global and project instructions for each model request.

This mirrors InfCode's durable instruction surface: root ``AGENTS.md`` and
``CLAUDE.md`` files plus first-level Markdown rules.  It is intentionally
separate from semantic memory: instruction files are authoritative project
context, while recalled memories are fallible background notes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from nz_coder.foundation.workspace_file_access import (
    FixedFileAccess,
    WorkspaceFileIdentity,
)


PER_SOURCE_MAX_BYTES = 20 * 1024
TOTAL_MAX_BYTES = 32 * 1024
_PER_FILE_NOTICE = (
    "[NZ-Coder notice: This rule file was truncated due to the per-file size limit.]"
)
_TOTAL_TRUNCATED_NOTICE = (
    "[NZ-Coder notice: This rule file was truncated due to the cumulative rules size limit.]"
)
_TOTAL_OMITTED_NOTICE = (
    "[NZ-Coder notice: This rule file was omitted due to the cumulative rules size limit.]"
)
_TRACKED_CACHE: dict[tuple[str, str, tuple[int, int, int]], bool] = {}
_TRACKED_LOCK = threading.Lock()
_TRACKED_CACHE_MAX_ENTRIES = 2048
_STATE_LOCK = threading.RLock()
_STATE_FILENAME = "instruction-file-state.json"
_STATE_MAX_BYTES = 64_000
INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md")
INSTRUCTION_SCOPES = ("global", "project")


@dataclass(frozen=True)
class InstructionFileInfo:
    """One root instruction file exposed through the control plane."""

    id: str
    scope: str
    filename: str
    path: str
    enabled: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "scope": self.scope,
            "filename": self.filename,
            "path": self.path,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class InstructionFileWarning:
    """Non-fatal instruction control-plane warning."""

    path: str
    message: str

    def as_dict(self) -> dict:
        return {"path": self.path, "message": self.message}


@dataclass(frozen=True)
class InstructionFileListResult:
    """Existing root instruction files and state-loading warnings."""

    files: tuple[InstructionFileInfo, ...]
    warnings: tuple[InstructionFileWarning, ...]

    def as_dict(self) -> dict:
        return {
            "files": [item.as_dict() for item in self.files],
            "warnings": [item.as_dict() for item in self.warnings],
        }


@dataclass(frozen=True)
class InstructionSource:
    """One discovered instruction source with deterministic budget priority."""

    path: Path
    scope: str
    kind: str
    order: float
    priority: int


@dataclass(frozen=True)
class InstructionBundle:
    """Rendered instructions plus observable budget metadata."""

    reminder: str
    source_count: int
    included_count: int
    truncated_count: int
    per_file_truncated_count: int
    total_truncated_count: int
    omitted_count: int
    included_bytes: int
    paths: tuple[str, ...]
    disabled_count: int = 0
    warnings: tuple[str, ...] = ()


def _validate_scope(scope: str) -> str:
    if scope not in INSTRUCTION_SCOPES:
        raise ValueError("instruction scope must be 'global' or 'project'")
    return scope


def _validate_filename(filename: str) -> str:
    if filename not in INSTRUCTION_FILENAMES:
        raise ValueError("instruction filename must be AGENTS.md or CLAUDE.md")
    return filename


def _roots(
    workspace: str | Path,
    home: str | Path | None,
) -> tuple[Path, Path]:
    project = Path(workspace).resolve()
    user_home = (
        Path(home).expanduser().resolve()
        if home is not None
        else Path.home().resolve()
    )
    return project, user_home / ".config" / "nz-coder"


def _instruction_file_path(
    workspace: str | Path,
    scope: str,
    filename: str,
    *,
    home: str | Path | None = None,
) -> Path:
    selected_scope = _validate_scope(scope)
    selected_filename = _validate_filename(filename)
    project, global_root = _roots(workspace, home)
    root = global_root if selected_scope == "global" else project
    return root / selected_filename


def _instruction_state_path(
    workspace: str | Path,
    scope: str,
    *,
    home: str | Path | None = None,
) -> Path:
    selected_scope = _validate_scope(scope)
    project, global_root = _roots(workspace, home)
    root = global_root if selected_scope == "global" else project / ".nz-coder"
    return root / _STATE_FILENAME


def _instruction_access(
    workspace: str | Path,
    scope: str,
    *,
    home: str | Path | None,
    create_root: bool = False,
) -> tuple[FixedFileAccess | None, str, str, Path]:
    selected_scope = _validate_scope(scope)
    project, global_root = _roots(workspace, home)
    root = global_root if selected_scope == "global" else project
    is_junction = getattr(root, "is_junction", lambda: False)
    if root.is_symlink() or is_junction():
        raise ValueError("Instruction control root is unsafe")
    if selected_scope == "global":
        available = _ensure_global_instruction_root(
            root.parents[1], create=create_root,
        )
        if not available:
            return None, "", "", root
    elif create_root:
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        return None, "", "", root
    state = _STATE_FILENAME if selected_scope == "global" else f".nz-coder/{_STATE_FILENAME}"
    allowed = (*INSTRUCTION_FILENAMES, state)
    return FixedFileAccess(root, allowed), state, selected_scope, root


def _ensure_global_instruction_root(home: Path, *, create: bool) -> bool:
    """Reach ``~/.config/nz-coder`` without following redirected components."""
    parts = (".config", "nz-coder")
    if not home.exists():
        if not create:
            return False
        home.mkdir(parents=True, mode=0o700)
    if home.is_symlink() or not home.is_dir():
        raise ValueError("Instruction control root is unsafe")
    if os.name == "nt":
        from nz_coder.foundation.project_control import (
            UnsafeProjectControl,
            _windows_close,
            _windows_open,
        )

        handles: list[int] = []
        try:
            parent = _windows_open(home, directory=True)
            assert parent is not None
            handles.append(parent)
            cursor = home
            for part in parts:
                cursor /= part
                try:
                    child = _windows_open(
                        cursor, directory=True, missing_ok=not create, parent=parent,
                    )
                except FileNotFoundError:
                    child = None
                if child is None:
                    if not create:
                        return False
                    cursor.mkdir(mode=0o700)
                    child = _windows_open(cursor, directory=True, parent=parent)
                assert child is not None
                handles.append(child)
                parent = child
            return True
        except (OSError, UnsafeProjectControl) as exc:
            raise ValueError("Instruction control root is unsafe") from exc
        finally:
            for handle in reversed(handles):
                _windows_close(handle)

    flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(home, flags)
    try:
        for part in parts:
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    return False
                os.mkdir(part, 0o700, dir_fd=descriptor)
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return True
    except OSError as exc:
        raise ValueError("Instruction control root is unsafe") from exc
    finally:
        os.close(descriptor)


def _read_enabled_state(
    access: FixedFileAccess | None, relative: str,
) -> tuple[dict[str, bool], str | None, WorkspaceFileIdentity]:
    if access is None:
        return {}, None, WorkspaceFileIdentity.missing()
    try:
        raw, identity = access.read_text_with_identity(
            relative, maximum=_STATE_MAX_BYTES,
        )
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("state must be a version 1 object")
        raw_enabled = payload.get("enabled", {})
        if not isinstance(raw_enabled, dict):
            raise ValueError("enabled state must be an object")
        enabled: dict[str, bool] = {}
        for filename, value in raw_enabled.items():
            if filename not in INSTRUCTION_FILENAMES or not isinstance(value, bool):
                raise ValueError("enabled state contains an invalid entry")
            enabled[filename] = value
        return enabled, None, identity
    except FileNotFoundError:
        return {}, None, WorkspaceFileIdentity.missing()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return (
            {}, "Failed to load instruction file enabled state safely",
            WorkspaceFileIdentity.missing(),
        )


def _write_enabled_state(
    access: FixedFileAccess,
    relative: str,
    enabled: dict[str, bool],
    expected: WorkspaceFileIdentity,
) -> None:
    payload = json.dumps(
        {"version": 1, "enabled": enabled},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    access.write_text(relative, payload, expected=expected)


def _delete_enabled_row(
    access: FixedFileAccess, state_relative: str, filename: str,
) -> None:
    with _STATE_LOCK:
        enabled, warning, identity = _read_enabled_state(access, state_relative)
        if warning:
            # A corrupt two-key state file is recoverable. Replacing it avoids
            # making create/delete permanently unusable.
            enabled = {}
        enabled.pop(filename, None)
        if not enabled:
            if identity.expected_exists:
                access.delete(state_relative, expected=identity)
            return
        _write_enabled_state(access, state_relative, enabled, identity)


def list_instruction_files(
    workspace: str | Path,
    scope: str = "project",
    *,
    home: str | Path | None = None,
) -> InstructionFileListResult:
    """List existing root AGENTS.md/CLAUDE.md files and enabled state."""
    selected_scope = _validate_scope(scope)
    access, state_relative, _scope, root = _instruction_access(
        workspace, selected_scope, home=home,
    )
    state_path = _instruction_state_path(workspace, selected_scope, home=home)
    with _STATE_LOCK:
        enabled, warning, _identity = _read_enabled_state(access, state_relative)
    files: list[InstructionFileInfo] = []
    warnings: list[InstructionFileWarning] = []
    if warning:
        warnings.append(InstructionFileWarning(str(state_path), warning))
    for filename in INSTRUCTION_FILENAMES:
        path = _instruction_file_path(
            workspace,
            selected_scope,
            filename,
            home=home,
        )
        try:
            if access is None:
                continue
            access.stat(filename)
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            warnings.append(InstructionFileWarning(
                str(root / filename), "Instruction file is not a safe regular file",
            ))
            continue
        files.append(InstructionFileInfo(
            id=f"{selected_scope}:{filename}",
            scope=selected_scope,
            filename=filename,
            path=str(path),
            enabled=enabled.get(filename, True),
        ))
    return InstructionFileListResult(tuple(files), tuple(warnings))


def set_instruction_file_enabled(
    workspace: str | Path,
    scope: str,
    filename: str,
    enabled: bool,
    *,
    home: str | Path | None = None,
) -> InstructionFileInfo:
    """Persist one root instruction file's enabled state atomically."""
    selected_scope = _validate_scope(scope)
    selected_filename = _validate_filename(filename)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    access, state_relative, _scope, _root = _instruction_access(
        workspace, selected_scope, home=home, create_root=True,
    )
    assert access is not None
    with _STATE_LOCK:
        state, warning, identity = _read_enabled_state(access, state_relative)
        if warning:
            raise ValueError(warning)
        try:
            access.stat(selected_filename)
        except FileNotFoundError:
            pass
        state[selected_filename] = enabled
        _write_enabled_state(access, state_relative, state, identity)
    path = _instruction_file_path(
        workspace,
        selected_scope,
        selected_filename,
        home=home,
    )
    return InstructionFileInfo(
        id=f"{selected_scope}:{selected_filename}",
        scope=selected_scope,
        filename=selected_filename,
        path=str(path),
        enabled=enabled,
    )


def create_instruction_file(
    workspace: str | Path,
    scope: str = "project",
    *,
    home: str | Path | None = None,
) -> InstructionFileInfo:
    """Create the scope's AGENTS.md exclusively and reset its state row."""
    selected_scope = _validate_scope(scope)
    filename = "AGENTS.md"
    path = _instruction_file_path(
        workspace,
        selected_scope,
        filename,
        home=home,
    )
    access, state_relative, _scope, _root = _instruction_access(
        workspace, selected_scope, home=home, create_root=True,
    )
    assert access is not None
    access.write_text(
        filename, "", expected=WorkspaceFileIdentity.missing(), overwrite=False,
    )
    try:
        _delete_enabled_row(access, state_relative, filename)
    except Exception:
        try:
            _text, identity = access.read_text_with_identity(filename)
            access.delete(filename, expected=identity)
        except Exception:
            pass
        raise
    return InstructionFileInfo(
        id=f"{selected_scope}:{filename}",
        scope=selected_scope,
        filename=filename,
        path=str(path),
        enabled=True,
    )


def delete_instruction_file(
    workspace: str | Path,
    scope: str,
    filename: str,
    *,
    home: str | Path | None = None,
) -> None:
    """Delete one supported root instruction file and its enabled row."""
    selected_scope = _validate_scope(scope)
    selected_filename = _validate_filename(filename)
    access, state_relative, _scope, _root = _instruction_access(
        workspace, selected_scope, home=home,
    )
    if access is None:
        return
    try:
        _text, identity = access.read_text_with_identity(selected_filename)
    except FileNotFoundError:
        identity = WorkspaceFileIdentity.missing()
    if identity.expected_exists:
        access.delete(selected_filename, expected=identity)
    _delete_enabled_row(access, state_relative, selected_filename)


def _rule_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return sorted(
            path for path in root.iterdir()
            if path.is_file() and path.suffix.lower() == ".md"
        )
    except OSError:
        return []


def discover_instruction_sources(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
) -> list[InstructionSource]:
    """Discover the same global/project instruction classes used by InfCode."""
    sources, _warnings, _disabled_count = _discover_instruction_sources_result(
        workspace,
        home=home,
    )
    return sources


def _discover_instruction_sources_result(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
) -> tuple[list[InstructionSource], tuple[str, ...], int]:
    project, global_root = _roots(workspace, home)

    result: list[InstructionSource] = []
    for index, path in enumerate(_rule_files(global_root / "rules")):
        result.append(InstructionSource(path, "global", "rule", index / 1000, 10))
    warnings: list[str] = []
    disabled_count = 0
    for listed in (
        list_instruction_files(project, "global", home=home),
        list_instruction_files(project, "project", home=home),
    ):
        warnings.extend(item.message for item in listed.warnings)
        for item in listed.files:
            if not item.enabled:
                disabled_count += 1
                continue
            if item.scope == "global":
                order, priority = (
                    (1.0, 20) if item.filename == "CLAUDE.md" else (2.0, 30)
                )
            else:
                order, priority = (
                    (4.0, 50) if item.filename == "CLAUDE.md" else (5.0, 60)
                )
            kind = "claude" if item.filename == "CLAUDE.md" else "agents"
            result.append(InstructionSource(
                Path(item.path), item.scope, kind, order, priority
            ))

    for index, path in enumerate(_rule_files(project / ".nz-coder" / "rules")):
        result.append(InstructionSource(path, "project", "rule", 3.0 + index / 1000, 40))
    return (
        sorted(result, key=lambda item: (item.order, str(item.path))),
        tuple(warnings),
        disabled_count,
    )


def _decode_prefix(data: bytes, max_bytes: int) -> tuple[str, int, bool]:
    selected = data[:max(0, max_bytes)]
    while selected:
        try:
            return selected.decode("utf-8"), len(selected), len(selected) < len(data)
        except UnicodeDecodeError as error:
            selected = selected[:error.start]
    return "", 0, bool(data)


def _strip_rule_frontmatter(text: str) -> str:
    """Strip recognized rule metadata without treating ordinary `---` as YAML."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    try:
        close = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return text
    metadata = "\n".join(lines[1:close]).lower()
    if not any(f"{key}:" in metadata for key in ("name", "description", "trigger")):
        return text
    return "\n".join(lines[close + 1:])


def _read_source(source: InstructionSource) -> bytes:
    try:
        data = source.path.read_bytes()
    except OSError:
        return b""
    if source.kind != "rule":
        return data
    text = _strip_rule_frontmatter(data.decode("utf-8", errors="replace"))
    return text.encode("utf-8")


def _enabled_from_snapshot(
    data: bytes | None,
) -> tuple[dict[str, bool], str | None]:
    if data is None:
        return {}, None
    if len(data) > _STATE_MAX_BYTES:
        return {}, "Failed to load instruction file enabled state: state exceeds limit"
    try:
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("state must be a version 1 object")
        raw_enabled = payload.get("enabled", {})
        if not isinstance(raw_enabled, dict):
            raise ValueError("enabled state must be an object")
        enabled: dict[str, bool] = {}
        for filename, value in raw_enabled.items():
            if filename not in INSTRUCTION_FILENAMES or not isinstance(value, bool):
                raise ValueError("enabled state contains an invalid entry")
            enabled[filename] = value
        return enabled, None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}, "Failed to load instruction file enabled state: invalid state"


def _snapshot_instruction_sources(
    config_snapshot,
) -> tuple[list[InstructionSource], dict[Path, bytes], tuple[str, ...], int]:
    """Project immutable instructions plus a separately pinned user snapshot."""
    sources: list[InstructionSource] = []
    payloads: dict[Path, bytes] = {}
    warnings: list[str] = []
    disabled_count = 0

    def collect(snapshot, *, root: Path, scope: str, project_layout: bool) -> None:
        nonlocal disabled_count
        if snapshot is None or (scope == "project" and not snapshot.trusted):
            return
        state_name = (
            ".nz-coder/instruction-file-state.json"
            if project_layout else "instruction-file-state.json"
        )
        state_file = snapshot.get(state_name)
        enabled, warning = _enabled_from_snapshot(
            state_file.content if state_file is not None else None
        )
        if warning:
            warnings.append(warning)
        rule_prefix = ".nz-coder/rules/" if project_layout else "rules/"
        rules = sorted(
            item for item in snapshot.files.values()
            if item.kind == "rule" and item.relative_path.startswith(rule_prefix)
        )
        for index, item in enumerate(rules):
            path = root / item.relative_path
            text = _strip_rule_frontmatter(
                item.content.decode("utf-8", errors="replace")
            )
            payloads[path] = text.encode("utf-8")
            sources.append(InstructionSource(
                path,
                scope,
                "rule",
                (3.0 if scope == "project" else 0.0) + index / 1000,
                40 if scope == "project" else 10,
            ))
        for filename in INSTRUCTION_FILENAMES:
            item = snapshot.get(filename)
            if item is None:
                continue
            if not enabled.get(filename, True):
                disabled_count += 1
                continue
            path = root / filename
            payloads[path] = item.content
            if scope == "global":
                order, priority = (
                    (1.0, 20) if filename == "CLAUDE.md" else (2.0, 30)
                )
            else:
                order, priority = (
                    (4.0, 50) if filename == "CLAUDE.md" else (5.0, 60)
                )
            kind = "claude" if filename == "CLAUDE.md" else "agents"
            sources.append(InstructionSource(path, scope, kind, order, priority))

    user_snapshot = getattr(config_snapshot, "user_instructions", None)
    user_root_text = (
        str(user_snapshot.workspace_identity.get("lexical") or "")
        if user_snapshot is not None else ""
    )
    if user_root_text:
        collect(
            user_snapshot,
            root=Path(user_root_text),
            scope="global",
            project_layout=False,
        )
    collect(
        config_snapshot.project_control,
        root=Path(config_snapshot.workspace),
        scope="project",
        project_layout=True,
    )
    return (
        sorted(sources, key=lambda item: (item.order, str(item.path))),
        payloads,
        tuple(warnings),
        disabled_count,
    )


def _escape_reminder(text: str) -> str:
    return re.sub(
        r"</?\s*system-reminder\b",
        lambda match: match.group(0).replace("<", "&lt;", 1),
        text,
        flags=re.IGNORECASE,
    )


def _git_index_signature(project: Path) -> tuple[int, int, int]:
    """Return a cheap worktree-aware identity for tracked-file decisions."""
    marker = project / ".git"
    try:
        marker_mtime = marker.stat().st_mtime_ns
    except OSError:
        return (-1, -1, -1)
    git_dir = marker
    if marker.is_file():
        try:
            declaration = marker.read_text(encoding="utf-8")[:4096].strip()
        except OSError:
            return (marker_mtime, -1, -1)
        prefix = "gitdir:"
        if not declaration.lower().startswith(prefix):
            return (marker_mtime, -1, -1)
        candidate = Path(declaration[len(prefix):].strip())
        git_dir = candidate if candidate.is_absolute() else marker.parent / candidate
    try:
        index_stat = (git_dir / "index").stat()
    except OSError:
        return (marker_mtime, -1, -1)
    return (marker_mtime, index_stat.st_mtime_ns, index_stat.st_size)


def _is_checked_in(project: Path, path: Path) -> bool:
    """Best-effort cached equivalent of InfCode's project instruction label."""
    try:
        relative = path.resolve().relative_to(project.resolve()).as_posix()
    except (OSError, ValueError):
        return False
    project_key = str(project.resolve())
    key = (project_key, relative, _git_index_signature(project))
    with _TRACKED_LOCK:
        cached = _TRACKED_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
        tracked = completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        tracked = False
    with _TRACKED_LOCK:
        for stale_key in tuple(_TRACKED_CACHE):
            if stale_key[:2] == key[:2] and stale_key != key:
                _TRACKED_CACHE.pop(stale_key, None)
        _TRACKED_CACHE[key] = tracked
        while len(_TRACKED_CACHE) > _TRACKED_CACHE_MAX_ENTRIES:
            _TRACKED_CACHE.pop(next(iter(_TRACKED_CACHE)))
    return tracked


def _scope_label(source: InstructionSource, project: Path) -> str:
    if source.scope == "global":
        return "user's private global instructions for all projects"
    if _is_checked_in(project, source.path):
        return "project instructions, checked into the codebase"
    return "user's private project instructions, not checked in"


def load_instruction_context(
    workspace: str | Path,
    *,
    home: str | Path | None = None,
    config_snapshot=None,
) -> InstructionBundle:
    """Read, prioritize, budget, and render durable instruction files."""
    project = Path(workspace).resolve()
    snapshot_payloads: dict[Path, bytes] | None = None
    if config_snapshot is None:
        sources, warnings, disabled_count = _discover_instruction_sources_result(
            workspace,
            home=home,
        )
    else:
        sources, snapshot_payloads, warnings, disabled_count = (
            _snapshot_instruction_sources(config_snapshot)
        )
    prepared: dict[Path, tuple[str, int, bool, bool, bool]] = {}
    remaining = TOTAL_MAX_BYTES
    per_file_truncated_count = 0
    total_truncated_count = 0
    omitted_count = 0

    # Higher-priority project sources win cumulative budget, while final
    # rendering still follows global-to-project order.
    for source in sorted(sources, key=lambda item: (-item.priority, item.order, str(item.path))):
        data = (
            snapshot_payloads.get(source.path, b"")
            if snapshot_payloads is not None
            else _read_source(source)
        )
        file_content, file_used, decode_truncated = _decode_prefix(
            data,
            min(PER_SOURCE_MAX_BYTES, len(data)),
        )
        file_truncated = decode_truncated or file_used < len(data)
        content_bytes = file_content.encode("utf-8")
        content, used, total_decode_truncated = _decode_prefix(
            content_bytes,
            min(remaining, len(content_bytes)),
        )
        omitted = bool(content_bytes) and remaining <= 0
        total_truncated = (
            not omitted
            and (total_decode_truncated or used < len(content_bytes))
        )
        if file_truncated:
            per_file_truncated_count += 1
        if total_truncated:
            total_truncated_count += 1
        if omitted:
            omitted_count += 1
        prepared[source.path] = (
            content,
            used,
            file_truncated,
            total_truncated,
            omitted,
        )
        remaining -= used

    entries: list[str] = []
    included_paths: list[str] = []
    included_bytes = 0
    for source in sources:
        content, used, file_truncated, total_truncated, omitted = prepared.get(
            source.path,
            ("", 0, False, False, False),
        )
        if not content and not (file_truncated or total_truncated or omitted):
            continue
        notice = (
            _TOTAL_OMITTED_NOTICE
            if omitted
            else _TOTAL_TRUNCATED_NOTICE
            if total_truncated
            else _PER_FILE_NOTICE
            if file_truncated
            else ""
        )
        body = "\n\n".join(part for part in (notice, content) if part)
        label = _scope_label(source, project)
        entries.append(f"Contents of {source.path} ({label}):\n\n{body}")
        included_paths.append(str(source.path))
        included_bytes += used

    if entries:
        body = "\n\n".join(_escape_reminder(entry) for entry in entries)
        reminder = (
            "<system-reminder>\n"
            "As you answer the user's questions, you can use the following context:\n"
            "Codebase and user instructions are shown below. Be sure to adhere to these "
            "instructions. IMPORTANT: These instructions OVERRIDE any default behavior "
            "and you MUST follow them exactly as written.\n\n"
            f"{body}\n\n"
            "      IMPORTANT: this context may or may not be relevant to your tasks. "
            "You should not respond to this context unless it is highly relevant to your task.\n"
            "</system-reminder>"
        )
    else:
        reminder = ""

    return InstructionBundle(
        reminder=reminder,
        source_count=len(sources),
        included_count=len(entries),
        truncated_count=sum(
            1
            for _content, _used, file_cut, total_cut, omitted in prepared.values()
            if file_cut or total_cut or omitted
        ),
        per_file_truncated_count=per_file_truncated_count,
        total_truncated_count=total_truncated_count,
        omitted_count=omitted_count,
        included_bytes=included_bytes,
        paths=tuple(included_paths),
        disabled_count=disabled_count,
        warnings=warnings,
    )
