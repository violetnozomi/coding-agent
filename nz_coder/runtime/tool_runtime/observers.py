"""Declared post-batch enrichment, separate from authoritative execution settlement."""

from __future__ import annotations

from typing import Callable

from nz_coder.runtime.core.tool_contracts import (
    DispatchedTools,
    TextDisplay,
    ToolBatchState,
    ToolTrace,
)
from nz_coder.runtime.process.tool_snapshots import ToolStepSnapshots
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.tools.plan_mode import PlanModeController


class CodingToolObserver:
    """Only focused effect/snapshot services and a named legacy batch-hook bridge."""

    def __init__(
        self,
        *,
        post_write: Callable[[DispatchedTools, list[dict]], None],
        after_batch: Callable[[list[dict], ToolBatchState, TextDisplay | None], None],
        snapshots: ToolStepSnapshots,
        plan_mode: PlanModeController | None,
        terminal_summary: Callable[[str], None],
        trace: ToolTrace,
    ) -> None:
        self._post_write = post_write
        self._after_batch = after_batch
        self.snapshots = snapshots
        self.plan_mode = plan_mode
        self.terminal_summary = terminal_summary
        self.trace = trace

    def post_write(self, dispatched: DispatchedTools, messages: list[dict]) -> None:
        self._post_write(dispatched, messages)

    def after_batch(
        self, messages: list[dict], state: ToolBatchState, on_text: TextDisplay | None
    ) -> None:
        self._after_batch(messages, state, on_text)

    def apply_plan_mode(self) -> None:
        apply_pending_plan_mode(self.plan_mode, self.terminal_summary, self.trace)

    async def capture_snapshot(self, processor: SessionProcessor) -> str | None:
        if not processor.step_snapshot:
            return None
        return await self.snapshots.capture_async("step-finish", processor.message_id)

    def record_patch(
        self, messages: list[dict], processor: SessionProcessor, snapshot: str | None
    ) -> None:
        self.snapshots.record_patch(messages, processor, snapshot)


def apply_pending_plan_mode(
    controller: PlanModeController | None,
    terminal_summary: Callable[[str], None],
    trace: ToolTrace,
) -> None:
    """Apply an approved transition after the batch, never during dispatch."""
    if controller is None:
        return
    transition = controller.apply_pending_mode()
    if transition is None:
        return
    previous, current = transition
    summary = str(controller.pending_terminal_summary or "").strip()
    if summary and controller.pending_exit_terminal:
        terminal_summary(summary)
    trace("plan_mode_changed", previous=previous, current=current, source="plan_exit")


class NoOpToolObserver:
    """Explicitly disabled product enrichment; never an execution/persistence substitute."""

    def post_write(self, dispatched: DispatchedTools, messages: list[dict]) -> None:
        pass

    def after_batch(
        self, messages: list[dict], state: ToolBatchState, on_text: TextDisplay | None
    ) -> None:
        pass

    def apply_plan_mode(self) -> None:
        pass

    async def capture_snapshot(self, processor: SessionProcessor) -> str | None:
        return None

    def record_patch(
        self, messages: list[dict], processor: SessionProcessor, snapshot: str | None
    ) -> None:
        pass
