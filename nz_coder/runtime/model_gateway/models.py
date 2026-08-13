"""Immutable request, event, and terminal models for the model Gateway."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum

from nz_coder.runtime.model_gateway.usage import NormalizedUsage


class ModelCallPurpose(str, Enum):
    """Diagnostic purpose of a Provider request."""

    CODING = "coding"
    PLANNING = "planning"
    REPLANNING = "replanning"
    COMPACTION = "compaction"
    MEMORY = "memory"
    VERIFIER = "verifier"
    STALL_SIDECAR = "stall_sidecar"
    VISION = "vision"


class ModelCallStatus(str, Enum):
    """Normalized terminal state of one bounded model call."""

    COMPLETED = "completed"
    CONTEXT_OVERFLOW = "context_overflow"
    CLIENT_ERROR = "client_error"
    CANCELLED = "cancelled"
    ABORTED = "aborted"


@dataclass(frozen=True)
class ModelCall:
    """Complete Provider-neutral input for one logical model call."""

    purpose: ModelCallPurpose
    messages: tuple[dict, ...] | list[dict]
    max_output_tokens: int
    tools: tuple[dict, ...] | list[dict] = field(default_factory=tuple)
    tool_choice: dict | None = None
    streaming: bool = False
    timeout_seconds: float = 600.0
    response_format: dict | None = None
    capability_options: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.purpose, ModelCallPurpose):
            raise TypeError("ModelCall purpose must be a ModelCallPurpose")
        if (
            not isinstance(self.max_output_tokens, int)
            or isinstance(self.max_output_tokens, bool)
            or self.max_output_tokens <= 0
        ):
            raise ValueError("ModelCall max_output_tokens must be positive")
        if not isinstance(self.timeout_seconds, (int, float)) or self.timeout_seconds <= 0:
            raise ValueError("ModelCall timeout_seconds must be positive")
        object.__setattr__(self, "messages", copy.deepcopy(tuple(self.messages)))
        object.__setattr__(self, "tools", copy.deepcopy(tuple(self.tools)))
        object.__setattr__(self, "tool_choice", copy.deepcopy(self.tool_choice))
        object.__setattr__(self, "response_format", copy.deepcopy(self.response_format))
        object.__setattr__(self, "capability_options", copy.deepcopy(self.capability_options))
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))


_STREAM_EVENT_KINDS = frozenset({
    "text",
    "reasoning",
    "tool_delta",
    "usage",
    "provider_metadata",
    "finish",
})


@dataclass(frozen=True)
class ModelStreamEvent:
    """One normalized observable event from a streaming Provider response."""

    kind: str
    data: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _STREAM_EVENT_KINDS:
            raise ValueError(f"Unknown ModelStreamEvent kind: {self.kind}")
        object.__setattr__(self, "data", copy.deepcopy(self.data))


@dataclass(frozen=True)
class ModelCallOutcome:
    """Terminal Provider-neutral result returned by the Gateway."""

    status: ModelCallStatus
    content: str = ""
    reasoning: str = ""
    tool_calls: tuple[dict, ...] | list[dict] = field(default_factory=tuple)
    provider_metadata: dict = field(default_factory=dict)
    finish_reason: str = ""
    usage: NormalizedUsage = field(default_factory=NormalizedUsage)
    cost: float | None = None
    cost_source: str | None = None
    duration_ms: float = 0.0
    first_token_ms: float | None = None
    attempts: int = 1
    error: str = ""
    diagnostic: str = ""
    retryable: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.status, ModelCallStatus):
            raise TypeError("ModelCallOutcome status must be a ModelCallStatus")
        if self.attempts < 1:
            raise ValueError("ModelCallOutcome attempts must be positive")
        object.__setattr__(self, "tool_calls", copy.deepcopy(tuple(self.tool_calls)))
        object.__setattr__(self, "provider_metadata", copy.deepcopy(self.provider_metadata))

    @classmethod
    def completed(cls, **values) -> ModelCallOutcome:  # noqa: ANN003
        return cls(status=ModelCallStatus.COMPLETED, **values)

    @classmethod
    def context_overflow(cls, error: str, **values) -> ModelCallOutcome:  # noqa: ANN003
        return cls(
            status=ModelCallStatus.CONTEXT_OVERFLOW,
            error=str(error),
            retryable=False,
            **values,
        )

    @classmethod
    def client_error(cls, error: str, **values) -> ModelCallOutcome:  # noqa: ANN003
        return cls(
            status=ModelCallStatus.CLIENT_ERROR,
            error=str(error),
            retryable=False,
            **values,
        )

    @classmethod
    def cancelled(cls, error: str = "cancelled", **values) -> ModelCallOutcome:  # noqa: ANN003
        return cls(
            status=ModelCallStatus.CANCELLED,
            error=str(error),
            retryable=False,
            **values,
        )

    @classmethod
    def aborted(
        cls,
        error: str,
        *,
        retryable: bool = False,
        **values,
    ) -> ModelCallOutcome:  # noqa: ANN003
        return cls(
            status=ModelCallStatus.ABORTED,
            error=str(error),
            retryable=bool(retryable),
            **values,
        )
