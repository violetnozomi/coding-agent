"""Persistent message identity and additive WithParts-style projections."""
from __future__ import annotations

import copy
import json
import math
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from nz_coder.protocol.attachments import (
    SUPPORTED_DOCUMENT_MIMES,
    normalize_attachments,
    normalize_document_attachments,
    normalize_user_file_parts,
)
from nz_coder.protocol.public_error import (
    PublicError,
    PublicRuntimeError,
    TrustedPublicMessage,
    public_error_from_wire,
    to_public_error,
)

MESSAGE_SCHEMA_VERSION = 1
MESSAGE_ID_KEY = "_nz_message_id"
PARTS_KEY = "_nz_parts"
SESSION_ID_KEY = "_nz_session_id"
SYNTHETIC_USER_KEY = "_nz_synthetic"
COMPACTION_KEY = "_nz_compaction"
CONTINUATION_KEY = "_nz_continuation"
TOOL_COMPACTED_AT_KEY = "_nz_tool_compacted_at"
SUMMARY_KEY = "_nz_summary"
SESSION_SUMMARY_KEY = "_nz_session_summary"
ASSISTANT_FINISH_KEY = "_nz_finish"
ASSISTANT_ERROR_KEY = "_nz_assistant_error"
ASSISTANT_COST_KEY = "_nz_cost"
ASSISTANT_CHILD_COST_KEY = "_nz_child_cost"
ASSISTANT_USAGE_KEY = "_nz_usage"
ASSISTANT_PROVIDER_KEY = "_nz_provider_id"
ASSISTANT_MODEL_KEY = "_nz_model_id"
ASSISTANT_PARENT_KEY = "_nz_parent_id"
ASSISTANT_TIME_KEY = "_nz_time"
ASSISTANT_MODE_KEY = "_nz_mode"
ASSISTANT_AGENT_KEY = "_nz_agent"
ASSISTANT_PATH_KEY = "_nz_path"
ASSISTANT_VARIANT_KEY = "_nz_variant"
ASSISTANT_END_STATE_KEY = "_nz_end_state"
USER_TIME_KEY = ASSISTANT_TIME_KEY
USER_AGENT_KEY = "_nz_user_agent"
USER_MODEL_KEY = "_nz_user_model"
INTERACTION_RUN_ID_KEY = "_nz_interaction_run_id"
VISIBLE_KEY = "_nz_visible"
INTERNAL_KEY = "_nz_internal"
AUTHORITATIVE_KEY = "_nz_authoritative"
PROVIDER_EXTRA_KEY = "_nz_provider_extra"
PROVIDER_REASONING_KEY = "_nz_provider_reasoning_content"
PROVIDER_TOOL_METADATA_KEY = "_nz_provider_metadata"

RESERVED_MESSAGE_KEYS = frozenset({
    "role",
    "content",
    "tool_calls",
    MESSAGE_ID_KEY,
    SESSION_ID_KEY,
    INTERACTION_RUN_ID_KEY,
    VISIBLE_KEY,
    INTERNAL_KEY,
    AUTHORITATIVE_KEY,
    PARTS_KEY,
    ASSISTANT_ERROR_KEY,
    "_nz_error",
    ASSISTANT_FINISH_KEY,
    ASSISTANT_END_STATE_KEY,
    ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY,
})
_SUPPORTED_PROVIDER_EXTENSION_KEYS = frozenset({
    "provider_extra",
    "reasoning_content",
})


def sanitize_provider_extra(extra: object) -> dict[str, object]:
    """Keep only JSON-safe Provider extensions outside Agent Core state."""
    if not isinstance(extra, dict):
        return {}
    selected: dict[str, object] = {}
    for raw_key, raw_value in extra.items():
        if not isinstance(raw_key, str):
            continue
        if (
            raw_key in RESERVED_MESSAGE_KEYS
            or raw_key.startswith("_nz_")
            or raw_key not in _SUPPORTED_PROVIDER_EXTENSION_KEYS
        ):
            continue
        safe, accepted = _json_safe_provider_value(raw_value)
        if accepted:
            selected[raw_key] = safe
    return selected


def provider_private_state(extra: object) -> dict[str, object]:
    """Map JSON-safe Provider continuation state onto private durable keys."""
    selected = sanitize_provider_extra(extra)
    private: dict[str, object] = {}
    if "reasoning_content" in selected:
        private[PROVIDER_REASONING_KEY] = selected["reasoning_content"]
    if "provider_extra" in selected:
        private[PROVIDER_EXTRA_KEY] = selected["provider_extra"]
    return private


def project_public_protocol_value(value: object) -> object:
    """Recursively remove Provider-private and NZ-private protocol fields."""
    if isinstance(value, dict):
        return {
            str(key): project_public_protocol_value(item)
            for key, item in value.items()
            if isinstance(key, str)
            and not key.startswith("_nz_")
            and key not in _SUPPORTED_PROVIDER_EXTENSION_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [project_public_protocol_value(item) for item in value]
    return value


def project_public_message_part(part: object) -> dict:
    """Return one visible Part without Provider continuation state."""
    if not isinstance(part, dict):
        return {}
    if part.get("internal") is True or part.get("visible") is False:
        return {}
    projected = project_public_tool_part(part)
    safe = project_public_protocol_value(projected)
    return safe if isinstance(safe, dict) else {}


def project_public_tool_part(part: object) -> dict:
    """Project nested ToolPart failures through the typed public boundary."""
    if not isinstance(part, dict):
        return {}
    projected = copy.deepcopy(part)
    if projected.get("type") != "tool":
        return projected
    state = projected.get("state")
    if not isinstance(state, dict):
        projected["state"] = {}
        return projected
    status = str(state.get("status") or "").casefold()
    if status not in {"error", "failed", "blocked", "nonzero", "interrupted"}:
        return projected
    public = None
    for candidate in (
        state.get("public_error"),
        state.get("error"),
        state.get("output"),
    ):
        if isinstance(
            candidate,
            (PublicError, PublicRuntimeError, TrustedPublicMessage),
        ):
            public = to_public_error(candidate)
            break
        public = public_error_from_wire(candidate)
        if public is not None:
            break
    if public is None:
        public = PublicError("tool_execution_failed", "Tool execution failed.")
    state.pop("raw", None)
    state.pop("public_error", None)
    state["output"] = public.message
    state["error"] = public.to_dict()
    projected["state"] = state
    return projected


def _json_safe_provider_value(
    value: object,
    *,
    _depth: int = 0,
) -> tuple[object, bool]:
    if _depth >= 12:
        return None, False
    if value is None or isinstance(value, (str, bool, int)):
        return value, True
    if isinstance(value, float):
        return (value, True) if math.isfinite(value) else (None, False)
    if isinstance(value, (list, tuple)):
        result = []
        for item in value[:200]:
            safe, accepted = _json_safe_provider_value(item, _depth=_depth + 1)
            if not accepted:
                return None, False
            result.append(safe)
        return result, True
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:200]:
            if not isinstance(key, str):
                return None, False
            safe, accepted = _json_safe_provider_value(item, _depth=_depth + 1)
            if not accepted:
                return None, False
            result[key] = safe
        return result, True
    return None, False

_LEGACY_SYNTHETIC_USER_PREFIXES = (
    "<api-error-diagnostic",
    "<context-injection",
    "<doom-loop-diagnostic",
    "<hook-guidance",
    "<interrupted",
    "<patch-risk-review",
    "<reflection-review",
    "<reminder",
    "<system-reminder",
    "<test-failure-diagnostic",
    "<tool-failure-diagnostic",
    "<transaction-rollback",
    "<user-frustration-context",
    "<verification-required",
)


