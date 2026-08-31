"""Offline performance benchmark for task-tool parallel scheduling.

The benchmark exercises :meth:`AgentLoop._dispatch_tool_calls_async` with a
synthetic, deterministic latency executor.  It needs no model credentials and
reports both throughput improvement and scheduler safety properties.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from threading import Lock
import time
from types import MethodType

from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.execution.loop import AgentLoop


@dataclass(frozen=True)
class ParallelBenchmarkResult:
    """Measured serial and parallel task-dispatch performance."""

    task_count: int
    delay_seconds: float
    parallel_limit: int
    serial_seconds: float
    parallel_seconds: float
    speedup: float
    serial_peak_concurrency: int
    parallel_peak_concurrency: int
    order_preserved: bool

    def to_dict(self) -> dict:
        """Return a JSON-serializable representation."""
        return asdict(self)


class _NoToolHooks:
    def has_pre_tool_use_hooks(self) -> bool:
        return False


class _LatencyExecutor:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self._lock = Lock()
        self._active = 0
        self.max_active = 0

    def execute_one(self, tool_call: dict, index: int) -> int:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self.delay_seconds)
            return index
        finally:
            with self._lock:
                self._active -= 1


def _task_call(agent_type: str, index: int) -> dict:
    return {
        "id": f"benchmark-task-{index}",
        "function": {
            "name": "task",
            "arguments": {"agent_type": agent_type, "prompt": f"task {index}"},
        },
    }


async def _run_batch(
    agent_types: list[str],
    delay_seconds: float,
) -> tuple[float, int, list[int]]:
    agent = AgentLoop.__new__(AgentLoop)
    agent.hooks = _NoToolHooks()
    executor = _LatencyExecutor(delay_seconds)
    agent.executor = executor

    def execute_with_hooks(self, tool_call: dict, index: int, messages: list) -> int:
        return self.executor.execute_one(tool_call, index)

    agent._execute_tool_call_with_hooks = MethodType(execute_with_hooks, agent)
    calls = [_task_call(agent_type, index) for index, agent_type in enumerate(agent_types)]
    started = time.perf_counter()
    results = await agent._dispatch_tool_calls_async(calls, False, [])
    elapsed = time.perf_counter() - started
    return elapsed, executor.max_active, [item[2] for item in results]


async def run_parallel_benchmark_async(
    *,
    task_count: int = 6,
    delay_seconds: float = 0.05,
    parallel_limit: int = 3,
) -> ParallelBenchmarkResult:
    """Run the scheduler benchmark without network or model calls."""
    if task_count < 2:
        raise ValueError("task_count must be at least 2")
    if delay_seconds <= 0:
        raise ValueError("delay_seconds must be positive")
    if parallel_limit < 1:
        raise ValueError("parallel_limit must be at least 1")

    with scoped_runtime_overrides(max_parallel_tasks=parallel_limit):
        serial = ["general-purpose"] * task_count
        parallel = ["explore"] * task_count
        serial_seconds, serial_peak, serial_order = await _run_batch(serial, delay_seconds)
        parallel_seconds, parallel_peak, parallel_order = await _run_batch(parallel, delay_seconds)

    expected_order = list(range(task_count))
    return ParallelBenchmarkResult(
        task_count=task_count,
        delay_seconds=delay_seconds,
        parallel_limit=parallel_limit,
        serial_seconds=serial_seconds,
        parallel_seconds=parallel_seconds,
        speedup=serial_seconds / parallel_seconds,
        serial_peak_concurrency=serial_peak,
        parallel_peak_concurrency=parallel_peak,
        order_preserved=(
            serial_order == expected_order and parallel_order == expected_order
        ),
    )


def run_parallel_benchmark(
    *,
    task_count: int = 6,
    delay_seconds: float = 0.05,
    parallel_limit: int = 3,
) -> ParallelBenchmarkResult:
    """Synchronous entry point for scripts and tests."""
    return asyncio.run(
        run_parallel_benchmark_async(
            task_count=task_count,
            delay_seconds=delay_seconds,
            parallel_limit=parallel_limit,
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark NZ-Coder task parallel scheduling without an API.",
    )
    parser.add_argument("--tasks", type=int, default=6)
    parser.add_argument("--delay", type=float, default=0.05)
    parser.add_argument("--parallel-limit", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_parallel_benchmark(
        task_count=args.tasks,
        delay_seconds=args.delay,
        parallel_limit=args.parallel_limit,
    )
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(
            f"serial={result.serial_seconds:.3f}s "
            f"parallel={result.parallel_seconds:.3f}s "
            f"speedup={result.speedup:.2f}x "
            f"peak={result.parallel_peak_concurrency} "
            f"order_preserved={result.order_preserved}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
