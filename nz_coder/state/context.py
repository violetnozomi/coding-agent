"""Context budgeting, persistence, pruning, and conversation compaction.

改进点（对标 Claude Code）：
  1. micro_compact 改为按 token 量而非条数截断（大结果优先压缩）
  2. 新增时间间隔触发的 micro_compact（idle 超过阈值则清除老结果）
  3. estimate_tokens 针对 CJK 字符修正权重（减少误差）
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from dataclasses import dataclass

from nz_coder import config
from nz_coder.message_schema import (
    COMPACTION_KEY,
    MESSAGE_ID_KEY,
    SESSION_SUMMARY_KEY,
    TOOL_COMPACTED_AT_KEY,
    is_synthetic_user_message,
)
from nz_coder.state.workdir import current_workdir
from nz_coder.sessions import (
    session_runtime_dir,
    session_tool_results_dir,
    session_transcript_dir,
)

PREVIEW_CHARS = config.PERSIST_PREVIEW_CHARS
TRIGGER_CHARS = config.PERSIST_OUTPUT_TRIGGER

# 时间触发 micro_compact 的空闲阈值（分钟），超过则认为 server cache 已凉
# 对标 Claude Code timeBasedMCConfig，保守设为 30 分钟
_TIME_BASED_MC_GAP_MINUTES = 30


_COMPACT_TAIL_TURNS = 2
_COMPACT_INPUT_MAX_TOKENS = 20_000
_COMPACT_RECENT_MIN_TOKENS = 2_000
_COMPACT_RECENT_MAX_TOKENS = 8_000
_SUMMARY_OPEN = "<session-summary>"
_SUMMARY_CLOSE = "</session-summary>"
_COMPACTION_RECOVERY_PREFIX = (
    "The previous compaction request exceeded the provider's 4MB payload limit.\n\n"
    "Older tool outputs and media attachments were removed from this compaction request."
)
_OVERSIZED_PASTE_PLACEHOLDER = (
    "[The previous turn contained an over-long pasted block that was omitted "
    "because it exceeded the model context. If you need its contents, reference "
    "the file with @file or resend it in smaller parts.]"
)
_OVERSIZED_HISTORY_PLACEHOLDER = (
    "[Earlier conversation history was omitted because it exceeded the model "
    "context in aggregate; only the most recent turns are kept. If you need "
    "earlier information, please provide the key details again.]"
)
_SUMMARY_TEMPLATE = """Output exactly this Markdown structure and keep the section order unchanged:

## Goal
- Current task and success criteria

## Constraints & Preferences
- User constraints, preferences, and project rules

## Progress
### Done
- Completed work and exact files changed
### In Progress
- Current work
### Blocked
- Blockers or failed approaches

## Key Decisions
- Decisions and reasons

## Next Steps
- Ordered next actions

## Critical Context
- Exact identifiers, commands, errors, and facts needed to continue

## Relevant Files
- Exact path and why it matters

Use terse bullets. Keep every section, using \"(none)\" when empty. Do not mention compaction."""


@dataclass(frozen=True)
class PromptBudget:
    """Model-window-aware token limits shared by request and compaction paths."""

    context_tokens: int
    output_reserve_tokens: int
    usable_input_tokens: int
    soft_preflight_tokens: int
    expansion_budget_tokens: int
    tool_prune_protect_tokens: int
    tool_prune_minimum_tokens: int
    context_metadata_missing: bool


