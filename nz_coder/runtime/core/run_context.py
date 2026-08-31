"""Mutable state owned by exactly one AgentRunner invocation."""
from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, field

from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.result import RunStatus, TokenUsage
from nz_coder.runtime.session.model import Session


@dataclass
class RunContext:
    """Run-scoped counters and policy state around one Session-owned transcript."""

    request: RunRequest
    session: Session
    active_agent: str
    turn_count: int = 0
    iteration_count: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    retry_count: int = 0
    compaction_attempts: int = 0
    budget_zones_emitted: list[str] = field(default_factory=list)
    cancellation: object | None = None
    metadata: dict = field(default_factory=dict)
    terminal_status: RunStatus | None = None
    finalized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.request, RunRequest):
            raise TypeError("RunContext request must be RunRequest")
        if not isinstance(self.session, Session):
            raise TypeError("RunContext session must be Session")
        if not isinstance(self.active_agent, str) or not self.active_agent.strip():
            raise ValueError("RunContext active_agent must be non-empty")
        self.metadata = copy.deepcopy(dict(self.metadata))
        self._state_lock = threading.RLock()

    @property
    def transcript(self) -> list[dict]:
        """Expose the one live transcript owned by this context's Session."""
        return self.session.transcript

    def begin_turn(self) -> int:
        """Advance and return the one-based model turn counter."""
        with self._state_lock:
            if self.finalized:
                raise RuntimeError("Cannot begin a turn after RunContext finalization")
            self.turn_count += 1
            return self.turn_count

    def add_usage(self, usage: TokenUsage) -> None:
        """Accumulate provider-independent usage for this run."""
        with self._state_lock:
            if self.finalized:
                raise RuntimeError("Cannot add usage after RunContext finalization")
            self.usage = self.usage.add(usage)

    def finish(self, status: RunStatus) -> None:
        """Commit the terminal run state exactly once in memory."""
        with self._state_lock:
            if self.finalized or self.terminal_status is not None:
                raise RuntimeError("RunContext is already terminal")
            if not isinstance(status, RunStatus):
                raise TypeError("RunContext status must be RunStatus")
            self.terminal_status = status
            self.finalized = True
