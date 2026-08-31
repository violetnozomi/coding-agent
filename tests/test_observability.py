"""Tests for tool timing, scheduler, barrier, and recovery observability."""
from __future__ import annotations

import asyncio
import json
import pickle
import time
from pathlib import Path

from nz_coder.runtime.process.workdir import scoped_workdir


class _AllowPermissions:
    def check(self, _name, _tool_input):
        return {"behavior": "allow", "reason": "test"}


class _SleepExecutor:
    def execute_one(self, tool_call, index):
        time.sleep(0.012 if tool_call["function"]["name"] == "read" else 0.003)
        return index


def _mixed_calls():
    return [
        {"id": str(index), "function": {"name": name, "arguments": "{}"}}
        for index, name in enumerate(["read", "read", "serial", "read", "read"])
    ]


def test_tool_executor_records_duration_without_changing_result_contract():
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.tools import register

    def slow_probe():
        time.sleep(0.008)
        return "ok"

    register(
        "_observability_slow_probe",
        "test timing probe",
        {"type": "object", "properties": {}},
        slow_probe,
        execution="read",
    )
    executor = ToolExecutor(_AllowPermissions())

    result = executor.execute_one(
        {
            "id": "timed",
            "function": {"name": "_observability_slow_probe", "arguments": "{}"},
        },
        0,
    )

    assert result.output == "ok"
    assert result.executed is True
    assert result.duration_ms >= 5
    assert result.queue_wait_ms == 0.0


def test_sync_and_async_schedulers_report_parallel_segments_and_barrier_wait():
    from nz_coder.runtime.execution.loop import _execute_scheduled, _execute_scheduled_async

    def predicate(call):
        return call["function"]["name"] == "read"
    sync_segments = []
    sync_results = _execute_scheduled(
        _SleepExecutor(),
        _mixed_calls(),
        predicate,
        on_segment=sync_segments.append,
    )
    async_segments = []
    async_results = asyncio.run(_execute_scheduled_async(
        _SleepExecutor(),
        _mixed_calls(),
        predicate,
        on_segment=async_segments.append,
    ))

    for results, segments in (
        (sync_results, sync_segments),
        (async_results, async_segments),
    ):
        assert [item[0] for item in results] == [0, 1, 2, 3, 4]
        assert [item["kind"] for item in segments] == [
            "parallel_read", "serial_barrier", "parallel_read",
        ]
        assert segments[0]["peak_concurrency"] == 2
        assert segments[1]["barrier_wait_ms"] >= 8
        assert segments[1]["call_count"] == 1


def test_recovery_reports_streak_reset_reason_without_changing_observe_result():
    from nz_coder.runtime.verification.recovery import RecoveryState

    recovery = RecoveryState()
    assert recovery.observe_tool_call("read_file", {"path": "a.py"}, threshold=3) == {
        "count": 1,
        "should_block": False,
    }
    recovery.observe_tool_call("read_file", {"path": "a.py"}, threshold=3)
    changed = recovery.observe_tool_call("read_file", {"path": "b.py"}, threshold=3)
    event = recovery.consume_tool_streak_event()

    assert changed == {"count": 1, "should_block": False}
    assert event == {
        "reason": "arguments_changed",
        "previous_tool": "read_file",
        "previous_count": 2,
        "next_tool": "read_file",
        "reset_count": 1,
    }
    assert recovery.consume_tool_streak_event() is None


def test_agent_trace_and_summary_expose_a013_observability(tmp_path):
    from nz_coder.loop import AgentLoop
    from nz_coder.tools import register
    from nz_coder.state.trace import TraceRecorder, summarize_trace

    def read_probe():
        time.sleep(0.01)
        return "read"

    def serial_probe():
        time.sleep(0.003)
        return "serial"

    register(
        "_observability_read_probe",
        "test read probe",
        {"type": "object", "properties": {}},
        read_probe,
        execution="read",
    )
    register(
        "_observability_serial_probe",
        "test serial probe",
        {"type": "object", "properties": {}},
        serial_probe,
        execution="serial",
    )

    with scoped_workdir(tmp_path):
        tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=object(),
            tracer=tracer,
        )
        calls = [
            {"id": "r1", "function": {"name": "_observability_read_probe", "arguments": "{}"}},
            {"id": "r2", "function": {"name": "_observability_read_probe", "arguments": "{}"}},
            {"id": "s1", "function": {"name": "_observability_serial_probe", "arguments": "{}"}},
        ]
        messages = []
        agent._execute_tools(calls, messages)
        agent._find_repeated_tool_calls([
            {"id": "a", "function": {"name": "read_file", "arguments": {"path": "a.py"}}},
        ])
        agent._find_repeated_tool_calls([
            {"id": "b", "function": {"name": "read_file", "arguments": {"path": "b.py"}}},
        ])

        rows = [json.loads(line) for line in tracer.path.read_text(encoding="utf-8").splitlines()]
        tool_rows = [row for row in rows if row["event"] == "tool_call"]
        batch = next(row for row in rows if row["event"] == "tool_batch_completed")
        resets = [row for row in rows if row["event"] == "doom_loop_streak_reset"]
        runtime = agent._runtime_summary()["tool_observability"]

        assert [row["tool_call_id"] for row in tool_rows] == ["r1", "r2", "s1"]
        assert all(row["duration_ms"] > 0 for row in tool_rows)
        assert batch["peak_concurrency"] == 2
        assert batch["barrier_wait_ms"] > 0
        assert resets[-1]["reason"] == "arguments_changed"
        assert runtime["batches"] == 1
        assert runtime["calls"] == 3
        assert runtime["peak_concurrency"] == 2
        assert runtime["streak_resets"] == len(resets)

        summary = summarize_trace(tracer.path)
        assert "Tool duration" in summary
        assert "peak=2" in summary
        assert "barrier_wait=" in summary
        assert f"streak_resets={len(resets)}" in summary


