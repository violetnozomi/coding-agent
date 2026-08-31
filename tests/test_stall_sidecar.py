"""Lifecycle contracts for the InfCodeX-style asynchronous stall sidecar."""
from __future__ import annotations

import threading


def _tool(call_id: str, name: str = "read_file") -> dict:
    return {"id": call_id, "name": name, "input": {"path": "app.py"}}


def test_stall_verdict_is_non_blocking_and_consumed_by_the_next_tool():
    """Catches awaiting L2 on the triggering call or losing its later nudge."""
    from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator

    release = threading.Event()

    def evaluate(user_message: str) -> dict:
        release.wait(timeout=2)
        return {"is_stuck": True, "nudge": "Use grep_search with a narrower symbol."}

    sidecar = StallSidecarOrchestrator(evaluate=evaluate)
    assert not sidecar.record_tool_use(_tool("1"))
    sidecar.record_tool_result("1", "first result")
    assert not sidecar.record_tool_use(_tool("2"))
    sidecar.record_tool_result("2", "second result")

    assert sidecar.record_tool_use(_tool("3"))
    assert sidecar.consume_pending_nudge() is None

    release.set()
    assert sidecar.settle(timeout=2)
    assert sidecar.consume_pending_nudge() == "Use grep_search with a narrower symbol."
    assert sidecar.consume_pending_nudge() is None


def test_stall_prompt_contains_bounded_third_person_tool_transcript():
    """Catches sending only the L1 envelope or allowing unbounded history."""
    from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator

    prompts: list[str] = []
    sidecar = StallSidecarOrchestrator(
        evaluate=lambda prompt: prompts.append(prompt) or {"is_stuck": False},
        transcript_window=4,
    )
    for index in range(1, 4):
        sidecar.record_tool_use(_tool(str(index)))
        sidecar.record_tool_result(str(index), f"result-{index}")

    assert sidecar.settle(timeout=2)
    assert len(prompts) == 1
    assert "[Stall detector signal]" in prompts[0]
    assert "you are reading, not authoring" in prompts[0]
    assert 'tool_use: read_file({"path":"app.py"}) [id=1]' not in prompts[0]
    assert "result-1" in prompts[0]
    assert "result-2" in prompts[0]
    assert "tool_use: read_file" in prompts[0]
    assert "result-3" not in prompts[0]


def test_reset_discards_a_stale_async_verdict():
    """Catches a pre-compaction L2 completion arming the new transcript."""
    from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator

    release = threading.Event()

    def evaluate(_prompt: str) -> dict:
        release.wait(timeout=2)
        return {"is_stuck": True, "nudge": "stale nudge"}

    sidecar = StallSidecarOrchestrator(evaluate=evaluate)
    for index in range(1, 4):
        sidecar.record_tool_use(_tool(str(index)))

    sidecar.reset()
    release.set()
    assert sidecar.settle(timeout=2)
    assert sidecar.consume_pending_nudge() is None
    assert sidecar.transcript_size == 0


def test_cancel_and_settle_signals_cancel_aware_provider_worker():
    """A terminal run must not leave a Provider observer in the next run."""
    from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator

    started = threading.Event()
    cancelled = threading.Event()

    def evaluate(_prompt: str, cancel_event: threading.Event) -> dict:
        started.set()
        assert cancel_event.wait(timeout=2)
        cancelled.set()
        return {"is_stuck": False, "trace": "cancelled"}

    sidecar = StallSidecarOrchestrator(evaluate=evaluate)
    for index in range(1, 4):
        sidecar.record_tool_use(_tool(str(index)))

    assert started.wait(timeout=1)
    assert sidecar.cancel_and_settle(timeout=1)
    assert cancelled.is_set()
    assert sidecar.consume_pending_nudge() is None


def test_sidecar_error_and_malformed_verdict_fail_open():
    """Catches L2 failures suppressing a valid main-agent tool call."""
    from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator

    events: list[dict] = []
    verdicts = iter((RuntimeError("offline"), {"is_stuck": "yes"}))

    def evaluate(_prompt: str) -> dict:
        verdict = next(verdicts)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    sidecar = StallSidecarOrchestrator(evaluate=evaluate, on_event=events.append)
    for offset in (0, 3):
        if offset:
            sidecar.reset()
        for index in range(1 + offset, 4 + offset):
            sidecar.record_tool_use(_tool(str(index)))
        assert sidecar.settle(timeout=2)
        assert sidecar.consume_pending_nudge() is None

    assert [event["trace"] for event in events] == ["provider_error", "invalid_verdict"]


def test_sidecar_timeout_fails_open_and_ignores_late_result():
    """Catches a hung L2 worker or a late verdict suppressing future work."""
    from nz_coder.runtime.verification.stall_sidecar import StallSidecarOrchestrator

    release = threading.Event()
    events: list[dict] = []

    def evaluate(_prompt: str) -> dict:
        release.wait(timeout=2)
        return {"is_stuck": True, "nudge": "too late"}

    sidecar = StallSidecarOrchestrator(
        evaluate=evaluate,
        timeout_seconds=0.02,
        on_event=events.append,
    )
    for index in range(1, 4):
        sidecar.record_tool_use(_tool(str(index)))

    assert sidecar.settle(timeout=1)
    assert events[-1]["trace"] == "timeout"
    assert sidecar.consume_pending_nudge() is None
    release.set()
    assert sidecar.consume_pending_nudge() is None
