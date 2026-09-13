"""Tools: read_file, write_file, edit_file, list_directory."""
from __future__ import annotations

import difflib
import html
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from nz_coder.foundation import config
from nz_coder.foundation.workspace_file_access import (
    WorkspaceFileAccess,
    WorkspaceFileIdentity,
)
from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.protocol.attachments import (
    MAX_IMAGE_BYTES,
    make_image_attachment,
    sniff_image_mime,
)
from nz_coder.protocol.public_error import PublicInputError, format_public_error
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.capabilities.documents import (
    DOCX_MIME,
    PDF_MIME,
    read_document_file,
)
from nz_coder.state.sessions import active_session_id
from nz_coder.tools import ToolOutput, current_tool_cancel_event, register
from nz_coder.tools.read_support import (
    DEFAULT_READ_LIMIT,
    MAX_READ_BYTES,
    SAMPLE_BYTES,
    is_binary_file,
    read_text_lines_bytes,
    warm_lsp,
)
from nz_coder.state.workspace import git_file_status

# Context-local bindings avoid cross-talk between concurrent agent runs.
_txn_manager: ContextVar[object | None] = ContextVar(
    "nz_coder_file_txn_manager", default=None,
)
_change_tracker: ContextVar[object | None] = ContextVar(
    "nz_coder_file_change_tracker", default=None,
)


def _get_txn():
    return _txn_manager.get()


def set_txn_manager(txn):
    """Bind the transaction manager to the current execution context."""
    _txn_manager.set(txn)


def set_change_tracker(tracker):
    """Bind the change tracker to the current execution context."""
    _change_tracker.set(tracker)


def _get_change_tracker():
    return _change_tracker.get()


@contextmanager
def bind_tool_state(txn=None, change_tracker=None):
    """Temporarily bind file-tool state to the current execution context."""
    txn_token = None
    tracker_token = None
    if txn is not None:
        txn_token = _txn_manager.set(txn)
    if change_tracker is not None:
        tracker_token = _change_tracker.set(change_tracker)
    try:
        yield
    finally:
        if tracker_token is not None:
            _change_tracker.reset(tracker_token)
        if txn_token is not None:
            _txn_manager.reset(txn_token)


def _safe_path(p: str) -> Path:
    """Validate host-owned internal access for compatibility callers."""
    return WorkspacePathPolicy(current_workdir()).validate_internal_access(p)


def _model_read_path(p: str) -> Path:
    return WorkspacePathPolicy(current_workdir()).validate_model_read(p)


def _model_write_path(p: str) -> Path:
    return WorkspacePathPolicy(current_workdir()).validate_model_write(p)


def _model_list_path(p: str) -> Path:
    return WorkspacePathPolicy(current_workdir()).validate_model_list(p)


def _file_access() -> WorkspaceFileAccess:
    return WorkspaceFileAccess(current_workdir())


def _read_for_mutation(
    access: WorkspaceFileAccess, path: str, *, errors: str = "strict",
) -> tuple[str, WorkspaceFileIdentity]:
    try:
        return access.read_text_with_identity(path, errors=errors)
    except FileNotFoundError:
        return "", WorkspaceFileIdentity.missing()



_BLOCKED_WRITE_FILENAMES = {".env", ".env.local", ".env.production", "id_rsa", "id_dsa", "known_hosts", "credentials"}
_BLOCKED_WRITE_DIRS = {".ssh"}


def _blocked_write_reason(path: str) -> str:
    p = Path(path)
    name = p.name.lower()
    parts = {part.lower() for part in p.parts}
    if name.startswith(".env") or name in _BLOCKED_WRITE_FILENAMES:
        return "writing .env or credential-like files is blocked"
    if parts & _BLOCKED_WRITE_DIRS:
        return "writing SSH or credential directories is blocked"
    if name.endswith((".pem", ".key", ".p12")):
        return "writing private-key-like files is blocked"
    return ""


def _begin_local_txn():
    txn = _get_txn()
    manage_locally = False
    if txn is None:
        from nz_coder.state.transaction import TransactionManager
        txn = TransactionManager()
        manage_locally = True
    elif not txn.active:
        manage_locally = True
    if manage_locally:
        txn.begin()
    return txn, manage_locally


