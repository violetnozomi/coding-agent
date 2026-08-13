"""Immutable capability profiles for all Agent runtime surfaces."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunMode(str, Enum):
    """Stable execution-surface identifiers persisted in runtime metadata."""

    MAIN = "main"
    READ_CHILD = "read_child"
    WRITE_CHILD = "write_child"
    BACKGROUND = "background"
    WORKFLOW = "workflow"


@dataclass(frozen=True)
class RunProfile:
    """Host-independent capabilities applied to one Runner frame."""

    name: str
    mode: RunMode
    allow_mutation: bool = True
    allow_child_agents: bool = False
    interactive_questions: bool = False
    durable_session: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Run profile name must be non-empty")
        if not isinstance(self.mode, RunMode):
            raise TypeError("Run profile mode must be a RunMode")


MAIN_PROFILE = RunProfile(
    name="main",
    mode=RunMode.MAIN,
    allow_mutation=True,
    allow_child_agents=True,
    interactive_questions=True,
)
READ_CHILD_PROFILE = RunProfile(
    name="read-child",
    mode=RunMode.READ_CHILD,
    allow_mutation=False,
)
WRITE_CHILD_PROFILE = RunProfile(
    name="write-child",
    mode=RunMode.WRITE_CHILD,
    allow_mutation=True,
)
BACKGROUND_PROFILE = RunProfile(
    name="background",
    mode=RunMode.BACKGROUND,
    allow_mutation=False,
)
WORKFLOW_PROFILE = RunProfile(
    name="workflow",
    mode=RunMode.WORKFLOW,
    allow_mutation=True,
    allow_child_agents=True,
)

_PROFILES = {
    profile.mode: profile
    for profile in (
        MAIN_PROFILE,
        READ_CHILD_PROFILE,
        WRITE_CHILD_PROFILE,
        BACKGROUND_PROFILE,
        WORKFLOW_PROFILE,
    )
}


def profile_for_mode(mode: RunMode | str) -> RunProfile:
    """Return the canonical profile for a typed or serialized run mode."""
    try:
        normalized = mode if isinstance(mode, RunMode) else RunMode(str(mode))
    except ValueError as exc:
        raise ValueError(f"Unknown run mode: {mode}") from exc
    return _PROFILES[normalized]
