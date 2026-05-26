"""Tools: read_file, write_file, edit_file, list_directory."""
from __future__ import annotations

import difflib
from pathlib import Path

from nz_coder import config
from nz_coder.tools import register
from nz_coder.workspace import git_file_status

# Lazy import to avoid circular dependency at module load time
_txn_manager = None
_change_tracker = None


def _get_txn():
    global _txn_manager
    return _txn_manager


def set_txn_manager(txn):
    """Called by AgentLoop to inject the transaction manager."""
    global _txn_manager
    _txn_manager = txn


def set_change_tracker(tracker):
    """Called by AgentLoop to inject the change tracker."""
    global _change_tracker
    _change_tracker = tracker


def _get_change_tracker():
    global _change_tracker
    return _change_tracker


def _safe_path(p: str) -> Path:
    path = (config.WORKDIR / p).resolve()
    try:
        path.relative_to(config.WORKDIR.resolve())
    except ValueError:
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def _format_diff(path: str, before: str, after: str) -> str:
    if before == after:
        return "(no changes)"
    diff = "".join(difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    ))
    if len(diff) > config.CONTEXT_TRUNCATE_CHARS:
        return _truncate_output(diff, config.CONTEXT_TRUNCATE_CHARS)
    return diff


def _nearby_context(content: str, needle: str, radius: int = 3) -> str:
    """Return nearby numbered lines based on tokens from a failed old_text."""
    tokens = [
        token.strip(' "\'():,.[]{}')
        for token in (needle or "").replace("\n", " ").split()
        if len(token.strip(' "\'():,.[]{}')) >= 4
    ]
    if not tokens:
        tokens = [
            token.strip(' "\'():,.[]{}')
            for token in (needle or "").replace("\n", " ").split()
            if token.strip(' "\'():,.[]{}')
        ]
    if not tokens:
        return ""
    lines = content.splitlines()
    matches = []
    _collect_context_matches(lines, tokens, matches)
    if not matches:
        short_tokens = [
            token.strip(' "\'():,.[]{}')
            for token in (needle or "").replace("\n", " ").split()
            if token.strip(' "\'():,.[]{}')
        ]
        _collect_context_matches(lines, short_tokens, matches)
    if not matches:
        return ""
    chunks = []
    seen = set()
    for idx in matches:
        start = max(0, idx - radius)
        end = min(len(lines), idx + radius + 1)
        for line_no in range(start, end):
            if line_no in seen:
                continue
            seen.add(line_no)
            chunks.append(f"{line_no + 1:4d} | {lines[line_no]}")
        chunks.append("   ...")
    return "\n".join(chunks).rstrip(".\n")


def _collect_context_matches(lines: list[str], tokens: list[str], matches: list[int]) -> None:
    lowered_tokens = [t.lower() for t in tokens[:8]]
    for idx, line in enumerate(lines):
        low = line.lower()
        if any(token in low for token in lowered_tokens):
            matches.append(idx)
            if len(matches) >= 3:
                break


def _dirty_warning(path: str) -> str:
    status = git_file_status(path)
    if not status:
        return ""
    return f"Warning: {path} already has git changes: {status}\n\n"


def _track_before(path: str, fp: Path, content: str, exists: bool) -> None:
    tracker = _get_change_tracker()
    if tracker:
        tracker.record_before(path, exists, content)


def _track_after(path: str, content: str, exists: bool) -> None:
    tracker = _get_change_tracker()
    if tracker:
        tracker.record_after(path, exists, content)


def _truncate_output(text: str, limit: int) -> str:
    """Middle-truncate large outputs preserving both head and tail."""
    if len(text) <= limit:
        return text
    half = limit // 2
    omitted = len(text) - limit
    return (
        text[:half]
        + f"\n\n... [{omitted} characters omitted] ...\n\n"
        + text[-half:]
    )


def read_file(path: str, offset: int = None, limit: int = None) -> str:
    try:
        fp = _safe_path(path)
        lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = (offset or 1) - 1
        start = max(0, min(start, total))
        if limit:
            end = start + limit
        else:
            end = total
        selected = lines[start:end]
        header = f"[{fp.name}: lines {start+1}-{min(end, total)} of {total}]"
        content = "\n".join(f"{start+1+i:4d} | {line}" for i, line in enumerate(selected))
        if end < total:
            content += f"\n... ({total - end} more lines)"
        return f"{header}\n{content}"
    except Exception as e:
        return f"Error: {e}"


