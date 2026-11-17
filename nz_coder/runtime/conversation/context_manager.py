"""Model-window-aware context preparation for every Agent Runner profile."""
from __future__ import annotations

import math
import copy
from typing import Any
from dataclasses import dataclass

from nz_coder.state.context import (
    CompactionGenerationError,
    estimate_tokens,
    micro_compact,
    persist_oversized_user_inputs,
)
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.core.run_context import current_run_context, request_current_run_cancel
from nz_coder.runtime.conversation.continuation_context import (
    continuation_projection_details,
)
from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.runtime.conversation.usage_history import (
    last_assistant_usage_total as _last_assistant_usage_total,
)
from nz_coder.protocol.message_schema import COMPACTION_KEY
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.state.input_expansion import (
    compact_stored as compact_stored_input_expansions,
    resolve_and_apply_budget as resolve_input_expansions,
)
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.conversation.request_snapshot import (
    FrozenRequestSnapshot,
    message_version,
)

MAX_COMPACTIONS_WITHOUT_RESPONSE = 3


class CompactionAttemptsExhausted(RuntimeError):
    """Raised before a fourth compaction without a usable coding response."""


@dataclass
class CompactionAttemptState:
    """Count total operations separately from consecutive recovery attempts."""

    attempts: int = 0
    without_response: int = 0
    suppressed_replay_version: str = ""

    def reserve(self) -> int:
        if self.without_response >= MAX_COMPACTIONS_WITHOUT_RESPONSE:
            raise CompactionAttemptsExhausted(
                "Compaction exhausted: no usable model response after "
                f"{MAX_COMPACTIONS_WITHOUT_RESPONSE} consecutive attempts"
            )
        self.attempts += 1
        self.without_response += 1
        return self.attempts

    def observe_response(self, result: LLMResult) -> None:
        """Only a settled, usable main-model response renews recovery."""
        if (
            result.needs_compaction or result.aborted or result.diagnostic
            or result.assistant_error is not None or result.post_tool_stream_error
            or result.finish_reason in {"error", "length", "max_tokens"}
        ):
            return
        usable_call = any(
            isinstance(call, dict)
            and bool(call.get("id"))
            and isinstance(call.get("function"), dict)
            and bool(call["function"].get("name"))
            for call in result.tool_calls
        )
        if str(result.content or "").strip() or usable_call:
            self.without_response = 0
            self.suppressed_replay_version = ""


