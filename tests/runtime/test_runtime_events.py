"""Runtime event contracts and projection tests."""
from __future__ import annotations

import asyncio

from nz_coder.runtime.core.events import (
    RuntimeEvent,
    RuntimeEventMiddleware,
    RuntimeEventName,
)
from nz_coder.runtime.core.middleware import MiddlewarePipeline


class Sink:
    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    def publish(self, event: RuntimeEvent) -> None:
        self.events.append(event)


class Context:
    class Request:
        session_id = "session-1"
        parent_run_id = "parent-run"

        class Agent:
            name = "worker"
        agent = Agent()

    request = Request()
    metadata = {"run_id": "run-1", "session_open": "created"}


def test_runtime_event_middleware_emits_ordered_run_model_tool_terminal() -> None:
    sink = Sink()
    pipeline = MiddlewarePipeline((RuntimeEventMiddleware(sink),))
    context = Context()

    async def execute_run():
        await pipeline.run("model", context, lambda: asyncio.sleep(0, result="answer"))
        await pipeline.run("tool_batch", context, lambda: asyncio.sleep(0, result="ok"))
        return {"status": "completed"}

    asyncio.run(pipeline.run("run", context, execute_run))

    assert [event.name for event in sink.events] == [
        RuntimeEventName.SESSION_CREATED,
        RuntimeEventName.RUN_STARTED,
        RuntimeEventName.MODEL_STARTED,
        RuntimeEventName.MODEL_FINISHED,
        RuntimeEventName.TOOL_STARTED,
        RuntimeEventName.TOOL_FINISHED,
        RuntimeEventName.RUN_COMPLETED,
    ]
    assert all(event.run_id == "run-1" for event in sink.events)


def test_event_sink_failure_is_fail_open() -> None:
    class BrokenSink:
        def publish(self, _event):
            raise RuntimeError("renderer unavailable")

    pipeline = MiddlewarePipeline((RuntimeEventMiddleware(BrokenSink()),))
    result = asyncio.run(pipeline.run(
        "run", Context(), lambda: asyncio.sleep(0, result={"status": "completed"}),
    ))
    assert result == {"status": "completed"}