def write_file(path: str, content: str) -> str:
    try:
        fp = _safe_path(path)
        existed = fp.exists()
        before = fp.read_text(encoding="utf-8", errors="replace") if existed else ""
        warning = _dirty_warning(path)
        _track_before(path, fp, before, existed)
        txn = _get_txn()
        if txn and txn.active:
            txn.track(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
        _track_after(path, content, True)
        diff = _format_diff(path, before, content)
        action = "Updated" if existed else "Created"
        return f"{warning}{action} {path} ({len(content)} bytes)\n\nDiff:\n{diff}"
    except Exception as e:
        return f"Error: {e}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    try:
        fp = _safe_path(path)
        content = fp.read_text(encoding="utf-8")
        warning = _dirty_warning(path)
        _track_before(path, fp, content, True)
        count = content.count(old_text)
        if count == 0:
            context = _nearby_context(content, old_text)
            extra = f"\nNearby context:\n{context}" if context else ""
            return f"Error: old_text not found in {path}. Re-read or copy the exact snippet from nearby context.{extra}"
        if count > 1:
            return f"Error: old_text matches {count} locations in {path}. Be more specific."
        updated = content.replace(old_text, new_text, 1)
        txn = _get_txn()
        if txn and txn.active:
            txn.track(path)
        fp.write_text(updated, encoding="utf-8")
        _track_after(path, updated, True)
        diff = _format_diff(path, content, updated)
        return f"{warning}Edited {path} (replaced 1 occurrence)\n\nDiff:\n{diff}"
    except Exception as e:
        return f"Error: {e}"


def replace_lines(path: str, start_line: int, end_line: int, new_text: str) -> str:
    """Replace a 1-based inclusive line range in a file."""
    try:
        if not isinstance(new_text, str):
            return "Error: new_text must be a string"
        # Normalize literal \n sequences that LLMs sometimes emit instead of real newlines.
        # In JSON function arguments, a real newline is encoded as \n (which Python parses to
        # chr(10)). But some models write \\n (which parses to backslash+n). Detect and fix.
        if "\\n" in new_text and "\n" not in new_text:
            new_text = new_text.replace("\\n", "\n")
        try:
            start = int(start_line)
            end = int(end_line)
        except (TypeError, ValueError):
            return "Error: start_line and end_line must be integers"
        if start < 1 or end < start:
            return "Error: line range must satisfy 1 <= start_line <= end_line"

        fp = _safe_path(path)
        content = fp.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        if end > len(lines):
            return f"Error: line range {start}-{end} exceeds {path} length {len(lines)}"

        warning = _dirty_warning(path)
        _track_before(path, fp, content, True)
        replacement = new_text.splitlines(keepends=True)
        if new_text and not new_text.endswith(("\n", "\r")):
            replacement[-1] = replacement[-1] + "\n"
        updated = "".join(lines[: start - 1] + replacement + lines[end:])

        txn = _get_txn()
        if txn and txn.active:
            txn.track(path)
        fp.write_text(updated, encoding="utf-8")
        _track_after(path, updated, True)
        diff = _format_diff(path, content, updated)
        return f"{warning}Replaced lines {start}-{end} in {path}\n\nDiff:\n{diff}"
    except Exception as e:
        return f"Error: {e}"


def apply_patch(changes: list, dry_run: bool = False) -> str:
    """Apply exact replacement/create/delete hunks after validating every change."""
    try:
        if not isinstance(changes, list) or not changes:
            return "Error: changes must be a non-empty list"
        if len(changes) > 20:
            return "Error: Max 20 changes per patch"

        prepared: dict[str, dict] = {}
        for i, change in enumerate(changes):
            if not isinstance(change, dict):
                return f"Error: change {i} must be an object"
            op = str(change.get("op", "replace")).strip().lower()
            path = str(change.get("path", "")).strip()
            old_text = change.get("old_text")
            new_text = change.get("new_text", change.get("content"))
            if op not in ("replace", "create", "delete"):
                return f"Error: change {i} has invalid op '{op}'"
            if not path:
                return f"Error: change {i} requires path"

            fp = _safe_path(path)
            key = str(fp)
            if key not in prepared:
                if fp.exists():
                    before = fp.read_text(encoding="utf-8")
                    exists_before = True
                else:
                    before = ""
                    exists_before = False
                prepared[key] = {
                    "path": path,
                    "fp": fp,
                    "before": before,
                    "after": before,
                    "exists_before": exists_before,
                }

            current = prepared[key]["after"]
            if current is None:
                return f"Error: change {i} targets {path} after it was deleted"

            if op == "create":
                if prepared[key]["exists_before"] and not change.get("overwrite", False):
                    return f"Error: change {i} create target already exists: {path}"
                if not isinstance(new_text, str):
                    return f"Error: change {i} create requires content or new_text"
                prepared[key]["after"] = new_text
                continue

            if op == "delete":
                if not prepared[key]["exists_before"]:
                    return f"Error: change {i} delete target not found: {path}"
                if isinstance(old_text, str) and old_text not in current:
                    return f"Error: change {i} delete guard old_text not found in {path}"
                prepared[key]["after"] = None
                continue

            if not prepared[key]["exists_before"]:
                return f"Error: change {i} replace target not found: {path}"
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return f"Error: change {i} replace requires old_text and new_text"
            count = current.count(old_text)
            if count == 0:
                context = _nearby_context(current, old_text)
                extra = f"\nNearby context:\n{context}" if context else ""
                return f"Error: change {i} old_text not found in {path}. Re-read or copy the exact snippet from nearby context.{extra}"
            if count > 1:
                return f"Error: change {i} old_text matches {count} locations in {path}. Be more specific."
            prepared[key]["after"] = current.replace(old_text, new_text, 1)

        txn = _get_txn()
        reports = []
        for item in prepared.values():
            after = item["after"]
            diff_after = "" if after is None else after
            warning = _dirty_warning(item["path"])
            reports.append(f"{warning}Diff for {item['path']}:\n{_format_diff(item['path'], item['before'], diff_after)}")
            if dry_run:
                continue
            _track_before(item["path"], item["fp"], item["before"], item["exists_before"])
            if txn and txn.active:
                txn.track(item["path"])
            if after is None:
                item["fp"].unlink()
                _track_after(item["path"], "", False)
            else:
                item["fp"].parent.mkdir(parents=True, exist_ok=True)
                item["fp"].write_text(after, encoding="utf-8")
                _track_after(item["path"], after, True)

        verb = "Patch preview" if dry_run else "Applied patch"
        return f"{verb} ({len(changes)} changes across {len(prepared)} files)\n\n" + "\n\n".join(reports)
    except Exception as e:
        return f"Error: {e}"


def list_directory(path: str = ".", depth: int = 1) -> str:
    try:
        dp = _safe_path(path)
        if not dp.is_dir():
            return f"Error: {path} is not a directory"
        lines = []
        _walk(dp, dp, lines, 0, depth)
        if not lines:
            return f"{path}/ (empty)"
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def _walk(base: Path, current: Path, lines: list, level: int, max_depth: int):
    if level >= max_depth:
        return
    try:
        entries = sorted(current.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    except PermissionError:
        return
    indent = "  " * level
    for entry in entries:
        if entry.name.startswith(".") and level == 0:
            continue  # skip hidden at top level
        rel = entry.relative_to(base)
        if entry.is_dir():
            lines.append(f"{indent}{rel}/")
            _walk(base, entry, lines, level + 1, max_depth)
        else:
            lines.append(f"{indent}{rel}")


# Register tools
register(
    name="read_file",
    description="Read file contents with line numbers. Use offset/limit to read specific portions of large files.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from workspace root."},
            "offset": {"type": "integer", "description": "Start line number (1-based). Default: 1."},
            "limit": {"type": "integer", "description": "Number of lines to read. Default: all."},
        },
        "required": ["path"],
    },
    handler=read_file,
)

