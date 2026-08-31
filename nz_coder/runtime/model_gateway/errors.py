"""Provider-neutral error classification for model Gateway attempts."""
from __future__ import annotations

from nz_coder.runtime.verification.recovery import RecoveryState, is_context_overflow_error


def provider_status_code(error: BaseException) -> int | None:
    """Extract an HTTP-like status code without importing an SDK."""
    for owner in (error, getattr(error, "response", None)):
        for name in ("status_code", "status"):
            value = getattr(owner, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def is_provider_client_error(error: BaseException) -> bool:
    """Return whether the model can repair a malformed request."""
    status = provider_status_code(error)
    if status in {400, 422}:
        return True
    if status is not None:
        return False
    text = str(error).lower()
    return any(marker in text for marker in (
        "invalid_request_error",
        "invalid json",
        "malformed request",
    ))


def is_provider_terminal_client_error(error: BaseException) -> bool:
    """Return whether account/auth/routing state requires external action."""
    status = provider_status_code(error)
    if status in {401, 402, 403, 404}:
        return True
    text = str(error).casefold()
    return any(marker in text for marker in (
        "insufficient balance",
        "payment required",
        "invalid api key",
        "authentication failed",
        "authentication error",
        "unauthorized",
    ))


def should_fallback_for_forced_tool_choice(error: BaseException) -> bool:
    """Detect gateways that reject a named/required tool choice.

    InfCodeX retries judge calls with the same tools but without the forced
    choice when an OpenAI-compatible gateway cannot combine that field with
    the selected model mode.  Keep unrelated 4xx failures terminal.
    """
    text = str(error).casefold()
    mentions_tool_choice = any(marker in text for marker in (
        "tool_choice",
        "tool choice",
        "toolchoice",
    ))
    explicitly_rejected = mentions_tool_choice and any(marker in text for marker in (
        "unknown parameter",
        "invalid parameter",
        "unsupported",
        "does not support",
        "not support",
    ))
    status = provider_status_code(error)
    return explicitly_rejected or bool(status is not None and 500 <= status <= 599)


def classify_provider_error(error: BaseException) -> str:
    """Classify in strict precedence order used by every model consumer."""
    if is_context_overflow_error(error):
        return "context_overflow"
    if is_provider_terminal_client_error(error):
        return "fatal"
    if is_provider_client_error(error):
        return "client_error"
    if RecoveryState.is_retryable(error):
        return "retryable"
    return "fatal"
