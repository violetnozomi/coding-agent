"""Deterministic context boundaries for continuing unfinished Agent runs."""
from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Iterator
import copy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import time

from nz_coder.protocol.message_schema import (
    ASSISTANT_END_STATE_KEY,
    ASSISTANT_ERROR_KEY,
    ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY,
    AUTHORITATIVE_KEY,
    COMPACTION_KEY,
    CONTINUATION_KEY,
    INTERNAL_KEY,
    MESSAGE_ID_KEY,
    PARTS_KEY,
    SESSION_ID_KEY,
    VISIBLE_KEY,
    is_synthetic_user_message,
    project_public_message_part,
)


CONTINUATION_VERSION = 1
RESUMABLE_STATUSES = frozenset({"max_turns", "interrupted", "blocked"})
MAX_CONTINUATION_CHARS = 6_000
# 老会话的纯文本消息在加载时会补上身份、父消息、时间和文本块。
_LEGACY_TEXT_FIELDS = frozenset({
    "role", "content", "_timestamp", MESSAGE_ID_KEY, SESSION_ID_KEY, ASSISTANT_PARENT_KEY,
    ASSISTANT_TIME_KEY, PARTS_KEY,
})
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
    projection = _continuation_projection(messages)
    if projection is None:
        return messages
    details, dialogue = projection
    boundary = details["boundary"]
    tail_start = details["tail_start"]

    projected = list(messages[tail_start:])
    originals = copy.deepcopy(dialogue)
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


def _check_value(value: str) -> str:
    return _safe_context_text(json.dumps(_bounded_text(value, 320), ensure_ascii=False))


def _execution_detail(fact, *, historical: bool, matched: bool, show_id: bool, known_reason: str = "") -> str:
    kind = "Historical execution" if historical else "Recorded execution" if matched else "Execution only"
    generation = str(fact.after.generation)
    if fact.before.generation != fact.after.generation:
        generation = f"{fact.before.generation}->{fact.after.generation}"
    line = (f"{kind} ({fact.outcome}, exit={fact.exit_code}); generation={generation}; "
            f"acceptance generation={fact.after.acceptance_generation}")
    if fact.targets:
        line += "; targets=" + _check_value(", ".join(fact.targets))
    if not fact.output_complete:
        reason = "incomplete output"
    elif not fact.reliable and fact.command.opaque:
        reason = "individual checks unproven; use current verified results and separate missing checks if needed"
    elif not fact.reliable:
        reason = "execution cannot establish a successful check"
    elif fact.test_count == 0:
        reason = "no tests executed"
    elif not matched:
        reason = "no matching check decision"
    else:
        reason = ""
    if reason and reason != known_reason:
        line += "; " + reason
    if fact.reason and fact.reason != known_reason and fact.outcome != "succeeded":
        line += "; " + _check_value(fact.reason)
    if show_id:
        line += "; receipt=" + _check_value(fact.execution_id)
    return line + "."


@dataclass
class _CheckContext:
    current: tuple[str, ...]
    history: Iterator[str]


def _execution_is_historical(fact, snapshot) -> bool:
    return (fact.scope != snapshot.scope or snapshot.workspace_mutating
            or fact.before.acceptance_generation != snapshot.version.acceptance_generation
            or fact.after.acceptance_generation != snapshot.version.acceptance_generation)


def _remaining_receipts(facts, rendered_ids, matched_ids, cross_command_ids, snapshot, specs):
    for fact in reversed(facts):
        if fact.execution_id in rendered_ids:
            continue
        matched = fact.execution_id in matched_ids
        # 同一检查的旧回执失去当前决定的引用后，仍可解释上次运行了什么。
        prior = not matched and any(spec.capability in fact.capabilities
                                    and (not spec.targets or set(spec.targets).issubset(fact.targets)) for spec in specs)
        yield _execution_detail(fact, historical=prior or _execution_is_historical(fact, snapshot),
                                matched=matched, show_id=fact.execution_id in cross_command_ids)


