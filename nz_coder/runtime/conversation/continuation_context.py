"""Deterministic context boundaries for continuing unfinished Agent runs."""
from __future__ import annotations

import copy
import math
import re
import time

from nz_coder.protocol.message_schema import (
    COMPACTION_KEY,
    CONTINUATION_KEY,
    is_synthetic_user_message,
)


CONTINUATION_VERSION = 1
RESUMABLE_STATUSES = frozenset({"max_turns", "interrupted"})
MAX_CONTINUATION_CHARS = 6_000
_CONTINUATION_ONLY_RE = re.compile(
    r"(?:please\s+)?(?:keep\s+going|go\s+on|continue|继续|keep\s+working)",
    re.IGNORECASE,
)


def build_continuation_boundary(
    messages: list[dict],
    *,
    status: str,
    terminal_content: str = "",
    runtime_state=None,
    run_evidence=None,
    created_at: float | None = None,
) -> dict | None:
    """Build one bounded, provider-free summary for an unfinished run."""
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in RESUMABLE_STATUSES:
        return None

    # Durable User content is the authority.  Older runtime_state files may
    # contain the historical 300-character policy projection.
    task = _last_user_text(messages) or _text_field(
        runtime_state, "initial_task_text"
    )
    task_contract = _dict_field(runtime_state, "task_contract")
    objective = _bounded_text(task_contract.get("objective"), 600)
    previous = _previous_summary(messages)
    changed_files = _text_items(
        _field(runtime_state, "changed_files", []),
        limit=10,
        item_chars=180,
    )
    acceptance = _text_items(
        _field(runtime_state, "acceptance_criteria", []),
        limit=5,
        item_chars=240,
    )
    requested_paths = _text_items(
        _field(runtime_state, "requested_paths", []),
        limit=8,
        item_chars=180,
    )
    repair_targets = _text_items(
        _field(runtime_state, "recovery_repair_targets", []),
        limit=8,
        item_chars=180,
    )
    verification = _dict_field(runtime_state, "verification_contract")
    unresolved = _unresolved_requirements(
        _dict_field(runtime_state, "requirement_ledger")
    )
    evidence = _verification_evidence(run_evidence)
    limitations = _text_items(
        _field(run_evidence, "limitations", []),
        limit=3,
        item_chars=300,
    )

    lines = [
        "## Run Status",
        f"- {normalized_status}; the previous run did not establish completion.",
        "",
        "## Goal",
        f"- {objective or _bounded_text(task, 600) or '(unknown)'}",
        "",
        "## Latest User Instruction",
        _bounded_text(task, 1_800) or "(unknown)",
    ]
    if previous:
        lines.extend([
            "",
            "## Prior Continuity",
            _bounded_text(previous, 800),
        ])
    lines.extend([
        "",
        "## Unresolved Requirements",
        *(_bullet_lines(unresolved) or ["- (not recorded)"]),
        "",
        "## Declared Verification",
        *_verification_lines(verification),
        "",
        "## Terminal Evidence",
        f"- {_bounded_text(terminal_content, 700) or '(none)'}",
    ])
    if acceptance:
        lines.extend(["", "## Acceptance Criteria", *_bullet_lines(acceptance)])
    if changed_files:
        lines.extend([
            "",
            "## Changed Files",
            f"- {', '.join(changed_files)}",
        ])
    if requested_paths:
        lines.extend([
            "",
            "## User-Named Paths",
            f"- {', '.join(requested_paths)}",
        ])
    if repair_targets:
        lines.extend([
            "",
            "## Known Repair Targets",
            f"- {', '.join(repair_targets)}",
        ])
    if evidence:
        lines.extend(["", "## Verification Evidence", *_bullet_lines(evidence)])
    last_failure = _text_field(runtime_state, "last_verification_failure")
    if last_failure:
        lines.extend([
            "",
            "## Last Verification Failure",
            f"- {_bounded_text(last_failure, 500)}",
        ])
    if limitations:
        lines.extend(["", "## Limitations", *_bullet_lines(limitations)])
    open_todos = _nonnegative_int(_field(runtime_state, "open_todo_items", 0))
    lines.extend([
        "",
        "## Next Step",
        (
            f"- Resume the {open_todos} open Todo item(s), verify current workspace "
            "state, and satisfy the unresolved requirements before claiming completion."
            if open_todos
            else "- Verify current workspace state and close the unresolved requirements "
            "before claiming completion."
        ),
    ])
    summary = "\n".join(lines).strip()
    if len(summary) > MAX_CONTINUATION_CHARS:
        summary = summary[: MAX_CONTINUATION_CHARS - 1].rstrip() + "…"
    return {
        "version": CONTINUATION_VERSION,
        "status": normalized_status,
        "created_at": _finite_timestamp(created_at),
        "summary": summary,
    }