def test_trace_summary_exposes_model_wait_context_and_child_span(tmp_path):
    from nz_coder.state.trace import TraceRecorder, summarize_trace

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    tracer.log("run_start")
    tracer.log("llm_request", token_estimate=1200)
    tracer.log(
        "llm_response",
        duration_ms=40.0,
        first_token_ms=12.0,
        attempts=1,
        tool_calls=1,
        content_len=0,
    )
    tracer.log("llm_request", token_estimate=4800)
    tracer.log(
        "llm_response",
        duration_ms=60.0,
        first_token_ms=20.0,
        attempts=2,
        tool_calls=0,
        content_len=20,
    )
    tracer.log("subagent_spawn", child_trace_id="child-1")
    tracer.log("subagent_complete", child_trace_id="child-1")
    tracer.log("run_end", status="completed")

    summary = summarize_trace(tracer.path)

    assert "Model calls      : 2" in summary
    assert "Model wait       : total=100.0ms" in summary
    assert "First token      : avg=16.0ms max=20.0ms" in summary
    assert "Input estimate   : first=1200 max=4800 tokens" in summary
    assert "Child agent wait" in summary


def test_trace_summary_zero_recent_limit_emits_no_event_rows(tmp_path):
    from nz_coder.state.trace import TraceRecorder, summarize_trace

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    tracer.log("run_start")
    tracer.log("run_end", status="completed")

    summary = summarize_trace(tracer.path, max_events=0)

    assert summary.endswith("=== Recent events ===")


def test_trace_summary_skips_structurally_corrupt_numeric_rows(tmp_path):
    """Hand-edited or old extension rows must not break trace diagnostics."""
    from nz_coder.state.trace import summarize_trace

    path = tmp_path / "trace.jsonl"
    rows = [
        [],
        {"event": "run_start", "ts": "bad"},
        {"event": "llm_request", "token_estimate": "bad", "ts": "bad"},
        {
            "event": "llm_response",
            "duration_ms": "bad",
            "first_token_ms": "bad",
            "attempts": "bad",
            "ts": "bad",
        },
        {"event": "tool_call", "name": "read_file", "duration_ms": "bad"},
        {
            "event": "tool_batch_completed",
            "peak_concurrency": "bad",
            "wall_ms": "bad",
            "barrier_wait_ms": "bad",
        },
        {"event": "run_end", "ts": "bad"},
    ]
    path.write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )

    summary = summarize_trace(path)

    assert "Total tool calls : 1" in summary
    assert "Model calls      : 1" in summary
    assert "Tool scheduling  : batches=1 peak=0" in summary


def test_latest_trace_tolerates_concurrent_rotation_deletion(tmp_path, monkeypatch):
    from nz_coder.state.trace import latest_trace

    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    stale = trace_dir / "stale.jsonl"
    keep = trace_dir / "keep.jsonl"
    stale.write_text("{}\n", encoding="utf-8")
    keep.write_text("{}\n", encoding="utf-8")
    original_stat = Path.stat

    def racing_stat(path, *args, **kwargs):
        if path == stale:
            raise FileNotFoundError(str(path))
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", racing_stat)

    assert latest_trace(trace_dir=trace_dir) == keep


def test_trace_write_failure_is_best_effort(tmp_path, monkeypatch):
    from nz_coder.state.trace import TraceRecorder

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    original_open = Path.open

    def fail_target(path, *args, **kwargs):
        if path == tracer.path:
            raise OSError("trace disk unavailable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_target)

    tracer.log("tool_batch_completed", batch_id="batch-1")

    assert tracer.dropped_events == 1
    assert tracer.last_write_error == "trace disk unavailable"


def test_trace_sanitizes_recursive_and_unbounded_extension_metadata(tmp_path):
    """A third-party tool payload must not recurse forever or explode trace size."""
    from nz_coder.state.trace import TraceRecorder

    recursive = {"items": list(range(1000))}
    recursive["self"] = recursive
    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)

    tracer.log("extension_metadata", metadata=recursive)

    row = json.loads(tracer.path.read_text(encoding="utf-8"))
    assert row["metadata"]["self"] == "[circular reference]"
    assert len(row["metadata"]["items"]) < 250
    assert row["metadata"]["items"][-1].endswith("more items)")


def test_trace_replaces_nonfinite_numbers_with_strict_json_null(tmp_path):
    """Public JSONL trajectories remain consumable by strict JSON parsers."""
    from nz_coder.state.trace import TraceRecorder

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    tracer.log(
        "provider_metrics",
        duration_ms=float("nan"),
        usage={"cost": float("inf")},
    )

    def reject_constant(value):
        raise ValueError(f"non-standard JSON constant: {value}")

    row = json.loads(
        tracer.path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
    )
    assert row["duration_ms"] is None
    assert row["usage"]["cost"] is None


def test_trace_serialization_failure_never_escapes_agent_control_flow(tmp_path):
    """Observability is best effort even when an extension object has a bad repr."""
    from nz_coder.state.trace import TraceRecorder

    class BrokenString:
        def __str__(self):
            raise RuntimeError("broken repr")

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)

    tracer.log("extension_metadata", value=BrokenString())

    assert tracer.dropped_events == 1
    assert "broken repr" in str(tracer.last_write_error)


def test_trace_recorder_can_cross_spawn_process_boundary(tmp_path):
    from nz_coder.state.trace import TraceRecorder

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)

    restored = pickle.loads(pickle.dumps(tracer))
    restored.log("spawn_child", status="ok")

    row = json.loads(tracer.path.read_text(encoding="utf-8"))
    assert row["event"] == "spawn_child"
    assert row["status"] == "ok"
