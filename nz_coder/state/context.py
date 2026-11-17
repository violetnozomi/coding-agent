"""Context budgeting, persistence, pruning, and conversation compaction.

改进点（对标 Claude Code）：
  1. micro_compact 按完整工具批次与 token 额度清理旧结果
  2. 新增时间间隔触发的 micro_compact（idle 超过阈值则清除老结果）
  3. estimate_tokens 针对 CJK 字符修正权重（减少误差）
"""
from __future__ import annotations

import copy
import asyncio
import json
import math
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from nz_coder.foundation import config
from nz_coder.foundation.run_context import active_run_settings_object
from nz_coder.foundation.json_safety import json_safe_value
from nz_coder.protocol.message_schema import (
    COMPACTION_KEY,
    MESSAGE_ID_KEY,
    SESSION_SUMMARY_KEY,
    TOOL_COMPACTED_AT_KEY,
    is_synthetic_user_message,
)
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.state.compaction_summary import (
    SUMMARY_INSTRUCTIONS, parse_compaction_summary, render_compaction_summary,
)
from nz_coder.state.workdir import current_workdir
from nz_coder.state.sessions import active_session_id, session_transcript_dir

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
_COMPACTION_SYSTEM_PROMPT = """You are a context summarization specialist.

CRITICAL: Respond with exactly one JSON object. Do NOT call any tools.
Tool calls will be rejected and waste the compaction turn.

Do not continue the conversation or answer the user's task. Produce only the
requested continuation summary."""
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
    replay_compaction_tokens: int = 0

    def __post_init__(self) -> None:
        for name in (
            "context_tokens",
            "output_reserve_tokens",
            "usable_input_tokens",
            "soft_preflight_tokens",
            "expansion_budget_tokens",
            "tool_prune_protect_tokens",
            "tool_prune_minimum_tokens",
            "replay_compaction_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"PromptBudget {name} must be a non-negative integer"
                )
        if not isinstance(self.context_metadata_missing, bool):
            raise ValueError("PromptBudget context_metadata_missing must be boolean")


