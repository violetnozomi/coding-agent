"""Provider-neutral error classification for model Gateway attempts."""
from __future__ import annotations

from nz_coder.runtime.recovery import RecoveryState, is_context_overflow_error


def provider_status_code(error: BaseException) -> int | None:
    """Extract an HTTP-like status code without importing an SDK."""
    for owner in (error, getattr(error, "response", None)):
        for name in ("status_code", "status"):
            value = getattr(owner, name, None)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


def is_provider_client_error(error: BaseException) -> bool:
    """Return whether retrying the same request cannot succeed."""
    status = provider_status_code(error)
    if status in {400, 401, 403, 404, 422}:
        return True
    text = str(error).lower()
    return any(marker in text for marker in (
        "invalid_request_error",
        "invalid api key",
        "authentication",
        "unauthorized",
    ))


def classify_provider_error(error: BaseException) -> str:
    """Classify in strict precedence order used by every model consumer."""
    if is_context_overflow_error(error):
        return "context_overflow"
    if is_provider_client_error(error):
        return "client_error"
    if RecoveryState.is_retryable(error):
        return "retryable"
    return "fatal"