def rebind_fork_history(messages: list[dict], session_id: str) -> list[dict]:
    """Clone a transcript and re-key its complete message/part reference graph."""
    rebound = copy.deepcopy(messages)
    source_session = next(
        (
            str(message.get(SESSION_ID_KEY))
            for message in rebound
            if isinstance(message, dict) and message.get(SESSION_ID_KEY)
        ),
        "fork-source",
    )
    ensure_message_identities(rebound, source_session)
    message_ids = {
        str(message[MESSAGE_ID_KEY]): new_message_id()
        for message in rebound
        if isinstance(message, dict)
    }
    part_ids = {
        str(part["id"]): f"part-{uuid.uuid4().hex}"
        for message in rebound
        if isinstance(message, dict)
        for part in message.get(PARTS_KEY, []) or []
        if isinstance(part, dict) and isinstance(part.get("id"), str)
    }
    for message in rebound:
        if not isinstance(message, dict):
            continue
        old_message_id = str(message[MESSAGE_ID_KEY])
        message[MESSAGE_ID_KEY] = message_ids[old_message_id]
        message[SESSION_ID_KEY] = session_id
        parent_id = message.get(ASSISTANT_PARENT_KEY)
        if isinstance(parent_id, str):
            if parent_id in message_ids:
                message[ASSISTANT_PARENT_KEY] = message_ids[parent_id]
            else:
                message.pop(ASSISTANT_PARENT_KEY, None)
        _remap_fork_references(message, message_ids, part_ids)
        for part in message.get(PARTS_KEY, []) or []:
            if not isinstance(part, dict):
                continue
            part["id"] = part_ids.get(str(part.get("id")), f"part-{uuid.uuid4().hex}")
            part["message_id"] = message[MESSAGE_ID_KEY]
    ensure_message_identities(rebound, session_id)
    return rebound


def _remap_fork_references(
    value,
    message_ids: dict[str, str],
    part_ids: dict[str, str],
) -> None:
    """Rewrite only typed fork-reference fields, never user text values."""
    if isinstance(value, dict):
        for child_key, child in value.items():
            if child_key in {"source_message_id", "tail_start_id"} and isinstance(child, str):
                if child in message_ids:
                    value[child_key] = message_ids[child]
            elif child_key == "head_message_ids" and isinstance(child, list):
                value[child_key] = [message_ids.get(item, item) for item in child]
            elif child_key == "source_id" and isinstance(child, str):
                if child in part_ids:
                    value[child_key] = part_ids[child]
            elif child_key not in {MESSAGE_ID_KEY, SESSION_ID_KEY, ASSISTANT_PARENT_KEY}:
                _remap_fork_references(child, message_ids, part_ids)
    elif isinstance(value, list):
        for child in value:
            _remap_fork_references(child, message_ids, part_ids)

_MESSAGE_ID_RE = re.compile(r"^msg-[A-Za-z0-9_-]{1,128}$")
_PART_ID_RE = re.compile(r"^part-[A-Za-z0-9_-]{1,128}$")


def is_synthetic_user_message(message: dict) -> bool:
    """Identify internal control prompts, including legacy unmarked sessions."""
    if message.get("role") != "user":
        return False
    if message.get(SYNTHETIC_USER_KEY):
        return True
    content = message.get("content", "")
    return (
        isinstance(content, str)
        and content.lstrip().lower().startswith(_LEGACY_SYNTHETIC_USER_PREFIXES)
    )


def new_message_id() -> str:
    """Return one opaque public message identity."""
    return f"msg-{uuid.uuid4().hex}"


def ensure_message_identities(messages: list[dict], session_id: str) -> None:
    """Add stable internal message/part metadata without changing API content."""
    seen_message_ids: set[str] = set()
    seen_part_ids: set[str] = set()
    last_user_id = ""
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        message_id = message.get(MESSAGE_ID_KEY)
        invalid_identity = (
            message.get(SESSION_ID_KEY) != session_id
            or not isinstance(message_id, str)
            or not _MESSAGE_ID_RE.fullmatch(message_id)
            or message_id in seen_message_ids
        )
        if invalid_identity:
            nonce = 0
            while True:
                message_id = _legacy_message_id(session_id, index, message, nonce)
                if message_id not in seen_message_ids:
                    break
                nonce += 1
            message[MESSAGE_ID_KEY] = message_id
        seen_message_ids.add(message_id)
        message[SESSION_ID_KEY] = session_id
        parts = _normalized_parts(
            message.get(PARTS_KEY),
            message_id=message_id,
            content=message.get("content"),
            compaction=message.get(COMPACTION_KEY),
            compacted_at=message.get(TOOL_COMPACTED_AT_KEY),
            expansions=message.get("_nz_input_expansions"),
        )
        for part_index, item in enumerate(parts):
            if item["id"] in seen_part_ids:
                nonce = part_index
                while True:
                    part_id = _legacy_part_id(message_id, nonce)
                    if part_id not in seen_part_ids:
                        break
                    nonce += 1
                item["id"] = part_id
            seen_part_ids.add(item["id"])
        message[PARTS_KEY] = parts
        _normalize_user_state(message)
        _normalize_assistant_state(message)
        if message.get("role") == "user" and not is_synthetic_user_message(message):
            last_user_id = message_id
        elif message.get("role") == "assistant":
            parent_id = message.get(ASSISTANT_PARENT_KEY)
            if not isinstance(parent_id, str) or parent_id not in seen_message_ids:
                if last_user_id:
                    message[ASSISTANT_PARENT_KEY] = last_user_id
                else:
                    message.pop(ASSISTANT_PARENT_KEY, None)


def attach_message_identity(
    message: dict,
    message_id: str | None = None,
    *,
    session_id: str | None = None,
) -> str:
    """Attach a new or explicitly supplied identity to one live message."""
    selected = message_id if isinstance(message_id, str) else new_message_id()
    if not _MESSAGE_ID_RE.fullmatch(selected):
        raise ValueError("message_id must be a valid NZ-Coder message ID")
    message[MESSAGE_ID_KEY] = selected
    if session_id is not None:
        message[SESSION_ID_KEY] = str(session_id)
    message.setdefault(PARTS_KEY, [])
    if message.get("role") == "user":
        stamp_user_message(message)
    return selected


def stamp_user_message(message: dict, created: float | None = None) -> dict:
    """Bind an authoritative creation time to one live User message."""
    if message.get("role") != "user":
        return message
    existing = _user_time(message.get(USER_TIME_KEY))
    if existing is not None:
        message[USER_TIME_KEY] = existing
        return message
    selected = time.time() if created is None else created
    if not _valid_time(selected):
        raise ValueError("created must be a finite non-negative timestamp")
    message[USER_TIME_KEY] = {"created": float(selected)}
    return message


def bind_user_context(
    message: dict,
    *,
    agent: str,
    provider_id: str,
    model_id: str,
    variant: str | None = None,
) -> dict:
    """Bind the Agent and logical model selected for one live User turn."""
    if message.get("role") != "user":
        return message
    stamp_user_message(message)
    existing_agent = _bounded_string(message.get(USER_AGENT_KEY), 200)
    existing_model = _user_model(message.get(USER_MODEL_KEY))
    if existing_agent and existing_model is not None:
        message[USER_AGENT_KEY] = existing_agent
        message[USER_MODEL_KEY] = existing_model
        return message
    normalized_agent = _bounded_string(agent, 200)
    normalized_provider = _bounded_string(provider_id, 200)
    normalized_model = _bounded_string(model_id, 500)
    if not normalized_agent or not normalized_provider or not normalized_model:
        raise ValueError("agent, provider_id, and model_id must be non-empty strings")
    model = {
        "provider_id": normalized_provider,
        "model_id": normalized_model,
    }
    normalized_variant = _bounded_string(variant, 200)
    if normalized_variant:
        model["variant"] = normalized_variant
    message[USER_AGENT_KEY] = normalized_agent
    message[USER_MODEL_KEY] = model
    return message


