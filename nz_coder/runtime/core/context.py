"""Focused immutable capabilities consumed by Context Runtime operations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nz_coder.state.context import PromptBudget


@dataclass(frozen=True)
class ContextExecutionContext:
    """Run-scoped Context capabilities without an AgentLoop dependency."""

    workspace: Path
    budget: PromptBudget
    projected_tokens: Callable[[list[dict]], int]
    compact: Callable[[list[dict]], list[dict]]
    stamp_auto_compaction: Callable[[list[dict]], None]
    trace: Callable[..., None]
    report_pressure: Callable[[dict], None] = lambda _payload: None
    projected_replay_tokens: Callable[[list[dict]], int] | None = None
    cancel_compaction: Callable[[], object] | None = None

    def __post_init__(self) -> None:
        workspace = Path(self.workspace).resolve()
        if not isinstance(self.budget, PromptBudget):
            raise TypeError("ContextExecutionContext budget must be PromptBudget")
        for name in (
            "projected_tokens",
            "compact",
            "stamp_auto_compaction",
            "trace",
            "report_pressure",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"ContextExecutionContext {name} must be callable")
        if (
            self.projected_replay_tokens is not None
            and not callable(self.projected_replay_tokens)
        ):
            raise TypeError(
                "ContextExecutionContext projected_replay_tokens must be callable"
            )
        if self.cancel_compaction is not None and not callable(self.cancel_compaction):
            raise TypeError(
                "ContextExecutionContext cancel_compaction must be callable"
            )
        object.__setattr__(self, "workspace", workspace)