def _write_files_batch_impl(files: list[dict], overwrite: bool = False) -> dict:
    if not isinstance(files, list) or not files:
        raise PublicInputError("files must be a non-empty list")
    if len(files) > 50:
        raise PublicInputError("max 50 files per batch")

    settings = current_run_settings()
    access = _file_access()
    prepared: list[dict] = []
    seen_paths: set[str] = set()
    total_bytes = 0
    for i, item in enumerate(files):
        if not isinstance(item, dict):
            raise PublicInputError(f"file {i} must be an object")
        path = str(item.get("path", "")).strip()
        content = item.get("content")
        if not path:
            raise PublicInputError(f"file {i} requires path")
        if not isinstance(content, str):
            raise PublicInputError(f"file {i} requires string content")
        blocked = _blocked_write_reason(path)
        if blocked:
            raise PublicInputError(f"{path}: {blocked}")
        fp = _model_write_path(path)
        key = str(fp)
        if key in seen_paths:
            raise PublicInputError(f"duplicate path in batch: {path}")
        seen_paths.add(key)
        before, expected = _read_for_mutation(access, path, errors="replace")
        existed = expected.expected_exists
        if existed and not overwrite:
            raise PublicInputError(
                f"target already exists and overwrite=false: {path}"
            )
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > settings.write_batch_file_bytes:
            raise PublicInputError(
                f"file too large ({content_bytes} bytes > "
                f"{settings.write_batch_file_bytes}): {path}"
            )
        total_bytes += content_bytes
        if total_bytes > settings.write_batch_total_bytes:
            raise PublicInputError(
                f"batch too large ({total_bytes} bytes > {settings.write_batch_total_bytes})"
            )
        prepared.append({
            "path": path,
            "fp": fp,
            "before": before,
            "content": content,
            "existed": existed,
            "expected": expected,
            "purpose": str(item.get("purpose", "")).strip(),
        })

    txn, manage_locally = _begin_local_txn()
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []
    try:
        for item in prepared:
            _track_before(item["path"], item["fp"], item["before"], item["existed"])
            access.write_text(
                item["path"], item["content"], transaction=txn,
                expected=item["expected"], overwrite=overwrite,
            )
            _track_after(item["path"], item["content"], True)
            if item["existed"]:
                updated.append(item["path"])
            else:
                created.append(item["path"])
        if manage_locally:
            txn.commit()
    except Exception:
        if manage_locally:
            txn.rollback()
        raise

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": [],
        "total_bytes": total_bytes,
    }




def write_files_batch(files: list[dict], overwrite: bool = False) -> str:
    try:
        result = _write_files_batch_impl(files, overwrite=overwrite)
    except Exception as e:
        return format_public_error(e)

    touched = len(result["created"]) + len(result["updated"])
    lines = [
        f"Batch write completed ({touched} files, {result['total_bytes']} bytes)",
        f"Created: {len(result['created'])}",
    ]
    lines.extend(f"- {path}" for path in result["created"][:20])
    lines.append(f"Updated: {len(result['updated'])}")
    lines.extend(f"- {path}" for path in result["updated"][:20])
    lines.append(f"Skipped: {len(result['skipped'])}")
    lines.append(f"Failed: {len(result['failed'])}")
    return "\n".join(lines)

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


def _missing_file_message(access: WorkspaceFileAccess, requested: str) -> str:
    """Build bounded suggestions from a handle-anchored parent enumeration."""
    raw = Path(requested)
    parent = raw.parent.as_posix() or "."
    basename = raw.name.lower()
    suggestions: list[str] = []
    try:
        entries = access.walk_directory(
            parent,
            max_depth=1,
            include_hidden_root=True,
            directories_first=False,
        )
    except (OSError, ValueError):
        entries = ()
    for entry in entries:
        name = Path(entry.path).name
        lowered = name.lower()
        if basename in lowered or lowered in basename:
            suggestions.append(str(raw.parent / name))
            if len(suggestions) == 3:
                break
    message = f"File not found: {requested}"
    if suggestions:
        message += "\n\nDid you mean one of these?\n" + "\n".join(suggestions)
    return message