def _check_receipt_groups(snapshot, declarations) -> list[_CheckContext]:
    """按完整命令身份组织；决定只引用自己的回执，写法不同的实际命令仍可追溯。"""
    specs_by_command = defaultdict(list)
    executions_by_command = defaultdict(list)
    decisions = {item.check_id: item for item in snapshot.checks}
    facts = {fact.execution_id: fact for fact in snapshot.executions
             if fact.capabilities and fact.scope.task == snapshot.scope.task
             and fact.scope.agent_id == snapshot.scope.agent_id}
    for fact in facts.values():
        executions_by_command[fact.command].append(fact)
    matched_ids, cross_command_ids = set(), set()
    for spec in snapshot.specs:
        decision = decisions[spec.id]
        fact = facts.get(decision.execution_id)
        if spec.required or fact or spec.command in executions_by_command:
            specs_by_command[spec.command].append(spec)
        if fact:
            matched_ids.add(fact.execution_id)
            if fact.command != spec.command:
                cross_command_ids.add(fact.execution_id)

    declared = {item["check_ids"][0]: item for item in declarations}
    groups = []
    for command in dict.fromkeys((*specs_by_command, *executions_by_command)):
        specs = specs_by_command[command]
        declaration = next((declared[spec.id] for spec in specs if spec.id in declared), None)
        identity = f"{_check_value(command.original)}; cwd={_check_value(command.cwd)}"
        if declaration:
            status = declaration["status"]
            if status == "passed" and not all(snapshot.current_success(facts.get(decisions[check_id].execution_id))
                                              for check_id in declaration["check_ids"]):
                status = "pending"
            header = f"Declared verification: status={status}; command={identity}."
        else:
            heading = "Check definition" if specs else "Executed command"
            header = f"{heading}: {identity}."

        lines = [header]
        rendered_ids = set()
        for spec in specs:
            decision = decisions[spec.id]
            fact = facts.get(decision.execution_id)
            label = f"{spec.capability}, {'required' if spec.required else 'observed'}"
            if decision.status == "passed" and snapshot.current_success(fact):
                line = f"Verified check ({label}, current acceptance generation {snapshot.version.acceptance_generation})"
            else:
                status = "current validity unknown" if decision.status == "passed" else decision.status
                line = f"Check decision ({label}, {status})"
                if decision.reason:
                    line += "; reason=" + _check_value(decision.reason)
            if spec.targets and (not fact or fact.targets != spec.targets or fact.command != command):
                line += "; targets=" + _check_value(", ".join(spec.targets))
            if fact and fact.command != command:
                # 预算可能裁掉来源所在的整组；保留此行时仍须说明哪一方实际执行。
                line += "; a different command supplied the evidence (its details may be omitted); receipt=" + _check_value(fact.execution_id)
            line += "."
            if fact and fact.command == command and fact.execution_id not in rendered_ids:
                line += " " + _execution_detail(fact, historical=_execution_is_historical(fact, snapshot),
                                                matched=True, show_id=fact.execution_id in cross_command_ids,
                                                known_reason=decision.reason)
                rendered_ids.add(fact.execution_id)
            lines.append(line)

        remaining = _remaining_receipts(executions_by_command[command], rendered_ids, matched_ids,
                                        cross_command_ids, snapshot, specs)
        if not specs:
            # 展示一次裸执行时，命令和最新结果组成同一个可独立理解的条目。
            lines[0] += " " + next(remaining)
        priority = (declaration is None, not any(spec.required for spec in specs),
                    not any(decisions[spec.id].status in {"failed", "blocked_environment"} for spec in specs))
        groups.append((priority, _CheckContext(tuple(lines), remaining)))
    return [group for _, group in sorted(groups, key=lambda item: item[0])]


