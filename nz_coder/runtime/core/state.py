"""Legacy mutable Runner state retained for direct compatibility callers."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field

from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.result import RunStatus, TokenUsage


@dataclass
class RunState:
    """Non-production compatibility snapshot superseded by RunContext."""

    transcript: list[dict]
    session_id: str
    active_agent: str
    parent_run_id: str | None = None
    parent_agent_id: str | None = None
    turn_count: int = 0
    iteration_count: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    status: RunStatus | None = None
    final_text: str = ""
    error: str = ""
    compaction_attempts: int = 0
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_request(cls, request: RunRequest) -> RunState:
        """Create an isolated mutable state snapshot from immutable input."""
        return cls(
            transcript=copy.deepcopy(list(request.messages)),
            session_id=request.session_id,
            active_agent=request.agent.name,
            parent_run_id=request.parent_run_id,
            parent_agent_id=request.parent_agent_id,
            metadata=copy.deepcopy(request.metadata),
        )

    @property
    def terminal(self) -> bool:
        """Return whether this frame has reached an irreversible terminal state."""
        return self.status is not None

    def append_message(self, message: dict) -> None:
        """Append one isolated transcript message before terminal finalization."""
        if self.terminal:
            raise RuntimeError("Cannot append a message after the run is terminal")
        if not isinstance(message, dict) or "role" not in message or "content" not in message:
            raise ValueError("RunState message requires role and content")
        self.transcript.append(copy.deepcopy(message))

    def begin_turn(self) -> int:
        """Advance and return the one-based model turn counter."""
        if self.terminal:
            raise RuntimeError("Cannot begin a turn after the run is terminal")
        self.turn_count += 1
        return self.turn_count

    def add_usage(self, usage: TokenUsage) -> None:
        """Accumulate immutable Provider usage into this run."""
        self.usage = self.usage.add(usage)

    def finish(self, status: RunStatus, final_text: str = "", error: str = "") -> None:
        """Commit the terminal state exactly once."""
        if self.terminal:
            raise RuntimeError("RunState is already terminal")
        if not isinstance(status, RunStatus):
            raise TypeError("RunState terminal status must be a RunStatus")
        self.status = status
        self.final_text = str(final_text)
        self.error = str(error)
