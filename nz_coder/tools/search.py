"""InfCode-aligned ripgrep-backed content and file pattern search tools."""

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.capabilities.ripgrep import (
    RipgrepCancelled,
    RipgrepSearchMatch,
    decode_ripgrep_event,
    list_ripgrep_files,
    search_ripgrep,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import ToolOutput, current_tool_cancel_event, register


_GREP_LIMIT = 100
_GLOB_LIMIT = 100
_MAX_GREP_LINE_LENGTH = 2000
_DEFAULT_IGNORED_DIRECTORIES = frozenset({
    ".git",
    ".hg",
    ".mypy_cache",
    ".nz-coder",
    ".nz-coder-runs",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
})


def _is_default_ignored_part(part: str) -> bool:
    return part in _DEFAULT_IGNORED_DIRECTORIES


def _m_in_workspace(m: Path, base: Path) -> bool:
    """Python 3.8-compatible replacement for m.resolve().is_relative_to(base)."""
    try:
        m.resolve().relative_to(base)
        return True
    except ValueError:
        return False


def _safe_path(p: str = ".") -> Path:
    return WorkspacePathPolicy(current_workdir()).validate_model_read(p or ".")


def _default_ignores_enabled(base: Path, pattern: str = "") -> bool:
    """Disable implicit ignores only for an explicitly named private scope."""
    workspace = current_workdir().resolve()
    try:
        relative_parts = base.resolve().relative_to(workspace).parts
    except ValueError:
        relative_parts = ()
    pattern_parts = Path(str(pattern).replace("\\", "/")).parts
    return not any(_is_default_ignored_part(part) for part in (*relative_parts, *pattern_parts))


def _default_repo_excluded(path: str) -> bool:
    return any(
        _is_default_ignored_part(part)
        for part in Path(str(path).replace("\\", "/")).parts
    )


def _default_ignore_globs() -> tuple[str, ...]:
    return tuple(
        f"!**/{name}/**" for name in sorted(_DEFAULT_IGNORED_DIRECTORIES)
    )


class _SearchInterrupted(Exception):
    """Internal cooperative stop for a running search worker."""


def _raise_if_cancelled() -> None:
    cancel_event = current_tool_cancel_event()
    if cancel_event is not None and cancel_event.is_set():
        raise _SearchInterrupted


_RGMatch = RipgrepSearchMatch
_decode_rg_event = decode_ripgrep_event


@dataclass(frozen=True)
class _SearchMatch:
    """One existing workspace match enriched for final sorting/rendering."""

    path: Path
    text: str
    line: int
    mtime: float


def _run_rg_search(
    cwd: Path,
    pattern: str,
    *,
    include: str | None = None,
    files: list[str] | None = None,
    case_insensitive: bool = False,
    use_default_ignores: bool = True,
) -> tuple[list[_RGMatch], bool]:
    """Compatibility adapter around the shared Ripgrep.search producer."""
    try:
        result = search_ripgrep(
            cwd,
            pattern,
            patterns=(
                ((include,) if include else ())
                + (_default_ignore_globs() if use_default_ignores else ())
            ),
            files=tuple(files) if files is not None else None,
            case_insensitive=case_insensitive,
            cancel_event=current_tool_cancel_event(),
        )
    except RipgrepCancelled as error:
        raise _SearchInterrupted from error
    return list(result.items), result.partial


def _python_rg_search(
    cwd: Path,
    pattern: str,
    *,
    include: str | None,
    files: list[str] | None,
    case_insensitive: bool,
    use_default_ignores: bool = True,
) -> tuple[list[_RGMatch], bool]:
    """Best-effort producer for installations without a ripgrep binary."""
    try:
        regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
    except re.error as error:
        raise ValueError(f"Invalid regex: {error}") from error
    if files:
        candidates = [cwd / item for item in files]
    else:
        candidates = _iter_fallback_files(cwd)
    access = WorkspaceFileAccess(current_workdir())
    matches: list[_RGMatch] = []
    partial = False
    for candidate in candidates:
        _raise_if_cancelled()
        if not candidate.is_file():
            continue
        try:
            relative = candidate.relative_to(cwd)
        except ValueError:
            continue
        if use_default_ignores and _default_repo_excluded(relative.as_posix()):
            continue
        if include and not _matches_glob(relative, include):
            continue
        absolute_offset = 0
        try:
            relative_to_workspace = candidate.resolve().relative_to(access.root).as_posix()
            source = access.read_text(relative_to_workspace, errors="replace")
            for line_number, text in enumerate(source.splitlines(keepends=True), 1):
                _raise_if_cancelled()
                found = list(regex.finditer(text))
                if found:
                    submatches = tuple({
                        "text": item.group(0),
                        "start": len(text[:item.start()].encode("utf-8")),
                        "end": len(text[:item.end()].encode("utf-8")),
                    } for item in found)
                    matches.append(_RGMatch(
                        path=relative.as_posix(),
                        text=text,
                        line=line_number,
                        absolute_offset=absolute_offset,
                        submatches=submatches,
                    ))
                absolute_offset += len(text.encode("utf-8"))
        except (OSError, ValueError):
            partial = True
    return matches, partial


def _search_matches(
    pattern: str,
    path: str,
    include: str | None,
    case_insensitive: bool,
) -> tuple[list[_SearchMatch], bool]:
    search = _safe_path(path)
    cwd = search if search.is_dir() else search.parent
    files = None if search.is_dir() else [search.name]
    use_default_ignores = _default_ignores_enabled(search)
    try:
        rows, partial = _run_rg_search(
            cwd,
            pattern,
            include=include,
            files=files,
            case_insensitive=case_insensitive,
            use_default_ignores=use_default_ignores,
        )
    except FileNotFoundError:
        rows, partial = _python_rg_search(
            cwd,
            pattern,
            include=include,
            files=files,
            case_insensitive=case_insensitive,
            use_default_ignores=use_default_ignores,
        )
    by_path: dict[Path, float | None] = {}
    matches: list[_SearchMatch] = []
    workspace = current_workdir().resolve()
    policy = WorkspacePathPolicy(workspace)
    for row in rows:
        _raise_if_cancelled()
        full = (cwd / row.path).resolve()
        if not _m_in_workspace(full, workspace):
            continue
        if not policy.is_model_visible(full):
            continue
        if full not in by_path:
            try:
                by_path[full] = full.stat().st_mtime if full.is_file() else None
            except OSError:
                by_path[full] = None
        mtime = by_path[full]
        if mtime is None:
            continue
        matches.append(_SearchMatch(full, row.text, row.line, mtime))
    matches.sort(key=lambda item: item.mtime, reverse=True)
    return matches, partial


def _bounded_line(text: str) -> str:
    if len(text) <= _MAX_GREP_LINE_LENGTH:
        return text
    return text[:_MAX_GREP_LINE_LENGTH] + "..."


def _render_infcode_content(
    pattern: str,
    matches: list[_SearchMatch],
    *,
    head_limit: int | None,
    offset: int,
    context: int,
    partial: bool,
) -> ToolOutput:
    limit = _GREP_LIMIT if head_limit is None else int(head_limit)
    if limit < 0:
        raise ValueError("head_limit must be non-negative")
    selected = matches[offset:] if limit == 0 else matches[offset:offset + limit]
    truncated = offset > 0 or (limit > 0 and len(matches) - offset > limit)
    output = [
        f"Found {len(matches)} matches"
        + (f" (showing first {len(selected)})" if truncated else "")
    ]
    current: Path | None = None
    emitted_context: set[tuple[Path, int]] = set()
    source_cache: dict[Path, list[str]] = {}
    access = WorkspaceFileAccess(current_workdir())
    for match in selected:
        if current != match.path:
            if current is not None:
                output.append("")
            current = match.path
            output.append(f"{match.path}:")
        if context <= 0:
            output.append(f"  Line {match.line}: {_bounded_line(match.text)}")
            continue
        if match.path not in source_cache:
            try:
                relative = match.path.relative_to(access.root).as_posix()
                source_cache[match.path] = access.read_text(
                    relative, errors="replace",
                ).splitlines(keepends=True)
            except OSError:
                source_cache[match.path] = []
        source_lines = source_cache[match.path]
        start = max(1, match.line - context)
        end = min(len(source_lines), match.line + context)
        for line_number in range(start, end + 1):
            identity = (match.path, line_number)
            if identity in emitted_context:
                continue
            emitted_context.add(identity)
            output.append(
                f"  Line {line_number}: {_bounded_line(source_lines[line_number - 1])}"
            )
    if truncated:
        hidden = max(0, len(matches) - len(selected))
        output.extend([
            "",
            f"(Results truncated: showing {len(selected)} of {len(matches)} matches "
            f"({hidden} hidden). Consider using a more specific path or pattern.)",
        ])
    if partial:
        output.extend(["", "(Some paths were inaccessible and skipped)"])
    return ToolOutput(
        "\n".join(output),
        title=pattern,
        metadata={"matches": len(matches), "truncated": truncated},
    )


def grep_search(
    pattern: str,
    path: str = ".",
    include: str = None,
    output_mode: str = "content",
    head_limit: int = None,
    offset: int = 0,
    context: int = 0,
    case_insensitive: bool = False,
) -> str:
    """Search file contents through ripgrep JSON match events.

    Args:
        pattern: Regex pattern to search for.
        path: Directory or file to search in. Default: workspace root.
        include: Glob filter, e.g. ``'*.py'``.
        output_mode: ``'content'`` (default), ``'files_with_matches'``, or ``'count'``.
        head_limit: Max results. Default: 100 (content), 50 otherwise. 0 = unlimited.
        offset: Skip first N results. Default: 0.
        context: Lines of context around matches (content mode only). Default: 0.
        case_insensitive: Use ``-i`` flag. Default: False.
    """
    try:
        if not isinstance(pattern, str) or not pattern:
            return "Error: pattern is required"
        offset = max(0, int(offset or 0))
        context = max(0, int(context or 0))
        matches, partial = _search_matches(
            pattern,
            path,
            include,
            bool(case_insensitive),
        )
        if not matches:
            return ToolOutput(
                "No files found",
                title=pattern,
                metadata={"matches": 0, "truncated": False},
            )
        if output_mode not in {"files_with_matches", "count"}:
            return _render_infcode_content(
                pattern,
                matches,
                head_limit=head_limit,
                offset=offset,
                context=context,
                partial=partial,
            )

        grouped: dict[Path, int] = {}
        for match in matches:
            grouped[match.path] = grouped.get(match.path, 0) + 1
        entries = list(grouped.items())
        limit = 50 if head_limit is None else int(head_limit)
        if limit < 0:
            return "Error: head_limit must be non-negative"
        selected = entries[offset:] if limit == 0 else entries[offset:offset + limit]
        truncated = offset > 0 or (limit > 0 and len(entries) - offset > limit)
        if output_mode == "files_with_matches":
            body = [str(item[0]) for item in selected]
            prefix = f"Found {len(entries)} file(s) matching '{pattern}'"
        else:
            body = [f"{item[0]}:{item[1]}" for item in selected]
            prefix = f"Found match counts for '{pattern}' across {len(entries)} file(s)"
        if truncated:
            body.append(
                f"\n[Showing {len(selected)}/{len(entries)} files "
                f"(limit={limit}, offset={offset})]"
            )
        if partial:
            body.append("\n(Some paths were inaccessible and skipped)")
        return ToolOutput(
            prefix + "\n" + "\n".join(body),
            title=pattern,
            metadata={"matches": len(matches), "truncated": truncated},
        )

    except _SearchInterrupted:
        return "Error: Search cancelled"
    except subprocess.TimeoutExpired:
        return "Error: Search timed out (30s)"
    except Exception as e:
        return f"Error: {e}"

def _expand_braces(pattern: str, limit: int = 64) -> tuple[str, ...]:
    """Expand bounded comma braces used by ripgrep's globset syntax."""
    values = [pattern]
    while len(values) < limit:
        expanded = False
        next_values: list[str] = []
        for value in values:
            left = value.find("{")
            right = value.find("}", left + 1) if left >= 0 else -1
            if left < 0 or right < 0 or "," not in value[left + 1:right]:
                next_values.append(value)
                continue
            choices = value[left + 1:right].split(",")
            for choice in choices:
                next_values.append(value[:left] + choice + value[right + 1:])
                if len(next_values) >= limit:
                    break
            expanded = True
            if len(next_values) >= limit:
                break
        values = next_values
        if not expanded:
            break
    return tuple(values[:limit])


def _matches_glob(path: Path, pattern: str) -> bool:
    """Approximate ripgrep globset semantics for the no-rg fallback."""
    normalized = pattern.replace("\\", "/")
    excluded = normalized.startswith("!")
    if excluded:
        normalized = normalized[1:]
    value = path.as_posix()
    matched = False
    for expanded in _expand_braces(normalized):
        if "/" not in expanded:
            matched = fnmatch.fnmatchcase(path.name, expanded)
        else:
            candidates = {expanded}
            current = expanded
            while "**/" in current:
                current = current.replace("**/", "", 1)
                candidates.add(current)
            matched = any(
                fnmatch.fnmatchcase(value, candidate)
                for candidate in candidates
            )
        if matched:
            break
    return not matched if excluded else matched


def _split_absolute_glob(pattern: str) -> tuple[Path, str] | None:
    normalized = pattern.replace("\\", "/")
    if not Path(normalized).is_absolute():
        return None
    wildcard = next(
        (index for index, value in enumerate(normalized) if value in "*?{["),
        -1,
    )
    if wildcard < 0:
        return Path(normalized), "*"
    slash = normalized.rfind("/", 0, wildcard)
    root = normalized[:slash] if slash > 0 else "/"
    return Path(root), normalized[slash + 1:] or "*"


def _iter_fallback_files(base: Path):
    """Yield lexical files with cancellation points when ripgrep is absent."""
    for root, directories, filenames in os.walk(base, followlinks=False):
        _raise_if_cancelled()
        root_path = Path(root)
        for filename in filenames:
            _raise_if_cancelled()
            yield root_path / filename


def _run_rg_files(
    base: Path,
    pattern: str,
    limit: int,
    *,
    use_default_ignores: bool = True,
) -> tuple[list[str], bool]:
    """Compatibility wrapper around the shared Ripgrep.files producer."""
    try:
        result = list_ripgrep_files(
            base,
            patterns=(pattern,),
            hidden=True,
            follow=False,
            limit=limit,
            exclude=_default_repo_excluded if use_default_ignores else None,
            cancel_event=current_tool_cancel_event(),
        )
    except RipgrepCancelled as error:
        raise _SearchInterrupted from error
    return list(result.files), result.truncated


def glob_search(pattern: str, path: str = ".") -> str:
    try:
        raw = pattern or "*"
        absolute = _split_absolute_glob(raw)
        if absolute is None:
            base = _safe_path(path)
        else:
            base = WorkspacePathPolicy(current_workdir()).validate_model_list(absolute[0])
            raw = absolute[1]
        if base.is_file():
            return f"Error: glob path must be a directory: {base}"
        if not base.is_dir():
            return f"Error: No such directory: {base}"
        use_default_ignores = _default_ignores_enabled(base, raw)
        files, truncated = (
            _run_rg_files(base, raw, _GLOB_LIMIT)
            if use_default_ignores
            else _run_rg_files(
                base,
                raw,
                _GLOB_LIMIT,
                use_default_ignores=False,
            )
        )
        entries: list[tuple[str, float]] = []
        policy = WorkspacePathPolicy(current_workdir())
        for relative in files:
            _raise_if_cancelled()
            full = (base / relative).resolve()
            if not _m_in_workspace(full, current_workdir().resolve()):
                continue
            if not policy.is_model_visible(full):
                continue
            try:
                mtime = full.stat().st_mtime
            except OSError:
                mtime = 0.0
            entries.append((str(full), mtime))
        entries.sort(key=lambda item: item[1], reverse=True)
        output: list[str] = [item[0] for item in entries]
        if not output:
            output.append("No files found")
        elif truncated:
            output.extend([
                "",
                "(Results are truncated: showing first 100 results. "
                "Consider using a more specific path or pattern.)",
            ])
        try:
            title = str(base.relative_to(current_workdir().resolve()))
        except ValueError:
            title = str(base)
        if title == ".":
            title = ""
        return ToolOutput(
            "\n".join(output),
            title=title,
            metadata={"count": len(entries), "truncated": truncated},
        )
    except _SearchInterrupted:
        return "Error: Search cancelled"
    except subprocess.TimeoutExpired:
        return "Error: Search timed out (30s)"
    except Exception as e:
        return f"Error: {e}"

register(
    name="grep_search",
    description=(
        "Search file contents with regex. Default mode returns matching lines "
        "grouped by absolute file path and sorted by file modification time. "
        "Use output_mode='files_with_matches' for paths only, or 'count' for counts. "
        "NEVER use bash grep/rg — always use this tool for search."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {"type": "string", "description": "Directory to search in. Default: workspace root."},
            "include": {"type": "string", "description": "File glob filter, e.g. '*.py'. Default: all files."},
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": "Output mode. 'content' (default) shows matching lines. 'files_with_matches' shows paths. 'count' shows per-file matching-line counts.",
            },
            "head_limit": {"type": "integer", "description": "Max results. Default: 100 (content) or 50 (files/count). Use 0 for unlimited."},
            "offset": {"type": "integer", "description": "Skip first N results for pagination. Default: 0."},
            "context": {"type": "integer", "description": "Lines of context around matches (content mode only). Default: 0."},
            "case_insensitive": {"type": "boolean", "description": "Case insensitive search (-i). Default: false."},
        },
        "required": ["pattern"],
    },
    handler=grep_search,
    execution="read",
)

register(
    name="glob_search",
    description=(
        "Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Results are sorted by modification time, most recent first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern to match files."},
            "path": {
                "type": "string",
                "description": "Directory to search in. Omit to use the workspace root.",
            },
        },
        "required": ["pattern"],
    },
    handler=glob_search,
    execution="read",
)