def bind_assistant_context(
    message: dict,
    *,
    mode: str,
    agent: str,
    cwd: str,
    root: str,
    variant: str | None = None,
) -> dict:
    """Bind the execution identity and workspace path for an Assistant step."""
    if message.get("role") != "assistant":
        return message
    normalized_mode = _bounded_string(mode, 200)
    normalized_agent = _bounded_string(agent, 200)
    normalized_cwd = _bounded_string(cwd, 4096)
    normalized_root = _bounded_string(root, 4096)
    if not all((normalized_mode, normalized_agent, normalized_cwd, normalized_root)):
        raise ValueError("mode, agent, cwd, and root must be non-empty strings")
    message[ASSISTANT_MODE_KEY] = normalized_mode
    message[ASSISTANT_AGENT_KEY] = normalized_agent
    message[ASSISTANT_PATH_KEY] = {
        "cwd": normalized_cwd,
        "root": normalized_root,
    }
    normalized_variant = _bounded_string(variant, 200)
    if normalized_variant:
        message[ASSISTANT_VARIANT_KEY] = normalized_variant
    else:
        message.pop(ASSISTANT_VARIANT_KEY, None)
    return message


def set_assistant_end_state(
    message: dict,
    reason: str,
    *,
    publish: Callable[[str, dict], None] | None = None,
) -> dict:
    """Persist one immutable InfCode-style terminal state on a final Assistant."""
    if message.get("role") != "assistant":
        raise ValueError("end state can only be attached to an assistant message")
    existing = _assistant_end_state(message.get(ASSISTANT_END_STATE_KEY))
    if existing is not None:
        return existing
    normalized = _assistant_end_state({"reason": reason})
    if normalized is None:
        raise ValueError("invalid assistant end-state reason")
    message[ASSISTANT_END_STATE_KEY] = normalized
    publish_assistant_state(message, publish)
    return copy.deepcopy(normalized)


def set_assistant_error(
    message: dict,
    error: object,
    *,
    name: str = "UnknownError",
    data: dict[str, Any] | None = None,
    publish: Callable[[str, dict], None] | None = None,
) -> dict:
    """Persist only an explicitly projected, public-safe Assistant error."""
    public = to_public_error(error)
    supplied_public = (
        public_error_from_wire(data.get("public_error"))
        if isinstance(data, dict)
        else None
    )
    if supplied_public is not None:
        public = supplied_public
    detail = public.message
    public_data: dict[str, Any] = {
        "message": detail,
        "public_error": public.to_dict(),
    }
    if name == "APIError":
        public_data["isRetryable"] = public.retryable
    if isinstance(data, dict):
        status = data.get("statusCode")
        if isinstance(status, int) and not isinstance(status, bool) and status >= 0:
            public_data["statusCode"] = status
        provider_id = data.get("providerID")
        if isinstance(provider_id, str) and provider_id:
            public_data["providerID"] = _bounded_string(provider_id, 200)
        retries = data.get("retries")
        if isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0:
            public_data["retries"] = retries
    payload = {
        "name": name,
        "data": public_data,
    }
    normalized = _assistant_error(payload)
    if normalized is None:
        normalized = {
            "name": "UnknownError",
            "data": {
                "message": detail[:4000],
                "public_error": public.to_dict(),
            },
        }
    message["_nz_error"] = detail
    message[ASSISTANT_ERROR_KEY] = normalized
    publish_assistant_state(message, publish)
    return copy.deepcopy(normalized)


