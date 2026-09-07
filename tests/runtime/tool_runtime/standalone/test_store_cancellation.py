"""Cancellation cannot let a started JSON save outlive its checkpoint owner."""

from __future__ import annotations

import asyncio
import copy
import json
import threading

import pytest

from nz_coder.runtime.session.model import SessionStatus
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.session.tool_progress import ToolSessionBoundary
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.state.sessions import session_dir

from .support import call
from .test_component_contract import components


@pytest.mark.parametrize("external", [False, True], ids=["direct", "external-callback"])
def test_cancelled_checkpoint_drains_real_store_before_interrupted_save(
    tmp_path,
    monkeypatch,
    external,
):
    """Removing either settled-await boundary must expose an orphaned running save."""
    started, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = 0
    completed = []
    save_sync = LegacyJsonSessionStore._save_sync

    def blocked_save(session):
        nonlocal active
        # Freeze the old state before blocking: a later mutation of the live
        # Session must not hide an out-of-order running write.
        snapshot = copy.deepcopy(session)
        with lock:
            active += 1
        try:
            if snapshot.status is SessionStatus.RUNNING:
                started.set()
                assert release.wait(5), "test did not release the started Store worker"
            save_sync(snapshot)
            with lock:
                completed.append(snapshot.status.value)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        LegacyJsonSessionStore, "_save_sync", staticmethod(blocked_save)
    )

    with components(tmp_path, True) as (dependencies, context):
        checkpoint = context.lifecycle.checkpoint

        async def run():
            boundary = ToolSessionBoundary(checkpoint, None, lambda event, data: None)
            interrupted = None
            if external:
                calls = [call()]
                dependencies.prepare(calls)

                async def external_checkpoint(status):
                    # Deliberately bypass ToolSessionBoundary: the public
                    # ToolRuntime callback must itself retain the real save.
                    await checkpoint(dependencies.messages, status)

                running = asyncio.create_task(
                    ProductionToolRuntime().execute_batch_async(
                        context,
                        calls,
                        dependencies.messages,
                        checkpoint=external_checkpoint,
                    )
                )
            else:
                running = asyncio.create_task(
                    boundary.checkpoint(dependencies.messages, "running")
                )

            try:
                assert await asyncio.wait_for(asyncio.to_thread(started.wait, 3), 4)
                running.cancel()
                await asyncio.sleep(0)
                running.cancel()
                if not external:
                    interrupted = asyncio.create_task(
                        boundary.checkpoint(dependencies.messages, "interrupted")
                    )
                for _ in range(5):
                    await asyncio.sleep(0)
                assert not running.done(), (
                    "A cancelled checkpoint must retain its Store worker"
                )
                if interrupted is not None:
                    assert not interrupted.done(), (
                        "The next save must wait for the running save"
                    )
                with lock:
                    assert active == 1
                    assert completed == []
            finally:
                release.set()
                tasks = [running] + ([interrupted] if interrupted is not None else [])
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), 5
                )

            with pytest.raises(asyncio.CancelledError):
                await running
            if interrupted is None:
                await asyncio.wait_for(
                    boundary.checkpoint(dependencies.messages, "interrupted"),
                    5,
                )
            else:
                await interrupted

            durable = await LegacyJsonSessionStore().load(
                context.run.session.identity,
                tmp_path,
            )
            assert durable is not None
            assert durable.status is SessionStatus.INTERRUPTED
            with lock:
                assert active == 0, (
                    "No started Store worker may survive cancellation cleanup"
                )
                assert completed == ["running", "interrupted"]
            assert not (tmp_path / "a.txt").exists()

        asyncio.run(run())
        path = session_dir() / "session-contract.json"
        assert path.is_file()
        assert (
            json.loads(path.read_text(encoding="utf-8"))["run_status"] == "interrupted"
        )
