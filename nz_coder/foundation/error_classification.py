"""Dependency-light error classifiers shared across product layers."""
from __future__ import annotations


def is_context_overflow_error(error: Exception | str) -> bool:
    """Recognize provider-specific context-window rejection messages.

    OpenAI-compatible providers commonly report this as a generic HTTP 400,
    either with a named error code or prose. Keep this narrower than generic
    client-error handling so invalid tool JSON follows its diagnostic path.
    """
    text = str(error).lower()
    markers = (
        "context_length_exceeded",
        "context window exceeded",
        "context window is exceeded",
        "maximum context length",
        "max context length",
        "context length overflow",
        "context overflow",
        "prompt is too long",
        "input is too long",
        "input exceeds context window",
        "too many tokens",
        "token limit exceeded",
    )
    return any(marker in text for marker in markers)
