"""Policy-approved publication boundaries for model output."""
from __future__ import annotations

import asyncio
import copy
import inspect
import uuid
from dataclasses import dataclass, field
from enum import Enum

from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.protocol.message_schema import (
    ASSISTANT_ERROR_KEY,
    ASSISTANT_FINISH_KEY,
    ASSISTANT_MODEL_KEY,
    ASSISTANT_PROVIDER_KEY,
    ASSISTANT_PROVIDER_INSTANCE_KEY,
    AUTHORITATIVE_KEY,
    INTERNAL_KEY,
    PARTS_KEY,
    POLICY_SETTLEMENT_KEY,
    VISIBLE_KEY,
    normalize_assistant_error,
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
    checkpoint_id: str = field(default_factory=lambda: f"settlement-{uuid.uuid4().hex}")
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def settled(self) -> bool:
        """Compatibility view for callers that previously read ``settled``."""
        return self.completed


def _part_retirement_committed(message_part: object) -> bool:
    """Recognize the durable marker written by MessageRuntime retirement."""
    return isinstance(message_part, dict) and message_part.get("retired") is True


def _assistant_error_committed(
    assistant_message: dict,
    *,
    name: str,
    public: PublicError,
) -> bool:
    """Recognize the exact typed public error after a callback failure."""
    current = normalize_assistant_error(
        assistant_message.get(ASSISTANT_ERROR_KEY)
    )
    if not isinstance(current, dict) or current.get("name") != name:
        return False
    data = current.get("data") if isinstance(current.get("data"), dict) else {}
    wire = data.get("public_error")
    return (
        isinstance(wire, dict)
        and wire.get("code") == public.code
        and wire.get("message") == public.message
    )


def _step_finish_committed(assistant_message: dict, reason: str) -> bool:
    """Recognize a fully persisted terminal step after publication raises."""
    if assistant_message.get(ASSISTANT_FINISH_KEY) != reason:
        return False
    return any(
        isinstance(part, dict)
        and part.get("type") == "step-finish"
        and part.get("reason") == reason
        for part in assistant_message.get(PARTS_KEY, [])
    )


def _snapshot_retirement_committed(
    snapshots: object,
    snapshot_task: object,
    snapshot_cancel: object,
) -> bool:
    """Recognize cancellation signalled before a retirement callback raised."""
    probe = getattr(snapshots, "retirement_committed", None)
    if callable(probe):
        try:
            return bool(probe(snapshot_task, snapshot_cancel))
        except Exception:
            return False
    is_set = getattr(snapshot_cancel, "is_set", None)
    if callable(is_set) and is_set():
        return True
    for name in ("cancelled", "done"):
        check = getattr(snapshot_task, name, None)
        if callable(check) and check():
            return True
    return False


def _policy_settlement_committed(assistant_message: dict, public: PublicError) -> bool:
    marker = assistant_message.get(POLICY_SETTLEMENT_KEY)
    return bool(
        isinstance(marker, dict)
        and marker.get("schema") == "nz.policy_settlement.v1"
        and marker.get("error_code") == public.code
    )


async def _checkpoint_committed(runtime: object, run_context: object, marker: str) -> bool:
    probe = getattr(runtime, "checkpoint_committed", None)
    if callable(probe):
        try:
            result = probe(run_context, marker)
            if inspect.isawaitable(result):
                result = await result
            return bool(result)
        except Exception:
            return False
    metadata = getattr(run_context, "metadata", None)
    return bool(
        isinstance(metadata, dict)
        and marker in (metadata.get("_nz_checkpoint_commits") or [])
    )


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
    source_provider_id: str = "",
    source_provider_instance_id: str = "",
    source_model_id: str = "",
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
        provider_id = str(
            source_provider_id
            or assistant_message.get(ASSISTANT_PROVIDER_KEY)
            or ""
        )
        model_id = str(
            source_model_id
            or assistant_message.get(ASSISTANT_MODEL_KEY)
            or ""
        )
        assistant_message["content"] = result.content or ""
        assistant_message.update(provider_private_state(
            result.extra,
            provider_id=provider_id,
            provider_instance_id=source_provider_instance_id,
            model_id=model_id,
        ))
        if provider_id:
            assistant_message[ASSISTANT_PROVIDER_KEY] = provider_id
        if source_provider_instance_id:
            assistant_message[ASSISTANT_PROVIDER_INSTANCE_KEY] = (
                source_provider_instance_id
            )
        if model_id:
            assistant_message[ASSISTANT_MODEL_KEY] = model_id
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
            try:
                if callable(retire_snapshot):
                    retire_snapshot(snapshot_task, snapshot_cancel)
            except BaseException:
                if _snapshot_retirement_committed(
                    context.snapshots,
                    snapshot_task,
                    snapshot_cancel,
                ):
                    settlement.snapshot_retired = True
                raise
            settlement.snapshot_retired = True
        if not settlement.part_retired:
            try:
                context.messages.retire_message_part(
                    message_part,
                    f"{str(failure_kind or 'attempt')}_failed",
                )
            except BaseException:
                if _part_retirement_committed(message_part):
                    settlement.part_retired = True
                raise
            settlement.part_retired = True
        if not settlement.policy_parts_settled:
            try:
                processor.settle_policy_failure(public)
            except BaseException:
                if _policy_settlement_committed(assistant_message, public):
                    settlement.policy_parts_settled = True
                raise
            settlement.policy_parts_settled = True
        if not settlement.error_attached:
            try:
                set_assistant_error(
                    assistant_message,
                    public,
                    name=error_name,
                    publish=context.messages.publish_event,
                )
            except BaseException:
                if _assistant_error_committed(
                    assistant_message,
                    name=error_name,
                    public=public,
                ):
                    settlement.error_attached = True
                raise
            settlement.error_attached = True
        if not settlement.step_finished:
            try:
                processor.finish_step(finish_reason)
            except BaseException:
                if _step_finish_committed(assistant_message, finish_reason):
                    settlement.step_finished = True
                    settlement.finish_reason = finish_reason
                raise
            settlement.step_finished = True
            settlement.finish_reason = finish_reason
        if not settlement.checkpointed:
            metadata = getattr(run_context, "metadata", None)
            if isinstance(metadata, dict):
                metadata["_nz_active_checkpoint_marker"] = settlement.checkpoint_id
            try:
                await services.session_runtime.checkpoint(run_context, "error")
            except BaseException:
                if await _checkpoint_committed(
                    services.session_runtime,
                    run_context,
                    settlement.checkpoint_id,
                ):
                    settlement.checkpointed = True
                raise
            settlement.checkpointed = True
        settlement.completed = True
        return True