def read_file(
    path: str,
    offset: int = None,
    limit: int = None,
    pages: str = None,
) -> str:
    try:
        access = _file_access()
        kind = access.kind(path)
        fp = access.display_path(path)
        if kind == "missing":
            return f"Error: {_missing_file_message(access, path)}"
        read_offset = _read_offset(offset)
        read_limit = _read_limit(limit)
        if kind == "directory":
            entries = [
                item.path + ("/" if item.is_directory else "")
                for item in access.walk_directory(
                    path,
                    max_depth=1,
                    include_hidden_root=True,
                    directories_first=False,
                )
            ]
            start = read_offset - 1
            selected = entries[start:start + read_limit]
            truncated = start + len(selected) < len(entries)
            suffix = (
                f"\n(Showing {len(selected)} of {len(entries)} entries. Use 'offset' "
                f"parameter to read beyond entry {read_offset + len(selected)})"
                if truncated
                else f"\n({len(entries)} entries)"
            )
            output = "\n".join([
                f"<path>{html.escape(str(fp), quote=True)}</path>",
                "<type>directory</type>",
                "<entries>",
                "\n".join(selected) + suffix,
                "</entries>",
            ])
            return ToolOutput(
                output,
                title=f"Read {path}",
                metadata={
                    "preview": "\n".join(selected[:20]),
                    "truncated": truncated,
                    "loaded": [],
                },
            )
        data = access.read_bytes(path, maximum=MAX_IMAGE_BYTES)
        sample = data[:SAMPLE_BYTES]
        image_mime = sniff_image_mime(sample)
        if image_mime:
            size = len(data)
            if size >= MAX_IMAGE_BYTES:
                raise PublicInputError(
                    f"Image size must be less than "
                    f"{MAX_IMAGE_BYTES // 1024 // 1024} MB ({size} bytes)."
                )
            attachment = make_image_attachment(data, image_mime, filename=fp.name)
            return ToolOutput(
                "Image read successfully",
                title=f"Read {fp.name}",
                metadata={"preview": "Image read successfully", "truncated": False},
                attachments=[attachment],
            )
        document_mime = (
            PDF_MIME if sample.startswith(b"%PDF-") or fp.suffix.lower() == ".pdf"
            else DOCX_MIME if fp.suffix.lower() == ".docx"
            else ""
        )
        if document_mime in {PDF_MIME, DOCX_MIME}:
            result = read_document_file(
                path,
                workspace=current_workdir(),
                session_id=active_session_id() or "default",
                pages=pages,
                offset=read_offset,
                limit=read_limit,
                cancel_event=current_tool_cancel_event(),
                source_bytes=data,
            )
            body = result.text
            if result.status == "error":
                body = f"Document read failed: {result.error}"
            elif result.more:
                first = read_offset
                last = first + result.read_lines - 1
                body += (
                    f"\n\n(Showing lines {first}-{last} of {result.total_lines}. "
                    f"Use offset={last + 1} to continue.)"
                )
            output = (
                f'<document_read filename="{html.escape(fp.name, quote=True)}" '
                f'path="{html.escape(path, quote=True)}">\n'
                f"{body}\n"
                "</document_read>"
            )
            return ToolOutput(
                output,
                title=f"Read {fp.name}",
                metadata={
                    "preview": result.text[:500],
                    "truncated": result.more,
                    "document_read": {
                        "status": result.status,
                        "error": result.error,
                        "total_lines": result.total_lines,
                        "read_lines": result.read_lines,
                        "total_pages": result.total_pages,
                        "read_pages": result.read_pages,
                    },
                },
            )
        if is_binary_file(fp, sample):
            return f"Error: Cannot read binary file: {path}"
        result = read_text_lines_bytes(data, offset=read_offset, limit=read_limit)
        if result.count < read_offset and not (
            result.count == 0 and read_offset == 1
        ):
            return (
                f"Error: Offset {read_offset} is out of range for this file "
                f"({result.count} lines)"
            )
        output = "\n".join([
            f"<path>{html.escape(str(fp), quote=True)}</path>",
            "<type>file</type>",
            "<content>",
        ])
        output += "\n" + "\n".join(
            f"{index + read_offset}: {line}"
            for index, line in enumerate(result.lines)
        )
        last = read_offset + len(result.lines) - 1
        next_offset = last + 1
        truncated = result.more or result.cut
        if result.cut:
            output += (
                f"\n\n(Output capped at {MAX_READ_BYTES // 1024} KB. Showing lines "
                f"{read_offset}-{last}. Use offset={next_offset} to continue.)"
            )
        elif result.more:
            output += (
                f"\n\n(Showing lines {read_offset}-{last} of {result.count}. "
                f"Use offset={next_offset} to continue.)"
            )
        else:
            output += f"\n\n(End of file - total {result.count} lines)"
        output += "\n</content>"
        warm_lsp(fp, current_workdir())
        return ToolOutput(
            output,
            title=f"Read {path}",
            metadata={
                "preview": "\n".join(result.lines[:20]),
                "truncated": truncated,
                "loaded": [],
                "encoding": result.encoding,
            },
        )
    except Exception as e:
        return format_public_error(e)