class ProductionContextManager:
    """Own preflight pruning and semantic compaction trigger ordering."""

    def prepare_sync(
        self, context: ContextExecutionContext,
        messages: list,
        on_text=None,
        *,
        attempt_state: Any | None = None,
        request_snapshot: FrozenRequestSnapshot | None = None,
    ) -> bool:
        """Clean at soft pressure; summarize at physical or replay-cost limits."""
        budget = context.budget
        hard_limit = budget.usable_input_tokens or budget.soft_preflight_tokens
        expansion = resolve_input_expansions(messages, budget, context.workspace)
        if any(expansion.values()):
            context.trace("context_input_expansion", **expansion)
        persisted = persist_oversized_user_inputs(
            messages,
            hard_limit,
        )
        before_prune = self._projected_tokens(context, messages, request_snapshot)
        continuation = continuation_projection_details(messages)
        if continuation is None:
            pruned = micro_compact(messages, budget=budget)
        else:
            pruned = 0
            context.trace(
                "context_continuation_boundary",
                status=continuation["status"],
                dropped_messages=continuation["dropped_messages"],
            )
        token_estimate = self._projected_tokens(context, messages, request_snapshot)
        replay_token_estimate = _projected_replay_tokens(context, messages)
        replay_limit = max(0, int(budget.replay_compaction_tokens))
        replay_pressure = _replay_limit_exceeded(replay_token_estimate, replay_limit)
        context.report_pressure({
            "context_window": budget.context_tokens,
            "used_tokens": token_estimate,
            "reserve_tokens": budget.output_reserve_tokens,
        })
        last_usage = _request_usage_total(messages, request_snapshot)
        usage_overflow = bool(last_usage and last_usage >= hard_limit)
        if pruned:
            context.trace(
                "context_tool_pruned",
                count=pruned,
                token_estimate_before=before_prune,
                token_estimate_after=token_estimate,
            )
        if persisted:
            context.trace(
                "context_user_input_persisted",
                count=persisted,
                token_estimate=token_estimate,
            )
        if (
            token_estimate <= budget.soft_preflight_tokens
            and not usage_overflow
            and not replay_pressure
        ):
            return False
        if token_estimate > budget.soft_preflight_tokens:
            context.trace(
                "context_preflight_over_soft",
                token_estimate=token_estimate,
                soft_limit=budget.soft_preflight_tokens,
                usable_input=hard_limit,
            )
            degraded = compact_stored_input_expansions(messages, "preflight")
            if degraded:
                token_estimate = self._projected_tokens(context, messages, request_snapshot)
                replay_token_estimate = _projected_replay_tokens(context, messages)
                replay_pressure = _replay_limit_exceeded(replay_token_estimate, replay_limit)
                context.trace(
                    "context_input_expansion_compacted",
                    count=degraded,
                    token_estimate=token_estimate,
                )
        request_overflow = token_estimate > hard_limit
        if not request_overflow and not usage_overflow and not replay_pressure:
            return False
        source_version = message_version(messages)
        if (not request_overflow and not usage_overflow and attempt_state is not None
                and attempt_state.suppressed_replay_version == source_version):
            return False
        snapshot = request_snapshot or context.freeze_request(messages)
        attempt = attempt_state.reserve() if attempt_state is not None else None
        if on_text:
            on_text("[auto-compact triggered]")
        trigger = (
            "provider_usage"
            if usage_overflow
            else "request_estimate"
            if request_overflow
            else "replay_cost"
        )
        context.trace(
            "compact",
            kind="auto",
            token_estimate=token_estimate,
            soft_limit=budget.soft_preflight_tokens,
            usable_input=budget.usable_input_tokens,
            output_reserve=budget.output_reserve_tokens,
            last_usage_tokens=last_usage,
            replay_token_estimate=replay_token_estimate,
            replay_limit=replay_limit,
            trigger=trigger,
            attempts=attempt,
        )
        try:
            compacted = context.compact(copy.deepcopy(messages))
        except (CompactionGenerationError, PublicRuntimeError) as exc:
            if not _skip_failed_replay_compaction(
                context, messages, exc, source_version=source_version,
                physical=request_overflow or usage_overflow, attempt_state=attempt_state,
            ):
                raise
            return False
        return accept_compaction(
            context, messages, compacted, snapshot=snapshot,
            source_version=source_version, trigger=trigger,
            physical=request_overflow or usage_overflow, attempt_state=attempt_state,
        )

    async def prepare_async(
        self, context: ContextExecutionContext,
        messages: list,
        on_text=None,
        *,
        attempt_state: Any | None = None,
        request_snapshot: FrozenRequestSnapshot | None = None,
    ) -> bool:
        """Async wrapper for soft cleanup and bounded semantic compaction."""
        budget = context.budget
        hard_limit = budget.usable_input_tokens or budget.soft_preflight_tokens
        expansion = resolve_input_expansions(messages, budget, context.workspace)
        if any(expansion.values()):
            context.trace("context_input_expansion", **expansion)
        persisted = persist_oversized_user_inputs(
            messages,
            hard_limit,
        )
        before_prune = self._projected_tokens(context, messages, request_snapshot)
        continuation = continuation_projection_details(messages)
        if continuation is None:
            pruned = micro_compact(messages, budget=budget)
        else:
            pruned = 0
            context.trace(
                "context_continuation_boundary",
                status=continuation["status"],
                dropped_messages=continuation["dropped_messages"],
            )
        token_estimate = self._projected_tokens(context, messages, request_snapshot)
        replay_token_estimate = _projected_replay_tokens(context, messages)
        replay_limit = max(0, int(budget.replay_compaction_tokens))
        replay_pressure = _replay_limit_exceeded(replay_token_estimate, replay_limit)
        context.report_pressure({
            "context_window": budget.context_tokens,
            "used_tokens": token_estimate,
            "reserve_tokens": budget.output_reserve_tokens,
        })
        last_usage = _request_usage_total(messages, request_snapshot)
        usage_overflow = bool(last_usage and last_usage >= hard_limit)
        if pruned:
            context.trace(
                "context_tool_pruned",
                count=pruned,
                token_estimate_before=before_prune,
                token_estimate_after=token_estimate,
            )
        if persisted:
            context.trace(
                "context_user_input_persisted",
                count=persisted,
                token_estimate=token_estimate,
            )
        if (
            token_estimate <= budget.soft_preflight_tokens
            and not usage_overflow
            and not replay_pressure
        ):
            return False
        if token_estimate > budget.soft_preflight_tokens:
            context.trace(
                "context_preflight_over_soft",
                token_estimate=token_estimate,
                soft_limit=budget.soft_preflight_tokens,
                usable_input=hard_limit,
            )
            degraded = compact_stored_input_expansions(messages, "preflight")
            if degraded:
                token_estimate = self._projected_tokens(context, messages, request_snapshot)
                replay_token_estimate = _projected_replay_tokens(context, messages)
                replay_pressure = _replay_limit_exceeded(replay_token_estimate, replay_limit)
                context.trace(
                    "context_input_expansion_compacted",
                    count=degraded,
                    token_estimate=token_estimate,
                )
        request_overflow = token_estimate > hard_limit
        if not request_overflow and not usage_overflow and not replay_pressure:
            return False
        source_version = message_version(messages)
        if (not request_overflow and not usage_overflow and attempt_state is not None
                and attempt_state.suppressed_replay_version == source_version):
            return False
        snapshot = request_snapshot or context.freeze_request(messages)
        attempt = attempt_state.reserve() if attempt_state is not None else None
        if on_text:
            on_text("[auto-compact triggered]")
        trigger = (
            "provider_usage"
            if usage_overflow
            else "request_estimate"
            if request_overflow
            else "replay_cost"
        )
        context.trace(
            "compact",
            kind="auto",
            token_estimate=token_estimate,
            soft_limit=budget.soft_preflight_tokens,
            usable_input=budget.usable_input_tokens,
            output_reserve=budget.output_reserve_tokens,
            last_usage_tokens=last_usage,
            replay_token_estimate=replay_token_estimate,
            replay_limit=replay_limit,
            trigger=trigger,
            attempts=attempt,
        )
        try:
            compacted = await _to_thread_settled(
                context.compact,
                copy.deepcopy(messages),
                cancel_callback=lambda: (request_current_run_cancel(), context.cancel_compaction()),
            )
        except (CompactionGenerationError, PublicRuntimeError) as exc:
            if not _skip_failed_replay_compaction(
                context, messages, exc, source_version=source_version,
                physical=request_overflow or usage_overflow, attempt_state=attempt_state,
            ):
                raise
            return False
        return accept_compaction(
            context, messages, compacted, snapshot=snapshot,
            source_version=source_version, trigger=trigger,
            physical=request_overflow or usage_overflow, attempt_state=attempt_state,
        )

    @staticmethod
    def _projected_tokens(
        context: ContextExecutionContext,
        messages: list,
        request_snapshot: FrozenRequestSnapshot | None = None,
    ) -> int:
        """Read a required Context metric with a conservative local fallback."""
        if request_snapshot is not None:
            return request_snapshot.estimate(messages)
        try:
            raw = context.projected_tokens(messages)
        except Exception as exc:
            _trace_metric_repair(
                context,
                "projected_tokens",
                error=type(exc).__name__,
            )
            return max(0, estimate_tokens(messages))
        normalized = _nonnegative_int(raw)
        if normalized is not None:
            return normalized
        _trace_metric_repair(context, "projected_tokens")
        return max(0, estimate_tokens(messages))


