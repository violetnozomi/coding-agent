"""Behavioral tests for the host-neutral Runtime middleware pipeline."""
from __future__ import annotations

import asyncio

import pytest

from nz_coder.runtime.core.middleware import MiddlewarePipeline


class Recorder:
    def __init__(self, name: str, events: list[str]) -> None:
        self.name = name
        self.events = events

    async def before_run(self, context) -> None:
        self.events.append(f"before:{self.name}")

    async def after_run(self, context, result) -> None:
        self.events.append(f"after:{self.name}:{result}")

    async def on_run_error(self, context, error) -> None:
        self.events.append(f"error:{self.name}:{error}")


def test_pipeline_runs_before_forward_and_after_reverse() -> None:
    events: list[str] = []
    pipeline = MiddlewarePipeline((Recorder("one", events), Recorder("two", events)))

    async def execute():
        events.append("execute")
        return "ok"

    result = asyncio.run(pipeline.run("run", None, execute))

    assert result == "ok"
    assert events == [
        "before:one", "before:two", "execute", "after:two:ok", "after:one:ok",
    ]


def test_pipeline_observes_original_error_in_reverse_order() -> None:
    events: list[str] = []
    failure = RuntimeError("original")
    pipeline = MiddlewarePipeline((Recorder("one", events), Recorder("two", events)))

    async def execute():
        raise failure

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(pipeline.run("run", None, execute))

    assert caught.value is failure
    assert events == [
        "before:one", "before:two", "error:two:original", "error:one:original",
    ]


def test_pipeline_does_not_swallow_middleware_failures() -> None:
    class Broken:
        async def before_model(self, context) -> None:
            raise ValueError("middleware failed")

    pipeline = MiddlewarePipeline((Broken(),))

    with pytest.raises(ValueError, match="middleware failed"):
        asyncio.run(pipeline.run("model", None, lambda: asyncio.sleep(0)))
