"""Policy-approved publication boundaries for model output."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from enum import Enum

from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.protocol.message_schema import (
    AUTHORITATIVE_KEY,
    INTERNAL_KEY,
    VISIBLE_KEY,
    provider_private_state,
    sanitize_provider_extra,
    set_assistant_error,
)
from nz_coder.protocol.public_error import PublicError, to_public_error


class OutputVisibility(str, Enum):
    """Describe whether an approved model result belongs in the user view."""

    USER_VISIBLE = "user_visible"
    INTERNAL_AGENT_RESULT = "internal_agent_result"
    HIDDEN_DIAGNOSTIC = "hidden_diagnostic"


@dataclass(frozen=True)
class ModelAttempt:
    """A normalized Provider result that has not crossed public policy yet."""

    result: LLMResult
    visibility: OutputVisibility


@dataclass(frozen=True)
class ApprovedModelResult:
    """A model result whose text is safe for its declared visibility."""

    result: LLMResult
    visibility: OutputVisibility


@dataclass
class FailedAttemptSettlement:
    """Resumable phase ledger for one Assistant attempt failure."""

    snapshot_retired: bool = False
    part_retired: bool = False
    policy_parts_settled: bool = False
    error_attached: bool = False
    step_finished: bool = False
    checkpointed: bool = False
    completed: bool = False
    finish_reason: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def settled(self) -> bool:
        """Compatibility view for callers that previously read ``settled``."""
        return self.completed


async def approve_model_result(
    *,
    context,
    result: LLMResult,
    messages: list[dict],
    visibility: OutputVisibility,
) -> ApprovedModelResult:
    """Run output policy for every textual finish reason before publication."""
    if not isinstance(result, LLMResult):
        raise TypeError("model attempt must contain LLMResult")
    candidate = copy.deepcopy(result)
    if candidate.content:
        candidate.content = await context.policy.run_output_guardrail(
            candidate.content,
            messages,
        )
    return ApprovedModelResult(candidate, visibility)


def commit_approved_model_result(
    approved: ApprovedModelResult,
    *,
    context,
    assistant_message: dict,
    processor,
    message_part: dict,
    messages: list[dict],
    reconcile: bool = False,
) -> None:
    """Cross the sole model-to-Message/Part publication boundary.

    Internal child results remain available to the transition runtime but do
    not create a user-visible TextPart or delta. Tool envelopes, when present,
    are committed only after the tool runtime has replaced them with approved
    calls.
    """
    if not isinstance(approved, ApprovedModelResult):
        raise TypeError("commit requires ApprovedModelResult")
    result = copy.deepcopy(approved.result)
    result.extra = sanitize_provider_extra(result.extra)
    internal = approved.visibility is not OutputVisibility.USER_VISIBLE
    if internal and not result.tool_calls:
        assistant_message["content"] = result.content or ""
        assistant_message.update(provider_private_state(result.extra))
        assistant_message["role"] = "assistant"
        assistant_message[VISIBLE_KEY] = False
        assistant_message[INTERNAL_KEY] = True
        assistant_message[AUTHORITATIVE_KEY] = True
        return
    if internal:
        result.content = ""
        result.extra.pop("reasoning_content", None)
    operation = (
        context.messages.reconcile_llm_result
        if reconcile
        else context.messages.materialize_llm_result
    )
    operation(
        result,
        assistant_message=assistant_message,
        processor=processor,
        message_part=message_part,
        messages=messages,
    )
    assistant_message["role"] = "assistant"
    assistant_message[VISIBLE_KEY] = not internal
    assistant_message[INTERNAL_KEY] = internal
    assistant_message[AUTHORITATIVE_KEY] = True


async def settle_failed_attempt(
    *,
    context,
    services,
    run_context,
    assistant_message: dict,
    processor,
    message_part: dict,
    public_error: PublicError | object,
    failure_kind: str,
    settlement: FailedAttemptSettlement,
    snapshot_task=None,
    snapshot_cancel=None,
) -> bool:
    """Atomically close one failed pre-commit policy attempt exactly once."""
    if not isinstance(settlement, FailedAttemptSettlement):
        raise TypeError("settlement must be a FailedAttemptSettlement")
    public = to_public_error(public_error)
    hook_point = str(public.metadata.get("hook_point") or "")
    policy_failure = public.code in {
        "guardrail_blocked",
        "guardrail_review_required",
    }
    finish_reason = (
        "cancelled"
        if public.code == "cancelled"
        else "blocked"
        if policy_failure
        else "error"
    )
    error_name = (
        "MessageAbortedError"
        if finish_reason == "cancelled"
        else "ToolGuardrailError"
        if hook_point == "tool"
        else "OutputGuardrailError"
        if hook_point == "output"
        else "UnknownError"
    )

    async with settlement.lock:
        if settlement.completed:
            return False
        retire_snapshot = getattr(context.snapshots, "retire", None)
        if not settlement.snapshot_retired:
            if callable(retire_snapshot):
                retire_snapshot(snapshot_task, snapshot_cancel)
            settlement.snapshot_retired = True
        if not settlement.part_retired:
            context.messages.retire_message_part(
                message_part,
                f"{str(failure_kind or 'attempt')}_failed",
            )
            settlement.part_retired = True
        if not settlement.policy_parts_settled:
            processor.settle_policy_failure(public)
            settlement.policy_parts_settled = True
        if not settlement.error_attached:
            set_assistant_error(
                assistant_message,
                public,
                name=error_name,
                publish=context.messages.publish_event,
            )
            settlement.error_attached = True
        if not settlement.step_finished:
            processor.finish_step(finish_reason)
            settlement.step_finished = True
            settlement.finish_reason = finish_reason
        if not settlement.checkpointed:
            await services.session_runtime.checkpoint(run_context, "error")
            settlement.checkpointed = True
        settlement.completed = True
        return True
