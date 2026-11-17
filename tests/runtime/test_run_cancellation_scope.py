"""外部取消必须到达模型接纳、实际工具与本轮终态，局部取消不反写父来源。"""

import asyncio
import copy
import threading
import time
from dataclasses import replace

import pytest

from nz_coder.runtime.execution import native_sdk
from settlement_scenarios import setup, payload, _reply, _call


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("kind", ["task", "event", "event_observed"])
def test_external_cancel_precedes_unfrozen_response(
    monkeypatch, tmp_path, stream, kind
):
    entered, release = threading.Event(), threading.Event()

    class ObservedCancel(threading.Event):
        def __init__(self):
            super().__init__()
            self.observed = threading.Event()

        def is_set(self):
            value = super().is_set()
            if value:
                self.observed.set()
            return value

    cancel = ObservedCancel()
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            entered.set()
            assert release.wait(5)
            return _reply(kwargs, text="late answer")

    request, options = setup(monkeypatch, tmp_path, Provider())
    request = replace(request, stream=stream)
    options = replace(options, cancellation=cancel)

    async def run():
        task = asyncio.create_task(
            native_sdk.NativeSDKRunner().run_result(request, options)
        )
        assert await asyncio.to_thread(entered.wait, 3)
        if kind == "task":
            task.cancel()
        else:
            cancel.set()
            if kind == "event_observed":
                assert await asyncio.to_thread(cancel.observed.wait, 2)
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status.value == "cancelled", (result.status, result.final_text)
        assert "late answer" not in result.final_text
        assert payload(tmp_path, request)["run_status"] == "cancelled"
        assert len(requests) == 1
        if not stream:
            assert result.usage.total_tokens == 3

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_external_cancel_stops_running_shell(monkeypatch, tmp_path):
    (tmp_path / "wait.py").write_text(
        "from pathlib import Path\nimport time\nPath('started').touch()\n"
        "while not Path('release').exists(): time.sleep(0.02)\nPath('effect').touch()\n"
    )
    cancel = threading.Event()
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _reply(
                    kwargs,
                    calls=[
                        _call(1, "bash", {"command": "python wait.py", "timeout": 5})
                    ],
                )
            return _reply(kwargs, text="Settled.")

    request, options = setup(monkeypatch, tmp_path, Provider())
    request = replace(
        request,
        messages=[
            {"role": "user", "content": "Run wait.py with python; stop if cancelled."}
        ],
        tool_names=("bash",),
    )
    options = replace(options, cancellation=cancel)

    async def run():
        task = asyncio.create_task(
            native_sdk.NativeSDKRunner().run_result(request, options)
        )
        try:
            deadline = time.monotonic() + 3
            while (
                not (tmp_path / "started").exists()
                and not task.done()
                and time.monotonic() < deadline
            ):
                await asyncio.sleep(0.01)
            if task.done() and task.exception():
                raise task.exception()
            assert (tmp_path / "started").exists(), (
                task.exception()
                if task.done() and task.exception()
                else task.result()
                if task.done()
                else "not started"
            )
            cancel.set()
            await asyncio.sleep(0.15)
            (tmp_path / "release").touch()
            result = await asyncio.wait_for(task, 4)
            assert not (tmp_path / "effect").exists(), (
                "command wrote after cancellation"
            )
            assert result.status.value == "cancelled"
            assert len(calls) == 1
        finally:
            (tmp_path / "release").touch()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_tool_local_scope_does_not_cancel_parent():
    from nz_coder.tools import ToolCancellationScope

    parent = threading.Event()
    scope = ToolCancellationScope(parent)
    scope.set()
    assert scope.is_set() and not parent.is_set()
    another = ToolCancellationScope(parent)
    parent.set()
    assert another.wait(0.1)


def test_frozen_execution_ignores_late_cancel(tmp_path):
    from test_task_intent_state import setup_run
    from nz_coder.runtime.core.result import RunStatus

    _, run = setup_run(tmp_path)
    run.freeze_execution(RunStatus.COMPLETED)
    run.request_cancel()
    run.check_cancelled()
    assert run.terminal_status is RunStatus.COMPLETED


def test_parent_cancel_reaches_parallel_workers(tmp_path):
    from nz_coder.runtime.tool_runtime.scheduler import _execute_scheduled_async
    from nz_coder.runtime.core.run_context import scoped_run_context
    from nz_coder.tool_platform.execution import ToolExecutionResult
    from nz_coder.tools import current_tool_cancel_event
    from test_task_intent_state import setup_run

    _, run = setup_run(tmp_path)
    parent = threading.Event()
    run.cancellation = parent
    entered = [threading.Event(), threading.Event()]
    seen = []

    class Executor:
        def execute_one(self, call, index):
            entered[index].set()
            seen.append(current_tool_cancel_event().wait(3))
            return ToolExecutionResult(
                name="read_file", tool_input={}, output="cancelled", executed=True
            )

    async def exercise():
        with scoped_run_context(run):
            task = asyncio.create_task(
                _execute_scheduled_async(
                    Executor(),
                    [
                        {"function": {"name": "read_file"}},
                        {"function": {"name": "read_file"}},
                    ],
                    lambda _: True,
                )
            )
            for event in entered:
                assert await asyncio.to_thread(event.wait, 2)
            parent.set()
            await asyncio.wait_for(task, 2)

    asyncio.run(exercise())
    assert seen == [True, True]


def test_parent_cancel_releases_only_its_permission_wait():
    from nz_coder.http_service.interactions import InteractionBroker
    from nz_coder.protocol.session_events import SessionEventBus
    from nz_coder.tools import ToolCancellationScope, scoped_tool_cancellation
    from nz_coder.tool_platform.permissioning.interaction import (
        PermissionInteractionError,
    )

    bus = SessionEventBus(session_id="permission-cancel")
    broker = InteractionBroker(
        session_id="permission-cancel", event_bus=bus, timeout_seconds=3
    )
    broker.begin_run()
    parent = threading.Event()

    def ask():
        with scoped_tool_cancellation(ToolCancellationScope(parent)):
            try:
                broker.ask_permission("bash", {"command": "echo checked"})
            except PermissionInteractionError as error:
                return error.reason

    async def exercise():
        task = asyncio.create_task(asyncio.to_thread(ask))
        deadline = time.monotonic() + 2
        while not broker.list() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert broker.list()
        parent.set()
        assert await asyncio.wait_for(task, 1) == "cancelled"
        assert not broker.list()

    try:
        asyncio.run(exercise())
    finally:
        broker.close()
        bus.close()


def test_cancel_before_freeze_wins_but_cancelled_result_can_freeze(tmp_path):
    from test_task_intent_state import setup_run
    from nz_coder.runtime.core.result import RunStatus

    _, run = setup_run(tmp_path)
    run.request_cancel()
    with pytest.raises(asyncio.CancelledError):
        run.freeze_execution(RunStatus.COMPLETED)
    assert run.terminal_status is None
    run.freeze_execution(RunStatus.CANCELLED)
    assert run.terminal_status is RunStatus.CANCELLED
