"""Model-window-aware context preparation for every Agent Runner profile."""
from __future__ import annotations

import math
from typing import Any
from dataclasses import dataclass

from nz_coder.state.context import (
    estimate_tokens,
    micro_compact,
    persist_oversized_user_inputs,
)
from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.conversation.continuation_context import (
    continuation_projection_details,
)
from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.runtime.conversation.usage_history import (
    last_assistant_usage_total as _last_assistant_usage_total,
)
from nz_coder.protocol.message_schema import COMPACTION_KEY
from nz_coder.state.input_expansion import (
    compact_stored as compact_stored_input_expansions,
    resolve_and_apply_budget as resolve_input_expansions,
)

MAX_COMPACTION_ATTEMPTS = 3


class CompactionAttemptsExhausted(RuntimeError):
    """Raised before an automatic fourth compaction in one user run."""


@dataclass
class CompactionAttemptState:
    """Single owner for every automatic compaction in one Agent run."""

    attempts: int = 0

    def reserve(self) -> int:
        if self.attempts >= MAX_COMPACTION_ATTEMPTS:
            raise CompactionAttemptsExhausted(
                "Compaction exhausted: context still exceeds model limits "
                f"after {MAX_COMPACTION_ATTEMPTS} attempts"
            )
        self.attempts += 1
        return self.attempts


class ProductionContextManager:
    """Own preflight pruning and semantic compaction trigger ordering."""

    def prepare_sync(
        self, context: ContextExecutionContext,
        messages: list,
        on_text=None,
        *,
        attempt_state: Any | None = None,
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
        before_prune = self._projected_tokens(context, messages)
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
        token_estimate = self._projected_tokens(context, messages)
        replay_token_estimate = _projected_replay_tokens(context, messages)
        replay_limit = max(0, int(budget.replay_compaction_tokens))
        replay_pressure = bool(
            replay_limit
            and replay_token_estimate is not None
            and replay_token_estimate > replay_limit
        )
        context.report_pressure({
            "context_window": budget.context_tokens,
            "used_tokens": token_estimate,
            "reserve_tokens": budget.output_reserve_tokens,
        })
        last_usage = _last_assistant_usage_total(messages)
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
                token_estimate = self._projected_tokens(context, messages)
                replay_token_estimate = _projected_replay_tokens(context, messages)
                replay_pressure = bool(
                    replay_limit
                    and replay_token_estimate is not None
                    and replay_token_estimate > replay_limit
                )
                context.trace(
                    "context_input_expansion_compacted",
                    count=degraded,
                    token_estimate=token_estimate,
                )
        request_overflow = token_estimate > hard_limit
        if not request_overflow and not usage_overflow and not replay_pressure:
            return False
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
        compacted = context.compact(messages)
        context.stamp_auto_compaction(compacted)
        _mark_compaction_trigger(compacted, trigger, request_overflow or usage_overflow)
        messages[:] = compacted
        return True

    async def prepare_async(
        self, context: ContextExecutionContext,
        messages: list,
        on_text=None,
        *,
        attempt_state: Any | None = None,
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
        before_prune = self._projected_tokens(context, messages)
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
        token_estimate = self._projected_tokens(context, messages)
        replay_token_estimate = _projected_replay_tokens(context, messages)
        replay_limit = max(0, int(budget.replay_compaction_tokens))
        replay_pressure = bool(
            replay_limit
            and replay_token_estimate is not None
            and replay_token_estimate > replay_limit
        )
        context.report_pressure({
            "context_window": budget.context_tokens,
            "used_tokens": token_estimate,
            "reserve_tokens": budget.output_reserve_tokens,
        })
        last_usage = _last_assistant_usage_total(messages)
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
                token_estimate = self._projected_tokens(context, messages)
                replay_token_estimate = _projected_replay_tokens(context, messages)
                replay_pressure = bool(
                    replay_limit
                    and replay_token_estimate is not None
                    and replay_token_estimate > replay_limit
                )
                context.trace(
                    "context_input_expansion_compacted",
                    count=degraded,
                    token_estimate=token_estimate,
                )
        request_overflow = token_estimate > hard_limit
        if not request_overflow and not usage_overflow and not replay_pressure:
            return False
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
        compacted = await _to_thread_settled(
            context.compact,
            messages,
            cancel_callback=context.cancel_compaction,
        )
        context.stamp_auto_compaction(compacted)
        _mark_compaction_trigger(compacted, trigger, request_overflow or usage_overflow)
        messages[:] = compacted
        return True

    @staticmethod
    def _projected_tokens(
        context: ContextExecutionContext,
        messages: list,
    ) -> int:
        """Read a required Context metric with a conservative local fallback."""
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
