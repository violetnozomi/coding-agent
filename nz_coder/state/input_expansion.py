"""Budget and persist system-expanded user context.

The user's natural-language text is never part of this budget.  Only tagged
content that NZ-Coder generated from an attachment or another resolvable
source may be truncated or replaced with a tombstone.
"""
from __future__ import annotations

import html
from pathlib import Path

from nz_coder.protocol.attachments import (
    MAX_DOCUMENT_BYTES,
    MAX_IMAGE_BYTES,
    make_document_attachment,
    make_image_attachment,
    normalize_attachments,
    sniff_image_mime,
)
from nz_coder.capabilities.documents import detect_document_mime
from nz_coder.foundation.workspace_paths import WorkspacePathPolicy
from nz_coder.protocol.message_schema import attach_file_parts, attach_message_identity
from nz_coder.state.context import estimate_tokens

INPUT_EXPANSIONS_KEY = "_nz_input_expansions"
USER_TEXT_KEY = "_nz_user_text"

_KINDS = {
    "file",
    "directory",
    "git_diff",
    "terminal",
    "skill",
    "mcp_resource",
    "editor_context",
    "image",
    "document",
}
_READABLE_KINDS = {"file", "directory", "editor_context"}


def tag_file_attachments(
    message: dict,
    user_text: str,
    attachments,
    *,
    workspace: str | Path | None = None,
    session_id: str | None = None,
) -> int:
    """Split user files into text expansions and durable image FileParts."""
    records = []
    inline_files: list[dict] = []
    root = Path(workspace).resolve() if workspace is not None else None
    for attachment in attachments:
        source = str(getattr(attachment, "path", "") or "").strip()
        if not source:
            continue
        original_bytes = max(0, int(getattr(attachment, "size", 0) or 0))
        private_source = str(getattr(attachment, "host_path", "") or "").strip()
        path = (
            _safe_private_source(root, source, private_source)
            if root is not None and private_source
            else (_safe_source(root, source) if root is not None else None)
        )
        if path is not None:
            try:
                original_bytes = path.stat().st_size
            except OSError:
                path = None
        mime = ""
        if path is not None:
            try:
                with path.open("rb") as stream:
                    mime = sniff_image_mime(stream.read(16))
            except OSError:
                mime = ""
        if mime:
            note = f"[Attached image: {source}]"
            candidate = None
            if original_bytes >= MAX_IMAGE_BYTES:
                note = (
                    f"[Attached image omitted from inline model input: {source} "
                    "is 10 MB or larger.]"
                )
            else:
                try:
                    candidate = make_image_attachment(
                        path.read_bytes(),
                        mime,
                        filename=path.name,
                    )
                    normalize_attachments([
                        *[
                            item for item in inline_files
                            if str(item.get("mime", "")).startswith("image/")
                        ],
                        candidate,
                    ])
                except (OSError, ValueError):
                    candidate = None
                    note = (
                        f"[Attached image omitted from inline model input: {source} "
                        "exceeded the attachment count or total-size limit.]"
                    )
            if candidate is not None:
                inline_files.append(candidate)
            records.append({
                "kind": "image",
                "source": source,
                "originalBytes": original_bytes,
                "originalTokens": estimate_tokens(note),
                "resolved": True,
                "text": note,
            })
            continue
        document_mime = detect_document_mime(path) if path is not None else ""
        if document_mime:
            note = f"[Attached document queued for document_read preflight: {source}]"
            candidate = None
            if original_bytes >= MAX_DOCUMENT_BYTES:
                note = (
                    f"[Attached document omitted: {source} is 10 MB or larger.]"
                )
            elif sum(
                1 for item in inline_files
                if item.get("mime") in {
                    "application/pdf",
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                }
            ) >= 4:
                note = f"[Attached document omitted: document count exceeds 4: {source}.]"
            else:
                try:
                    stat = path.stat()
                    relative = path.relative_to(root).as_posix()
                    candidate = make_document_attachment(
                        relative,
                        document_mime,
                        filename=path.name,
                        size=stat.st_size,
                        mtime_ns=stat.st_mtime_ns,
                    )
                except (OSError, ValueError):
                    candidate = None
                    note = f"[Attached document omitted: {source} could not be validated.]"
            if candidate is not None:
                inline_files.append(candidate)
            records.append({
                "kind": "document",
                "source": source,
                "originalBytes": original_bytes,
                "originalTokens": estimate_tokens(note),
                "resolved": True,
                "text": note,
            })
            continue
        records.append({
            "kind": "file",
            "source": source,
            "originalBytes": original_bytes,
            "resolved": False,
        })
    if not records:
        return 0
    if inline_files:
        if not isinstance(message.get("_nz_message_id"), str):
            if not isinstance(session_id, str) or not session_id:
                raise ValueError(
                    "session_id is required when attaching an image to a new message"
                )
            attach_message_identity(message, session_id=session_id)
        attach_file_parts(message, inline_files)
    message[USER_TEXT_KEY] = str(user_text or "")
    message[INPUT_EXPANSIONS_KEY] = records
    return len(records)


def _safe_source(workspace: Path, source: str) -> Path | None:
    try:
        resolved = WorkspacePathPolicy(workspace).validate_model_read(source)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def _safe_private_source(
    workspace: Path,
    source: str,
    host_path: str,
) -> Path | None:
    from nz_coder.foundation.user_paths import resolve_private_attachment

    try:
        expected = resolve_private_attachment(workspace, source)
        provided = Path(host_path).absolute()
        if expected.absolute() != provided or provided.is_symlink() or not provided.is_file():
            return None
        return provided
    except (OSError, ValueError):
        return None


