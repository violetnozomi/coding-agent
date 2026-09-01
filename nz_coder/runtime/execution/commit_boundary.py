"""Policy-approved publication boundaries for model output."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum

from nz_coder.runtime.conversation.model_result import LLMResult


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
    result = approved.result
    internal = approved.visibility is not OutputVisibility.USER_VISIBLE
    assistant_message["_nz_visible"] = not internal
    assistant_message["_nz_internal"] = internal
    assistant_message["_nz_authoritative"] = True
    if internal and not result.tool_calls:
        assistant_message["content"] = result.content or ""
        assistant_message.update(copy.deepcopy(result.extra or {}))
        return
    if internal:
        result = copy.deepcopy(result)
        result.content = ""
        result.extra = dict(result.extra or {})
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
