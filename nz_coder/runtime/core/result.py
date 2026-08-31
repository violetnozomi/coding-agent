"""Terminal result contracts shared by every Agent runtime host."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum


class RunStatus(str, Enum):
    """Normalized terminal states produced by the shared Runner."""

    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    ERROR = "error"
    MAX_TURNS = "max_turns"


@dataclass(frozen=True)
class TokenUsage:
    """Provider-independent token accounting for one call or complete run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"TokenUsage {name} must be a non-negative integer")

    @property
    def total_tokens(self) -> int:
        """Return the sum of the runtime's mutually exclusive token buckets."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cached_read_tokens
            + self.cached_write_tokens
            + self.reasoning_tokens
        )

    def add(self, other: TokenUsage) -> TokenUsage:
        """Return an immutable field-wise aggregate."""
        if not isinstance(other, TokenUsage):
            raise TypeError("TokenUsage.add requires TokenUsage")
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_read_tokens=self.cached_read_tokens + other.cached_read_tokens,
            cached_write_tokens=self.cached_write_tokens + other.cached_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )


@dataclass(frozen=True)
class RunResult:
    """Stable terminal envelope returned to CLI, HTTP, SDK, and evaluation hosts."""

    status: RunStatus
    final_text: str
    messages: tuple[dict, ...] | list[dict]
    usage: TokenUsage
    session_id: str
    active_agent: str
    error: str = ""
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, RunStatus):
            raise TypeError("RunResult status must be a RunStatus")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("RunResult session_id must be non-empty")
        if not isinstance(self.active_agent, str) or not self.active_agent.strip():
            raise ValueError("RunResult active_agent must be non-empty")
        object.__setattr__(self, "messages", copy.deepcopy(tuple(self.messages)))
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))
