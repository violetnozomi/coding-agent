"""真实 Gateway worker 的关闭归属、client 生命周期与环境重试。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import threading

import pytest

from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.model_gateway import ProductionModelGateway
from nz_coder.runtime.session.model import SessionIdentity
from tests.runtime.model_gateway.test_buffered_gateway import _call, _runtime
from tests.runtime.model_gateway.test_runtime import _AsyncClient, _Client
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request


@pytest.mark.parametrize("async_close", [False, True], ids=["close", "aclose"])
@pytest.mark.parametrize("owns_client", [False, True], ids=["borrowed", "owned"])
def test_live_gateway_worker_keeps_close_retryable(async_close, owns_client):
    """已登记线程未退出时不能结清资源，也不能提前关闭 client。"""
    release = threading.Event()

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            assert release.wait(5), "provider worker was not released"
            return _completion("late completion")

    client = _AsyncClient() if async_close else _Client()
    runtime = _runtime(Provider())
    runtime.client = client
    runtime.owns_client = owns_client
    gateway = ProductionModelGateway(runtime, max_retries=0)
    outcome = gateway.complete_sync(_call(timeout=.02))
    assert outcome.status.value == "aborted"
    workers = [worker for worker, _cancel in runtime._inflight.values()]
    assert len(workers) == 1 and workers[0].is_alive()

    def close():
        if async_close:
            asyncio.run(runtime.aclose())
        else:
            runtime.close()

    try:
        with pytest.raises(RuntimeError):
            close()
        assert runtime._closed is False
        assert runtime.inflight_status()["worker_still_running"] == 1
        assert client.close_calls == 0
        release.set()
        workers[0].join(3)
        assert not workers[0].is_alive()
        close()
        close()
        assert runtime._closed is True
        assert runtime.inflight_status() == {"worker_still_running": 0, "inflight_calls": 0, "tainted": False}
        assert client.close_calls == int(owns_client)
    finally:
        release.set()
        workers[0].join(3)
        close()


def test_native_cancel_keeps_environment_cleanup_pending_until_worker_exit(monkeypatch, tmp_path):
    """取消和会话冻结不等于 provider 线程已退出；原 owner 仍须可重试。"""
    entered, release = threading.Event(), threading.Event()

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            entered.set()
            assert release.wait(5), "provider worker was not released"
            return _completion("LATE_PROVIDER_TEXT")

    runtime = _install_runtime(monkeypatch, Provider())
    request = replace(_request(tmp_path), metadata={"permission_mode": "auto", "persist_session": True})
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    workers = []

    async def scenario():
        nonlocal runtime
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert await asyncio.to_thread(entered.wait, 3)
        runtime = environment.model_runtime
        workers.extend(worker for worker, _cancel in runtime._inflight.values())
        task.cancel()
        try:
            result = await asyncio.wait_for(task, 3)
        except asyncio.CancelledError:
            pass
        else:
            assert result.status.value == "cancelled"
        store = environment.runtime_services.session_runtime.store
        durable = await store.load(SessionIdentity(request.session_id), tmp_path)
        assert durable.status.value == "cancelled"
        before = durable.snapshot()
        assert "LATE_PROVIDER_TEXT" not in repr(before.transcript)
        environment.close()
        assert runtime.inflight_status()["worker_still_running"] == 1
        assert runtime._closed is False
        assert environment.environment_cleanup_complete is False
        assert "provider-runtimes" in environment.environment_cleanup_failures
        release.set()
        await asyncio.to_thread(workers[0].join, 3)
        assert not workers[0].is_alive()
        environment.close()
        assert environment.environment_cleanup_complete is True
        assert environment.environment_cleanup_failures == {}
        assert runtime._closed is True
        after = await store.load(SessionIdentity(request.session_id), tmp_path)
        assert after.snapshot() == before

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        for worker in workers:
            worker.join(3)
        environment.close()