def assistant_error_from_exception(
    error: Exception,
    *,
    provider_id: str = "",
    is_retryable: bool | None = None,
) -> dict:
    """Normalize a Provider exception into the persisted assistant error union."""
    public = to_public_error(error)
    message = public.message
    response = getattr(error, "response", None)
    status = getattr(error, "status_code", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    status = (
        status
        if isinstance(status, int) and not isinstance(status, bool) and status >= 0
        else None
    )
    if status in {401, 403} and provider_id:
        public = PublicError(
            public.code,
            public.message,
            public.retryable,
            {**public.metadata, "provider_id": _bounded_string(provider_id, 200)},
        )
        return _assistant_error({
            "name": "ProviderAuthError",
            "data": {
                "providerID": provider_id[:200],
                "message": message[:4000],
                "public_error": public.to_dict(),
            },
        }) or {
            "name": "UnknownError",
            "data": {"message": message, "public_error": public.to_dict()},
        }

    class_name = type(error).__name__
    api_shaped = (
        status is not None
        or is_retryable is not None
        or any(
            marker in class_name.lower()
            for marker in ("api", "http", "timeout", "connection", "ratelimit")
        )
    )
    if api_shaped:
        retryable = is_retryable
        if retryable is None:
            retryable = bool(
                status in {408, 409, 425, 429}
                or (status is not None and status >= 500)
                or any(
                    marker in class_name.lower()
                    for marker in ("timeout", "connection", "ratelimit")
                )
            )
        if bool(retryable) != public.retryable:
            public = PublicError(
                public.code,
                public.message,
                bool(retryable),
                public.metadata,
            )
        data: dict[str, Any] = {
            "message": message,
            "isRetryable": bool(retryable),
            "public_error": public.to_dict(),
        }
        if status is not None:
            data["statusCode"] = status
        return _assistant_error({"name": "APIError", "data": data}) or {
            "name": "UnknownError",
            "data": {"message": message[:4000], "public_error": public.to_dict()},
        }
    return _assistant_error({
        "name": "UnknownError",
        "data": {
            "message": message,
            "public_error": public.to_dict(),
        },
    }) or {
        "name": "UnknownError",
        "data": {"message": message[:4000], "public_error": public.to_dict()},
    }


def normalize_assistant_error(value: object) -> dict | None:
    """Return the public-safe Assistant error union used on wire surfaces."""
    return _assistant_error(value)


def publish_assistant_state(
    message: dict,
    publish: Callable[[str, dict], None] | None,
) -> dict | None:
    """Publish one sanitized assistant-info snapshot for live consumers."""
    if not callable(publish) or message.get("role") != "assistant":
        return None
    if message.get(INTERNAL_KEY) is True or message.get(VISIBLE_KEY) is False:
        return None
    session_id = message.get(SESSION_ID_KEY)
    if not isinstance(session_id, str) or not session_id:
        return None
    records = message_records([message], session_id)
    if not records:
        return None
    info = records[0]["info"]
    publish(
        "message.updated",
        {"message_id": info["id"], "info": copy.deepcopy(info)},
    )
    return info


def attach_text_part(message: dict, part: dict[str, Any]) -> None:
    """Persist one validated text-part snapshot on its owning message."""
    message_id = message.get(MESSAGE_ID_KEY)
    normalized = _validate_part(part, message_id=message_id)
    if normalized is None:
        raise ValueError("part must contain matching valid message/part IDs")
    parts = [item for item in message.get(PARTS_KEY, []) if isinstance(item, dict)]
    for index, item in enumerate(parts):
        if item.get("id") == normalized["id"]:
            parts[index] = normalized
            message[PARTS_KEY] = parts
            return
    message[PARTS_KEY] = [normalized, *parts]


def attach_file_parts(message: dict, attachments: list[dict]) -> list[dict]:
    """Persist validated user FileParts on an already identified message."""
    message_id = message.get(MESSAGE_ID_KEY)
    if not isinstance(message_id, str) or not _MESSAGE_ID_RE.fullmatch(message_id):
        raise ValueError("message must have an identity before attaching files")
    files = normalize_user_file_parts(attachments)
    retained = [
        item for item in message.get(PARTS_KEY, [])
        if isinstance(item, dict) and item.get("type") != "file"
    ]
    used = {
        item.get("id")
        for item in retained
        if isinstance(item.get("id"), str)
    }
    parts = []
    nonce = len(retained) + 1
    for file in files:
        while True:
            part_id = _legacy_part_id(message_id, nonce)
            nonce += 1
            if part_id not in used:
                break
        part = _file_part(
            {
                "id": part_id,
                "message_id": message_id,
                **file,
            },
            message_id,
        )
        if part is not None:
            parts.append(part)
            used.add(part_id)
    message[PARTS_KEY] = [*retained, *parts]
    return copy.deepcopy(parts)


def upsert_message_part(message: dict, part: dict[str, Any]) -> dict:
    """Insert or replace one validated durable message part by identity."""
    message_id = message.get(MESSAGE_ID_KEY)
    normalized = _validate_part(part, message_id=message_id)
    if normalized is None:
        raise ValueError("part must contain matching valid message/part IDs")
    parts = [item for item in message.get(PARTS_KEY, []) if isinstance(item, dict)]
    for index, item in enumerate(parts):
        if item.get("id") == normalized["id"]:
            parts[index] = normalized
            message[PARTS_KEY] = parts
            break
    else:
        message[PARTS_KEY] = [*parts, normalized]
    return normalized


def remove_message_part(message: dict, part_id: str) -> dict | None:
    """Remove one durable part and re-project legacy assistant text.

    A retry/removal event is not authoritative unless the owning message loses
    the same part.  Recomputing ``content`` here keeps legacy provider/session
    consumers aligned with the WithParts projection.
    """
    if not isinstance(part_id, str) or not part_id:
        return None
    parts = message.get(PARTS_KEY)
    if not isinstance(parts, list):
        return None
    removed = None
    retained = []
    for item in parts:
        if (
            removed is None
            and isinstance(item, dict)
            and item.get("id") == part_id
        ):
            removed = item
            continue
        if isinstance(item, dict):
            retained.append(item)
    if removed is None:
        return None
    message[PARTS_KEY] = retained
    if message.get("role") == "assistant" and removed.get("type") == "text":
        message["content"] = "".join(
            str(item.get("text") or "")
            for item in retained
            if item.get("type") == "text" and item.get("ignored") is not True
        )
    return copy.deepcopy(removed)


def settle_interrupted_parts(messages: list[dict]) -> int:
    """Close tool/question display state that cannot survive process restart."""
    settled = 0
    now = time.time()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for part in message.get(PARTS_KEY, []):
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool":
                state = part.get("state")
                if isinstance(state, dict) and state.get("status") in {"pending", "running"}:
                    start = state.get("time", {}).get("start", now)
                    part["state"] = {
                        "status": "error",
                        "input": copy.deepcopy(state.get("input"))
                        if isinstance(state.get("input"), dict)
                        else {},
                        "error": "Tool execution aborted",
                        "interrupted": True,
                        "time": {"start": start, "end": now},
                    }
                    settled += 1
            elif part.get("type") == "question" and part.get("status") == "pending":
                part["status"] = "terminated"
                part.pop("response", None)
                part.pop("error", None)
                settled += 1
    return settled


def cleanup_incomplete_tool_history(messages: list[dict]) -> list[dict]:
    """Return a Provider-safe copy with only adjacent complete tool pairs.

    A model tool call is valid only when the immediately following tool-result
    group contains exactly one result with the same non-empty ID.  Interrupted
    streams and failed dispatches can otherwise leave an orphan call (or an
    orphan result) that makes the next Provider request invalid.  Visible
    assistant text and NZ message parts are retained; only invalid protocol
    envelopes are removed.
    """
    cleaned: list[dict] = []
    active_call_ids: set[str] | None = None
    emitted_result_ids: set[str] = set()

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        copied = copy.deepcopy(message)

        if role == "assistant":
            calls = message.get("tool_calls")
            if not isinstance(calls, list):
                active_call_ids = None
                emitted_result_ids.clear()
                cleaned.append(copied)
                continue

            result_ids: set[str] = set()
            following = index + 1
            while following < len(messages):
                candidate = messages[following]
                if not isinstance(candidate, dict) or candidate.get("role") != "tool":
                    break
                result_id = candidate.get("tool_call_id")
                if isinstance(result_id, str) and result_id.strip():
                    result_ids.add(result_id)
                following += 1

            retained_calls: list[dict] = []
            retained_ids: set[str] = set()
            for call in calls:
                if not isinstance(call, dict):
                    continue
                call_id = call.get("id") or call.get("tool_call_id")
                if (
                    not isinstance(call_id, str)
                    or not call_id.strip()
                    or call_id not in result_ids
                    or call_id in retained_ids
                ):
                    continue
                retained_calls.append(copy.deepcopy(call))
                retained_ids.add(call_id)

            if retained_calls:
                copied["tool_calls"] = retained_calls
                active_call_ids = retained_ids
            else:
                copied.pop("tool_calls", None)
                active_call_ids = None
            emitted_result_ids.clear()
            cleaned.append(copied)
            continue

        if role == "tool":
            call_id = message.get("tool_call_id")
            if (
                active_call_ids is not None
                and isinstance(call_id, str)
                and call_id in active_call_ids
                and call_id not in emitted_result_ids
            ):
                cleaned.append(copied)
                emitted_result_ids.add(call_id)
            continue

        active_call_ids = None
        emitted_result_ids.clear()
        cleaned.append(copied)

    return cleaned


def legacy_messages(messages: list[dict]) -> list[dict]:
    """Return the pre-A022 conversation shape for backward compatibility."""
    projected = [
        project_public_protocol_value({
            key: copy.deepcopy(value)
            for key, value in message.items()
            if not key.startswith("_nz_")
        })
        for message in messages
        if isinstance(message, dict)
        and message.get(INTERNAL_KEY) is not True
        and message.get(VISIBLE_KEY) is not False
    ]
    return [item for item in projected if isinstance(item, dict)]


def message_records(messages: list[dict], session_id: str) -> list[dict]:
    """Project internal history into InfCode-style ``info`` plus ``parts``."""
    ensure_message_identities(messages, session_id)
    records = []
    for message in messages:
        if message.get(INTERNAL_KEY) is True or message.get(VISIBLE_KEY) is False:
            continue
        message_id = message[MESSAGE_ID_KEY]
        raw_info = {
            key: copy.deepcopy(value)
            for key, value in message.items()
            if not key.startswith("_nz_")
        }
        info = project_public_protocol_value(raw_info)
        info = info if isinstance(info, dict) else {}
        info.update({"id": message_id, "session_id": session_id})
        interaction_run_id = message.get(INTERACTION_RUN_ID_KEY)
        if isinstance(interaction_run_id, str) and interaction_run_id:
            info["interaction_run_id"] = interaction_run_id
        info["visible"] = message.get(VISIBLE_KEY) is not False
        info["internal"] = message.get(INTERNAL_KEY) is True
        info["authoritative"] = message.get(AUTHORITATIVE_KEY) is not False
        summary = _summary_metadata(message.get(SUMMARY_KEY))
        if summary:
            info["summary"] = summary
        if message.get("role") == "assistant":
            info.pop("finish", None)
            info.pop("error", None)
            info.pop("cost", None)
            info.pop("tokens", None)
            info.pop("provider_id", None)
            info.pop("model_id", None)
            info.pop("parent_id", None)
            info.pop("time", None)
            info.pop("mode", None)
            info.pop("agent", None)
            info.pop("path", None)
            info.pop("variant", None)
            info.pop("end_state", None)
            finish = message.get(ASSISTANT_FINISH_KEY)
            if isinstance(finish, str) and finish:
                info["finish"] = finish
            error = _assistant_error(message.get(ASSISTANT_ERROR_KEY))
            if error is not None:
                info["error"] = error
            cost = _assistant_cost(message.get(ASSISTANT_COST_KEY))
            if cost is not None:
                info["cost"] = cost
            tokens = _assistant_tokens(message.get(ASSISTANT_USAGE_KEY))
            if tokens is not None:
                info["tokens"] = tokens
            provider_id = message.get(ASSISTANT_PROVIDER_KEY)
            if isinstance(provider_id, str) and provider_id:
                info["provider_id"] = provider_id[:200]
            model_id = message.get(ASSISTANT_MODEL_KEY)
            if isinstance(model_id, str) and model_id:
                info["model_id"] = model_id[:500]
            parent_id = message.get(ASSISTANT_PARENT_KEY)
            if isinstance(parent_id, str) and _MESSAGE_ID_RE.fullmatch(parent_id):
                info["parent_id"] = parent_id
            timing = _assistant_time(message.get(ASSISTANT_TIME_KEY))
            if timing is not None:
                info["time"] = timing
            mode = _bounded_string(message.get(ASSISTANT_MODE_KEY), 200)
            agent = _bounded_string(message.get(ASSISTANT_AGENT_KEY), 200)
            path = _assistant_path(message.get(ASSISTANT_PATH_KEY))
            variant = _bounded_string(message.get(ASSISTANT_VARIANT_KEY), 200)
            if mode:
                info["mode"] = mode
            if agent:
                info["agent"] = agent
            if path is not None:
                info["path"] = path
            if variant:
                info["variant"] = variant
            end_state = _assistant_end_state(message.get(ASSISTANT_END_STATE_KEY))
            if end_state is not None:
                info["end_state"] = end_state
        elif message.get("role") == "user":
            # Public/user-supplied fields are not authoritative. Only the
            # private, validated timestamp is projected into the HTTP schema.
            info.pop("time", None)
            info.pop("agent", None)
            info.pop("model", None)
            timing = _user_time(message.get(USER_TIME_KEY))
            if timing is not None:
                info["time"] = timing
            agent = _bounded_string(message.get(USER_AGENT_KEY), 200)
            model = _user_model(message.get(USER_MODEL_KEY))
            if agent:
                info["agent"] = agent
            if model is not None:
                info["model"] = model
        records.append({
            "info": info,
            "parts": [
                projected
                for part in message[PARTS_KEY]
                if (projected := project_public_message_part(part))
            ],
        })
    return records


def _normalize_user_state(message: dict) -> None:
    """Migrate evidence-backed legacy User creation timestamps."""
    if message.get("role") != "user":
        return
    timing = _user_time(message.get(USER_TIME_KEY))
    if timing is None:
        created = message.get("_timestamp")
        if not _valid_time(created):
            created = next(
                (
                    part.get("time", {}).get("start")
                    for part in message.get(PARTS_KEY, [])
                    if isinstance(part, dict)
                    and isinstance(part.get("time"), dict)
                    and _valid_time(part.get("time", {}).get("start"))
                ),
                None,
            )
        if _valid_time(created):
            timing = {"created": float(created)}
    if timing is not None:
        message[USER_TIME_KEY] = timing
    else:
        message.pop(USER_TIME_KEY, None)
    agent = _bounded_string(message.get(USER_AGENT_KEY), 200)
    if agent:
        message[USER_AGENT_KEY] = agent
    else:
        message.pop(USER_AGENT_KEY, None)
    model = _user_model(message.get(USER_MODEL_KEY))
    if model is not None:
        message[USER_MODEL_KEY] = model
    else:
        message.pop(USER_MODEL_KEY, None)


def _normalize_assistant_state(message: dict) -> None:
    """Migrate assistant finish/error metadata into the typed durable shape."""
    if message.get("role") != "assistant":
        return
    finish = message.get(ASSISTANT_FINISH_KEY)
    if not isinstance(finish, str) or not finish:
        finish = next(
            (
                str(part.get("reason"))
                for part in reversed(message.get(PARTS_KEY, []))
                if isinstance(part, dict)
                and part.get("type") == "step-finish"
                and isinstance(part.get("reason"), str)
                and part.get("reason")
            ),
            "",
        )
    if finish:
        message[ASSISTANT_FINISH_KEY] = finish[:80]
    else:
        message.pop(ASSISTANT_FINISH_KEY, None)

    error = _assistant_error(message.get(ASSISTANT_ERROR_KEY))
    legacy = message.get("_nz_error")
    if error is None and isinstance(legacy, str) and legacy:
        if finish == "cancelled":
            name = "MessageAbortedError"
            public = PublicError(
                "cancelled",
                "Request interrupted by user",
                retryable=True,
            )
        elif finish == "context-overflow":
            name = "ContextOverflowError"
            public = PublicError(
                "context_overflow",
                "The request exceeded the model context window.",
                retryable=True,
            )
        else:
            name = "UnknownError"
            public = to_public_error(None)
        error = _assistant_error({
            "name": name,
            "data": {
                "message": public.message,
                "public_error": public.to_dict(),
            },
        })
    if error is not None:
        message[ASSISTANT_ERROR_KEY] = error
    else:
        message.pop(ASSISTANT_ERROR_KEY, None)

    timing = _assistant_time(message.get(ASSISTANT_TIME_KEY))
    if timing is None:
        created = message.get("_timestamp")
        if not _valid_time(created):
            created = next(
                (
                    part.get("time", {}).get("start")
                    for part in message.get(PARTS_KEY, [])
                    if isinstance(part, dict)
                    and isinstance(part.get("time"), dict)
                    and _valid_time(part.get("time", {}).get("start"))
                ),
                None,
            )
        completed = next(
            (
                part.get("time", {}).get("end")
                for part in reversed(message.get(PARTS_KEY, []))
                if isinstance(part, dict)
                and part.get("type") == "step-finish"
                and isinstance(part.get("time"), dict)
                and _valid_time(part.get("time", {}).get("end"))
            ),
            None,
        )
        if _valid_time(created):
            timing = {"created": float(created)}
            if _valid_time(completed) and float(completed) >= float(created):
                timing["completed"] = float(completed)
    if timing is not None:
        message[ASSISTANT_TIME_KEY] = timing
    else:
        message.pop(ASSISTANT_TIME_KEY, None)
    for key in (ASSISTANT_MODE_KEY, ASSISTANT_AGENT_KEY, ASSISTANT_VARIANT_KEY):
        normalized = _bounded_string(message.get(key), 200)
        if normalized:
            message[key] = normalized
        else:
            message.pop(key, None)
    path = _assistant_path(message.get(ASSISTANT_PATH_KEY))
    if path is not None:
        message[ASSISTANT_PATH_KEY] = path
    else:
        message.pop(ASSISTANT_PATH_KEY, None)
    end_state = _assistant_end_state(message.get(ASSISTANT_END_STATE_KEY))
    if end_state is not None:
        message[ASSISTANT_END_STATE_KEY] = end_state
    else:
        message.pop(ASSISTANT_END_STATE_KEY, None)


def _assistant_error(value: Any) -> dict | None:
    """Validate the bounded InfCode-style assistant error union."""
    if not isinstance(value, dict):
        return None
    name = value.get("name")
    data = value.get("data")
    if not isinstance(name, str) or not isinstance(data, dict):
        return None
    public = public_error_from_wire(data.get("public_error"))
    if public is None:
        public = to_public_error(None)
    message = public.message
    base_data: dict[str, Any] = {
        "message": message,
        "public_error": public.to_dict(),
    }
    if name == "MessageOutputLengthError":
        return {"name": name, "data": base_data}
    if name == "ProviderAuthError":
        provider = public.metadata.get("provider_id")
        if not isinstance(provider, str) or not provider:
            return {"name": "UnknownError", "data": base_data}
        return {
            "name": name,
            "data": {
                **base_data,
                "providerID": _bounded_string(provider, 200),
            },
        }
    if name == "APIError":
        result: dict[str, Any] = {
            **base_data,
            "isRetryable": public.retryable,
        }
        status = data.get("statusCode")
        if isinstance(status, int) and not isinstance(status, bool) and status >= 0:
            result["statusCode"] = status
        return {"name": name, "data": result}
    if name == "StructuredOutputError":
        retries = data.get("retries")
        if (
            not isinstance(retries, int)
            or isinstance(retries, bool)
            or retries < 0
        ):
            return None
        return {
            "name": name,
            "data": {**base_data, "retries": retries},
        }
    if name not in {
        "UnknownError",
        "MessageAbortedError",
        "ContextOverflowError",
        "OutputGuardrailError",
        "ToolGuardrailError",
        "ModelOutputLimitError",
        "EmptyModelResponseError",
    }:
        return None
    return {"name": name, "data": base_data}


def _assistant_cost(value: Any) -> float | None:
    """Return one bounded, finite USD cost without accepting booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    cost = float(value)
    if not math.isfinite(cost) or cost < 0 or cost > 1_000_000_000:
        return None
    return cost


def _assistant_tokens(value: Any) -> dict | None:
    """Project one normalized, mutually exclusive assistant usage record."""
    if not isinstance(value, dict):
        return None
    present = False
    parsed: dict[str, int] = {}
    for key in ("input", "output", "total", "reasoning"):
        number = value.get(key)
        if (
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            and number >= 0
        ):
            parsed[key] = int(number)
            present = True
    cache: dict[str, int] = {"read": 0, "write": 0}
    for source, target in (("cache_read", "read"), ("cache_write", "write")):
        number = value.get(source)
        if (
            isinstance(number, (int, float))
            and not isinstance(number, bool)
            and math.isfinite(float(number))
            and number >= 0
        ):
            cache[target] = int(number)
            present = True
    if not present:
        return None
    result: dict[str, Any] = {
        "input": parsed.get("input", 0),
        "output": parsed.get("output", 0),
        "reasoning": parsed.get("reasoning", 0),
        "cache": cache,
    }
    if "total" in parsed:
        result["total"] = parsed["total"]
    return result


def _assistant_time(value: Any) -> dict | None:
    if not isinstance(value, dict) or not _valid_time(value.get("created")):
        return None
    created = float(value["created"])
    result = {"created": created}
    completed = value.get("completed")
    if _valid_time(completed) and float(completed) >= created:
        result["completed"] = float(completed)
    return result


def _user_time(value: Any) -> dict | None:
    if not isinstance(value, dict) or not _valid_time(value.get("created")):
        return None
    return {"created": float(value["created"])}


def _user_model(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    provider_id = _bounded_string(value.get("provider_id"), 200)
    model_id = _bounded_string(value.get("model_id"), 500)
    if not provider_id or not model_id:
        return None
    result = {"provider_id": provider_id, "model_id": model_id}
    variant = _bounded_string(value.get("variant"), 200)
    if variant:
        result["variant"] = variant
    return result


def _assistant_path(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    cwd = _bounded_string(value.get("cwd"), 4096)
    root = _bounded_string(value.get("root"), 4096)
    if not cwd or not root:
        return None
    return {"cwd": cwd, "root": root}


def _assistant_end_state(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    reason = value.get("reason")
    if reason not in {"completed", "errored", "canceled", "interrupted"}:
        return None
    return {"reason": reason}


def _bounded_string(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else ""


def _valid_time(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def session_summary(messages: list[dict]) -> dict:
    """Return the latest snapshot-derived Session aggregate."""
    marker = _latest_session_summary(messages)
    if not marker:
        return {}
    result = {}
    for key in ("additions", "deletions", "files"):
        value = marker.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[key] = value
    return result if len(result) == 3 else {}


def session_diffs(messages: list[dict]) -> list[dict]:
    """Return bounded full diffs from the latest Session summary marker."""
    marker = _latest_session_summary(messages)
    diffs = marker.get("diffs") if marker else None
    if not isinstance(diffs, list):
        return []
    result = []
    for item in diffs[:10_000]:
        if not isinstance(item, dict):
            continue
        lightweight = _summary_metadata({"diffs": [item]}).get("diffs", [])
        if not lightweight:
            continue
        record = lightweight[0]
        patch = item.get("patch")
        record["patch"] = patch if isinstance(patch, str) else ""
        result.append(record)
    return result


def _latest_session_summary(messages: list[dict]) -> dict:
    for message in reversed(messages):
        if isinstance(message, dict) and isinstance(message.get(SESSION_SUMMARY_KEY), dict):
            return message[SESSION_SUMMARY_KEY]
    return {}


def _normalized_parts(
    value: Any,
    *,
    message_id: str,
    content: Any,
    compaction: Any = None,
    compacted_at: Any = None,
    expansions: Any = None,
) -> list[dict]:
    if isinstance(value, list):
        normalized = []
        seen_part_ids: set[str] = set()
        for item in value:
            part = _validate_part(item, message_id=message_id)
            if part is None or part["id"] in seen_part_ids:
                continue
            normalized.append(part)
            seen_part_ids.add(part["id"])
    else:
        normalized = []

    text_part = next((item for item in normalized if item.get("type") == "text"), None)
    if isinstance(content, str) and content:
        if text_part is None:
            text_part = {
                "id": _legacy_part_id(message_id),
                "message_id": message_id,
                "type": "text",
                "text": content,
            }
            normalized.insert(0, text_part)
        else:
            text_part["text"] = content
        if isinstance(compacted_at, (int, float)) and not isinstance(compacted_at, bool):
            text_part.setdefault("time", {})["compacted"] = float(compacted_at)
        expansion_metadata = _input_expansion_metadata(expansions)
        if expansion_metadata:
            text_part["metadata"] = {"input_expansions": expansion_metadata}

    if isinstance(compaction, dict):
        existing = next((item for item in normalized if item.get("type") == "compaction"), None)
        marker = _compaction_part(compaction, message_id, existing)
        normalized = [item for item in normalized if item.get("type") != "compaction"]
        if marker is not None:
            normalized.append(marker)
    return normalized


def _validate_part(value: Any, *, message_id: Any) -> dict | None:
    if not isinstance(value, dict) or not isinstance(message_id, str):
        return None
    part_id = value.get("id")
    if (
        not isinstance(part_id, str)
        or not _PART_ID_RE.fullmatch(part_id)
        or value.get("message_id") != message_id
        or value.get("type") not in {
            "text", "reasoning", "compaction", "step-start", "step-finish",
            "tool", "retry", "patch", "question", "question-summary", "file",
            "handoff",
        }
    ):
        return None
    part_type = value.get("type")
    if part_type == "compaction":
        result = _compaction_part(value, message_id, value)
    elif part_type in {"text", "reasoning"}:
        result = _textual_part(value, message_id, part_type)
    elif part_type == "file":
        result = _file_part(value, message_id)
    elif part_type == "tool":
        result = _tool_part(value, message_id)
    elif part_type == "question":
        result = _question_part(value, message_id)
    elif part_type == "question-summary":
        result = _question_summary_part(value, message_id)
    elif part_type == "patch":
        result = _patch_part(value, message_id)
    elif part_type == "handoff":
        result = _handoff_part(value, message_id)
    elif part_type in {"step-start", "step-finish"}:
        result = _step_part(value, message_id, part_type)
    elif part_type == "retry":
        result = _retry_part(value, message_id)
    else:
        result = None
    if result is None:
        return None
    for key in (
        "interaction_run_id",
        "run_id",
        "attempt_id",
        "generation_id",
    ):
        item = value.get(key)
        if isinstance(item, str) and 0 < len(item) <= 200:
            result[key] = item
    for key in ("generation", "version"):
        item = value.get(key)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            result[key] = item
    for key in ("visible", "internal", "authoritative"):
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
    status = value.get("status")
    if isinstance(status, str) and status in {
        "pending", "streaming", "completed", "error", "removed",
    }:
        result["status"] = status
    return result


def _file_part(value: dict, message_id: str) -> dict | None:
    """Validate image or workspace-document FileParts with shared bounds."""
    if value.get("mime") in SUPPORTED_DOCUMENT_MIMES:
        try:
            documents = normalize_document_attachments([value])
        except ValueError:
            return None
        return {
            "id": value["id"],
            "message_id": message_id,
            **documents[0],
        }
    try:
        attachments = normalize_attachments([{
            "type": "file",
            "mime": value.get("mime"),
            "url": value.get("url"),
            **(
                {"filename": value["filename"]}
                if isinstance(value.get("filename"), str) and value.get("filename")
                else {}
            ),
        }])
    except ValueError:
        return None
    if not attachments:
        return None
    return {
        "id": value["id"],
        "message_id": message_id,
        **attachments[0],
    }


def _textual_part(value: dict, message_id: str, part_type: str) -> dict | None:
    if not isinstance(value.get("text"), str):
        return None
    result = {
        "id": value["id"],
        "message_id": message_id,
        "type": part_type,
        "text": value["text"],
    }
    if part_type == "text" and value.get("ignored") is True:
        result["ignored"] = True
    if part_type == "text":
        for key in ("run_id", "attempt_id", "generation_id"):
            item = value.get(key)
            if isinstance(item, str) and 0 < len(item) <= 200:
                result[key] = item
        for key in ("generation", "version"):
            item = value.get(key)
            if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
                result[key] = item
    timing = value.get("time")
    if isinstance(timing, dict):
        normalized_time = {}
        for key in ("start", "end", "compacted"):
            item = timing.get(key)
            if (
                isinstance(item, (int, float))
                and not isinstance(item, bool)
                and math.isfinite(float(item))
                and item >= 0
            ):
                normalized_time[key] = float(item)
        if normalized_time:
            result["time"] = normalized_time
    metadata = value.get("metadata")
    if isinstance(metadata, dict) and part_type == "text":
        expansions = _input_expansion_metadata(metadata.get("input_expansions"))
        image_describe = _image_describe_metadata(metadata.get("image_describe"))
        document_read = _document_read_metadata(metadata.get("document_read"))
        clean_metadata = {}
        if expansions:
            clean_metadata["input_expansions"] = expansions
        if image_describe:
            clean_metadata["image_describe"] = image_describe
        if document_read:
            clean_metadata["document_read"] = document_read
        if clean_metadata:
            result["metadata"] = clean_metadata
    return result


def _image_describe_metadata(value: Any) -> dict:
    """Validate bounded durable state for one image-description preflight."""
    if not isinstance(value, dict):
        return {}
    status = value.get("status")
    source_message_id = value.get("source_message_id")
    items = value.get("items")
    if (
        status not in {"running", "completed", "interrupted"}
        or not isinstance(source_message_id, str)
        or not _MESSAGE_ID_RE.fullmatch(source_message_id)
        or not isinstance(items, list)
        or len(items) > 4
    ):
        return {}
    clean_items = []
    for item in items:
        if not isinstance(item, dict):
            return {}
        source_id = item.get("source_id")
        filename = item.get("filename")
        mime = item.get("mime")
        item_status = item.get("status")
        if (
            not isinstance(source_id, str)
            or not _PART_ID_RE.fullmatch(source_id)
            or not isinstance(filename, str)
            or not filename
            or not isinstance(mime, str)
            or mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}
            or item_status not in {"running", "completed", "error"}
        ):
            return {}
        clean = {
            "source_id": source_id,
            "filename": filename[:500],
            "mime": mime,
            "status": item_status,
        }
        if item_status == "completed":
            clean["text"] = str(item.get("text") or "")[:100_000]
        elif item_status == "error":
            clean["error"] = str(item.get("error") or "Image describe failed")[:4000]
        clean_items.append(clean)
    return {
        "status": status,
        "source_message_id": source_message_id,
        "items": clean_items,
    }


def _document_read_metadata(value: Any) -> dict:
    """Validate bounded durable state for one document-read preflight."""
    if not isinstance(value, dict):
        return {}
    status = value.get("status")
    source_message_id = value.get("source_message_id")
    items = value.get("items")
    if (
        status not in {"running", "completed", "error", "interrupted"}
        or not isinstance(source_message_id, str)
        or not _MESSAGE_ID_RE.fullmatch(source_message_id)
        or not isinstance(items, list)
        or len(items) > 4
    ):
        return {}
    clean_items = []
    for item in items:
        if not isinstance(item, dict):
            return {}
        source_id = item.get("source_id")
        filename = item.get("filename")
        item_status = item.get("status")
        if (
            not isinstance(source_id, str)
            or not _PART_ID_RE.fullmatch(source_id)
            or not isinstance(filename, str)
            or not filename
            or item_status not in {"running", "completed", "error"}
        ):
            return {}
        clean = {
            "source_id": source_id,
            "filename": filename[:500],
            "status": item_status,
        }
        if item_status == "error":
            clean["error"] = str(item.get("error") or "Document read failed")[:4000]
        clean_items.append(clean)
    return {
        "status": status,
        "source_message_id": source_message_id,
        "items": clean_items,
    }


def _normalized_time(value: Any, *, allow_compacted: bool = False) -> dict:
    if not isinstance(value, dict):
        return {}
    keys = ("start", "end", "compacted") if allow_compacted else ("start", "end")
    result = {}
    for key in keys:
        item = value.get(key)
        if (
            isinstance(item, (int, float))
            and not isinstance(item, bool)
            and math.isfinite(float(item))
            and item >= 0
        ):
            result[key] = float(item)
    return result


def _step_part(value: dict, message_id: str, part_type: str) -> dict:
    result = {
        "id": value["id"],
        "message_id": message_id,
        "type": part_type,
    }
    timing = _normalized_time(value.get("time"))
    if timing:
        result["time"] = timing
    snapshot = value.get("snapshot")
    if isinstance(snapshot, str) and snapshot:
        result["snapshot"] = snapshot[:256]
    if part_type == "step-finish":
        reason = value.get("reason")
        if isinstance(reason, str) and reason:
            result["reason"] = reason[:80]
        tokens = value.get("tokens")
        if isinstance(tokens, dict):
            clean_tokens = {}
            for key in ("input", "output", "total", "reasoning"):
                number = tokens.get(key)
                if isinstance(number, (int, float)) and not isinstance(number, bool) and number >= 0:
                    clean_tokens[key] = int(number)
            cache = tokens.get("cache")
            if isinstance(cache, dict):
                clean_cache = {}
                for key in ("read", "write"):
                    number = cache.get(key)
                    if (
                        isinstance(number, (int, float))
                        and not isinstance(number, bool)
                        and number >= 0
                    ):
                        clean_cache[key] = int(number)
                if clean_cache:
                    clean_tokens["cache"] = clean_cache
            if clean_tokens:
                result["tokens"] = clean_tokens
        cost = _assistant_cost(value.get("cost"))
        if cost is not None:
            result["cost"] = cost
    return result


def _handoff_part(value: dict, message_id: str) -> dict | None:
    source = value.get("from")
    target = value.get("to")
    kind = value.get("kind")
    if not all(isinstance(item, str) and item.strip() for item in (source, target)):
        return None
    if kind not in {"continuation", "as-tool", "as-tool-return"}:
        return None
    result = {
        "id": value["id"],
        "message_id": message_id,
        "type": "handoff",
        "from": source.strip()[:200],
        "to": target.strip()[:200],
        "kind": kind,
    }
    description = value.get("description")
    if isinstance(description, str) and description.strip():
        result["description"] = description.strip()[:1000]
    timing = _normalized_time(value.get("time"))
    if timing:
        result["time"] = timing
    return result


def _tool_part(value: dict, message_id: str) -> dict | None:
    tool = value.get("tool")
    call_id = value.get("call_id")
    state = value.get("state")
    if not isinstance(tool, str) or not tool or not isinstance(call_id, str) or not call_id:
        return None
    if not isinstance(state, dict) or state.get("status") not in {
        "pending", "running", "completed", "error",
    }:
        return None
    status = state["status"]
    clean_state = {
        "status": status,
        "input": copy.deepcopy(state.get("input")) if isinstance(state.get("input"), dict) else {},
    }
    timing = _normalized_time(state.get("time"), allow_compacted=True)
    if timing:
        clean_state["time"] = timing
    if status == "pending" and isinstance(state.get("raw"), str):
        clean_state["raw"] = state["raw"][:4096]
    if status == "running":
        title = state.get("title")
        if isinstance(title, str) and title:
            clean_state["title"] = title[:240]
        if isinstance(state.get("metadata"), dict):
            clean_state["metadata"] = copy.deepcopy(state["metadata"])
    if status == "completed":
        clean_state["output"] = str(state.get("output") or "")
        title = state.get("title")
        if isinstance(title, str) and title:
            clean_state["title"] = title[:240]
        if isinstance(state.get("metadata"), dict):
            clean_state["metadata"] = copy.deepcopy(state["metadata"])
        try:
            attachments = normalize_attachments(state.get("attachments"))
        except ValueError:
            attachments = []
        if attachments:
            clean_state["attachments"] = attachments
    if status == "error":
        clean_state["error"] = str(
            state.get("error") or "Tool execution failed"
        )[:4000]
        if isinstance(state.get("interrupted"), bool):
            clean_state["interrupted"] = state["interrupted"]
    result = {
        "id": value["id"],
        "message_id": message_id,
        "type": "tool",
        "tool": tool,
        "call_id": call_id,
        "state": clean_state,
    }
    index = value.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
        result["index"] = index
    if isinstance(value.get("metadata"), dict):
        result["metadata"] = copy.deepcopy(value["metadata"])
    private_metadata = value.get(PROVIDER_TOOL_METADATA_KEY)
    safe_private, accepted = _json_safe_provider_value(private_metadata)
    if accepted and isinstance(safe_private, dict) and safe_private:
        result[PROVIDER_TOOL_METADATA_KEY] = safe_private
    return result


def _question_items(value: Any) -> list[dict] | None:
    """Validate the bounded display shape shared by Question parts."""
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        return None
    result = []
    for item in value:
        if not isinstance(item, dict):
            return None
        question = item.get("question")
        header = item.get("header")
        options = item.get("options")
        multiple = item.get("multiple", False)
        if (
            not isinstance(question, str) or not question
            or not isinstance(header, str) or not header
            or not isinstance(multiple, bool)
            or not isinstance(options, list) or not 2 <= len(options) <= 5
        ):
            return None
        clean_options = []
        for option in options:
            if not isinstance(option, dict):
                return None
            label = option.get("label")
            description = option.get("description")
            if not isinstance(label, str) or not label or not isinstance(description, str):
                return None
            clean_options.append({
                "label": label[:500],
                "description": description[:2000],
            })
        result.append({
            "question": question[:4000],
            "header": header[:80],
            "options": clean_options,
            "multiple": multiple,
            "custom": bool(item.get("custom", True)),
        })
    return result


def _question_answers(value: Any, count: int) -> list[list[str]] | None:
    if not isinstance(value, list) or len(value) != count:
        return None
    result = []
    for answer in value:
        if not isinstance(answer, list) or len(answer) > 100:
            return None
        if not all(isinstance(item, str) for item in answer):
            return None
        result.append([item[:2000] for item in answer])
    return result


def _question_part(value: dict, message_id: str) -> dict | None:
    tool_call_id = value.get("tool_call_id")
    status = value.get("status")
    questions = _question_items(value.get("questions"))
    if (
        not isinstance(tool_call_id, str) or not tool_call_id
        or status not in {"pending", "completed", "terminated", "error"}
        or questions is None
    ):
        return None
    result = {
        "id": value["id"],
        "message_id": message_id,
        "type": "question",
        "tool_call_id": tool_call_id[:160],
        "questions": questions,
        "status": status,
    }
    request_id = value.get("request_id")
    if isinstance(request_id, str) and request_id:
        result["request_id"] = request_id[:160]
    title = value.get("title")
    if title is None or isinstance(title, str):
        if title is not None:
            result["title"] = title[:240]
    if status == "completed":
        response = value.get("response")
        answers = (
            _question_answers(response.get("answers"), len(questions))
            if isinstance(response, dict)
            else None
        )
        if answers is None:
            return None
        result["response"] = {"answers": answers}
    if status == "error":
        result["error"] = str(value.get("error") or "Question failed")[:4000]
    return result


def _question_summary_part(value: dict, message_id: str) -> dict | None:
    tool_call_id = value.get("tool_call_id")
    questions = _question_items(value.get("questions"))
    answers = (
        _question_answers(value.get("answers"), len(questions))
        if questions is not None
        else None
    )
    if not isinstance(tool_call_id, str) or not tool_call_id or answers is None:
        return None
    return {
        "id": value["id"],
        "message_id": message_id,
        "type": "question-summary",
        "tool_call_id": tool_call_id[:160],
        "questions": questions,
        "answers": answers,
    }


def _retry_part(value: dict, message_id: str) -> dict | None:
    attempt = value.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        return None
    message = str(value.get("message") or "Retrying provider request")[:1000]
    error = _assistant_error(value.get("error"))
    if error is None:
        error = {"name": "UnknownError", "data": {"message": message}}
    result = {
        "id": value["id"],
        "message_id": message_id,
        "type": "retry",
        "attempt": attempt,
        "error": error,
        "message": message,
    }
    timing = value.get("time")
    if isinstance(timing, dict) and _valid_time(timing.get("created")):
        result["time"] = {"created": float(timing["created"])}
    next_at = value.get("next")
    if _valid_time(next_at):
        result["next"] = float(next_at)
    return result


def _patch_part(value: dict, message_id: str) -> dict | None:
    snapshot = value.get("hash")
    files = value.get("files")
    if (
        not isinstance(snapshot, str)
        or not snapshot.startswith("snap-")
        or not isinstance(files, list)
    ):
        return None
    clean_files = [
        item[:4096]
        for item in files[:10_000]
        if isinstance(item, str) and item
    ]
    return {
        "id": value["id"],
        "message_id": message_id,
        "type": "patch",
        "hash": snapshot[:128],
        "files": clean_files,
    }


def _summary_metadata(value: Any) -> dict:
    if not isinstance(value, dict):
        return {}
    diffs = value.get("diffs")
    if not isinstance(diffs, list):
        return {}
    clean = []
    for item in diffs[:10_000]:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            continue
        additions = item.get("additions")
        deletions = item.get("deletions")
        if (
            not isinstance(additions, int) or isinstance(additions, bool) or additions < 0
            or not isinstance(deletions, int) or isinstance(deletions, bool) or deletions < 0
        ):
            continue
        record = {
            "file": item["file"][:4096],
            "additions": additions,
            "deletions": deletions,
        }
        if item.get("status") in {"added", "deleted", "modified"}:
            record["status"] = item["status"]
        clean.append(record)
    return {"diffs": clean}


def _input_expansion_metadata(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        source = item.get("source")
        if not isinstance(kind, str) or not isinstance(source, str):
            continue
        record = {"kind": kind, "source": source}
        for key in ("originalBytes", "originalTokens"):
            number = item.get(key)
            if isinstance(number, (int, float)) and not isinstance(number, bool) and number >= 0:
                record[key] = int(number)
        for key in ("truncated", "compacted", "resolved", "budgetApplied"):
            if isinstance(item.get(key), bool):
                record[key] = item[key]
        reason = item.get("compactionReason")
        if isinstance(reason, str) and reason:
            record["compactionReason"] = reason[:80]
        result.append(record)
    return result


def _compaction_part(value: dict, message_id: str, existing: dict | None = None) -> dict | None:
    auto = value.get("auto")
    if not isinstance(auto, bool):
        return None
    tail_start_id = value.get("tail_start_id")
    if tail_start_id is not None and (
        not isinstance(tail_start_id, str) or not _MESSAGE_ID_RE.fullmatch(tail_start_id)
    ):
        tail_start_id = None
    part_id = (existing or {}).get("id")
    if not isinstance(part_id, str) or not _PART_ID_RE.fullmatch(part_id):
        part_id = _legacy_part_id(message_id, 1)
    result = {
        "id": part_id,
        "message_id": message_id,
        "type": "compaction",
        "auto": auto,
    }
    for key in ("overflow", "resume"):
        if isinstance(value.get(key), bool):
            result[key] = value[key]
    if tail_start_id is not None:
        result["tail_start_id"] = tail_start_id
    return result


def _legacy_message_id(
    session_id: str,
    index: int,
    message: dict,
    nonce: int = 0,
) -> str:
    public = {
        key: value
        for key, value in message.items()
        if not key.startswith("_nz_")
    }
    canonical = json.dumps(public, sort_keys=True, ensure_ascii=False, default=str)
    seed = f"nz-coder-message:{session_id}:{index}:{nonce}:{canonical}"
    return f"msg-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex}"


def _legacy_part_id(message_id: str, nonce: int = 0) -> str:
    seed = f"nz-coder-part:{message_id}:{nonce}"
    return f"part-{uuid.uuid5(uuid.NAMESPACE_URL, seed).hex}"