def _read_offset(value: int | None) -> int:
    if value is None or value == 0:
        return 1
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicInputError("offset must be a non-negative integer")
    return value


def _read_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_READ_LIMIT
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicInputError("limit must be a non-negative integer")
    return value


def write_file(path: str, content: str) -> str:
    try:
        access = _file_access()
        fp = _model_write_path(path)
        before, expected = _read_for_mutation(access, path, errors="replace")
        existed = expected.expected_exists
        warning = _dirty_warning(path)
        _track_before(path, fp, before, existed)
        txn = _get_txn()
        access.write_text(path, content, transaction=txn, expected=expected)
        _track_after(path, content, True)
        diff = _format_diff(path, before, content)
        action = "Updated" if existed else "Created"
        return f"{warning}{action} {path} ({len(content)} bytes)\n\nDiff:\n{diff}"
    except Exception as e:
        return format_public_error(e)


def edit_file(path: str, old_text: str, new_text: str) -> str:
    try:
        access = _file_access()
        fp = _model_write_path(path)
        content, expected = access.read_text_with_identity(path)
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
        access.write_text(path, updated, transaction=txn, expected=expected)
        _track_after(path, updated, True)
        diff = _format_diff(path, content, updated)
        return f"{warning}Edited {path} (replaced 1 occurrence)\n\nDiff:\n{diff}"
    except Exception as e:
        return format_public_error(e)


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

        access = _file_access()
        fp = _model_write_path(path)
        content, expected = access.read_text_with_identity(path)
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
        access.write_text(path, updated, transaction=txn, expected=expected)
        _track_after(path, updated, True)
        diff = _format_diff(path, content, updated)
        return f"{warning}Replaced lines {start}-{end} in {path}\n\nDiff:\n{diff}"
    except Exception as e:
        return format_public_error(e)


