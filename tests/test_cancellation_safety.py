"""Cancellation safety across the asyncio-to-thread runtime boundary."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nz_coder.runtime.execution.loop import (
    AgentLoop,
    _execute_concurrent_async,
    _to_thread_settled,
)
from nz_coder.state.transaction import TransactionManager
from nz_coder.runtime.execution.tool_executor import ToolExecutionResult


class _Tracer:
    def log(self, *_args, **_kwargs) -> None:
        return None


def test_thread_bridge_settles_before_propagating_cancellation():
    started = threading.Event()
    release = threading.Event()

    def work():
        started.set()
        release.wait(timeout=2)
        return "settled"

    async def exercise():
        task = asyncio.create_task(_to_thread_settled(work))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())


def test_cancelled_write_thread_is_drained_then_rolled_back(monkeypatch, tmp_path):
    from nz_coder.state import transaction as transaction_module

    monkeypatch.setattr(transaction_module, "current_workdir", lambda: tmp_path)
    target = tmp_path / "late.txt"
    target.write_text("before", encoding="utf-8")
    started = threading.Event()
    release = threading.Event()

    class Harness:
        def __init__(self):
            self.txn = TransactionManager()
            self.tracer = _Tracer()

        def _tool_batch_has_write(self, _calls):
            return True

        async def _dispatch_tool_calls_async(self, _calls, _has_write, _messages):
            def late_write():
                started.set()
                release.wait(timeout=2)
                self.txn.track("late.txt")
                target.write_text("after-cancel", encoding="utf-8")

            await _to_thread_settled(late_write)
            return []

        def _finish_tool_transaction(self, has_write, all_succeeded, messages):
            return AgentLoop._finish_tool_transaction(
                self,
                has_write,
                all_succeeded,
                messages,
            )

    harness = Harness()

    async def exercise():
        task = asyncio.create_task(AgentLoop._execute_tools_async(harness, [{}], []))
        while not started.is_set():
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0.02)
        assert task.done() is False
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert target.read_text(encoding="utf-8") == "before"
    assert harness.txn.active is False


def test_single_read_tool_does_not_block_event_loop():
    release = threading.Event()
    ticked_at = []
    released_at = []

    class Executor:
        def execute_one(self, _tool_call, _index):
            release.wait(timeout=2)
            released_at.append(time.monotonic())
            return ToolExecutionResult(
                name="read_file",
                tool_input={},
                output="ok",
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
            )

    async def heartbeat():
        await asyncio.sleep(0.01)
        ticked_at.append(time.monotonic())

    async def exercise():
        timer = threading.Timer(0.08, release.set)
        timer.start()
        try:
            heartbeat_task = asyncio.create_task(heartbeat())
            await _execute_concurrent_async(
                Executor(),
                [{"function": {"name": "read_file"}}],
            )
            await heartbeat_task
        finally:
            timer.cancel()

    asyncio.run(exercise())

    assert ticked_at[0] < released_at[0]


def test_post_dispatch_failure_rolls_back_active_transaction(monkeypatch, tmp_path):
    from nz_coder.state import transaction as transaction_module

    monkeypatch.setattr(transaction_module, "current_workdir", lambda: tmp_path)
    target = tmp_path / "callback.txt"
    target.write_text("before", encoding="utf-8")

    class Harness:
        def __init__(self):
            self.txn = TransactionManager()
            self.tracer = _Tracer()

        def _tool_batch_has_write(self, _calls):
            return True

        async def _dispatch_tool_calls_async(self, _calls, _has_write, _messages):
            self.txn.track("callback.txt")
            target.write_text("after", encoding="utf-8")
            return []

        def _consume_dispatched_tools(self, _dispatched, _messages, *, on_tool=None):
            assert on_tool is not None
            on_tool("edit_file", "done")
            raise RuntimeError("renderer failed")

        def _finish_tool_transaction(self, has_write, all_succeeded, messages):
            return AgentLoop._finish_tool_transaction(
                self,
                has_write,
                all_succeeded,
                messages,
            )

    harness = Harness()

    async def exercise():
        with pytest.raises(RuntimeError, match="renderer failed"):
            await AgentLoop._execute_tools_async(
                harness,
                [{}],
                [],
                on_tool=lambda _name, _output: (_ for _ in ()).throw(
                    RuntimeError("renderer failed")
                ),
            )

    asyncio.run(exercise())

    assert target.read_text(encoding="utf-8") == "before"
    assert harness.txn.active is False
