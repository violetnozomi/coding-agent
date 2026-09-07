"""Offline performance benchmark for task-tool parallel scheduling.

The benchmark exercises the production task-concurrency policy and scheduler
with a synthetic, deterministic latency executor. It needs no model credentials
and reports both throughput improvement and scheduler safety properties.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
import json
from threading import Lock
import time

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
from nz_coder.runtime.core.tool_context import ToolPolicyContext
from nz_coder.runtime.tool_runtime.operations import parse_tool_input
from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
from nz_coder.runtime.tool_runtime.scheduler import _execute_scheduled_async
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.tool_platform.execution import ToolExecutionResult


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


class _LatencyExecutor:
    def __init__(self, delay_seconds: float):
        self.delay_seconds = delay_seconds
        self._lock = Lock()
        self._active = 0
        self.max_active = 0

    def execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult:
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            time.sleep(self.delay_seconds)
            return ToolExecutionResult(
                name=tool_call["function"]["name"],
                tool_input=parse_tool_input(tool_call["function"].get("arguments", {})),
                output=str(index),
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
            )
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
    policy = ProductionToolPolicy()
    context = ToolPolicyContext(
        agent_name="parallel-benchmark", agent_graph=None, tool_allowlist=None,
        admission_handle=None, runtime_state=None, recovery=RecoveryState(),
        permissions=PermissionManager("auto"), stall_orchestrator=None,
        parse_input=parse_tool_input, trace=lambda _event, **_payload: None,
    )
    executor = _LatencyExecutor(delay_seconds)
    calls = [_task_call(agent_type, index) for index, agent_type in enumerate(agent_types)]
    started = time.perf_counter()
    results = await _execute_scheduled_async(
        executor, calls,
        lambda call: policy.tool_call_can_run_concurrently(context, call),
    )
    elapsed = time.perf_counter() - started
    return elapsed, executor.max_active, [int(item[2].output) for item in results]


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