def build_current_facts(runtime_state, *, workspace, run_evidence=None, max_tokens: int = 2000) -> str:
    """按完整条目呈现当前状态；摘要文本不能反向成为验证真值。"""
    from nz_coder.runtime.agent.planning_runtime import render_execution_handoff
    from nz_coder.runtime.conversation.prompt_builder import estimate_text_tokens
    from nz_coder.runtime.execution.runtime_state import verification_snapshot_with_paths
    from nz_coder.runtime.verification.verification_contract import effective_acceptance_generation
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError

    if isinstance(runtime_state, dict):
        snapshot, changed = None, runtime_state.get("changed_files", ())
    else:
        snapshot, changed = verification_snapshot_with_paths(runtime_state)
    if snapshot is not None and Path(snapshot.scope.task.workspace) != Path(workspace).resolve():
        return "<current-task-facts>Verification unknown for this workspace.</current-task-facts>"
    header = "<current-task-facts>\nCurrent authoritative state (hypotheses remain unverified)."
    closing = "\n</current-task-facts>"
    generation = snapshot.version.generation if snapshot else int(_field(runtime_state, "mutation_generation", 0))
    acceptance = snapshot.version.acceptance_generation if snapshot else effective_acceptance_generation(runtime_state)
    base = f"\nWorkspace mutation generation: {generation}; acceptance generation: {acceptance}."
    handoff = (render_execution_handoff(runtime_state, workspace=workspace)
               if _field(runtime_state, "execution_handoff", None) else "")
    essential = header + base + ("\n" + handoff if handoff else "")
    omission = "\nAdditional complete items omitted by context budget; recheck authoritative tools."
    limit = min(2000, max_tokens)
    if estimate_text_tokens(essential + omission + closing) > limit:
        raise PublicRuntimeError(PublicError("context_cannot_shrink", "Minimum current state exceeds the context budget."))
    items: list[str | _CheckContext] = []
    if snapshot is not None:
        covered, complete = snapshot.syntax_coverage(changed)
        if covered:
            targets = ", ".join(_check_value(path) for path in covered)
            status = "passed" if complete else "partial"
            items.append(f"Scoped changed-file verification: status={status}; syntax targets={targets}; "
                         f"recorded generation={snapshot.version.generation}; not task acceptance.")
        checks = snapshot.acceptance_checks
        receipts = _check_receipt_groups(snapshot, checks)
        pending = [(item.requirement.id, item.status, item.requirement.description)
                   for item in snapshot.requirements if item.status != "satisfied"]
    else:
        from nz_coder.runtime.verification.verification_contract import declared_acceptance

        checks = declared_acceptance(runtime_state)
        # 无来源的旧检查点只能传递验收定义，无法证明当前工作区已经通过。
        receipts = [f"Declared verification: status=pending; command={_check_value(check['command'])}; "
                    f"cwd={_check_value(str(check.get('cwd') or '.'))}." for check in checks]
        pending = []
        for item in _dict_field(runtime_state, "requirement_ledger").get("items") or []:
            if isinstance(item, dict) and item.get("status") != "satisfied":
                requirement = item.get("requirement") or {}
                pending.append((requirement.get("id") or "?", item.get("status") or "pending",
                                requirement.get("description") or ""))
    if not checks and receipts:
        items.append("No user-declared acceptance check. The following results apply to their own checks only.")
    elif not checks:
        items.append("Current verification unknown; no declared verification result for this generation.")
    items.extend(receipts)
    items.extend(f"Unresolved requirement: {key} [{status}] {description}" for key, status, description in pending)
    items.extend("Changed file: " + str(path) for path in changed)
    if snapshot is None and _field(runtime_state, "last_verification_failure", ""):
        items.append("Last verification failure (historical unless generation matches): "
                     + str(_field(runtime_state, "last_verification_failure", "")))
    observations = [] if snapshot is not None else [
        *_field(run_evidence, "verification_evidence", []), *_field(run_evidence, "verification_results", [])]
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
    remaining = limit - estimate_text_tokens(essential + omission + closing)
    blocks: list[list[str]] = []
    history = deque()
    omitted = False

    def fits(line: str) -> bool:
        nonlocal remaining, omitted
        # 估算器按每四个 ASCII 字符取整。预留一格覆盖各条独立计数的进位。
        cost = estimate_text_tokens("\n" + line) + 1
        if cost > remaining:
            omitted = True
            return False
        remaining -= cost
        return True

    for item in items:
        lines = (item,) if isinstance(item, str) else item.current
        if not fits(lines[0]):
            continue
        block = [lines[0], *(line for line in lines[1:] if fits(line))]
        blocks.append(block)
        if isinstance(item, _CheckContext):
            history.append((block, item.history))
    # 所有当前决定优先。剩余预算在已入选命令之间轮流分给最近的历史回执。
    while history:
        block, previous = history.popleft()
        line = next(previous, None)
        if line is not None and fits(line):
            block.append(line)
            history.append((block, previous))
    return essential + "".join("\n" + line for block in blocks for line in block) + (omission if omitted else "") + closing