def _request_usage_total(messages: list, snapshot: FrozenRequestSnapshot | None) -> int:
    # 当前步骤的空占位消息尚未收到模型用量，不能遮住上一次真实响应。
    pending = {id(message) for message in snapshot.retained} if snapshot is not None else set()
    history = [message for message in messages if id(message) not in pending]
    return _last_assistant_usage_total(history)


def _skip_failed_replay_compaction(
    context: ContextExecutionContext,
    messages: list,
    error: Exception,
    *,
    source_version: str,
    physical: bool,
    attempt_state: CompactionAttemptState | None,
) -> bool:
    """只放弃可选的省费压缩；真实窗口、状态冲突和取消仍由调用者处理。"""
    code = (error.public_error.code if isinstance(error, PublicRuntimeError)
            else "compaction_model_failed")
    if physical or code not in {
        "invalid_compaction_summary", "compaction_budget_exhausted", "compaction_model_failed",
    }:
        return False
    run = current_run_context()
    if run is not None:
        run.check_cancelled()
    if message_version(messages) != source_version:
        raise PublicRuntimeError(PublicError(
            "context_changed", "Context changed while the compaction candidate was generated",
        )) from error
    if attempt_state is not None:
        attempt_state.suppressed_replay_version = source_version
    # 只记录稳定错误码，不把 Provider 原始响应或推理写入公开轨迹。
    context.trace("compaction_generation_failed", trigger="replay_cost", code=code,
                  action="continue_history")
    return True