def apply_patch(changes: list, dry_run: bool = False, path: str = "") -> str:
    """Apply exact hunks, accepting a top-level path for single-file patches."""
    try:
        if not isinstance(changes, list) or not changes:
            return "Error: changes must be a non-empty list"
        if len(changes) > 20:
            return "Error: Max 20 changes per patch"

        access = _file_access()
        prepared: dict[str, dict] = {}
        for i, change in enumerate(changes):
            if not isinstance(change, dict):
                return f"Error: change {i} must be an object"
            change = dict(change)
            if not str(change.get("path", "")).strip() and str(path or "").strip():
                change["path"] = path
            op = str(change.get("op", "replace")).strip().lower()
            path = str(change.get("path", "")).strip()
            old_text = change.get("old_text")
            new_text = change.get("new_text", change.get("content"))
            if op not in ("replace", "create", "delete", "append"):
                return f"Error: change {i} has invalid op '{op}'"
            if not path:
                return f"Error: change {i} requires path"

            fp = _model_write_path(path)
            key = str(fp)
            if key not in prepared:
                before, expected = _read_for_mutation(access, path)
                exists_before = expected.expected_exists
                prepared[key] = {
                    "path": path,
                    "fp": fp,
                    "before": before,
                    "after": before,
                    "exists_before": exists_before,
                    "expected": expected,
                    "overwrite": True,
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
                prepared[key]["overwrite"] = bool(change.get("overwrite", False))
                continue

            if op == "delete":
                if not prepared[key]["exists_before"]:
                    return f"Error: change {i} delete target not found: {path}"
                if isinstance(old_text, str) and old_text not in current:
                    return f"Error: change {i} delete guard old_text not found in {path}"
                prepared[key]["after"] = None
                continue

            if op == "append":
                if not prepared[key]["exists_before"]:
                    return f"Error: change {i} append target not found: {path}"
                if not isinstance(new_text, str):
                    return f"Error: change {i} append requires new_text"
                separator = ""
                if current and new_text and not (
                    current.endswith(("\n", "\r"))
                    or new_text.startswith(("\n", "\r"))
                ):
                    separator = "\n"
                prepared[key]["after"] = current + separator + new_text
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

        txn, manage_locally = _begin_local_txn()
        reports = []
        try:
            for item in prepared.values():
                after = item["after"]
                diff_after = "" if after is None else after
                warning = _dirty_warning(item["path"])
                reports.append(f"{warning}Diff for {item['path']}:\n{_format_diff(item['path'], item['before'], diff_after)}")
                if dry_run:
                    continue
                _track_before(item["path"], item["fp"], item["before"], item["exists_before"])
                if after is None:
                    access.delete(
                        item["path"], transaction=txn,
                        expected=item["expected"],
                    )
                    _track_after(item["path"], "", False)
                else:
                    access.write_text(
                        item["path"], after, transaction=txn,
                        expected=item["expected"], overwrite=item["overwrite"],
                    )
                    _track_after(item["path"], after, True)
            if manage_locally:
                txn.commit()
        except Exception:
            if manage_locally:
                txn.rollback()
            raise

        verb = "Patch preview" if dry_run else "Applied patch"
        return f"{verb} ({len(changes)} changes across {len(prepared)} files)\n\n" + "\n\n".join(reports)
    except Exception as e:
        return format_public_error(e)


def list_directory(path: str = ".", depth: int = 1) -> str:
    try:
        access = _file_access()
        if access.kind(path, operation="list") != "directory":
            return f"Error: {path} is not a directory"
        records = access.walk_directory(path, max_depth=depth)
        lines = [
            f"{'  ' * item.depth}{item.path}{'/' if item.is_directory else ''}"
            for item in records
        ]
        if not lines:
            return f"{path}/ (empty)"
        return "\n".join(lines)
    except Exception as e:
        return format_public_error(e)


# Register tools
register(
    name="read_file",
    description=(
        "Read file contents with line numbers. Use offset/limit for large text "
        "or converted PDF/DOCX files. PDF page ranges use pages=\"5\" or "
        "pages=\"1-10\" with at most 20 pages per request."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Relative path from workspace root."},
            "offset": {"type": "integer", "description": "Start line number (1-based). Default: 1."},
            "limit": {"type": "integer", "description": "Number of lines to read. Default: 2000."},
            "pages": {
                "type": "string",
                "description": (
                    'PDF page range, for example "5" or "1-10". '
                    "Maximum 20 pages per request."
                ),
            },
        },
        "required": ["path"],
    },
    handler=read_file,
    execution="read",
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
    execution="write",
    side_effect="mutates-fs",
)


register(
    name="write_files_batch",
    description=(
        "Write multiple files atomically. Validates every path before writing, blocks "
        "credential-like files, and fails the entire batch if any file conflicts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "description": "List of file payloads.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Relative path from workspace root."},
                        "content": {"type": "string", "description": "Full file content."},
                        "purpose": {"type": "string", "description": "Optional short description for the file."},
                    },
                    "required": ["path", "content"],
                },
            },
            "overwrite": {"type": "boolean", "description": "Allow overwriting existing files. Default: false."},
        },
        "required": ["files"],
    },
    handler=write_files_batch,
    execution="write",
    side_effect="mutates-fs",
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
    execution="write",
    side_effect="mutates-fs",
)

register(
    name="apply_patch",
    description=(
        "Apply exact text replacements, file creates, and file deletes atomically. "
        "Validates every hunk before writing and returns diffs. Every change must "
        "include its relative path."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Compatibility fallback for saved single-file calls; new calls "
                    "must set path on every change."
                ),
            },
            "changes": {
                "type": "array",
                "description": "List of patch hunks.",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": ["replace", "create", "delete", "append"],
                            "description": (
                                "Patch operation. Default: replace. Use append only "
                                "to add new content at end-of-file without old_text."
                            ),
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
    execution="write",
    side_effect="mutates-fs",
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
    execution="write",
    side_effect="mutates-fs",
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
    execution="read",
)
