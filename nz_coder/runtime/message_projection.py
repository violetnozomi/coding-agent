"""Provider-neutral projection of durable Session messages onto wire messages."""
from __future__ import annotations

import time

from nz_coder import config
from nz_coder.attachments import normalize_attachments
from nz_coder.message_schema import MESSAGE_ID_KEY, PARTS_KEY
from nz_coder.state.input_expansion import render_expanded_message


def project_provider_messages(
    messages: list,
    *,
    capabilities=None,
    include_attachments: bool = True,
) -> list:
    """Normalize Session history without exposing durable NZ-only fields."""
    now = time.time()
    preserve_reasoning = (
        capabilities.preserve_reasoning_content
        if capabilities is not None
        else getattr(config, "PASS_REASONING_CONTENT", True)
    )
    strip_extra = set() if preserve_reasoning else {"reasoning_content"}
    supports_images = bool(
        getattr(capabilities, "supports_image_input", False)
    )
    attachment_by_call: dict[str, list[dict]] = {}
    user_attachment_by_message: dict[str, list[dict]] = {}
    image_description_by_message: dict[str, list[str]] = {}
    document_read_by_message: dict[str, list[str]] = {}

    for owner in messages:
        if not isinstance(owner, dict):
            continue
        for part in owner.get(PARTS_KEY, []):
            if not isinstance(part, dict):
                continue
            metadata = part.get("metadata")
            image_description = (
                metadata.get("image_describe")
                if isinstance(metadata, dict)
                else None
            )
            if (
                not supports_images
                and part.get("type") == "text"
                and isinstance(image_description, dict)
                and image_description.get("status") == "completed"
                and isinstance(image_description.get("source_message_id"), str)
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                image_description_by_message.setdefault(
                    image_description["source_message_id"], []
                ).append(part["text"])
            document_read = (
                metadata.get("document_read")
                if isinstance(metadata, dict)
                else None
            )
            if (
                part.get("type") == "text"
                and isinstance(document_read, dict)
                and document_read.get("status") in {"completed", "error"}
                and isinstance(document_read.get("source_message_id"), str)
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            ):
                document_read_by_message.setdefault(
                    document_read["source_message_id"], []
                ).append(part["text"])
            if part.get("type") == "file":
                files = _safe_attachments([part])
                message_id = owner.get(MESSAGE_ID_KEY)
                if isinstance(message_id, str) and files:
                    user_attachment_by_message.setdefault(message_id, []).extend(files)
                continue
            if part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict) or state.get("status") != "completed":
                continue
            files = _safe_attachments(state.get("attachments"))
            call_id = part.get("call_id")
            if isinstance(call_id, str) and call_id and files:
                attachment_by_call[call_id] = files

    base: list[dict] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "")
        strip_keys = {
            key for key in message if key.startswith("_nz_")
        } | {"_timestamp"} | strip_extra
        clean = {key: value for key, value in message.items() if key not in strip_keys}
        message_id = str(message.get(MESSAGE_ID_KEY) or "")
        if role == "user" and isinstance(clean.get("content"), str):
            descriptions = image_description_by_message.get(message_id, [])
            if descriptions:
                clean["content"] = "\n\n".join(
                    [clean["content"], *descriptions]
                ).strip()
            documents = document_read_by_message.get(message_id, [])
            if documents:
                clean["content"] = _content_without_document_expansions(
                    message,
                    clean["content"],
                )
                clean["content"] = "\n\n".join(
                    [clean["content"], *documents]
                ).strip()
        if (
            include_attachments
            and role == "tool"
            and not message.get("_nz_tool_compacted_at")
            and supports_images
        ):
            attachments = _safe_attachments(
                message.get("_nz_attachments")
                or attachment_by_call.get(str(message.get("tool_call_id") or ""))
            )
            if attachments:
                clean["_nz_attachments"] = attachments
        if include_attachments and role == "user" and supports_images:
            attachments = _safe_attachments(
                message.get("_nz_user_attachments")
                or user_attachment_by_message.get(message_id)
            )
            if attachments:
                clean["_nz_user_attachments"] = attachments
        if role == "assistant":
            if clean.get("content") is None:
                clean["content"] = ""
            clean["_timestamp"] = message.get("_timestamp", now)
        base.append(clean)

    valid_tool_ids = {
        str(call.get("id") or call.get("tool_call_id"))
        for message in base
        if message.get("role") == "assistant"
        for call in (message.get("tool_calls") or [])
        if call.get("id") or call.get("tool_call_id")
    }
    result: list[dict] = []
    for message in base:
        role = message.get("role", "")
        if role == "assistant":
            content = message.get("content", "")
            if isinstance(content, str) and not content.strip() and not message.get("tool_calls"):
                continue
            result.append({key: value for key, value in message.items() if key != "_timestamp"})
            continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id")
            if tool_call_id and tool_call_id not in valid_tool_ids:
                continue
            result.append(message)
            continue
        if role == "user" and result and result[-1].get("role") == "user":
            if not _merge_adjacent_users(result, message):
                result.append(message)
            continue
        result.append(message)
    return result


def _safe_attachments(value) -> list[dict]:
    try:
        return normalize_attachments(value)
    except ValueError:
        return []


def _content_without_document_expansions(message: dict, content: str) -> str:
    expansions = message.get("_nz_input_expansions")
    user_text = message.get("_nz_user_text")
    if not isinstance(expansions, list) or not isinstance(user_text, str):
        return content
    projected = {
        "content": content,
        "_nz_user_text": user_text,
        "_nz_input_expansions": [
            item for item in expansions
            if not isinstance(item, dict) or item.get("kind") != "document"
        ],
    }
    render_expanded_message(projected)
    return str(projected["content"])


def _merge_adjacent_users(result: list[dict], current: dict) -> bool:
    previous = result[-1]
    previous_content = previous.get("content", "")
    current_content = current.get("content", "")
    if not isinstance(previous_content, str) or not isinstance(current_content, str):
        return False
    try:
        attachments = normalize_attachments([
            *previous.get("_nz_user_attachments", []),
            *current.get("_nz_user_attachments", []),
        ])
    except ValueError:
        return False
    merged = dict(previous, content=previous_content + "\n\n" + current_content)
    if attachments:
        merged["_nz_user_attachments"] = attachments
    result[-1] = merged
    return True
