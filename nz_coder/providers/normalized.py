"""Provider-neutral response objects consumed by the existing agent loop."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedFunction:
    """Function name and JSON arguments for a tool-call response."""

    name: str = ""
    arguments: str = ""


@dataclass
class NormalizedToolCall:
    """OpenAI-shaped tool call used by both complete and streamed responses."""

    index: int = 0
    id: str = ""
    function: NormalizedFunction = field(default_factory=NormalizedFunction)
    type: str = "function"
    provider_extra: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict:
        """Return the message-history representation expected by AgentLoop."""
        payload = {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }
        if self.provider_extra:
            payload["provider_extra"] = dict(self.provider_extra)
        return payload


@dataclass
class NormalizedMessage:
    """Provider-neutral non-streaming assistant message."""

    content: str = ""
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    reasoning_content: str = ""
    provider_extra: dict[str, Any] = field(default_factory=dict)

    def model_dump(self) -> dict:
        """Return an assistant message compatible with SDK serialization."""
        payload = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                tool_call.model_dump() for tool_call in self.tool_calls
            ]
        if self.reasoning_content:
            payload["reasoning_content"] = self.reasoning_content
        if self.provider_extra:
            payload["provider_extra"] = dict(self.provider_extra)
        return payload


@dataclass
class NormalizedDelta:
    """Provider-neutral streaming delta."""

    content: str = ""
    tool_calls: list[NormalizedToolCall] = field(default_factory=list)
    reasoning_content: str = ""
    provider_extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class NormalizedChoice:
    """Choice wrapper matching the fields read by AgentLoop."""

    message: NormalizedMessage | None = None
    delta: NormalizedDelta | None = None
    finish_reason: str | None = None


@dataclass
class NormalizedCompletion:
    """Non-streaming response matching the subset of the OpenAI SDK in use."""

    choices: list[NormalizedChoice]
    usage: dict[str, Any] | None = None


@dataclass
class NormalizedChunk:
    """Streaming response chunk matching the subset consumed by AgentLoop."""

    choices: list[NormalizedChoice]
    usage: dict[str, Any] | None = None


def completion(
    *,
    content: str = "",
    tool_calls: list[NormalizedToolCall] | None = None,
    reasoning_content: str = "",
    provider_extra: dict[str, Any] | None = None,
    finish_reason: str = "",
    usage: dict[str, Any] | None = None,
) -> NormalizedCompletion:
    """Build a normalized non-streaming completion."""
    return NormalizedCompletion(
        choices=[
            NormalizedChoice(
                message=NormalizedMessage(
                    content=content,
                    tool_calls=tool_calls or [],
                    reasoning_content=reasoning_content,
                    provider_extra=provider_extra or {},
                ),
                finish_reason=finish_reason or None,
            ),
        ],
        usage=dict(usage) if usage else None,
    )


def chunk(
    *,
    content: str = "",
    tool_calls: list[NormalizedToolCall] | None = None,
    reasoning_content: str = "",
    provider_extra: dict[str, Any] | None = None,
    finish_reason: str = "",
    usage: dict[str, Any] | None = None,
) -> NormalizedChunk:
    """Build a normalized streaming chunk."""
    return NormalizedChunk(
        choices=[
            NormalizedChoice(
                delta=NormalizedDelta(
                    content=content,
                    tool_calls=tool_calls or [],
                    reasoning_content=reasoning_content,
                    provider_extra=provider_extra or {},
                ),
                finish_reason=finish_reason or None,
            ),
        ],
        usage=dict(usage) if usage else None,
    )
