"""真实关闭并发使用子进程硬超时，避免死锁堵住 pytest 的事件循环。"""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import threading

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


class _Client:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.sync_entered = threading.Event()
        self.sync_release = threading.Event()
        self.fail = False

    async def aclose(self):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ValueError("close failed")

    def close(self):
        self.calls += 1
        self.sync_entered.set()
        assert self.sync_release.wait(2)


def _runtime(client, *, owns=True):
    return ResolvedModelRuntime(provider_id="offline", model_id="offline", request_model_id="offline",
        variant=None, provider=object(), client=client, owns_client=owns,
        capabilities=ModelCapabilities(provider="offline", model_id="offline"))


async def _exercise_async(case):
    client = _Client()
    runtime = _runtime(client, owns=case != "borrowed")
    if case == "borrowed":
        await asyncio.gather(runtime.aclose(), runtime.aclose())
        assert runtime._closed and client.calls == 0
        return
    first = asyncio.create_task(runtime.aclose())
    await client.entered.wait()
    if case == "sync_during_async":
        try:
            with pytest.raises(RuntimeError, match="aclose"):
                runtime.close()
        finally:
            client.release.set()
        await first
        assert runtime._closed and client.calls == 1
        return
    if case == "cross_loop":
        errors = []
        started = threading.Event()

        def second_loop():
            async def wait():
                started.set()
                await runtime.aclose()
            try:
                asyncio.run(wait())
            except BaseException as error:
                errors.append(error)

        second = threading.Thread(target=second_loop)
        second.start()
        assert await asyncio.to_thread(started.wait, 1)
        client.release.set()
        await first
        await asyncio.to_thread(second.join, 1)
        assert not second.is_alive() and not errors
        assert runtime._closed and client.calls == 1
        return
    second = asyncio.create_task(runtime.aclose())
    # 让等待方实际进入关闭函数；旧实现在此堵死，外层子进程负责超时。
    await asyncio.sleep(0)
    if case == "waiter_cancel":
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert not first.done() and not runtime._closed
        client.release.set()
        await first
        assert client.calls == 1 and runtime._closed
    elif case == "owner_failure":
        client.fail = True
        client.release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, ValueError) for result in results)
        assert not runtime._closed and client.calls == 1
        client.fail = False
        await runtime.aclose()
        assert runtime._closed and client.calls == 2
    elif case == "owner_cancel":
        first.cancel()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert not runtime._closed and client.calls == 1
        client.release.set()
        await runtime.aclose()
        assert runtime._closed and client.calls == 2
    else:
        client.release.set()
        await asyncio.gather(first, second)
        assert runtime._closed and client.calls == 1


def _exercise(case):
    if case not in {"async_during_sync", "sync_pair"}:
        asyncio.run(_exercise_async(case))
        return
    client = _Client()
    runtime = _runtime(client)
    errors = []

    def close():
        try:
            runtime.close()
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=close)
    first.start()
    assert client.sync_entered.wait(1)
    try:
        if case == "sync_pair":
            second = threading.Thread(target=close)
            second.start()
            client.sync_release.set()
            second.join(1)
            assert not second.is_alive()
        else:
            async def waiter():
                second = asyncio.create_task(runtime.aclose())
                await asyncio.sleep(0)
                client.sync_release.set()
                await second
            asyncio.run(waiter())
    finally:
        client.sync_release.set()
        first.join(1)
    assert not first.is_alive() and not errors
    assert runtime._closed and client.calls == 1


@pytest.mark.parametrize("case", [
    "async_pair", "waiter_cancel", "owner_failure", "owner_cancel", "cross_loop",
    "sync_during_async", "async_during_sync", "sync_pair", "borrowed",
])
def test_concurrent_cleanup_preserves_loop_and_client_ownership(case):
    script = "import runpy; module=runpy.run_path(" + repr(__file__) + "); module['_exercise'](" + repr(case) + ")"
    try:
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[3],
            capture_output=True, text=True, timeout=4)
    except subprocess.TimeoutExpired:
        pytest.fail("runtime close blocked the event loop; isolated child was terminated")
    assert result.returncode == 0, result.stdout + result.stderr
