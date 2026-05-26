"""Tools: grep_search, glob_search.

grep_search defaults to ``files_with_matches`` mode (returns file paths, sorted
by modification time) — matching Claude Code's GrepTool behavior. Use
``output_mode: "content"`` for matching lines with context.
"""

import re
import subprocess
from pathlib import Path

from nz_coder import config
from nz_coder.tools import register


_EXCLUDED_DIRS = {".nz-coder", ".nz-coder-runs", ".git"}


def _m_in_workspace(m: Path, base: Path) -> bool:
    """Python 3.8-compatible replacement for m.resolve().is_relative_to(base)."""
    try:
        m.resolve().relative_to(base)
        return True
    except ValueError:
        return False


def _safe_path(p: str = ".") -> Path:
    path = (config.WORKDIR / (p or ".")).resolve()
    try:
        path.relative_to(config.WORKDIR.resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _get_mtime(filepath: str) -> float:
    """Return mtime for a file path, or 0 if stat fails."""
    try:
        return (config.WORKDIR / filepath).stat().st_mtime
    except OSError:
        return 0.0


def _run_grep(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run grep with consistent options."""
    cmd = ["grep", "-Ern", "--color=never",
           "--exclude-dir=.nz-coder", "--exclude-dir=.nz-coder-runs",
           "--exclude-dir=.git"]
    cmd.extend(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, cwd=str(config.WORKDIR),
    )


def grep_search(
    pattern: str,
    path: str = ".",
    include: str = None,
    output_mode: str = "files_with_matches",
    head_limit: int = None,
    offset: int = 0,
    context: int = 0,
    case_insensitive: bool = False,
) -> str:
    """Search for a regex pattern in files.

    Default ``files_with_matches`` mode returns file paths sorted by
    modification time (most recent first). Use ``content`` mode for
    matching lines, or ``count`` for per-file match counts.

    Args:
        pattern: Regex pattern to search for.
        path: Directory or file to search in. Default: workspace root.
        include: Glob filter, e.g. ``'*.py'``.
        output_mode: ``'files_with_matches'`` (default), ``'content'``, or ``'count'``.
        head_limit: Max results. Default: 50 (files/count) or 250 (content). 0 = unlimited.
        offset: Skip first N results. Default: 0.
        context: Lines of context around matches (content mode only). Default: 0.
        case_insensitive: Use ``-i`` flag. Default: False.
    """
    try:
        base = _safe_path(path)
        offset = max(0, int(offset or 0))

        args = []
        if case_insensitive:
            args.append("-i")

        if output_mode == "files_with_matches":
            args.append("-l")
            default_limit = 50
        elif output_mode == "count":
            args.append("-c")
            default_limit = 50
        else:
            output_mode = "content"
            default_limit = 250
            if context > 0:
                args.extend(["-C", str(int(context))])

        if include:
            args.extend(["--include", include])

        args.extend(["--", pattern, str(base)])

        result = _run_grep(args)
        output = result.stdout.strip()

        if result.returncode not in (0, 1):
            error = result.stderr.strip() or f"grep exited with code {result.returncode}"
            return f"Error: {error}"
        if not output:
            return f"No matches found for '{pattern}'"

        all_lines = output.splitlines()
        applied_limit = head_limit if head_limit is not None else default_limit
        if applied_limit == 0:
            applied_limit = len(all_lines)  # unlimited

        # ── files_with_matches: sort by mtime, most recent first ────────────
        if output_mode == "files_with_matches":
            # Each line is an absolute file path
            paths = []
            for line in all_lines:
                p = line.replace(str(config.WORKDIR) + "/", "").replace(str(config.WORKDIR) + "\\", "")
                paths.append((p, _get_mtime(p)))
            paths.sort(key=lambda x: x[1], reverse=True)

            sliced = paths[offset:offset + applied_limit]
            formatted = []
            for rel, mtime in sliced:
                formatted.append(rel)
            if len(paths) - offset > applied_limit:
                formatted.append(
                    f"\n[Showing {len(sliced)}/{len(paths)} files "
                    f"(limit={applied_limit}, offset={offset})]")
            prefix = f"Found {len(paths)} file(s) matching '{pattern}'\n"
            return prefix + "\n".join(formatted) if formatted else f"No matches found for '{pattern}'"

        # ── content / count: apply head_limit with pagination ────────────────
        sliced = all_lines[offset:offset + applied_limit]
        formatted = []
        for line in sliced:
            line = line.replace(str(config.WORKDIR) + "/", "").replace(str(config.WORKDIR) + "\\", "")
            formatted.append(line)

        trailer = ""
        if len(all_lines) - offset > applied_limit:
            trailer = (
                f"\n[Showing {len(sliced)}/{len(all_lines) - offset} results "
                f"(limit={applied_limit}, offset={offset})]")

        if output_mode == "content":
            prefix = f"Found {len(all_lines)} matching line(s) for '{pattern}'\n"
            return prefix + "\n".join(formatted) + trailer

        # count mode
        prefix = f"Found match counts for '{pattern}' across {len(all_lines)} file(s)\n"
        return prefix + "\n".join(formatted) + trailer

    except subprocess.TimeoutExpired:
        return "Error: Search timed out (30s)"
    except FileNotFoundError:
        return _python_grep_fallback(pattern, path, include, output_mode, head_limit or default_limit, offset)
    except Exception as e:
        return f"Error: {e}"


def _python_grep_fallback(pattern: str, path: str, include: str, output_mode: str, limit: int, offset: int) -> str:
    """Fallback grep in pure Python when system grep is not available."""
    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"Error: Invalid regex: {e}"

    try:
        base = _safe_path(path)
    except ValueError as e:
        return f"Error: {e}"
    if not base.exists():
        return f"Error: Path not found: {path}"

    results: list[tuple[str, int, str]] = []  # (filepath, lineno, line_text)
    file_paths: set[str] = set()
    files = base.rglob(include or "*") if base.is_dir() else [base]

    for fp in files:
        if not fp.is_file():
            continue
        if any(part in _EXCLUDED_DIRS for part in fp.parts):
            continue
        try:
            for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = str(fp.relative_to(config.WORKDIR))
                    if output_mode == "files_with_matches":
                        file_paths.add(rel)
                        break  # just need the filename
                    results.append((rel, i, line.rstrip()))
                    if output_mode != "files_with_matches" and len(results) >= limit + offset:
                        break
        except (PermissionError, OSError):
            continue

    if output_mode == "files_with_matches":
        paths = [(p, _get_mtime(p)) for p in file_paths]
        paths.sort(key=lambda x: x[1], reverse=True)
        sliced = paths[offset:offset + limit] if limit > 0 else paths
        formatted = [p for p, _ in sliced]
        return f"Found {len(paths)} file(s) matching '{pattern}'\n" + "\n".join(formatted)

    sliced = results[offset:offset + limit] if limit > 0 else results
    formatted = [f"{rel}:{lineno}:{text}" for rel, lineno, text in sliced]
    if not formatted:
        return f"No matches found for '{pattern}'"
    prefix = f"Found {len(results)} matching line(s) for '{pattern}'\n"
    return prefix + "\n".join(formatted)


def glob_search(pattern: str) -> str:
    try:
        raw = pattern or "*"
        if Path(raw).is_absolute() or ".." in Path(raw).parts:
            return f"Error: Pattern escapes workspace: {pattern}"
        _EXCLUDED = {".nz-coder", ".nz-coder-runs", ".git"}
        matches = sorted(
            m for m in config.WORKDIR.glob(raw)
            if _m_in_workspace(m, config.WORKDIR.resolve())
            and not any(part in _EXCLUDED for part in m.parts)
        )
        if not matches:
            return f"No files matching '{pattern}'"
        lines = []
        for m in matches[:100]:
            rel = m.relative_to(config.WORKDIR)
            suffix = "/" if m.is_dir() else ""
            lines.append(f"{rel}{suffix}")
        if len(matches) > 100:
            lines.append(f"... ({len(matches) - 100} more)")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


register(
    name="grep_search",
    description=(
        "Search file contents with regex. Default mode returns file paths "
        "sorted by modification time (most recent first). Use output_mode='content' "
        "for matching lines, or 'count' for per-file match counts. "
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
                "description": "Output mode. 'files_with_matches' (default) shows file paths sorted by mtime. 'content' shows matching lines. 'count' shows per-file match counts.",
            },
            "head_limit": {"type": "integer", "description": "Max results. Default: 50 (files/count) or 250 (content). Use 0 for unlimited."},
            "offset": {"type": "integer", "description": "Skip first N results for pagination. Default: 0."},
            "context": {"type": "integer", "description": "Lines of context around matches (content mode only). Default: 0."},
            "case_insensitive": {"type": "boolean", "description": "Case insensitive search (-i). Default: false."},
        },
        "required": ["pattern"],
    },
    handler=grep_search,
)

register(
    name="glob_search",
    description="Find files matching a glob pattern (e.g. '**/*.py', 'src/**/*.ts').",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern to match files."},
        },
        "required": ["pattern"],
    },
    handler=glob_search,
)
