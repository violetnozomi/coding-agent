"""Focused service owners consumed by the shared AgentRunner."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunnerExecutionContext:
    """Run-scoped service graph without a flat callback capability bag."""

    session_id: str
    runtime_state: object
    execution: object
    lifecycle: object
    policy: object
    planning: object
    control: object
    hooks: object
    messages: object
    snapshots: object

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id:
            raise ValueError("RunnerExecutionContext session_id must be non-empty")
        for name in (
            "execution", "lifecycle", "policy", "planning", "control",
            "hooks", "messages", "snapshots",
        ):
            if getattr(self, name) is None:
                raise TypeError(f"RunnerExecutionContext {name} must be provided")
