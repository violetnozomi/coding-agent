"""Durable Session identity, transcript ownership, and lifecycle state."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Iterable

from nz_coder.runtime.core.result import TokenUsage


class SessionStatus(str, Enum):
    """Persistent lifecycle state for one root or child Session."""

    IDLE = "idle"
    RUNNING = "running"
    COMPACTING = "compacting"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    ERROR = "error"
    ABORTED = "aborted"
    MAX_TURNS = "max_turns"

    @property
    def terminal(self) -> bool:
        """Return whether no further message may be appended in this run."""
        return self not in {
            SessionStatus.IDLE,
            SessionStatus.RUNNING,
            SessionStatus.COMPACTING,
        }


@dataclass(frozen=True)
class SessionIdentity:
    """Stable storage identity and optional parent relationship."""

    session_id: str
    parent_session_id: str | None = None

    def __post_init__(self) -> None:
        _validate_session_id(self.session_id, "session_id")
        if self.parent_session_id is not None:
            _validate_session_id(self.parent_session_id, "parent_session_id")
            if self.parent_session_id == self.session_id:
                raise ValueError("parent_session_id cannot equal session_id")


@dataclass(frozen=True)
class SessionSnapshot:
    """Deeply isolated persistence view of a live Session."""

    identity: SessionIdentity
    workspace: Path
    transcript: tuple[dict, ...]
    status: SessionStatus
    metadata: dict
    usage: TokenUsage
    closed: bool


@dataclass
class Session:
    """Sole mutable owner of the complete durable conversation transcript."""

    identity: SessionIdentity
    workspace: Path
    transcript: list[dict]
    status: SessionStatus = SessionStatus.IDLE
    metadata: dict = field(default_factory=dict)
    usage: TokenUsage = field(default_factory=TokenUsage)
    dirty: bool = False
    closed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SessionIdentity):
            raise TypeError("Session identity must be SessionIdentity")
        self.workspace = Path(self.workspace).resolve()
        self.replace_transcript(self.transcript, allow_terminal=True)
        self.metadata = copy.deepcopy(dict(self.metadata))
        if not isinstance(self.status, SessionStatus):
            raise TypeError("Session status must be SessionStatus")
        if not isinstance(self.usage, TokenUsage):
            raise TypeError("Session usage must be TokenUsage")
        self.dirty = False

    @classmethod
    def create(
        cls,
        session_id: str,
        messages: Iterable[dict],
        *,
        workspace: Path | str,
        parent_session_id: str | None = None,
        metadata: dict | None = None,
    ) -> Session:
        """Create an isolated Session from caller-owned messages."""
        return cls(
            identity=SessionIdentity(session_id, parent_session_id),
            workspace=Path(workspace),
            transcript=copy.deepcopy(list(messages)),
            metadata=copy.deepcopy(metadata or {}),
        )

    @property
    def session_id(self) -> str:
        """Return the stable Session ID."""
        return self.identity.session_id

    @property
    def parent_session_id(self) -> str | None:
        """Return the parent Session ID for child/background runs."""
        return self.identity.parent_session_id

    def append(self, message: dict) -> None:
        """Append one validated message while the Session remains open."""
        self._ensure_open()
        _validate_message(message)
        self.transcript.append(copy.deepcopy(message))
        self.dirty = True

    def replace_transcript(
        self,
        messages: Iterable[dict],
        *,
        allow_terminal: bool = False,
    ) -> None:
        """Atomically replace the transcript with a validated deep copy."""
        if not allow_terminal:
            self._ensure_open()
        replacement = copy.deepcopy(list(messages))
        for message in replacement:
            _validate_message(message)
        self.transcript = replacement
        self.dirty = True

    def checkpoint(self, status: SessionStatus = SessionStatus.RUNNING) -> None:
        """Mark one non-terminal settled persistence boundary."""
        self._ensure_open()
        if not isinstance(status, SessionStatus) or status.terminal:
            raise ValueError("Session checkpoint requires a non-terminal status")
        self.status = status
        self.dirty = True

    def record_status(self, status: SessionStatus) -> None:
        """Persist an observation status without finalizing the active RunContext."""
        self._ensure_open()
        if not isinstance(status, SessionStatus):
            raise TypeError("Session status must be SessionStatus")
        self.status = status
        self.dirty = True

    def finish(self, status: SessionStatus) -> None:
        """Record a terminal Run state without closing the conversation."""
        self._ensure_open()
        if not isinstance(status, SessionStatus) or not status.terminal:
            raise ValueError("Session finish requires a terminal status")
        self.status = status
        self.dirty = True

    def begin_run(self) -> None:
        """Reopen run status for a new turn in this durable Session."""
        self._ensure_open()
        self.status = SessionStatus.RUNNING
        self.dirty = True

    def close(self) -> None:
        """Permanently close the Session against future transcript mutation."""
        if self.closed:
            return
        self.closed = True
        self.dirty = True

    def mark_persisted(self) -> None:
        """Mark the current in-memory snapshot as durably stored."""
        self.dirty = False

    def mark_dirty(self) -> None:
        """Record an in-place mutation made by the SessionProcessor."""
        self._ensure_open()
        self.dirty = True

    def snapshot(self) -> SessionSnapshot:
        """Return a deeply isolated persistence snapshot."""
        return SessionSnapshot(
            identity=self.identity,
            workspace=self.workspace,
            transcript=tuple(copy.deepcopy(self.transcript)),
            status=self.status,
            metadata=copy.deepcopy(self.metadata),
            usage=self.usage,
            closed=self.closed,
        )

    def _ensure_open(self) -> None:
        if self.closed:
            raise RuntimeError("Cannot mutate a closed Session")


def _validate_message(message: dict) -> None:
    if (
        not isinstance(message, dict)
        or not isinstance(message.get("role"), str)
        or "content" not in message
    ):
        raise ValueError("Session message requires role and content")


def _validate_session_id(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"Session {field_name} must be a safe non-empty ID")