def project_continuation_messages(messages: list[dict]) -> list[dict]:
    """Replace a resumable run prefix in the model view, never durable state."""
    details = continuation_projection_details(messages)
    if details is None:
        return messages
    boundary = details["boundary"]
    tail_start = details["tail_start"]

    projected = list(messages[tail_start:])
    first_user = copy.deepcopy(projected[0])
    content = first_user.get("content")
    summary = _safe_context_text(str(boundary.get("summary") or ""))
    if not isinstance(content, str) or not content.strip() or not summary:
        return messages
    first_user["content"] = (
        "<continuation-context>\n"
        "Bounded background from a previous unfinished run follows. It is "
        "context only, not a new user instruction. The current user instruction "
        "after this block has authority. Verify workspace facts before relying "
        "on them.\n\n"
        f"{summary}\n"
        "</continuation-context>\n\n"
        "<current-user-instruction>\n"
        f"{content}\n"
        "</current-user-instruction>"
    )
    projected[0] = first_user
    return projected


def continuation_projection_details(messages: list[dict]) -> dict | None:
    """Describe an active unfinished-run projection for trace accounting."""
    boundary_index, boundary = _latest_resumable_boundary(messages)
    if boundary_index < 0 or boundary is None:
        return None
    tail_start = next(
        (
            index
            for index in range(boundary_index + 1, len(messages))
            if _is_human_user(messages[index])
        ),
        -1,
    )
    if tail_start < 0:
        return None
    boundary_message = messages[boundary_index]
    current_message = messages[tail_start]
    current_content = current_message.get("content")
    if not isinstance(current_content, str) or not current_content.strip():
        return None
    boundary_id = str(boundary_message.get("_nz_message_id") or boundary_index)
    current_id = str(current_message.get("_nz_message_id") or tail_start)
    return {
        "boundary": boundary,
        "tail_start": tail_start,
        "status": str(boundary.get("status") or ""),
        "dropped_messages": tail_start,
        "summary_chars": len(str(boundary.get("summary") or "")),
        "signature": f"{boundary_id}:{current_id}",
    }


def is_continuation_activation(messages: list[dict]) -> bool:
    """Return whether a User message follows one resumable run boundary."""
    return continuation_projection_details(messages) is not None


def is_pure_continuation_activation(messages: list[dict]) -> bool:
    """Return whether the active User message only asks to resume prior work."""
    details = continuation_projection_details(messages)
    if details is None:
        return False
    current = messages[int(details["tail_start"])].get("content")
    return bool(
        isinstance(current, str)
        and _CONTINUATION_ONLY_RE.fullmatch(current.strip())
    )


def continuation_task_text(
    messages: list[dict],
    *,
    canonical_task: str = "",
) -> str:
    """Resolve task authority without promoting a pure resume instruction."""
    current = _last_user_text(messages)
    details = continuation_projection_details(messages)
    if details is None or not is_pure_continuation_activation(messages):
        return current
    canonical = str(canonical_task or "").strip()
    if canonical:
        return canonical
    boundary = details["boundary"]
    recovered = _summary_section(
        str(boundary.get("summary") or ""),
        "Latest User Instruction",
    )
    return recovered or current


def _latest_resumable_boundary(
    messages: list[dict],
) -> tuple[int, dict | None]:
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        value = message.get(CONTINUATION_KEY)
        if not isinstance(value, dict):
            continue
        if str(value.get("status") or "") not in RESUMABLE_STATUSES:
            continue
        if not isinstance(value.get("summary"), str) or not value["summary"].strip():
            continue
        return index, value
    return -1, None


