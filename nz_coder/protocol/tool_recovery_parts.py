"""Pure recovery-part projections shared by compaction and runtime views."""
from __future__ import annotations

import json
import re

from nz_coder.protocol.message_schema import (
    MESSAGE_ID_KEY,
    PARTS_KEY,
    SESSION_ID_KEY,
    attach_message_identity,
    project_public_message_part,
)


def carry_tool_recovery_parts(messages: list[dict], summary: dict, *, session_id: str) -> None:
    """Carry admitted identities across semantic compaction, not model prose.

    These are bounded result previews and original ToolPart identities. The
    ledger remains authoritative and may replace their recovery fields later.
    Full content remains in the existing compaction transcript archive.
    """
    parts = []
    for owner in messages:
        if not isinstance(owner, dict):
            continue
        for part in owner.get(PARTS_KEY, []) or []:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state")
            if not isinstance(state, dict):
                continue
            carried = _public_recovery_part(part)
            if not carried:
                continue
            carried["internal"] = True
            carried["authoritative"] = False
            metadata = part.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            carried["metadata"] = {
                "recovery_source_message_id": metadata.get("recovery_source_message_id") or owner.get(MESSAGE_ID_KEY, ""),
                "recovery_source_session_id": metadata.get("recovery_source_session_id") or owner.get(SESSION_ID_KEY, session_id),
            }
            carried_state = carried["state"]
            carried_state.pop("raw", None)
            carried_state.pop("metadata", None)
            if isinstance(carried_state.get("output"), str):
                carried_state["output"] = _preview(carried_state["output"], 400)
            artifact = _artifact_reference(state)
            if artifact:
                carried_state.setdefault("recovery", {"version": 1}).setdefault("result_ref", artifact)
            parts.append(carried)
    if not parts:
        return
    message_id = attach_message_identity(summary, session_id=session_id)
    for part in parts:
        part["message_id"] = message_id
    summary[PARTS_KEY] = parts


def _preview(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=lambda _value: "[unavailable]")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _public_recovery_part(part: dict) -> dict:
    """Only runtime-carried projections may bypass the internal display flag."""
    metadata = part.get("metadata")
    if (
        part.get("internal") is True and part.get("authoritative") is False
        and isinstance(metadata, dict)
        and isinstance(metadata.get("recovery_source_message_id"), str)
        and metadata["recovery_source_message_id"].startswith("msg-")
    ):
        part = {**part, "internal": False}
    return project_public_message_part(part)


def _artifact_reference(state: dict) -> str:
    """Legacy result metadata may contain an opaque ID, never expose host paths."""
    metadata = state.get("metadata")
    projection = metadata.get("projection") if isinstance(metadata, dict) else None
    artifact = projection.get("artifact_path") if isinstance(projection, dict) else None
    return artifact if isinstance(artifact, str) and re.fullmatch(r"artifact_[a-f0-9]{32}", artifact) else ""
