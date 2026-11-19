"""Deterministic context boundaries for continuing unfinished Agent runs."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import time

from nz_coder.protocol.message_schema import (
    COMPACTION_KEY,
    CONTINUATION_KEY,
    is_synthetic_user_message,
)


CONTINUATION_VERSION = 1
RESUMABLE_STATUSES = frozenset({"max_turns", "interrupted", "blocked"})
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
    from nz_coder.runtime.verification.verification_contract import declared_acceptance
    checks = declared_acceptance(runtime_state)
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
        *(line for check in checks for line in _verification_lines({"command": check["command"], "passed": check["status"] == "passed" if check["status"] != "pending" else None, "output": check.get("output", "")})),
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
            "## Candidate Investigation Paths",
            "- Diagnostic suggestions; not confirmed repair locations.",
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
        # 旧自由文本混有当前状态，不能未经结构保证就改名为纯背景。
        "background": "",
        "current_state_snapshot": {
            "changed_files": changed_files, "unresolved": unresolved,
            "verification": {"acceptance_checks": list(checks)},
        },
    }


def has_authoritative_task(messages: list, runtime_state) -> bool:
    """只有能与当前真实用户要求核对的状态，才可覆盖历史边界的兜底。"""
    task = _text_field(runtime_state, "initial_task_text")
    if not task:
        return False
    authority = _text_field(runtime_state, "task_authority_message_id")
    return any(_is_human_user(message) and (
        str(message.get("_nz_user_text") or message.get("content") or "").strip() == task
        or bool(authority) and message.get("_nz_message_id") == authority
    ) for message in messages)


def project_continuation_messages(messages: list[dict], *, authoritative_state: bool = False) -> list[dict]:
    """Replace a resumable run prefix in the model view, never durable state."""
    details = continuation_projection_details(messages)
    if details is None:
        return messages
    boundary = details["boundary"]
    tail_start = details["tail_start"]

    projected = list(messages[tail_start:])
    originals = [copy.deepcopy(message) for message in messages[:tail_start] if _is_human_user(message)]
    first_user = copy.deepcopy(projected[0])
    content = first_user.get("content")
    summary = _safe_context_text(str(boundary.get("background" if authoritative_state else "summary") or ""))
    if authoritative_state and not summary:
        return [*originals, *projected]
    if not isinstance(content, str) or not content.strip() or not summary:
        return messages
    first_user["content"] = (
        "<continuation-context>\n"
        "Historical snapshot; current verification unknown. Bounded background from a previous unfinished run follows. It is "
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
    return [*originals, *projected]


def _recent_check_receipts(snapshot) -> list[str]:
    """执行来自宿主，只有当前检查决定能说明哪一项验证已获采信。"""
    def quoted(value):
        return _safe_context_text(json.dumps(_bounded_text(value, 320), ensure_ascii=False))

    facts = {fact.execution_id: fact for fact in snapshot.executions if fact.scope == snapshot.scope}
    order = {key: index for index, key in enumerate(facts)}
    decisions = {item.check_id: item for item in snapshot.checks}
    matched = set()
    checks = []
    for spec in snapshot.specs:
        decision = decisions[spec.id]
        fact = facts.get(decision.execution_id)
        if fact is None and not spec.required:
            continue
        identity = f"{quoted(spec.command.original)} cwd={quoted(spec.command.cwd)}"
        label = f"{spec.capability}, {'required' if spec.required else 'observed'}"
        if fact is not None:
            matched.add(fact.execution_id)
        current = (fact is not None and not snapshot.integrity_errors and not snapshot.rebuild_required
                   and snapshot.version.scope_complete and fact.scope_complete
                   and fact.before.scope_complete and fact.after.scope_complete
                   and fact.before.acceptance_generation == fact.after.acceptance_generation
                   == snapshot.version.acceptance_generation)
        if decision.status == "passed" and current:
            targets = " targets=" + quoted(", ".join(spec.targets)) if spec.targets else ""
            line = (f"Verified check ({label}, current acceptance generation "
                    f"{snapshot.version.acceptance_generation}): {identity}{targets}.")
        else:
            status = "current validity unknown" if decision.status == "passed" else decision.status
            line = f"Check decision ({label}, {status}): {identity}."
            if decision.status == "pending" and decision.reason:
                line += f" Reason: {quoted(decision.reason)}."
            if fact is not None:
                line += f" Recorded execution: {fact.outcome}, exit={fact.exit_code}."
        checks.append((not spec.required, decision.status in {"passed", "covered"},
                       -order.get(decision.execution_id, -1), line))

    # 优先展示必需及未结清的检查，再合并原因相同的普通执行。
    items = [entry[-1] for entry in sorted(checks)]
    observed_reasons = set()
    for fact in reversed(snapshot.executions):
        if fact.scope != snapshot.scope or not fact.capabilities or fact.execution_id in matched:
            continue
        if not fact.output_complete:
            reason = "incomplete output"
        elif not fact.reliable and fact.command.opaque:
            reason = "individual checks unproven; use current verified results and separate missing checks if needed"
        elif not fact.reliable:
            reason = "execution cannot establish a successful check"
        elif fact.test_count == 0:
            reason = "no tests executed"
        else:
            reason = "no matching check decision"
        if reason in observed_reasons:
            continue
        observed_reasons.add(reason)
        candidates = " candidate targets=" + quoted(", ".join(fact.targets)) if fact.targets else ""
        items.append(f"Execution only ({fact.outcome}, exit={fact.exit_code}): {quoted(fact.command.original)} "
                     f"cwd={quoted(fact.command.cwd)}; {reason}.{candidates}")
    return items


def build_current_facts(runtime_state, *, workspace, run_evidence=None, max_tokens: int = 2000) -> str:
    """按完整条目呈现当前状态；摘要文本不能反向成为验证真值。"""
    from nz_coder.runtime.agent.planning_runtime import render_execution_handoff
    from nz_coder.runtime.conversation.prompt_builder import estimate_text_tokens
    from nz_coder.runtime.verification.verification_contract import effective_acceptance_generation
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError

    domain = _field(runtime_state, "verification_domain", None)
    snapshot = domain.snapshot() if domain is not None and not isinstance(runtime_state, dict) else None
    if snapshot is not None and Path(snapshot.scope.task.workspace) != Path(workspace).resolve():
        return "<current-task-facts>Verification unknown for this workspace.</current-task-facts>"
    header = "<current-task-facts>\nCurrent authoritative state (hypotheses remain unverified)."
    closing = "\n</current-task-facts>"
    generation = int(_field(runtime_state, "mutation_generation", 0))
    acceptance = effective_acceptance_generation(runtime_state)
    base = f"\nWorkspace mutation generation: {generation}; acceptance generation: {acceptance}."
    handoff = (render_execution_handoff(runtime_state, workspace=workspace)
               if _field(runtime_state, "execution_handoff", None) else "")
    essential = header + base + ("\n" + handoff if handoff else "")
    omission = "\nAdditional complete items omitted by context budget; recheck authoritative tools."
    limit = min(2000, max_tokens)
    if estimate_text_tokens(essential + omission + closing) > limit:
        raise PublicRuntimeError(PublicError("context_cannot_shrink", "Minimum current state exceeds the context budget."))
    items = []
    static_current = bool(
        _field(runtime_state, "changed_files_verified", False)
        and _field(runtime_state, "py_compile_ok", False)
        and _field(runtime_state, "verification_generation", -1) == generation
    )
    if static_current:
        items.append(f"Scoped changed-file verification: status=passed; recorded generation={generation}; not task acceptance.")
    elif _field(runtime_state, "verification_attempts", 0):
        items.append("Scoped changed-file verification: current validity unknown; rerun after mutations.")
    from nz_coder.runtime.verification.verification_contract import declared_acceptance
    checks = snapshot.acceptance_checks if snapshot is not None else declared_acceptance(runtime_state)
    for check in checks:
        items.append("Declared verification: status=" + check["status"] + "; command=" + check["command"]
                     + "; cwd=" + str(check.get("cwd") or "."))
    receipts = _recent_check_receipts(snapshot) if snapshot is not None else []
    if not checks and receipts:
        items.append("No user-declared acceptance check. The following results apply to their own checks only.")
    elif not checks:
        items.append("Current verification unknown; no declared verification result for this generation.")
    items.extend(receipts)
    for item in _dict_field(runtime_state, "requirement_ledger").get("items") or []:
        if not isinstance(item, dict) or item.get("status") == "satisfied":
            continue
        requirement = item.get("requirement") or {}
        items.append("Unresolved requirement: " + str(requirement.get("id") or "?") + " ["
                     + str(item.get("status") or "pending") + "] " + str(requirement.get("description") or ""))
    items.extend("Changed file: " + str(path) for path in _field(runtime_state, "changed_files", []))
    if _field(runtime_state, "last_verification_failure", ""):
        items.append("Last verification failure (historical unless generation matches): "
                     + str(_field(runtime_state, "last_verification_failure", "")))
    observations = [*_field(run_evidence, "verification_evidence", []), *_field(run_evidence, "verification_results", [])]
    seen_observations = set()
    for evidence in observations[-12:]:
        if not isinstance(evidence, dict):
            continue
        identity = (evidence.get("tool"), evidence.get("command"), evidence.get("stage") or evidence.get("scope"))
        if identity in seen_observations:
            continue
        seen_observations.add(identity)
        # 观测只作为来源；没有可核对代次时明确未知，不伪造当前通过。
        items.append("Verification observation: " + str(evidence.get("tool") or evidence.get("stage") or "unknown")
                     + "; current validity unknown; command=" + str(evidence.get("command") or ""))
    rendered = essential
    omitted = False
    for item in items:
        if estimate_text_tokens(rendered + "\n" + item + omission + closing) <= limit:
            rendered += "\n" + item
        else:
            omitted = True
    return rendered + (omission if omitted else "") + closing


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
    from nz_coder.runtime.core.execution_context import is_genuine_runtime_followup

    activation_index = next((
        index for index in range(len(messages) - 1, boundary_index, -1)
        if is_genuine_runtime_followup(messages[index])
    ), None)
    if activation_index is not None and any(
        isinstance(message, dict) and message.get("role") == "assistant"
        for message in messages[activation_index + 1:]
    ):
        activation_index = None
    current_message = messages[tail_start]
    current_content = current_message.get("content")
    if not isinstance(current_content, str) or not current_content.strip():
        return None
    boundary_id = str(boundary_message.get("_nz_message_id") or boundary_index)
    current_id = str(current_message.get("_nz_message_id") or tail_start)
    return {
        "boundary": boundary,
        "tail_start": tail_start,
        "activation_index": activation_index,
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
    activation_index = details["activation_index"]
    if activation_index is None:
        return False
    current = messages[activation_index].get("content")
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
