"""Single secret-safe projection for externally visible failures."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class PublicError:
    """Stable error payload safe for Session, HTTP, journal, and terminal UI."""

    code: str
    message: str
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "metadata": dict(self.metadata),
        }


class PublicRuntimeError(RuntimeError):
    """Exception wrapper that cannot reveal a private cause via ``str``."""

    def __init__(self, error: PublicError):
        self.public_error = error
        super().__init__(error.message)


def to_public_error(error: BaseException | str) -> PublicError:
    """Project a failure without formatting its private exception chain."""
    if isinstance(error, PublicRuntimeError):
        return error.public_error
    if isinstance(error, str):
        return PublicError("runtime_error", _bounded(error) or "Request failed.")
    # Protocol is a lower layer than the runtime. Guardrail exceptions expose
    # a stable structural contract, so recognize that contract without a
    # reverse import from protocol into runtime.agent.
    error_name = type(error).__name__
    if error_name == "GuardrailBlockedError":
        name = _safe_name(getattr(error, "guardrail_name", "policy"))
        return PublicError(
            "guardrail_blocked",
            f'Output blocked by guardrail "{name}".',
            metadata={
                "guardrail": name,
                "hook_point": str(getattr(error, "hook_point", "output")),
            },
        )
    if error_name == "GuardrailEscalateError":
        return PublicError(
            "guardrail_review_required",
            "Output requires policy review.",
            metadata={
                "hook_point": str(getattr(error, "hook_point", "output")),
            },
        )
    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
        return PublicError("cancelled", "Request cancelled.", retryable=True)
    status = getattr(error, "status_code", None)
    retryable = bool(
        status in {408, 409, 425, 429}
        or (isinstance(status, int) and status >= 500)
        or any(
            marker in type(error).__name__.casefold()
            for marker in ("timeout", "connection", "ratelimit")
        )
    )
    metadata = {"error_type": _safe_name(type(error).__name__)}
    if isinstance(status, int) and not isinstance(status, bool):
        metadata["status_code"] = status
    return PublicError(
        "provider_error" if status is not None else "internal_error",
        "The provider request failed." if status is not None else "An internal error occurred.",
        retryable=retryable,
        metadata=metadata,
    )


def public_error_message(value: object) -> str:
    """Read a safe message from a projected wire payload."""
    if isinstance(value, PublicError):
        return value.message
    if isinstance(value, dict):
        message = value.get("message")
        if isinstance(message, str):
            return _bounded(message)
        nested = value.get("error")
        if nested is not value:
            return public_error_message(nested)
    return "Request failed."


def _safe_name(value: object) -> str:
    return _SAFE_NAME.sub("-", str(value or "policy"))[:120] or "policy"


def _bounded(value: str) -> str:
    return " ".join(str(value or "").split())[:1000]
