"""Tests for the offline task parallelism benchmark."""
from __future__ import annotations

import asyncio

import pytest

from nz_coder.foundation import config
from nz_coder.evaluation.parallel_benchmark import (
    run_parallel_benchmark,
    run_parallel_benchmark_async,
)


def test_parallel_benchmark_measures_speedup_and_scheduler_guards():
    original_limit = config.MAX_PARALLEL_TASKS
    result = run_parallel_benchmark(
        task_count=6,
        delay_seconds=0.03,
        parallel_limit=3,
    )

    assert result.order_preserved
    assert result.serial_peak_concurrency == 1
    assert result.parallel_peak_concurrency == 3
    assert result.parallel_seconds < result.serial_seconds
    assert result.speedup >= 1.6
    assert config.MAX_PARALLEL_TASKS == original_limit


def test_parallel_benchmark_restores_config_and_validates_arguments():
    with pytest.raises(ValueError, match="task_count"):
        asyncio.run(run_parallel_benchmark_async(task_count=1))
    with pytest.raises(ValueError, match="delay_seconds"):
        asyncio.run(run_parallel_benchmark_async(delay_seconds=0))
    with pytest.raises(ValueError, match="parallel_limit"):
        asyncio.run(run_parallel_benchmark_async(parallel_limit=0))
