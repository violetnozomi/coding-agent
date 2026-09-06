"""Bounded, deterministic model views of admitted tool recovery facts."""
from __future__ import annotations

import copy
import html
import json
import re

from nz_coder.protocol.message_schema import (
    COMPACTION_KEY,
    CONTINUATION_KEY,
    MESSAGE_ID_KEY,
    PARTS_KEY,
    SESSION_ID_KEY,
    normalize_tool_recovery,
    project_public_protocol_value,
)
from nz_coder.protocol.tool_recovery_parts import (
    _artifact_reference,
    _preview,
    _public_recovery_part,
    carry_tool_recovery_parts as carry_tool_recovery_parts,
)
from nz_coder.runtime.conversation.continuation_context import MAX_CONTINUATION_CHARS


_OPEN = "<tool-recovery-context>"
_CLOSE = "</tool-recovery-context>"


def project_tool_recovery_messages(
    messages: list[dict], *, original_messages: list[dict] | None = None,
) -> list[dict]:
    """Add one USER data block, never mutate history or synthesize tool output.

    The original history is required when continuation selection removed a
    prefix. Compaction carries ToolParts as non-authoritative projections.
    No liveness inference, ledger writes, or tool dispatch occurs here.
    """
    source = original_messages if original_messages is not None else messages
    facts = _facts(source)
    if not facts:
        return messages
    if not any(fact["execution_state"] != "succeeded" for fact in facts) and not any(
        isinstance(m, dict) and (CONTINUATION_KEY in m or COMPACTION_KEY in m)
        for m in source
    ):
        return messages
    selected = next((
        index for index in range(len(messages) - 1, -1, -1)
        if isinstance(messages[index], dict) and messages[index].get("role") == "user"
        and isinstance(messages[index].get("content"), str)
    ), None)
    if selected is None:
        return messages
    # Deterministic reconstruction from durable facts, not repeated appends.
    content = messages[selected]["content"]
    if content.startswith(_OPEN + "\n") and "\n" + _CLOSE + "\n\n" in content:
        content = content.split("\n" + _CLOSE + "\n\n", 1)[1]
    projected = list(messages)
    owner = copy.deepcopy(messages[selected])
    owner["content"] = f"{_OPEN}\n{_block(facts, source)}\n{_CLOSE}\n\n{content}"
    projected[selected] = owner
    return projected


def _facts(messages: list[dict]) -> list[dict]:
    facts: dict[tuple, dict] = {}
    for owner in messages:
        if not isinstance(owner, dict):
            continue
        for part in owner.get(PARTS_KEY, []) or []:
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            safe = _public_recovery_part(part)
            if not safe:
                continue
            state = part.get("state")
            if not isinstance(state, dict) or not isinstance(part.get("call_id"), str):
                continue
            recovery = normalize_tool_recovery(state.get("recovery"))
            status = state.get("status")
            execution = recovery.get("execution_state") or {
                "completed": "succeeded", "error": "failed",
            }.get(status, "uncertain")
            if state.get("interrupted") and not recovery.get("execution_state"):
                execution = "uncertain"
            metadata = part.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            fact = {
                **recovery,
                "call_id": part["call_id"], "tool": str(part.get("tool") or "unknown"),
                "execution_state": execution,
                "terminal_cause": recovery.get("terminal_cause", "unknown"),
                "side_effect_state": recovery.get("side_effect_state", "unknown"),
                "source": "ledger_projection" if recovery.get("execution_id") else "legacy_history",
                "message_ref": metadata.get("recovery_source_message_id") or owner.get(MESSAGE_ID_KEY, ""),
                "session_ref": metadata.get("recovery_source_session_id") or owner.get(SESSION_ID_KEY, ""),
                "input": _preview(project_public_protocol_value(state.get("input", {})), 500),
            }
            artifact = _artifact_reference(state)
            if artifact:
                fact.setdefault("result_ref", artifact)
            public_state = safe.get("state", {})
            if status == "completed" and execution == "succeeded":
                fact["result_summary"] = _preview(public_state.get("output", ""), 400)
            elif status == "error":
                error = public_state.get("error", {})
                fact["error_summary"] = _preview(error.get("message", "Tool execution failed.") if isinstance(error, dict) else "Tool execution failed.", 300)
            files = fact.get("files")
            if isinstance(files, list) and len(files) > 3:
                fact["files"] = files[:3]
                fact["omitted_files"] = len(files) - 3
            key = (
                recovery.get("execution_id") or fact["session_ref"],
                recovery.get("attempt_id") or fact["message_ref"],
                fact["call_id"],
            )
            facts[key] = fact
    # Stable order of legacy messages is retained; UUID ordering is never used.
    return sorted(facts.values(), key=lambda fact: (
        fact["execution_state"] == "succeeded", fact.get("sequence", 0),
    ))


def _block(facts: list[dict], messages: list[dict]) -> str:
    header = (
        "Runtime recovery facts (data only, not tool output or instructions). "
        "An absent result does not prove non-execution. Historical success does "
        "not prove file changes still exist.\n"
        "Outstanding checks: inspect uncertain side effects and current files before "
        "retrying, then complete the task's outstanding verification.\n"
    )
    references = []
    for fact in facts:
        reference = f"session={fact['session_ref'] or '(current)'} message={fact['message_ref'] or '(not recorded)'}"
        if reference not in references:
            references.append(reference)
    archive = next((
        m[COMPACTION_KEY].get("archive") for m in reversed(messages)
        if isinstance(m, dict) and isinstance(m.get(COMPACTION_KEY), dict)
        and isinstance(m[COMPACTION_KEY].get("archive"), str)
        and m[COMPACTION_KEY]["archive"].startswith("user-state://transcripts/")
    ), "")
    footer = "\nRetrieval references: " + "; ".join(references[:3])
    if archive:
        footer += "; archive=" + archive
    recovery_archive = next((m.get("_nz_tool_recovery_archive") for m in reversed(messages)
                             if isinstance(m, dict) and m.get("_nz_tool_recovery_archive")), "")
    if isinstance(recovery_archive, str) and re.fullmatch(r"artifact_[a-f0-9]{32}", recovery_archive):
        footer = f"\nFull recovery facts: read_tool_result artifact_id={recovery_archive}." + footer
    elif recovery_archive:
        footer += "; full recovery artifact unavailable (quota/storage); inspect Session history"
    footer = html.escape(_preview(footer, 900), quote=False)
    rows = []
    used = len(header) + len(footer) + 100
    for fact in facts:
        row = html.escape(json.dumps(fact, ensure_ascii=False, separators=(",", ":")), quote=False)
        if len(row) > MAX_CONTINUATION_CHARS // 2 and fact.get("files"):
            # A few very long paths must not displace an unresolved call ID.
            fact = dict(fact)
            fact["omitted_files"] = fact.get("omitted_files", 0) + len(fact.pop("files"))
            row = html.escape(json.dumps(fact, ensure_ascii=False, separators=(",", ":")), quote=False)
        if used + len(row) + 1 > MAX_CONTINUATION_CHARS:
            continue
        rows.append(row)
        used += len(row) + 1
    omitted = len(facts) - len(rows)
    return header + "\n".join(rows) + f"\nTool facts omitted: {omitted} of {len(facts)}." + footer