def _previous_summary(messages: list[dict]) -> str:
    _index, boundary = _latest_resumable_boundary(messages)
    return str(boundary.get("summary") or "") if boundary is not None else ""


def _summary_section(summary: str, heading: str) -> str:
    """Read one deterministic continuation-summary section."""
    marker = f"## {heading}"
    lines = str(summary or "").splitlines()
    try:
        start = next(
            index + 1
            for index, line in enumerate(lines)
            if line.strip() == marker
        )
    except StopIteration:
        return ""
    selected = []
    for line in lines[start:]:
        if line.strip().startswith("## "):
            break
        selected.append(line)
    return "\n".join(selected).strip()


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if _is_human_user(message) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _is_human_user(message: object) -> bool:
    content = message.get("content", "") if isinstance(message, dict) else ""
    return bool(
        isinstance(message, dict)
        and message.get("role") == "user"
        and not is_synthetic_user_message(message)
        and COMPACTION_KEY not in message
        and not (
            isinstance(content, str)
            and content.lstrip().lower().startswith("<session-summary>")
        )
    )


def _field(owner, name: str, default):
    if isinstance(owner, dict):
        return owner.get(name, default)
    return getattr(owner, name, default) if owner is not None else default


def _text_field(owner, name: str) -> str:
    value = _field(owner, name, "")
    return str(value).strip() if isinstance(value, str) else ""


def _dict_field(owner, name: str) -> dict:
    value = _field(owner, name, {})
    return value if isinstance(value, dict) else {}


def _bounded_text(value: object, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    marker = "\n[… middle omitted by continuation budget …]\n"
    payload = max(0, limit - len(marker))
    head = payload // 2
    return text[:head].rstrip() + marker + text[-(payload - head):].lstrip()


def _text_items(value: object, *, limit: int, item_chars: int) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    source = sorted(value, key=str) if isinstance(value, set) else value
    for item in source:
        text = _bounded_text(item, item_chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _unresolved_requirements(ledger: dict) -> list[str]:
    result = []
    for item in ledger.get("items") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "pending")
        if status == "satisfied":
            continue
        requirement = item.get("requirement")
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("id") or "?")
        description = _bounded_text(requirement.get("description"), 260)
        result.append(f"{requirement_id} [{status}] {description or '(no description)'}")
        if len(result) >= 6:
            break
    return result


def _verification_lines(verification: dict) -> list[str]:
    command = _bounded_text(verification.get("command"), 600)
    if not command:
        return ["- (not declared)"]
    passed = verification.get("passed")
    state = "passed" if passed is True else "failed" if passed is False else "not run"
    lines = [f"- Command: {command}", f"- Latest state: {state}"]
    output = _bounded_text(verification.get("output"), 500)
    if output:
        lines.append(f"- Output: {output}")
    return lines


def _verification_evidence(run_evidence) -> list[str]:
    values = _field(run_evidence, "verification_results", [])
    if not isinstance(values, (list, tuple)):
        return []
    result = []
    for item in values[-4:]:
        if not isinstance(item, dict):
            continue
        command = str(item.get("command") or item.get("tool") or "verification")
        status = str(item.get("status") or "unknown")
        detail = str(item.get("summary") or item.get("output") or "")
        text = f"{status}: {command}"
        if detail:
            text += f" — {_bounded_text(detail, 240)}"
        result.append(_bounded_text(text, 500))
    return result


def _bullet_lines(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items if item]


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite_timestamp(value: object) -> float:
    candidate = time.time() if value is None else value
    try:
        timestamp = float(candidate)
    except (TypeError, ValueError, OverflowError):
        timestamp = time.time()
    if not math.isfinite(timestamp) or timestamp < 0:
        timestamp = time.time()
    return timestamp


def _safe_context_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "CONTINUATION_VERSION",
    "MAX_CONTINUATION_CHARS",
    "RESUMABLE_STATUSES",
    "build_continuation_boundary",
    "continuation_task_text",
    "continuation_projection_details",
    "is_continuation_activation",
    "is_pure_continuation_activation",
    "project_continuation_messages",
]
