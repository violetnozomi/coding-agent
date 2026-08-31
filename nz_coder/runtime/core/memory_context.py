"""Focused state and dependencies for production memory operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class MemoryRecallState:
    """Mutable recall cache owned by one Agent execution."""

    last_query: str = ""
    last_block: str = ""


@dataclass(frozen=True)
class MemoryExecutionContext:
    """Narrow memory dependencies without a broad Agent host."""

    manager: object
    session_id: str
    client: object | None
    model_id: str
    tracer: object | None
    lineage: object | None
    recall: MemoryRecallState
    commit_recall: Callable[[MemoryRecallState], None] = lambda _state: None
    provider: object | None = None
    capabilities: object | None = None
    observer: Callable[[str, dict], None] | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("MemoryExecutionContext session_id must be non-empty")
        if not isinstance(self.recall, MemoryRecallState):
            raise TypeError("MemoryExecutionContext recall must be MemoryRecallState")
        if not callable(self.commit_recall):
            raise TypeError("MemoryExecutionContext commit_recall must be callable")
        if self.observer is not None and not callable(self.observer):
            raise TypeError("MemoryExecutionContext observer must be callable")
