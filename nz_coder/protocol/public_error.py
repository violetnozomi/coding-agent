"""Single secret-safe projection for externally visible failures."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")
_PUBLIC_CODE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
PUBLIC_ERROR_SCHEMA = "nz.public_error.v1"


@dataclass(frozen=True)
class PublicError:
    """Stable error payload safe for Session, HTTP, journal, and terminal UI."""

    code: str
    message: str
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        code = (
            self.code
            if isinstance(self.code, str) and _PUBLIC_CODE.fullmatch(self.code)
            else "internal_error"
        )
        return {
            "schema": PUBLIC_ERROR_SCHEMA,
            "code": code,
            "message": _bounded(self.message) or "An internal error occurred.",
            "retryable": bool(self.retryable),
            "metadata": _safe_metadata(self.metadata),
        }


@dataclass(frozen=True)
class TrustedPublicMessage:
    """Explicit opt-in for a caller-authored message safe for public surfaces."""

    code: str
    message: str
    retryable: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class PublicRuntimeError(RuntimeError):
    """Exception wrapper that cannot reveal a private cause via ``str``."""

    def __init__(self, error: PublicError):
        self.public_error = error
        super().__init__(error.message)


def to_public_error(error: object) -> PublicError:
    """Project a failure without formatting its private exception chain."""
    if isinstance(error, PublicError):
        return _normalized_public_error(
            error.code,
            error.message,
            error.retryable,
            error.metadata,
        )
    if isinstance(error, TrustedPublicMessage):
        return _normalized_public_error(
            error.code,
            error.message,
            error.retryable,
            error.metadata,
        )
    if isinstance(error, PublicRuntimeError):
        return to_public_error(error.public_error)
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
        name = _safe_name(getattr(error, "guardrail_name", "policy"))
        return PublicError(
            "guardrail_review_required",
            "Output requires policy review.",
            metadata={
                "guardrail": name,
                "hook_point": str(getattr(error, "hook_point", "output")),
            },
        )
    if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)):
        return PublicError("cancelled", "Request cancelled.", retryable=True)
    if not isinstance(error, BaseException):
        return PublicError("internal_error", "An internal error occurred.")
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
    if isinstance(value, (PublicError, TrustedPublicMessage, PublicRuntimeError)):
        return to_public_error(value).message
    if isinstance(value, dict):
        public = public_error_from_wire(value)
        if public is not None:
            return public.message
        nested = value.get("error")
        if nested is not value:
            public = public_error_from_wire(nested)
            if public is not None:
                return public.message
    return "Request failed."


def public_error_from_wire(value: object) -> PublicError | None:
    """Parse only the explicitly versioned public-error wire contract."""
    if not isinstance(value, dict) or value.get("schema") != PUBLIC_ERROR_SCHEMA:
        return None
    code = value.get("code")
    message = value.get("message")
    retryable = value.get("retryable")
    metadata = value.get("metadata")
    if (
        not isinstance(code, str)
        or _PUBLIC_CODE.fullmatch(code) is None
        or not isinstance(message, str)
        or not message.strip()
        or not isinstance(retryable, bool)
        or not isinstance(metadata, dict)
    ):
        return None
    return _normalized_public_error(code, message, retryable, metadata)


def _safe_name(value: object) -> str:
    return _SAFE_NAME.sub("-", str(value or "policy"))[:120] or "policy"


def _bounded(value: str) -> str:
    return " ".join(str(value or "").split())[:1000]


def _normalized_public_error(
    code: object,
    message: object,
    retryable: object,
    metadata: object,
) -> PublicError:
    normalized_code = str(code or "internal_error")
    if _PUBLIC_CODE.fullmatch(normalized_code) is None:
        normalized_code = "internal_error"
    normalized_message = _bounded(str(message or "")) or "An internal error occurred."
    return PublicError(
        normalized_code,
        normalized_message,
        bool(retryable),
        _safe_metadata(metadata),
    )


def _safe_metadata(value: object, *, _depth: int = 0) -> dict[str, Any]:
    """Keep trusted metadata small, JSON-shaped, and credential-key safe."""
    if not isinstance(value, dict) or _depth >= 4:
        return {}
    result: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:50]:
        key = _bounded(str(raw_key))[:120]
        if not key:
            continue
        lowered = key.casefold()
        if any(
            marker in lowered
            for marker in (
                "authorization",
                "cookie",
                "password",
                "secret",
                "token",
                "api-key",
                "apikey",
            )
        ):
            result[key] = "[REDACTED]"
        elif isinstance(raw_value, dict):
            result[key] = _safe_metadata(raw_value, _depth=_depth + 1)
        elif isinstance(raw_value, (list, tuple)):
            result[key] = [
                _safe_metadata(item, _depth=_depth + 1)
                if isinstance(item, dict)
                else item
                if isinstance(item, (str, int, float, bool)) or item is None
                else type(item).__name__
                for item in list(raw_value)[:50]
            ]
        elif isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            result[key] = _bounded(raw_value) if isinstance(raw_value, str) else raw_value
        else:
            result[key] = type(raw_value).__name__
    return result
