"""Data models for child-agent worktree state."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Worktree:
    """Persistent metadata for a child-agent workspace."""

    id: str
    path: str
    branch: str
    based_on: str
    head_commit: str
    mode: str = "git"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"))
