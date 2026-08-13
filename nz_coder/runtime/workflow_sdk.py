"""Asynchronous host SDK for managed declarative Workflow runs."""
from __future__ import annotations

import asyncio
from concurrent.futures import Future
from contextvars import copy_context
import threading
import uuid

from nz_coder.runtime.agent_manager import scoped_background_agent_manager
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.runtime.workflow_runtime import workflow_run


class WorkflowStartError(RuntimeError):
    """The Workflow failed preflight or approval before publishing started."""


class WorkflowRunHandle:
    """Expose first-started and terminal futures for one managed run identity."""

    def __init__(self, run_id: str, manager) -> None:  # noqa: ANN001
        self.run_id = run_id
        self.manager = manager
        self._started: Future = Future()
        self._result: Future = Future()

    def _publish_started(self, snapshot: dict) -> None:
        if not self._started.done():
            self._started.set_result(dict(snapshot))

    def _publish_result(self, value: str) -> None:
        if not self._started.done():
            self._started.set_exception(WorkflowStartError(str(value)))
        if not self._result.done():
            self._result.set_result(value)

    def _publish_error(self, error: BaseException) -> None:
        if not self._started.done():
            self._started.set_exception(error)
        if not self._result.done():
            self._result.set_exception(error)

    def wait_started(self, timeout: float | None = None) -> dict:
        return self._started.result(timeout=timeout)

    def wait(self, timeout: float | None = None) -> str:
        return self._result.result(timeout=timeout)

    async def first_started(self) -> dict:
        return await asyncio.wrap_future(self._started)

    async def result(self) -> str:
        return await asyncio.wrap_future(self._result)

    def add_done_callback(self, callback) -> None:  # noqa: ANN001
        """Register a terminal callback without exposing the Future owner."""
        self._result.add_done_callback(lambda _future: callback(self))

    def pause(self) -> bool:
        return self.manager.pause_workflow_run(self.run_id)

    def resume(self) -> bool:
        return self.manager.resume_workflow_run(self.run_id)

    def stop(self, reason: str = "stopped by host") -> bool:
        return self.manager.stop_workflow_run(self.run_id, reason)


class WorkflowHostSDK:
    """Start Workflow execution without blocking a terminal or HTTP host."""

    def __init__(self, manager) -> None:  # noqa: ANN001
        self.manager = manager

    def start(self, **workflow_options) -> WorkflowRunHandle:  # noqa: ANN003
        run_id = f"workflow-{uuid.uuid4().hex}"
        handle = WorkflowRunHandle(run_id, self.manager)
        options = dict(workflow_options)
        options["_run_id"] = run_id
        options["_on_started"] = handle._publish_started
        context = copy_context()

        def execute() -> None:
            try:
                with scoped_workdir(self.manager.workspace):
                    with scoped_background_agent_manager(self.manager):
                        value = workflow_run(**options)
                handle._publish_result(value)
            except BaseException as exc:
                handle._publish_error(exc)

        thread = threading.Thread(
            target=lambda: context.run(execute),
            name=f"nz-{run_id}",
            daemon=True,
        )
        thread.start()
        return handle