def prompt_budget(
    context_tokens: int | None = None,
    output_tokens: int | None = None,
) -> PromptBudget:
    """Derive all context thresholds from one model-window budget.

    Small context windows reserve at most 25% for output, matching InfCode's
    proportional policy. A conservative 64K-derived base is used only for
    ratio thresholds when model context metadata is unavailable.
    """
    context = max(0, int(
        config.MAX_CONTEXT_TOKENS if context_tokens is None else context_tokens
    ))
    requested_output = max(1, int(
        config.MAX_OUTPUT_TOKENS if output_tokens is None else output_tokens
    ))
    missing = context <= 0

    if missing:
        output_reserve = requested_output
        usable_input = 0
        budget_base = 48_000
    else:
        output_reserve = min(requested_output, max(1, context // 2))
        if context <= 128_000:
            proportional_cap = max(8_000, context // 4)
            output_reserve = min(output_reserve, proportional_cap)
        usable_input = max(0, context - output_reserve)
        budget_base = usable_input

    return PromptBudget(
        context_tokens=context,
        output_reserve_tokens=output_reserve,
        usable_input_tokens=usable_input,
        soft_preflight_tokens=int(budget_base * 0.85),
        expansion_budget_tokens=min(32_000, int(budget_base * 0.15)),
        tool_prune_protect_tokens=min(40_000, int(budget_base * 0.25)),
        tool_prune_minimum_tokens=min(20_000, int(budget_base * 0.10)),
        context_metadata_missing=missing,
    )


def estimate_tokens(value: object) -> int:
    """Estimate token count from a JSON-serializable value.

    改进：对 ASCII 以外字符（CJK 等）减少权重，避免 JSON 字节数高估 token 数。
    原方案 len(json) // 4 在 CJK 密集文本下会高估 ~3x，导致过早触发 auto_compact。

    新方案：
    - ASCII 字符：4 bytes per token（JSON 序列化后）
    - 非 ASCII 字符：按 1 token/char 计（CJK 一个字约 1 token）
    """
    serialized = json.dumps(value, default=str)
    ascii_bytes = sum(1 for c in serialized if ord(c) < 128)
    non_ascii_chars = len(serialized) - ascii_bytes
    # ASCII token 估算：4字节/token；非ASCII：1字节/token（CJK实际约1字/token）
    return ascii_bytes // 4 + non_ascii_chars


def estimate_request_tokens(messages: list, tools: list | None = None) -> int:
    """Estimate the complete request payload, including tool schemas."""
    return estimate_tokens(messages) + estimate_tokens(tools or [])


def persist_oversized_user_inputs(messages: list, max_tokens: int) -> int:
    """Persist individual user messages that cannot safely fit in one request.

    The original content remains available under the active session runtime
    directory. The in-memory message is replaced with a bounded preview and a
    workspace-relative path that the agent can inspect with ``read_file``.
    """
    if max_tokens <= 0:
        return 0

    persisted = 0
    input_dir = session_runtime_dir() / "user-inputs"
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = message.get("_nz_user_text", message.get("content"))
        if not isinstance(content, str) or estimate_tokens(content) <= max_tokens:
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        path = input_dir / f"user_input_{index}_{digest}.txt"
        input_dir.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        relative = path.relative_to(current_workdir())
        preview_chars = max(200, PREVIEW_CHARS)
        head = content[:preview_chars]
        tail = content[-preview_chars:] if len(content) > preview_chars else ""
        tail_block = f"\n\nEnding preview:\n{tail}" if tail else ""
        replacement = (
            "<oversized-user-input>\n"
            "The original user message exceeded this model's safe input budget. "
            f"Its full content is saved at: {relative}\n"
            "Read that file when the complete input is needed.\n\n"
            f"Beginning preview:\n{head}"
            f"{tail_block}\n"
            "</oversized-user-input>"
        )
        if "_nz_user_text" in message:
            message["_nz_user_text"] = replacement
            from nz_coder.state.input_expansion import render_expanded_message
            render_expanded_message(message)
        else:
            message["content"] = replacement
        persisted += 1
    return persisted


def persist_large_output(tool_call_id: str, output: str) -> str:
    if len(output) <= TRIGGER_CHARS:
        return output
    tool_results_dir = session_tool_results_dir()
    tool_results_dir.mkdir(parents=True, exist_ok=True)
    safe_id = re.sub(r"[^a-zA-Z0-9_.-]", "_", tool_call_id or "unknown")
    path = tool_results_dir / f"{safe_id}.txt"
    if not path.exists():
        path.write_text(output, encoding="utf-8")
    rel = path.relative_to(current_workdir())
    preview = output[:PREVIEW_CHARS]
    size_kb = len(output) / 1024
    return (
        f"<persisted-output>\n"
        f"Output too large ({size_kb:.1f}KB). Full output saved to: {rel}\n\n"
        f"Preview (first {PREVIEW_CHARS} chars):\n{preview}\n"
        f"</persisted-output>"
    )


def _has_failure_signal(content: str) -> bool:
    """Return True if a tool result contains a traceback or test failure worth keeping."""
    if not isinstance(content, str):
        return False
    return (
        "Traceback (most recent call last)" in content
        or "FAILURES" in content
        or bool(re.search(r"FAILED\s+[\w/\\.\-]+::", content))
        or (content.startswith("Command exited with code") and len(content) > 300)
    )


def micro_compact(
    messages: list,
    *,
    budget: PromptBudget | None = None,
) -> int:
    """Prune stale tool output without erasing the active reasoning window.

    InfCode protects the two most recent user turns, then keeps a
    model-window-derived amount of older tool output. The previous NZ-Coder
    implementation kept only three results once all tool output crossed a
    fixed 8K-token threshold. A review that needed four or more files could
    therefore continually erase and re-read its own evidence.

    Return the number of results replaced, primarily for observability/tests.
    """
    active_budget = budget or prompt_budget()
    candidates = _tool_results_before_recent_turns(messages, keep_turns=2)
    if not candidates:
        return 0

    if _time_based_compaction_due(messages):
        return _replace_tool_results(candidates)

    protected_tokens = active_budget.tool_prune_protect_tokens
    minimum_tokens = active_budget.tool_prune_minimum_tokens
    seen_tokens = 0
    to_prune: list[dict] = []
    prunable_tokens = 0
    for message in reversed(candidates):
        tokens = estimate_tokens(message.get("content", ""))
        seen_tokens += tokens
        if seen_tokens <= protected_tokens:
            continue
        if _has_failure_signal(message.get("content", "")):
            continue
        to_prune.append(message)
        prunable_tokens += tokens

    if prunable_tokens <= minimum_tokens:
        return 0
    return _replace_tool_results(to_prune)


_COMPACTED_TOOL_RESULT = (
    "[Earlier tool result compacted. Do not repeat the identical call merely "
    "to recover it; re-run only when the workspace changed or exact content "
    "is essential.]"
)


def _tool_results_before_recent_turns(messages: list, *, keep_turns: int) -> list[dict]:
    """Return tool results older than the newest complete user turns."""
    user_indexes = [
        index for index, message in enumerate(messages)
        if _is_human_user_turn(message)
    ]
    if len(user_indexes) <= keep_turns:
        return []
    cutoff = user_indexes[-keep_turns]
    return [
        message for message in messages[:cutoff]
        if message.get("role") == "tool"
    ]


def _replace_tool_results(messages: list[dict]) -> int:
    """Replace eligible, non-diagnostic tool results with compact markers."""
    replaced = 0
    for message in messages:
        content = message.get("content", "")
        if (
            isinstance(content, str)
            and len(content) > 200
            and content != _COMPACTED_TOOL_RESULT
            and not _has_failure_signal(content)
        ):
            message["content"] = _COMPACTED_TOOL_RESULT
            message[TOOL_COMPACTED_AT_KEY] = time.time()
            replaced += 1
    return replaced


def _time_based_compaction_due(messages: list) -> bool:
    """Return whether old-turn cleanup is useful after a long idle gap."""
    last_assistant_time: float | None = None
    for message in reversed(messages):
        if message.get("role") == "assistant":
            timestamp = message.get("_timestamp")
            if timestamp:
                last_assistant_time = float(timestamp)
            break
    if last_assistant_time is None:
        return False
    gap_minutes = (time.time() - last_assistant_time) / 60.0
    return gap_minutes >= _TIME_BASED_MC_GAP_MINUTES


def _is_compaction_summary(message: dict) -> bool:
    content = message.get("content", "")
    return message.get("role") == "user" and isinstance(content, str) and _SUMMARY_OPEN in content


def _is_human_user_turn(message: dict) -> bool:
    """Return whether a message represents an actual user-authored turn."""
    return (
        message.get("role") == "user"
        and not is_synthetic_user_message(message)
        and not _is_compaction_summary(message)
    )


def _extract_previous_summary(messages: list[dict]) -> str:
    for message in reversed(messages):
        content = message.get("content", "")
        if not isinstance(content, str):
            continue
        start = content.find(_SUMMARY_OPEN)
        end = content.find(_SUMMARY_CLOSE)
        if start >= 0 and end > start:
            return content[start + len(_SUMMARY_OPEN):end].strip()
    return ""


def _preserve_recent_tokens(budget: PromptBudget) -> int:
    """Return InfCode-compatible recent-tail headroom for compaction."""
    usable = max(0, budget.usable_input_tokens)
    return min(
        _COMPACT_RECENT_MAX_TOKENS,
        max(_COMPACT_RECENT_MIN_TOKENS, int(usable * 0.25)),
    )


def _valid_tail_boundary(message: dict) -> bool:
    """Avoid creating an OpenAI history that starts with an orphan tool result."""
    return message.get("role") in {"user", "assistant"}


def _select_compaction_parts(
    messages: list[dict],
    budget: PromptBudget | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split summary head from a model-budgeted recent tail.

    The newest two human turns are preferred.  If a complete turn does not
    fit, retain the newest valid suffix inside that turn.  Starting at a tool
    result is forbidden because it would orphan the corresponding tool call.
    """
    active_budget = budget or prompt_budget()
    preserve_tokens = _preserve_recent_tokens(active_budget)
    user_starts = [
        index
        for index, message in enumerate(messages)
        if _is_human_user_turn(message)
    ]
    if not user_starts:
        return list(messages), []

    recent = user_starts[-_COMPACT_TAIL_TURNS:]
    keep_start: int | None = None
    for start in reversed(recent):
        if start <= 0:
            continue
        if estimate_tokens(messages[start:]) <= preserve_tokens:
            keep_start = start
            continue

        # InfCode can split an oversized recent turn at a message boundary.
        # Keep the newest suffix that remains protocol-safe for OpenAI-style
        # chat histories.
        for suffix_start in range(start + 1, len(messages)):
            if not _valid_tail_boundary(messages[suffix_start]):
                continue
            if estimate_tokens(messages[suffix_start:]) <= preserve_tokens:
                keep_start = suffix_start
                break
        break

    if keep_start is not None:
        return list(messages[:keep_start]), list(messages[keep_start:])
    return list(messages), []


def _select_summary_input(head: list[dict], max_tokens: int) -> list[dict]:
    candidates = [message for message in head if not _is_compaction_summary(message)]
    selected: list[dict] = []
    remaining = max(1, int(max_tokens))
    for message in reversed(candidates):
        public_message = {
            key: value
            for key, value in message.items()
            if not key.startswith("_nz_")
        }
        tokens = estimate_tokens(public_message)
        if tokens > remaining:
            continue
        selected.append(public_message)
        remaining -= tokens
        if remaining <= 0:
            break
    selected.reverse()
    return selected


def _is_compaction_payload_error(error: Exception) -> bool:
    """Recognize only InfCode's summary-payload/context recovery failures."""
    from nz_coder.recovery import is_context_overflow_error

    text = str(error).lower()
    return (
        is_context_overflow_error(error)
        or "request entity too large" in text
        or "function_payload_too_large" in text
    )


def _strip_compaction_payload(head: list[dict]) -> tuple[int, int]:
    """Persistently shed tool output and tagged expansion payload from the head."""
    from nz_coder.state.input_expansion import compact_stored

    stripped_tools = 0
    for message in head:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if message.get("content") == _COMPACTED_TOOL_RESULT:
            continue
        message["content"] = _COMPACTED_TOOL_RESULT
        message[TOOL_COMPACTED_AT_KEY] = time.time()
        stripped_tools += 1
    degraded_expansions = compact_stored(head, "compaction-failed")
    return stripped_tools, degraded_expansions


def _natural_user_tokens(message: dict) -> int:
    """Estimate only user-authored text, excluding tagged synthetic expansion."""
    if not _is_human_user_turn(message):
        return 0
    natural = message.get("_nz_user_text", message.get("content", ""))
    return estimate_tokens(natural if isinstance(natural, str) else "")


def _compaction_fallback(
    head: list[dict],
    tail: list[dict],
    usable_input_tokens: int,
) -> tuple[str, str] | None:
    """Return InfCode's bounded placeholder fallback and its reason."""
    threshold = max(1, int(usable_input_tokens))
    if any(_natural_user_tokens(message) > threshold for message in head):
        return _OVERSIZED_PASTE_PLACEHOLDER, "oversized-user-turn"
    if tail:
        return _OVERSIZED_HISTORY_PLACEHOLDER, "aggregate-head"
    return None


def _create_compaction_completion(
    client,
    provider,
    capabilities,
    request: dict,
):
    from nz_coder.model_gateway import (
        ModelCall,
        ModelCallPurpose,
        ModelCallStatus,
        ModelSelectionRequest,
        OpenAIClientBridgeProvider,
        ProductionModelGateway,
        resolve_model_runtime,
    )

    active_provider = provider or OpenAIClientBridgeProvider()
    runtime = resolve_model_runtime(ModelSelectionRequest(
        provider_name=str(getattr(active_provider, "name", "openai-compatible")),
        model_id=str(request["model"]),
        provider=active_provider,
        client=client,
        owns_client=False,
    ))
    if capabilities is not None:
        runtime.capabilities = capabilities
    outcome = ProductionModelGateway(runtime).complete_sync(ModelCall(
        purpose=ModelCallPurpose.COMPACTION,
        messages=request["messages"],
        max_output_tokens=int(request.get("max_tokens") or 4000),
        timeout_seconds=600.0,
    ))
    if outcome.status is not ModelCallStatus.COMPLETED:
        raise RuntimeError(outcome.error or outcome.status.value)
    return outcome.content


def auto_compact(
    messages: list,
    client,
    model: str,
    focus: str = None,
    *,
    provider=None,
    capabilities=None,
    budget: PromptBudget | None = None,
    auto: bool = False,
    overflow: bool = False,
) -> list:
    """Update an anchored summary while preserving a bounded recent turn tail."""
    transcript_dir = session_transcript_dir()
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{time.time_ns()}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")

    active_budget = budget or prompt_budget()
    head, tail = _select_compaction_parts(messages, active_budget)
    previous_summary = _extract_previous_summary(head)
    usable = active_budget.usable_input_tokens or active_budget.soft_preflight_tokens
    summary_input_tokens = min(
        _COMPACT_INPUT_MAX_TOKENS,
        max(2_000, int(usable * 0.65)),
    )
    selected = _select_summary_input(head, summary_input_tokens)
    conv_text = json.dumps(selected, default=str, ensure_ascii=False)

    # 改进：summary prompt 包含文件修改状态，防止 compact 后丢失"已编辑"信息
    git_diff_context = _get_git_diff_summary()
    diff_section = f"\n\nCurrent workspace changes (git diff --stat):\n{git_diff_context}" if git_diff_context else ""

    if previous_summary:
        anchor = (
            "Update the previous summary with the new conversation. Preserve still-true facts, "
            f"remove stale details, and merge new progress.\n<previous-summary>\n{previous_summary}\n</previous-summary>"
        )
    else:
        anchor = "Create a new continuity summary from the conversation."
    prompt = f"{anchor}\n\n{_SUMMARY_TEMPLATE}{diff_section}"
    if focus:
        prompt += f"\nPay special attention to: {focus}\n"

    def request_summary(request_prompt: str, conversation: str):
        return _create_compaction_completion(
            client,
            provider,
            capabilities,
            {
                "model": model,
                "messages": [{
                    "role": "user",
                    "content": request_prompt + "\n\n" + conversation,
                }],
                "max_tokens": 4000,
            },
        )

    recovery: dict[str, object] | None = None
    try:
        summary = request_summary(prompt, conv_text) or "(summary unavailable)"
    except Exception as first_error:
        if not _is_compaction_payload_error(first_error):
            raise
        before_bytes = len(conv_text.encode("utf-8"))
        stripped_tools, degraded_expansions = _strip_compaction_payload(head)
        recovered_input = _select_summary_input(head, summary_input_tokens)
        recovered_text = json.dumps(
            recovered_input,
            default=str,
            ensure_ascii=False,
        )
        after_bytes = len(recovered_text.encode("utf-8"))
        recovery = {
            "before_bytes": before_bytes,
            "after_bytes": after_bytes,
            "stripped_tool_results": stripped_tools,
            "degraded_input_expansions": degraded_expansions,
            "retried": False,
        }
        recovered_summary = None
        terminal_error = first_error
        if after_bytes < before_bytes:
            recovery["retried"] = True
            try:
                recovered_summary = request_summary(
                    _COMPACTION_RECOVERY_PREFIX + "\n\n" + prompt,
                    recovered_text,
                )
            except Exception as retry_error:
                if not _is_compaction_payload_error(retry_error):
                    raise
                terminal_error = retry_error
        if recovered_summary is None:
            fallback = _compaction_fallback(head, tail, usable)
            if fallback is None:
                raise terminal_error
            summary, fallback_reason = fallback
            recovery["fallback"] = fallback_reason
        else:
            summary = recovered_summary or "(summary unavailable)"
    summary_message = {
        "role": "user",
        "content": (
            f"{_SUMMARY_OPEN}\n{summary}\n{_SUMMARY_CLOSE}\n\n"
            "Continue from this summary and the preserved recent turns."
        ),
        COMPACTION_KEY: {
            "auto": bool(auto),
            "overflow": bool(overflow),
            "resume": bool(auto),
            "tail_start_id": (
                tail[0].get(MESSAGE_ID_KEY)
                if tail and isinstance(tail[0].get(MESSAGE_ID_KEY), str)
                else None
            ),
            "head_message_ids": [
                message.get(MESSAGE_ID_KEY)
                for message in head
                if isinstance(message.get(MESSAGE_ID_KEY), str)
            ],
            "created_at": time.time(),
            "archive": str(path.relative_to(current_workdir())),
        },
    }
    if recovery is not None:
        summary_message[COMPACTION_KEY]["payload_recovery"] = recovery
    latest_summary = next(
        (
            message.get(SESSION_SUMMARY_KEY)
            for message in reversed(messages)
            if isinstance(message, dict)
            and isinstance(message.get(SESSION_SUMMARY_KEY), dict)
        ),
        None,
    )
    if latest_summary is not None:
        summary_message[SESSION_SUMMARY_KEY] = copy.deepcopy(latest_summary)
    return [summary_message, *tail]


def _get_git_diff_summary() -> str:
    """Get a brief git diff --stat for the current workspace."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "diff", "--stat", "--no-color"],
            cwd=current_workdir(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # Limit to 20 lines to avoid bloating the summary prompt
            lines = result.stdout.strip().splitlines()
            if len(lines) > 20:
                lines = lines[:20] + [f"... ({len(lines) - 20} more files)"]
            return "\n".join(lines)
    except Exception:
        pass
    return ""
