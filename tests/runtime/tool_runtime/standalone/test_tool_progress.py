"""Owned progress persistence cannot outlive failure or deadlock tool workers."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from nz_coder.runtime.session.tool_progress import ToolSessionBoundary


@pytest.mark.parametrize("fail", [False, True])
def test_direct_checkpoint_cancel_drains_started_store_before_unlock(fail):
    async def run():
        started, release = threading.Event(), threading.Event()
        writes = []

        def save(status):
            if status == "running":
                started.set()
                assert release.wait(5)
            writes.append(status)
            if fail and status == "running":
                raise OSError("private storage detail")

        async def persist(messages, status):
            await asyncio.to_thread(save, status)

        boundary = ToolSessionBoundary(persist, None, lambda event, data: None)
        running = asyncio.create_task(boundary.checkpoint([], "running"))
        assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        interrupted = asyncio.create_task(boundary.checkpoint([], "interrupted"))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert not running.done(), "Cancellation must retain ownership of the save"
            assert not interrupted.done(), "The serial boundary cannot release early"
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError) as error:
                await running
            await interrupted
        assert writes == ["running", "interrupted"]
        assert boundary._pending == []
        if fail and hasattr(error.value, "add_note"):
            notes = str(getattr(error.value, "__notes__", []))
            assert "OSError" in notes and "private storage detail" not in notes

    asyncio.run(run())


def test_failed_progress_still_drains_later_writes_before_interrupted_state():
    async def run():
        release = asyncio.Event()
        second_started = asyncio.Event()
        writes = []
        sequence = 0

        async def persist(messages, status):
            nonlocal sequence
            sequence += 1
            if sequence == 1:
                raise OSError("fake storage error")
            if sequence == 2:
                second_started.set()
                await release.wait()
            writes.append(status)

        boundary = ToolSessionBoundary(persist, None, lambda event, data: None)
        boundary.progress_checkpoint([], "running")
        boundary.progress_checkpoint([], "running")
        checkpoint = asyncio.create_task(boundary.checkpoint([], "interrupted"))
        await asyncio.wait_for(second_started.wait(), 2)
        try:
            assert not checkpoint.done(), (
                "A later save must retain a live owner after the first fails"
            )
        finally:
            release.set()
            with pytest.raises(OSError):
                await checkpoint
        await boundary.checkpoint([], "interrupted")
        assert writes == ["running", "interrupted"]

    asyncio.run(run())


def test_unfinished_step_drains_progress_even_with_external_checkpoint():
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
    from .support import Dependencies, call

    async def run():
        dependencies = Dependencies()
        writes = []

        async def persist(messages, status):
            await asyncio.to_thread(writes.append, status)

        boundary = ToolSessionBoundary(persist, None, lambda event, data: None)

        def execute(selected, index, messages):
            boundary.progress_checkpoint(messages, "progress")
            return dependencies.executor.execute_one(selected, index)

        async def external_checkpoint(status):
            writes.append("external-" + status)

        context = dependencies.context(
            checkpoint=boundary.checkpoint,
            drain_progress=boundary.drain_progress,
            execute_one=execute,
        )
        dependencies.prepare([call("read_file", path="a.txt")])
        await ProductionToolRuntime().execute_batch_async(
            context,
            [call("read_file", path="a.txt")],
            dependencies.messages,
            finish_step=False,
            checkpoint=external_checkpoint,
        )
        assert writes == ["external-running", "progress"]
        assert boundary._pending == []

    asyncio.run(run())


def test_progress_save_uses_same_single_worker_pool_without_deadlock():
    async def run():
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )
        writes = []

        async def persist(messages, status):
            await asyncio.to_thread(writes.append, status)

        boundary = ToolSessionBoundary(persist, None, lambda event, data: None)
        await asyncio.wait_for(
            asyncio.to_thread(boundary.progress_checkpoint, [], "running"), 2
        )
        await asyncio.wait_for(boundary.checkpoint([], "interrupted"), 2)
        assert writes == ["running", "interrupted"]

    asyncio.run(run())


def test_repeated_cancellation_does_not_orphan_progress_save():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        writes = []

        async def persist(messages, status):
            started.set()
            await release.wait()
            writes.append(status)

        boundary = ToolSessionBoundary(persist, None, lambda event, data: None)
        boundary.progress_checkpoint([], "running")
        drain = asyncio.create_task(boundary.drain_progress())
        await asyncio.wait_for(started.wait(), 2)
        drain.cancel()
        await asyncio.sleep(0)
        drain.cancel()
        await asyncio.sleep(0)
        assert not drain.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await drain
        await boundary.checkpoint([], "interrupted")
        assert writes == ["running", "interrupted"]

    asyncio.run(run())
