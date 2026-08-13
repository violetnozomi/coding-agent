"""Model-window-aware context preparation for every Agent Runner profile."""
from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from nz_coder.context import micro_compact, persist_oversized_user_inputs
from nz_coder.runtime.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.core.context import ContextExecutionContext
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
        """Clean at the soft threshold and summarize only at the hard limit."""
        budget = context.budget
        hard_limit = budget.usable_input_tokens or budget.soft_preflight_tokens
        expansion = resolve_input_expansions(messages, budget, context.workspace)
        if any(expansion.values()):
            context.trace("context_input_expansion", **expansion)
        persisted = persist_oversized_user_inputs(
            messages,
            hard_limit,
        )
        before_prune = context.projected_tokens(messages)
        pruned = micro_compact(messages, budget=budget)
        token_estimate = context.projected_tokens(messages)
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
        if token_estimate <= budget.soft_preflight_tokens and not usage_overflow:
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
                token_estimate = context.projected_tokens(messages)
                context.trace(
                    "context_input_expansion_compacted",
                    count=degraded,
                    token_estimate=token_estimate,
                )
        if token_estimate <= hard_limit and not usage_overflow:
            return False
        attempt = attempt_state.reserve() if attempt_state is not None else None
        if on_text:
            on_text("[auto-compact triggered]")
        context.trace(
            "compact",
            kind="auto",
            token_estimate=token_estimate,
            soft_limit=budget.soft_preflight_tokens,
            usable_input=budget.usable_input_tokens,
            output_reserve=budget.output_reserve_tokens,
            last_usage_tokens=last_usage,
            trigger="provider_usage" if usage_overflow else "request_estimate",
            attempts=attempt,
        )
        compacted = context.compact(messages)
        context.stamp_auto_compaction(compacted)
        messages[:] = compacted
        return True

    async def prepare_async(
        self, context: ContextExecutionContext,
        messages: list,
        on_text=None,
        *,
        attempt_state: Any | None = None,
    ) -> bool:
        """Async wrapper for soft cleanup and hard-limit compaction."""
        budget = context.budget
        hard_limit = budget.usable_input_tokens or budget.soft_preflight_tokens
        expansion = resolve_input_expansions(messages, budget, context.workspace)
        if any(expansion.values()):
            context.trace("context_input_expansion", **expansion)
        persisted = persist_oversized_user_inputs(
            messages,
            hard_limit,
        )
        before_prune = context.projected_tokens(messages)
        pruned = micro_compact(messages, budget=budget)
        token_estimate = context.projected_tokens(messages)
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
        if token_estimate <= budget.soft_preflight_tokens and not usage_overflow:
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
                token_estimate = context.projected_tokens(messages)
                context.trace(
                    "context_input_expansion_compacted",
                    count=degraded,
                    token_estimate=token_estimate,
                )
        if token_estimate <= hard_limit and not usage_overflow:
            return False
        attempt = attempt_state.reserve() if attempt_state is not None else None
        if on_text:
            on_text("[auto-compact triggered]")
        context.trace(
            "compact",
            kind="auto",
            token_estimate=token_estimate,
            soft_limit=budget.soft_preflight_tokens,
            usable_input=budget.usable_input_tokens,
            output_reserve=budget.output_reserve_tokens,
            last_usage_tokens=last_usage,
            trigger="provider_usage" if usage_overflow else "request_estimate",
            attempts=attempt,
        )
        compacted = await _to_thread_settled(
            context.compact,
            messages,
        )
        context.stamp_auto_compaction(compacted)
        messages[:] = compacted
        return True

def _last_assistant_usage_total(messages: list[dict]) -> int:
    """Return usage produced after the latest completed compaction boundary."""
    compacted_at = max(
        (
            float(marker.get("created_at", 0) or 0)
            for message in messages
            if isinstance(message, dict)
            and isinstance((marker := message.get("_nz_compaction")), dict)
        ),
        default=0.0,
    )
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        timestamp = message.get("_timestamp")
        if compacted_at and (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
            or float(timestamp) <= compacted_at
        ):
            continue
        usage = message.get("_nz_usage")
        if not isinstance(usage, dict):
            return 0
        total = usage.get("total")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            return max(0, int(total))
        input_tokens = usage.get("input", 0)
        output_tokens = usage.get("output", 0)
        if all(isinstance(item, (int, float)) for item in (input_tokens, output_tokens)):
            return max(0, int(input_tokens) + int(output_tokens))
        return 0
    return 0