def accept_compaction(
    context: ContextExecutionContext,
    messages: list,
    candidate: list,
    *,
    snapshot: FrozenRequestSnapshot,
    source_version: str,
    trigger: str,
    physical: bool,
    attempt_state: CompactionAttemptState | None = None,
    before_tokens: int | None = None,
) -> bool:
    """Validate a candidate before changing history or retiring read evidence."""
    if message_version(messages) != source_version:
        raise PublicRuntimeError(PublicError(
            "context_changed", "Context changed while the compaction candidate was generated",
        ))
    candidate = snapshot.restore_retained(candidate)
    before = snapshot.estimate(messages) if before_tokens is None else before_tokens
    after = snapshot.estimate(candidate)
    marker = candidate[0].get("_nz_compaction") if candidate else None
    operation_id = marker.get("operation_id") if isinstance(marker, dict) else None
    limit = context.budget.usable_input_tokens or context.budget.soft_preflight_tokens
    if physical and (after >= before or after > limit):
        context.trace("compaction_candidate_rejected", trigger=trigger, reason="cannot_fit",
                      before_tokens=before, after_tokens=after, operation_id=operation_id)
        raise PublicRuntimeError(PublicError(
            "context_cannot_shrink",
            f"Context cannot shrink to fit the input budget ({before} -> {after}, limit {limit})",
        ))
    if not physical and after >= before:
        if attempt_state is not None:
            attempt_state.suppressed_replay_version = source_version
        context.trace("compaction_candidate_rejected", trigger=trigger,
                      reason="no_savings", before_tokens=before, after_tokens=after, operation_id=operation_id)
        return False
    context.stamp_auto_compaction(candidate)
    _mark_compaction_trigger(candidate, trigger, physical)
    messages[:] = candidate
    context.trace("compaction_candidate_installed", trigger=trigger,
                  before_tokens=before, after_tokens=after, operation_id=operation_id)
    return True


def replay_compaction_pressure(context: ContextExecutionContext, messages: list) -> bool:
    """让冻结前的记忆整理感知相同回放压力；不承诺清理后一定压缩。"""
    limit = max(0, int(context.budget.replay_compaction_tokens))
    return bool(limit and _replay_limit_exceeded(_projected_replay_tokens(context, messages), limit))


def _replay_limit_exceeded(tokens: int | None, limit: int) -> bool:
    return bool(limit and tokens is not None and tokens > limit)


def _projected_replay_tokens(
    context: ContextExecutionContext,
    messages: list,
) -> int | None:
    """Return provider-visible history size when the host exposes that projection."""
    projector = context.projected_replay_tokens
    if projector is None:
        return None
    try:
        raw = projector(messages)
    except Exception as exc:
        _trace_metric_repair(
            context,
            "projected_replay_tokens",
            error=type(exc).__name__,
        )
        return None
    normalized = _nonnegative_int(raw)
    if normalized is None:
        _trace_metric_repair(context, "projected_replay_tokens")
    return normalized


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _trace_metric_repair(
    context: ContextExecutionContext,
    metric: str,
    *,
    error: str = "invalid_value",
) -> None:
    try:
        context.trace("context_metric_repaired", metric=metric, error=error)
    except Exception:
        return


def _mark_compaction_trigger(
    compacted: list,
    trigger: str,
    overflow: bool,
) -> None:
    """Keep proactive cost compaction distinct from physical overflow recovery."""
    if not compacted or not isinstance(compacted[0], dict):
        return
    marker = compacted[0].get(COMPACTION_KEY)
    if not isinstance(marker, dict):
        return
    marker["trigger"] = trigger
    marker["overflow"] = bool(overflow)
