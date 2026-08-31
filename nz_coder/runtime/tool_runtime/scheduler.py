"""Ordered tool-call scheduling with cooperative cancellation and write barriers."""
from __future__ import annotations

import asyncio
import concurrent.futures as _futures
from contextvars import copy_context
import threading
import time

from nz_coder.foundation.async_utils import to_thread_settled as _to_thread_settled
from nz_coder.runtime.core.execution_context import max_parallel_tasks
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.tools import current_tool_cancel_event, scoped_tool_cancellation

def _notify_schedule_observer(observer, payload: dict) -> None:
    """Keep best-effort observability callbacks outside the execution contract."""
    if observer is None:
        return
    try:
        observer(dict(payload))
    except Exception:
        return


def _execute_with_tool_cancellation(
    cancel_event: threading.Event,
    function,
    *args,
):
    """Run one tool worker with an InfCode-style cooperative abort context."""
    with scoped_tool_cancellation(cancel_event):
        return function(*args)


class _ConcurrentProbe:
    """Measure actual worker concurrency and queue delay for one read segment."""

    def __init__(self) -> None:
        self.segment_started = time.perf_counter()
        self._lock = threading.Lock()
        self._active = 0
        self.peak = 0
        self.queue_waits: list[float] = []

    def execute(self, executor, tool_call: dict, index: int):
        queue_wait_ms = (time.perf_counter() - self.segment_started) * 1000
        with self._lock:
            self._active += 1
            self.peak = max(self.peak, self._active)
            self.queue_waits.append(queue_wait_ms)
        try:
            result = executor.execute_one(tool_call, index)
            if isinstance(result, ToolExecutionResult):
                result.queue_wait_ms = round(queue_wait_ms, 3)
            return result
        finally:
            with self._lock:
                self._active -= 1

    def metrics(self) -> dict:
        waits = self.queue_waits or [0.0]
        return {
            "duration_ms": round((time.perf_counter() - self.segment_started) * 1000, 3),
            "peak_concurrency": self.peak,
            "max_queue_wait_ms": round(max(waits), 3),
            "avg_queue_wait_ms": round(sum(waits) / len(waits), 3),
        }


def _execute_scheduled(
    executor,
    tool_calls_raw: list,
    can_run_concurrently,
    on_segment=None,
) -> list:
    """Run consecutive read segments concurrently and side-effect calls as barriers."""
    results = []
    index = 0
    segment_index = 0
    preceding_parallel_ms = 0.0
    while index < len(tool_calls_raw):
        if not can_run_concurrently(tool_calls_raw[index]):
            tc = tool_calls_raw[index]
            started = time.perf_counter()
            result = executor.execute_one(tc, index)
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append(
                (
                    index,
                    tc,
                    result,
                )
            )
            _notify_schedule_observer(on_segment, {
                "segment_index": segment_index,
                "kind": "serial_barrier",
                "call_count": 1,
                "names": [tc["function"]["name"]],
                "duration_ms": duration_ms,
                "peak_concurrency": 1,
                "barrier_wait_ms": round(preceding_parallel_ms, 3),
                "max_queue_wait_ms": float(getattr(result, "queue_wait_ms", 0.0) or 0.0),
            })
            segment_index += 1
            preceding_parallel_ms = 0.0
            index += 1
            continue
        end = index + 1
        while end < len(tool_calls_raw) and can_run_concurrently(tool_calls_raw[end]):
            end += 1
        segment = list(enumerate(tool_calls_raw[index:end]))
        calls = [tc for _local_index, tc in segment]
        indexes = list(range(index, end))
        adapter = _IndexedExecutor(executor, indexes)
        concurrent_metrics: dict = {}
        for local_index, tc, result in _execute_concurrent(
            adapter,
            calls,
            on_metrics=concurrent_metrics.update,
        ):
            original_index = indexes[local_index]
            results.append((original_index, tc, result))
        preceding_parallel_ms = float(concurrent_metrics.get("duration_ms", 0.0) or 0.0)
        _notify_schedule_observer(on_segment, {
            "segment_index": segment_index,
            "kind": "parallel_read",
            "call_count": len(calls),
            "names": [tc["function"]["name"] for tc in calls],
            "barrier_wait_ms": 0.0,
            **concurrent_metrics,
        })
        segment_index += 1
        index = end
    return results


class _IndexedExecutor:
    """Map segment-local worker indexes back to the original tool-call indexes."""

    def __init__(self, executor, indexes: list[int]) -> None:
        self._executor = executor
        self._indexes = indexes

    def execute_one(self, tool_call: dict, index: int):
        return self._executor.execute_one(tool_call, self._indexes[index])