def _read_bounded_text(path: Path, max_bytes: int) -> tuple[str, int, int]:
    try:
        original_bytes = path.stat().st_size
        with path.open("rb") as stream:
            data = stream.read(max(1, max_bytes))
    except OSError:
        return "", 0, 0
    return data.decode("utf-8", errors="replace"), original_bytes, len(data)


def _readable(record: dict) -> bool:
    return record.get("kind") in _READABLE_KINDS and bool(record.get("source"))


def tombstone(record: dict) -> str:
    """Return a compact, actionable replacement for one expansion."""
    kind = str(record.get("kind") or "context")
    source = str(record.get("source") or "(unknown)")
    if _readable(record):
        return (
            f"[Context omitted: {kind} {source} was too large to include this turn — "
            "use read_file with offset/limit if its contents are needed.]"
        )
    return (
        f"[Context omitted: {kind} {source} exceeded the user input expansion budget. "
        "Ask the user for a smaller portion if needed.]"
    )


def _truncate_note(record: dict) -> str:
    source = str(record.get("source") or "(unknown)")
    if _readable(record):
        return (
            f"\n\n[Context truncated: only the beginning of {source} is shown — "
            "use read_file with offset/limit to read the rest.]"
        )
    return f"\n\n[Context truncated: only the beginning of {source} is shown.]"


def render_expanded_message(message: dict) -> None:
    """Rebuild public message content from natural text and stored expansions."""
    records = message.get(INPUT_EXPANSIONS_KEY)
    if not isinstance(records, list):
        return
    parts = [str(message.get(USER_TEXT_KEY, "") or "").rstrip()]
    for record in records:
        if not isinstance(record, dict):
            continue
        source = html.escape(str(record.get("source") or "(unknown)"), quote=True)
        kind = html.escape(str(record.get("kind") or "context"), quote=True)
        text = str(record.get("text") or tombstone(record))
        parts.append(
            f'<system-expanded-context kind="{kind}" source="{source}">\n'
            f"{text}\n"
            "</system-expanded-context>"
        )
    message["content"] = "\n\n".join(part for part in parts if part)


def resolve_and_apply_budget(messages: list[dict], budget, workspace: str | Path) -> dict:
    """Resolve tagged expansions and apply InfCode's per-turn priority policy."""
    root = Path(workspace).resolve()
    token_budget = max(0, int(getattr(budget, "expansion_budget_tokens", 0) or 0))
    stats = {"resolved": 0, "truncated": 0, "compacted": 0}
    if token_budget <= 0:
        return stats

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        records = message.get(INPUT_EXPANSIONS_KEY)
        if not isinstance(records, list) or not records:
            continue
        if all(isinstance(record, dict) and record.get("budgetApplied") for record in records):
            render_expanded_message(message)
            continue

        expansions = []
        read_limit = max(64 * 1024, token_budget * 8)
        for record in records:
            if not isinstance(record, dict) or record.get("kind") not in _KINDS:
                continue
            if not record.get("resolved"):
                path = _safe_source(root, str(record.get("source") or "")) if _readable(record) else None
                if path is None:
                    record["text"] = tombstone(record)
                    record["compacted"] = True
                    record["resolved"] = True
                    stats["compacted"] += 1
                else:
                    text, original_bytes, sample_bytes = _read_bounded_text(
                        path,
                        read_limit,
                    )
                    record["text"] = text
                    record["originalBytes"] = original_bytes
                    sample_tokens = estimate_tokens(text)
                    density_estimate = (
                        (sample_tokens * original_bytes + sample_bytes - 1)
                        // sample_bytes
                        if sample_bytes > 0
                        else 0
                    )
                    record["originalTokens"] = max(
                        sample_tokens,
                        density_estimate,
                        (original_bytes + 3) // 4,
                    )
                    record["resolved"] = True
                    stats["resolved"] += 1
            tokens = max(0, int(record.get("originalTokens") or estimate_tokens(record.get("text", ""))))
            expansions.append((record, tokens))

        total = sum(tokens for _record, tokens in expansions if not _record.get("compacted"))
        if total > token_budget and len(expansions) == 1:
            record, _tokens = expansions[0]
            if not record.get("compacted"):
                keep_chars = max(0, token_budget * 4)
                record["text"] = str(record.get("text") or "")[:keep_chars] + _truncate_note(record)
                record["truncated"] = True
                stats["truncated"] += 1
        elif total > token_budget:
            remaining = token_budget
            for record, tokens in reversed(expansions):
                if record.get("compacted"):
                    continue
                if tokens <= remaining:
                    remaining -= tokens
                    continue
                record["text"] = tombstone(record)
                record["truncated"] = True
                record["compacted"] = True
                stats["compacted"] += 1
        for record, _tokens in expansions:
            record["budgetApplied"] = True
        render_expanded_message(message)
    return stats


def compact_stored(messages: list[dict], reason: str) -> int:
    """Persistently tombstone resolved expansions during preflight recovery."""
    degraded = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        records = message.get(INPUT_EXPANSIONS_KEY)
        if not isinstance(records, list):
            continue
        changed = False
        for record in records:
            if not isinstance(record, dict) or record.get("compacted"):
                continue
            record["text"] = tombstone(record)
            record["compacted"] = True
            record["compactionReason"] = str(reason)
            degraded += 1
            changed = True
        if changed:
            render_expanded_message(message)
    return degraded