register(
    name="write_file",
    description="Write content to a file. Creates parent directories if needed. Overwrites existing content.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from workspace root."},
            "content": {"type": "string", "description": "The full content to write."},
        },
        "required": ["path", "content"],
    },
    handler=write_file,
)

register(
    name="edit_file",
    description="Replace exact text in a file (first occurrence only). old_text must match exactly once.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from workspace root."},
            "old_text": {"type": "string", "description": "The exact text to find and replace. Must be unique in the file."},
            "new_text": {"type": "string", "description": "The replacement text."},
        },
        "required": ["path", "old_text", "new_text"],
    },
    handler=edit_file,
)

register(
    name="apply_patch",
    description="Apply exact text replacements, file creates, and file deletes atomically. Validates every hunk before writing and returns diffs.",
    parameters={
        "type": "object",
        "properties": {
            "changes": {
                "type": "array",
                "description": "List of patch hunks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["replace", "create", "delete"],
                            "description": "Patch operation. Default: replace.",
                        },
                        "path": {"type": "string", "description": "Relative path from workspace root."},
                        "old_text": {"type": "string", "description": "Exact text to replace, or delete guard text."},
                        "new_text": {"type": "string", "description": "Replacement or created file content."},
                        "content": {"type": "string", "description": "Alias for new_text when op=create."},
                        "overwrite": {"type": "boolean", "description": "Allow op=create to replace an existing file."},
                    },
                    "required": ["path"],
                },
            },
            "dry_run": {"type": "boolean", "description": "Preview diffs without writing files. Default: false."},
        },
        "required": ["changes"],
    },
    handler=apply_patch,
)

register(
    name="replace_lines",
    description=(
        "Replace a 1-based inclusive line range in a file. Use after read_file "
        "shows exact line numbers, especially when exact-text edits are brittle."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from workspace root."},
            "start_line": {"type": "integer", "description": "First line to replace, 1-based."},
            "end_line": {"type": "integer", "description": "Last line to replace, 1-based inclusive."},
            "new_text": {"type": "string", "description": "Replacement text for the line range."},
        },
        "required": ["path", "start_line", "end_line", "new_text"],
    },
    handler=replace_lines,
)

register(
    name="list_directory",
    description="List files and directories in a path. Use depth to control recursion.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path. Default: current directory."},
            "depth": {"type": "integer", "description": "Max depth to recurse. Default: 1."},
        },
    },
    handler=list_directory,
)