def _execute_concurrent(executor, tool_calls_raw: list, on_metrics=None) -> list:
    """Execute read-only tool calls concurrently using a thread pool.

    Returns list of (index, tc, ToolExecutionResult) in the original order,
    preserving message insertion order for API correctness.

    Only called when all tools in the batch are non-write tools (has_write=False).
    Write tools must run sequentially to avoid shared-state races (transaction
    manager, change tracker, verification manager).
    """
    probe = _ConcurrentProbe()
    if len(tool_calls_raw) <= 1:
        results = [
            (i, tc, probe.execute(executor, tc, i))
            for i, tc in enumerate(tool_calls_raw)
        ]
        _notify_schedule_observer(on_metrics, probe.metrics())
        return results

    parallel_limit = max_parallel_tasks()
    max_workers = min(len(tool_calls_raw), parallel_limit)
    with _futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(copy_context().run, probe.execute, executor, tc, i): (i, tc)
            for i, tc in enumerate(tool_calls_raw)
        }
        # Collect results preserving original order
        results_by_index = {}
        for fut in _futures.as_completed(futures):
            i, tc = futures[fut]
            try:
                result_r = fut.result()
            except Exception as exc:
                # Safety net: if a tool raises unexpectedly, surface as error
                from nz_coder.tool_platform.execution import ToolExecutionResult
                fn_name = tool_calls_raw[i]["function"]["name"]
                result_r = ToolExecutionResult(
                    name=fn_name,
                    tool_input={},
                    output=f"Error: tool execution raised: {exc}",
                    executed=True,
                    dispatch_failed=True,
                    command_failed=False,
                    is_write=False,
                )
            results_by_index[i] = (i, tc, result_r)

    results = [results_by_index[i] for i in range(len(tool_calls_raw))]
    _notify_schedule_observer(on_metrics, probe.metrics())
    return results


async def _execute_scheduled_async(
    executor,
    tool_calls_raw: list,
    can_run_concurrently,
    on_segment=None,
) -> list:
    """Async scheduler with the same ordered side-effect barriers as the sync path."""
    results = []
    index = 0
    segment_index = 0
    preceding_parallel_ms = 0.0
    while index < len(tool_calls_raw):
        if not can_run_concurrently(tool_calls_raw[index]):
            tc = tool_calls_raw[index]
            started = time.perf_counter()
            cancel_event = current_tool_cancel_event() or threading.Event()
            result = await _to_thread_settled(
                _execute_with_tool_cancellation,
                cancel_event,
                executor.execute_one,
                tc,
                index,
                cancel_callback=cancel_event.set,
            )
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            results.append(
                (
                    index,
                    tc,
                    result,
                )
            )
            _notify_schedule_observer(on_segment, {
                "segment_index": segment_index,
                "kind": "serial_barrier",
                "call_count": 1,
                "names": [tc["function"]["name"]],
                "duration_ms": duration_ms,
                "peak_concurrency": 1,
                "barrier_wait_ms": round(preceding_parallel_ms, 3),
                "max_queue_wait_ms": float(getattr(result, "queue_wait_ms", 0.0) or 0.0),
            })
            segment_index += 1
            preceding_parallel_ms = 0.0
            index += 1
            continue
        end = index + 1
        while end < len(tool_calls_raw) and can_run_concurrently(tool_calls_raw[end]):
            end += 1
        segment = list(enumerate(tool_calls_raw[index:end]))
        calls = [tc for _local_index, tc in segment]
        indexes = list(range(index, end))
        adapter = _IndexedExecutor(executor, indexes)
        concurrent_metrics: dict = {}
        for local_index, tc, result in await _execute_concurrent_async(
            adapter,
            calls,
            on_metrics=concurrent_metrics.update,
        ):
            original_index = indexes[local_index]
            results.append((original_index, tc, result))
        preceding_parallel_ms = float(concurrent_metrics.get("duration_ms", 0.0) or 0.0)
        _notify_schedule_observer(on_segment, {
            "segment_index": segment_index,
            "kind": "parallel_read",
            "call_count": len(calls),
            "names": [tc["function"]["name"] for tc in calls],
            "barrier_wait_ms": 0.0,
            **concurrent_metrics,
        })
        segment_index += 1
        index = end
    return results


async def _execute_concurrent_async(executor, tool_calls_raw: list, on_metrics=None) -> list:
    """Async read-only tool batch execution using asyncio.gather."""
    probe = _ConcurrentProbe()
    if len(tool_calls_raw) <= 1:
        results = []
        for i, tc in enumerate(tool_calls_raw):
            cancel_event = current_tool_cancel_event() or threading.Event()
            result = await _to_thread_settled(
                _execute_with_tool_cancellation,
                cancel_event,
                probe.execute,
                executor,
                tc,
                i,
                cancel_callback=cancel_event.set,
            )
            results.append((i, tc, result))
        _notify_schedule_observer(on_metrics, probe.metrics())
        return results

    async def run_one(i: int, tc: dict):
        cancel_event = current_tool_cancel_event() or threading.Event()
        try:
            result_r = await _to_thread_settled(
                _execute_with_tool_cancellation,
                cancel_event,
                probe.execute,
                executor,
                tc,
                i,
                cancel_callback=cancel_event.set,
            )
        except Exception as exc:
            from nz_coder.tool_platform.execution import ToolExecutionResult
            fn_name = tool_calls_raw[i]["function"]["name"]
            result_r = ToolExecutionResult(
                name=fn_name,
                tool_input={},
                output=f"Error: tool execution raised: {exc}",
                executed=True,
                dispatch_failed=True,
                command_failed=False,
                is_write=False,
            )
        return i, tc, result_r

    parallel_limit = max_parallel_tasks()
    results = []
    indexed_calls = list(enumerate(tool_calls_raw))
    for start in range(0, len(indexed_calls), parallel_limit):
        batch = indexed_calls[start:start + parallel_limit]
        results.extend(await asyncio.gather(*(run_one(i, tc) for i, tc in batch)))
    _notify_schedule_observer(on_metrics, probe.metrics())
    return results