def prompt_budget(
    context_tokens: int | None = None,
    output_tokens: int | None = None,
) -> PromptBudget:
    """Derive all context thresholds from one model-window budget.

    Small context windows reserve at most 25% for output, matching InfCode's
    proportional policy. A conservative 64K-derived base is used only for
    ratio thresholds when model context metadata is unavailable.
    """
    settings = active_run_settings_object()
    configured_context = (
        settings.max_context_tokens
        if settings is not None else config.MAX_CONTEXT_TOKENS
    )
    configured_output = (
        settings.max_output_tokens
        if settings is not None else config.MAX_OUTPUT_TOKENS
    )
    context = max(0, int(
        configured_context if context_tokens is None else context_tokens
    ))
    requested_output = max(1, int(
        configured_output if output_tokens is None else output_tokens
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

    configured_replay_limit = max(0, (
        settings.context_replay_compaction_tokens
        if settings is not None else config.CONTEXT_REPLAY_COMPACTION_TOKENS
    ))
    replay_limit = (
        min(usable_input, configured_replay_limit)
        if usable_input > 0 and configured_replay_limit > 0
        else 0
    )

    return PromptBudget(
        context_tokens=context,
        output_reserve_tokens=output_reserve,
        usable_input_tokens=usable_input,
        soft_preflight_tokens=int(budget_base * 0.85),
        expansion_budget_tokens=min(32_000, int(budget_base * 0.15)),
        tool_prune_protect_tokens=min(40_000, int(budget_base * 0.25)),
        tool_prune_minimum_tokens=min(20_000, int(budget_base * 0.10)),
        context_metadata_missing=missing,
        replay_compaction_tokens=replay_limit,
    )


def estimate_tokens(value: object) -> int:
    """Estimate token count from a JSON-serializable value.

    改进：对 ASCII 以外字符（CJK 等）减少权重，避免 JSON 字节数高估 token 数。
    原方案 len(json) // 4 在 CJK 密集文本下会高估 ~3x，导致过早触发 auto_compact。

    新方案：
    - ASCII 字符：4 bytes per token（JSON 序列化后）
    - 非 ASCII 字符：按 1 token/char 计（CJK 一个字约 1 token）
    """
    serialized = json.dumps(value, default=str, ensure_ascii=False)
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
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("_nz_user_text", message.get("content"))
        if not isinstance(content, str) or estimate_tokens(content) <= max_tokens:
            continue
        from nz_coder.tool_platform.artifacts import ArtifactStore

        artifact_id = ArtifactStore(
            current_workdir(),
            active_session_id() or "session",
        ).put(content, kind="user-input")
        preview_chars = max(200, PREVIEW_CHARS)
        head = content[:preview_chars]
        tail = content[-preview_chars:] if len(content) > preview_chars else ""
        tail_block = f"\n\nEnding preview:\n{tail}" if tail else ""
        replacement = (
            "<oversized-user-input>\n"
            "The original user message exceeded this model's safe input budget. "
            f"Its full content is saved as opaque artifact: {artifact_id}\n"
            "Use read_tool_result with that artifact id when the complete input is needed.\n\n"
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
    from nz_coder.tool_platform.artifacts import ArtifactStore

    artifact_id = ArtifactStore(
        current_workdir(),
        active_session_id() or "session",
    ).put(output, kind="tool-result")
    preview = output[:PREVIEW_CHARS]
    size_kb = len(output) / 1024
    return (
        f"<persisted-output>\n"
        f"Output too large ({size_kb:.1f}KB). Full output artifact: {artifact_id}\n"
        "Use read_tool_result with that artifact id to inspect it.\n\n"
        f"Preview (first {PREVIEW_CHARS} chars):\n{preview}\n"
        f"</persisted-output>"
    )


def _has_failure_signal(content: str) -> bool:
    """Return True if a tool result contains a traceback or test failure worth keeping."""
    if not isinstance(content, str):
        return False
    return (
        content.startswith("Error:")
        or "Traceback (most recent call last)" in content
        or "FAILURES" in content
        or bool(re.search(r"FAILED\s+[\w/\\.\-]+::", content))
        or (content.startswith("Command exited with code") and len(content) > 300)
    )


def _protected_tool_evidence(message: dict) -> bool:
    """Keep mutation facts and known failures even without traceback text."""
    return message.get("role") == "tool" and (
        message.get("_nz_evidence_kind") == "file_write"
        or (
            message.get("_nz_evidence_kind") == "verification"
            and message.get("_nz_verification_passed") is False
        )
        or _has_failure_signal(message.get("content", ""))
    )


def _artifact_references(content: object) -> list[str]:
    if not isinstance(content, str):
        return []
    return list(dict.fromkeys(re.findall(r"artifact_[a-f0-9]{32}", content)))


def micro_compact(
    messages: list,
    *,
    budget: PromptBudget | None = None,
) -> int:
    """Prune stale tool output without erasing the active reasoning window.

    Protect the two newest complete tool-call batches and every unfinished
    batch, then keep a model-window-derived amount of older tool output.
    User instructions and assistant conclusions are never replaced here.

    Return the number of results replaced, primarily for observability/tests.
    """
    active_budget = budget or prompt_budget()
    candidates = _tool_results_before_recent_batches(messages, keep_batches=2)
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
        if _protected_tool_evidence(message):
            continue
        to_prune.append(message)
        prunable_tokens += tokens

    if prunable_tokens <= minimum_tokens:
        return 0
    return _replace_tool_results(to_prune)


_COMPACTED_TOOL_RESULT = (
    "[Earlier tool result compacted. Re-run read-only tools when current file "
    "or search evidence is needed. Do not repeat the identical call for "
    "state-changing operations merely to recover its output.]"
)


def _tool_results_before_recent_batches(messages: list, *, keep_batches: int) -> list[dict]:
    """Return results belonging to older, fully settled tool-call batches."""
    completed: list[list[dict]] = []
    pending: set[str] = set()
    results: list[dict] = []
    for message in messages:
        if message.get("role") == "assistant":
            pending = {call["id"] for call in message.get("tool_calls") or []}
            results = []
        elif message.get("role") == "tool" and message.get("tool_call_id") in pending:
            pending.remove(message["tool_call_id"])
            results.append(message)
            # 同一模型回复中的并行调用全部返回后才计为一批；
            # 未完成或无法归属的结果不进入清理候选区。
            if not pending:
                completed.append(results)
                results = []
    cutoff = max(0, len(completed) - keep_batches)
    return [
        message for batch in completed[:cutoff] for message in batch
    ]


def _replace_tool_results(messages: list[dict]) -> int:
    """Drop old previews while preserving existing opaque result references."""
    replaced = 0
    for message in messages:
        content = message.get("content", "")
        if (
            isinstance(content, str)
            and len(content) > 200
            and not _protected_tool_evidence(message)
        ):
            replacement = _COMPACTED_TOOL_RESULT
            references = _artifact_references(content)
            if references:
                qualification = " (incomplete capture)" if "(incomplete" in content else ""
                replacement += (
                    f"\nSaved output references{qualification}: " + ", ".join(references)
                    + ". Use read_tool_result with an artifact ID to inspect the saved content."
                )
            if len(replacement) >= len(content):
                continue
            message["content"] = replacement
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
                try:
                    candidate = float(timestamp)
                except (TypeError, ValueError, OverflowError):
                    candidate = 0.0
                if math.isfinite(candidate) and candidate > 0:
                    last_assistant_time = candidate
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


def _public_compaction_message(message: dict) -> dict:
    """Project one durable message onto fields that can consume provider tokens."""
    return {
        key: value
        for key, value in message.items()
        if not key.startswith("_nz_") and key != "_timestamp"
    }


def _estimate_compaction_messages(
    messages: list[dict],
    project_history: Callable[[list], list] | None = None,
) -> int:
    """Estimate a compaction slice without counting durable Session metadata."""
    if project_history is not None:
        return estimate_tokens(project_history(copy.deepcopy(messages)))
    return estimate_tokens([
        _public_compaction_message(message)
        for message in messages
        if isinstance(message, dict)
    ])


def _select_compaction_parts(
    messages: list[dict],
    budget: PromptBudget | None = None,
    *,
    project_history: Callable[[list], list] | None = None,
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
        if (
            start > 0
            and _estimate_compaction_messages(messages[start:], project_history) <= preserve_tokens
        ):
            keep_start = start
            continue

        # InfCode can split an oversized recent turn at a message boundary.
        # Keep the newest suffix that remains protocol-safe for OpenAI-style
        # chat histories. This fallback also applies to a long Agent run that
        # still has only its initial human turn; Runtime diagnostics do not
        # create artificial turn boundaries.
        for suffix_start in range(start + 1, len(messages)):
            if not _valid_tail_boundary(messages[suffix_start]):
                continue
            if (
                _estimate_compaction_messages(messages[suffix_start:], project_history)
                <= preserve_tokens
            ):
                keep_start = suffix_start
                break
        break

    if keep_start is not None:
        return list(messages[:keep_start]), list(messages[keep_start:])
    return list(messages), []


def _select_summary_input(head: list[dict], max_tokens: int) -> list[dict]:
    candidates = [
        (index, message)
        for index, message in enumerate(head)
        if not _is_compaction_summary(message)
    ]
    selected: list[tuple[int, dict]] = []
    remaining = max(1, int(max_tokens))

    # A long single-turn coding run can fill the summary budget with recent
    # tool evidence while silently dropping the original task. Reserve the
    # first real user instruction before backfilling the newest evidence.
    anchor = _compaction_task_anchor(head, natural_only=False)
    anchor_index: int | None = None
    if anchor is not None:
        candidate_index, anchor_message = anchor
        anchor_tokens = estimate_tokens(anchor_message)
        if anchor_tokens > remaining:
            anchor = _compaction_task_anchor(head, natural_only=True)
            if anchor is not None:
                candidate_index, anchor_message = anchor
                anchor_tokens = estimate_tokens(anchor_message)
        if anchor_tokens <= remaining:
            selected.append((candidate_index, anchor_message))
            remaining -= anchor_tokens
            anchor_index = candidate_index

    # 先给已有修改、验证和存储句柄留位置，再用近期普通输出填充剩余预算。
    # 验证通过与失败同级，避免只选到旧失败而漏掉后续成功。
    prioritized = sorted(
        reversed(candidates),
        key=lambda item: (
            0 if (
                _protected_tool_evidence(item[1])
                or item[1].get("_nz_evidence_kind") == "verification"
            ) else 1 if _artifact_references(item[1].get("content", "")) else 2
        ),
    )
    for index, message in prioritized:
        if index == anchor_index:
            continue
        public_message = _public_compaction_message(message)
        tokens = estimate_tokens(public_message)
        if tokens > remaining:
            continue
        selected.append((index, public_message))
        remaining -= tokens
        if remaining <= 0:
            break
    selected.sort(key=lambda item: item[0])
    return [message for _, message in selected]


def _compaction_task_anchor(
    head: list[dict],
    *,
    natural_only: bool = True,
) -> tuple[int, dict] | None:
    """Return the earliest bounded, provider-visible human task message."""
    for index, message in enumerate(head):
        if not _is_human_user_turn(message):
            continue
        public_message = _public_compaction_message(message)
        natural = message.get("_nz_user_text", public_message.get("content"))
        if natural_only and isinstance(natural, str) and natural.strip():
            public_message["content"] = natural
        return index, public_message
    return None


def _is_compaction_payload_error(error: Exception) -> bool:
    """Recognize only InfCode's summary-payload/context recovery failures."""
    from nz_coder.foundation.error_classification import is_context_overflow_error

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


class CompactionGenerationError(RuntimeError):
    """已结清的摘要模型请求失败；不包含存储、取消或宿主错误。"""


def _create_compaction_completion(
    client,
    provider,
    capabilities,
    request: dict,
    observer: Callable[[str, dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    model_runtime=None,
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
    runtime = model_runtime or resolve_model_runtime(ModelSelectionRequest(
        provider_name=str(getattr(active_provider, "name", "openai-compatible")),
        model_id=str(request["model"]),
        provider=active_provider,
        client=client,
        owns_client=False,
    ))
    if capabilities is not None:
        runtime.capabilities = capabilities
    outcome = ProductionModelGateway(runtime, observer=observer).complete_sync(
        ModelCall(
            purpose=ModelCallPurpose.COMPACTION,
            messages=request["messages"],
            max_output_tokens=request["max_tokens"],
            timeout_seconds=600.0,
        ),
        cancel_event=cancel_event,
    )
    if outcome.status is ModelCallStatus.CANCELLED:
        raise asyncio.CancelledError()
    if outcome.status is not ModelCallStatus.COMPLETED:
        lifecycle = outcome.provider_metadata.get("request_lifecycle", {})
        if lifecycle.get("dispatch_blocked") or runtime.inflight_status()["inflight_calls"]:
            raise RuntimeError("Compaction Provider runtime is unavailable or request has not settled")
        raise CompactionGenerationError(outcome.error or outcome.status.value)
    if outcome.tool_calls or outcome.finish_reason != "stop":
        raise PublicRuntimeError(PublicError(
            "invalid_compaction_summary",
            "Invalid compaction summary: expected a complete JSON response without tool calls",
        ))
    return outcome.content


def _compaction_output_limit(messages: list[dict], budget: PromptBudget) -> int:
    """摘要沿用正常输出额度；完整摘要输入也必须留在模型窗口内。"""
    output = budget.output_reserve_tokens
    if budget.context_tokens > 0:
        # 与正式发送一致计算整个请求，并为本地 token 估算留出余量。
        input_tokens = math.ceil(estimate_request_tokens(messages) * 1.15)
        output = min(output, budget.context_tokens - input_tokens)
    if output <= 0:
        raise PublicRuntimeError(PublicError(
            "compaction_budget_exhausted",
            "Compaction input and output cannot fit in the model context window",
        ))
    return output


def auto_compact(
    messages: list,
    client,
    model: str,
    focus: str = None,
    *,
    provider=None,
    capabilities=None,
    model_runtime=None,
    observer: Callable[[str, dict], None] | None = None,
    cancel_event: threading.Event | None = None,
    budget: PromptBudget | None = None,
    auto: bool = False,
    overflow: bool = False,
    current_facts: str = "",
    project_history: Callable[[list], list] | None = None,
) -> list:
    """Update an anchored summary while preserving a bounded recent turn tail."""
    transcript_dir = session_transcript_dir()
    transcript_dir.mkdir(parents=True, exist_ok=True)
    path = transcript_dir / f"transcript_{time.time_ns()}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(
                json_safe_value(msg),
                default=str,
                ensure_ascii=False,
                allow_nan=False,
            ) + "\n")

    active_budget = budget or prompt_budget()
    head, tail = _select_compaction_parts(messages, active_budget, project_history=project_history)
    # 恢复尝试只修改候选；无效摘要或请求失败不能清掉调用者原证据。
    head = copy.deepcopy(head)
    # 用户原文独立于摘要模型和其选材预算；只压缩助手叙述及工具材料。
    original_users = copy.deepcopy([message for message in head if _is_human_user_turn(message)])
    operation_id = uuid.uuid4().hex
    request_index = 0
    previous_summary = _extract_previous_summary(head)
    usable = active_budget.usable_input_tokens or active_budget.soft_preflight_tokens
    summary_input_tokens = min(
        _COMPACT_INPUT_MAX_TOKENS,
        max(2_000, int(usable * 0.65)),
    )
    selected = _select_summary_input(head, summary_input_tokens)
    if observer is not None:
        observer("compaction_selection", {
            "operation_id": operation_id,
            **_selection_coverage(head, selected),
            "input_budget_tokens": summary_input_tokens,
            "selected_token_estimate": _estimate_compaction_messages(selected),
            "current_facts_token_estimate": estimate_tokens(current_facts) if current_facts else 0,
            "actual_cost": None,
            "preceding_coding_usage": next((message.get("_nz_usage") for message in reversed(messages)
                                             if message.get("role") == "assistant" and isinstance(message.get("_nz_usage"), dict)), None),
        })
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
    prompt = f"{anchor}\n\n{SUMMARY_INSTRUCTIONS}{diff_section}"
    if current_facts:
        prompt += ("\n\nThe following deterministic state is authoritative for this operation. "
                   "Earlier summaries are historical background; do not promote old validation to current. "
                   "Preserve historical reasoning without duplicating this replaceable current-state block.\n"
                   + current_facts)
    if focus:
        prompt += f"\nPay special attention to: {focus}\n"

    def request_summary(request_prompt: str, conversation: str):
        nonlocal request_index
        request_index += 1
        request_id = f"{operation_id}:{request_index}"
        def observe(name, payload):
            if observer is not None:
                usage = payload.get("usage")
                known = isinstance(usage, dict) and any(isinstance(value, (int, float)) and value > 0 for value in usage.values())
                observer(name, {**payload, "compaction_operation_id": operation_id,
                                "compaction_request_id": request_id,
                                "compaction_usage_known": known if name == "model_call_finish" else None,
                                "price_version": payload.get("price_version")})
        request_messages = [
            {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
            {"role": "user", "content": request_prompt + "\n\n" + conversation},
        ]
        return _create_compaction_completion(
            client,
            provider,
            capabilities,
            {
                "model": model,
                "messages": request_messages,
                "max_tokens": _compaction_output_limit(request_messages, active_budget),
            },
            observe,
            cancel_event,
            model_runtime=model_runtime,
        )

    recovery: dict[str, object] | None = None
    try:
        summary = request_summary(prompt, conv_text)
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
            raise terminal_error
        summary = recovered_summary
    try:
        structured, discarded_fields = parse_compaction_summary(summary)
    except PublicRuntimeError:
        if observer is not None:
            observer("compaction_summary_rejected", {"operation_id": operation_id, "reason": "invalid_structure"})
        raise
    summary = render_compaction_summary(structured)
    if observer is not None:
        observer("compaction_summary_validated", {
            "operation_id": operation_id, "discarded_fields": discarded_fields,
        })
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
            "operation_id": operation_id,
            "background_only": bool(current_facts),
            # Transcript archives are private runtime state, not project files.
            # Keep their host path out of model-visible/session metadata.
            "archive": (
                "user-state://transcripts/"
                f"{active_session_id() or 'session'}/{path.name}"
            ),
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
    return [summary_message, *original_users, *tail]


def _selection_coverage(head: list, selected: list) -> dict:
    """数量只说明选材可达性，不将其当成摘要语义保真率。"""
    from collections import Counter

    def key(message):
        return json.dumps(_public_compaction_message(message), ensure_ascii=False, sort_keys=True, default=str)
    remaining = Counter(key(message) for message in selected)
    categories = {}
    candidates = [message for message in head if not _is_compaction_summary(message)]
    for message in candidates:
        kind = str(message.get("_nz_evidence_kind") or message.get("role") or "unknown")
        bucket = categories.setdefault(kind, {"candidate": 0, "selected": 0, "omitted": 0})
        bucket["candidate"] += 1
        included = remaining[key(message)] > 0
        bucket["selected" if included else "omitted"] += 1
        if included:
            remaining[key(message)] -= 1
    return {
        "candidate_messages": len(candidates), "selected_messages": len(selected),
        "candidate_tool_batches": sum(bool(message.get("tool_calls")) for message in candidates),
        "selected_tool_batches": sum(bool(message.get("tool_calls")) for message in selected),
        "categories": categories,
    }


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