def continuation_projection_details(messages: list[dict]) -> dict | None:
    """Describe an active unfinished-run projection for trace accounting."""
    projection = _continuation_projection(messages)
    return projection[0] if projection is not None else None


def _continuation_projection(messages: list[dict]) -> tuple[dict, list[dict]] | None:
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
    dialogue = _retained_dialogue(messages, tail_start)
    return {
        "boundary": boundary,
        "tail_start": tail_start,
        "activation_index": activation_index,
        "status": str(boundary.get("status") or ""),
        "dropped_messages": tail_start - len(dialogue),
        "summary_chars": len(str(boundary.get("summary") or "")),
        "signature": f"{boundary_id}:{current_id}",
    }, dialogue


def _retained_dialogue(messages: list[dict], tail_start: int) -> list[dict]:
    dialogue = []
    reply_start = None
    for index in range(tail_start):
        if not _is_human_user(messages[index]):
            continue
        if reply_start is not None:
            answer = _completed_reply(messages, reply_start, index)
            if answer is not None:
                dialogue.append({"role": "assistant", "content": answer})
        dialogue.append(messages[index])
        reply_start = index + 1
    # 最后一个问题属于未完成轮，只保留原话；它的执行状态由恢复边界提供。
    return dialogue


def _completed_reply(messages: list[dict], start: int, stop: int) -> str | None:
    """只返回本轮最后一次公开结算的答复正文。"""
    for index in range(stop - 1, start - 1, -1):
        message = messages[index]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if (message.get(INTERNAL_KEY) is True or message.get(VISIBLE_KEY) is False
                or message.get(AUTHORITATIVE_KEY) is False):
            continue
        if message.get(ASSISTANT_ERROR_KEY) or message.get("_nz_error") or CONTINUATION_KEY in message:
            return None
        legacy_reply = ASSISTANT_END_STATE_KEY not in message
        if not legacy_reply:
            end = message[ASSISTANT_END_STATE_KEY]
            if not isinstance(end, dict) or end.get("reason") != "completed":
                return None
        else:
            # 旧会话没有终态标记，只认两个真人提问之间唯一的纯文本回复。
            if stop - start != 1 or not _LEGACY_TEXT_FIELDS.issuperset(message):
                continue
            user_id = messages[start - 1].get(MESSAGE_ID_KEY)
            has_matching_identity = bool(user_id) == bool(message.get(MESSAGE_ID_KEY))
            if not has_matching_identity or message.get(ASSISTANT_PARENT_KEY, user_id) != user_id:
                return None
        return _public_reply_text(message, legacy_reply)
    return None


def _public_reply_text(message: dict, legacy_reply: bool) -> str | None:
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    if message.get("tool_calls") or message.get("function_call"):
        return None
    parts = message.get(PARTS_KEY, [])
    if not isinstance(parts, list):
        return None
    if not parts:
        return content
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") == "tool":
            return None
        if part.get("type") != "text":
            if legacy_reply:
                return None
            continue
        public = project_public_message_part(part)
        if (public.get("ignored") is True or public.get("authoritative") is False
                or public.get("status") not in (None, "completed")
                or not isinstance(public.get("text"), str)):
            # 持久化层可能把合并正文写进第一个文本块。混有隐藏片段时无法可靠拆开。
            return None
        texts.append(public["text"])
    return content if content in texts or "".join(texts) == content else None


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
