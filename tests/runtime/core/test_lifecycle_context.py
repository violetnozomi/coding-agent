"""Run lifecycle state ownership tests."""
from __future__ import annotations

from nz_coder.runtime.core.lifecycle_context import LifecycleRunState


def test_lifecycle_run_state_isolated_between_runs() -> None:
    first = LifecycleRunState()
    second = LifecycleRunState()

    first.structured_outputs["coder"] = {"ok": True}
    first.tool_observability["batches"] = 2

    assert second.structured_outputs == {}
    assert second.tool_observability["batches"] == 0


def test_lifecycle_run_state_reset_clears_ephemeral_values() -> None:
    state = LifecycleRunState(
        tool_calls_this_run=9,
        used_save_memory=True,
        restored_state=True,
        replan_count=3,
        last_terminal_summary="old",
    )

    state.reset()

    assert state.tool_calls_this_run == 0
    assert state.used_save_memory is False
    assert state.restored_state is False
    assert state.replan_count == 0
    assert state.last_terminal_summary == ""
