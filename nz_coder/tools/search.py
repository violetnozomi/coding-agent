"""Tools: grep_search, glob_search."""

import re
import subprocess
from pathlib import Path

from nz_coder import config
from nz_coder.tools import register


def _safe_path(p: str = ".") -> Path:
    path = (config.WORKDIR / (p or ".")).resolve()
    if not path.is_relative_to(config.WORKDIR.resolve()):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def grep_search(pattern: str, path: str = ".", include: str = None, max_results: int = 50) -> str:
    try:
        base = _safe_path(path)
        cmd = ["grep", "-rn", "--color=never"]
        if include:
            cmd.extend(["--include", include])
        cmd.extend(["--", pattern, str(base)])
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, cwd=config.WORKDIR
        )
        output = result.stdout.strip()
        if result.returncode not in (0, 1):
            error = result.stderr.strip() or f"grep exited with code {result.returncode}"
            return f"Error: {error}"
        if not output:
            return f"No matches found for '{pattern}'"
        lines = output.splitlines()
        if len(lines) > max_results:
            lines = lines[:max_results]
            lines.append(f"... ({len(output.splitlines()) - max_results} more matches)")
        # Make paths relative to workspace
        formatted = []
        for line in lines:
            line = line.replace(str(config.WORKDIR) + "\\", "").replace(str(config.WORKDIR) + "/", "")
            formatted.append(line)
        return "\n".join(formatted)
    except subprocess.TimeoutExpired:
        return "Error: Search timed out (30s)"
    except FileNotFoundError:
        # grep not available, fall back to Python
        return _python_grep(pattern, path, include, max_results)
    except Exception as e:
        return f"Error: {e}"


def _python_grep(pattern: str, path: str, include: str, max_results: int) -> str:
    """Fallback grep implementation in pure Python."""
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

    results = []
    files = base.rglob(include or "*") if base.is_dir() else [base]

    for fp in files:
        if not fp.is_file():
            continue
        try:
            for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                if regex.search(line):
                    rel = fp.relative_to(config.WORKDIR)
                    results.append(f"{rel}:{i}:{line.rstrip()}")
                    if len(results) >= max_results:
                        results.append(f"... (max {max_results} results reached)")
                        return "\n".join(results)
        except (PermissionError, OSError):
            continue

    return "\n".join(results) if results else f"No matches found for '{pattern}'"


def glob_search(pattern: str) -> str:
    try:
        raw = pattern or "*"
        if Path(raw).is_absolute() or ".." in Path(raw).parts:
            return f"Error: Pattern escapes workspace: {pattern}"
        matches = sorted(
            m for m in config.WORKDIR.glob(raw)
            if m.resolve().is_relative_to(config.WORKDIR.resolve())
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
    description="Search for a regex pattern in files. Returns matching lines with file paths and line numbers.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for."},
            "path": {"type": "string", "description": "Directory to search in. Default: workspace root."},
            "include": {"type": "string", "description": "File glob filter, e.g. '*.py'. Default: all files."},
            "max_results": {"type": "integer", "description": "Max results. Default: 50."},
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
