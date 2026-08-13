"""Conservative tool normalization and provider/tool terminal classifiers."""
from __future__ import annotations

import copy
from dataclasses import dataclass
import re


_SEPARATORS = re.compile(r"[\s_-]+")
_TOOL_ERROR_CODE = re.compile(
    r"^(?:\[Tool Error\]|Error:)\s+[^:]+:\s+([A-Z][A-Z0-9_]*)\s*:",
)
_PROMISE = re.compile(
    r"<promise>(COMPLETE|BLOCKED|DECIDE)(?::(.*?))?</promise>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class ProviderAttemptDecision:
    """One deterministic recovery decision owned by a model-call attempt."""

    action: str
    reason: str
    attempt: int
    max_attempts: int
    fallback_used: bool


class ProviderAttemptController:
    """Bound streaming fallback and retries without sharing attempt state.

    A non-streaming fallback is a separate, single attempt.  It is only
    eligible before a stable content/tool boundary, which prevents replaying a
    partially visible answer or executing the same tool twice.
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        allow_non_streaming_fallback: bool = True,
    ) -> None:
        self.max_attempts = max(1, int(max_retries) + 1)
        self.allow_non_streaming_fallback = bool(allow_non_streaming_fallback)
        self.fallback_used = False

    def decide(
        self,
        error: BaseException,
        *,
        attempt: int,
        streaming: bool,
        stable_boundary: bool,
        retryable: bool,
    ) -> ProviderAttemptDecision:
        current = max(1, int(attempt))
        reason = describe_transient_provider_retry(error)
        fallback_candidate = reason in {
            "Stream interrupted before completion",
            "Stream stalled",
            "Provider response timed out",
            "Provider request timed out",
            "Provider connection error",
        }
        if not retryable:
            action = "abort"
        elif (
            streaming
            and not stable_boundary
            and fallback_candidate
            and self.allow_non_streaming_fallback
            and not self.fallback_used
        ):
            self.fallback_used = True
            action = "non_streaming_fallback"
        elif current < self.max_attempts:
            action = "retry"
        else:
            action = "abort"
        return ProviderAttemptDecision(
            action=action,
            reason=reason,
            attempt=current,
            max_attempts=self.max_attempts,
            fallback_used=self.fallback_used,
        )


def normalize_tool_name_key(name: str) -> str:
    return _SEPARATORS.sub("", str(name or "").lower())


def resolve_tool_name_alias(name: str, candidates: list[str] | tuple[str, ...] | set[str]) -> str | None:
    """Repair only a unique case/separator-equivalent tool name."""
    values = [str(item) for item in candidates]
    if name in values:
        return None
    key = normalize_tool_name_key(name)
    if not key:
        return None
    matches = [item for item in values if normalize_tool_name_key(item) == key]
    return matches[0] if len(matches) == 1 else None


def repair_tool_call_names(tool_calls: list[dict], candidates: list[str] | tuple[str, ...] | set[str]) -> tuple[list[dict], list[dict]]:
    """Return canonical immutable calls plus explicit repair observations."""
    repaired_calls = copy.deepcopy(tool_calls)
    repairs = []
    for call in repaired_calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict):
            continue
        original = str(function.get("name") or "")
        repaired = resolve_tool_name_alias(original, candidates)
        if repaired is not None:
            function["name"] = repaired
            repairs.append({"from": original, "to": repaired, "tool_call_id": str(call.get("id") or "")})
    return repaired_calls, repairs


def is_tool_result_error_content(content: str) -> bool:
    text = str(content or "")
    return bool(
        re.match(r"^\[(?:Tool Error|Cancelled|Blocked|Error)\]", text)
        or text.startswith(("Error:", "Denied", "Cancelled"))
    )


def is_cancelled_tool_result_content(content: str) -> bool:
    return str(content or "").startswith(("[Cancelled]", "Cancelled"))


def extract_structured_tool_error_code(content: str) -> str | None:
    match = _TOOL_ERROR_CODE.match(str(content or "").strip())
    return match.group(1) if match else None


def describe_transient_provider_retry(error: BaseException | str) -> str:
    name = getattr(error, "__class__", type(error)).__name__.lower()
    message = str(error).lower()
    if "streamincomplete" in name or "stream incomplete" in message:
        return "Stream interrupted before completion"
    if "stream stalled" in message or "delayed response" in message or "60s idle" in message:
        return "Stream stalled"
    if "hard timeout" in message or "10 minutes" in message:
        return "Provider response timed out"
    if any(item in message for item in (
        "socket hang up", "connection error", "econnrefused", "enotfound",
        "fetch failed", "network",
    )):
        return "Provider connection error"
    if any(item in message for item in ("timed out", "timeout", "etimedout")):
        return "Provider request timed out"
    if "aborted" in message:
        return "Provider stream aborted"
    return "Transient provider error"


def extract_terminal_promise_signal(text: str) -> tuple[str | None, str | None]:
    match = _PROMISE.search(str(text or ""))
    if not match:
        return None, None
    return match.group(1).upper(), (match.group(2) or "").strip() or None
