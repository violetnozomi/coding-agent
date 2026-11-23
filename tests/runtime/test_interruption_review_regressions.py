"""独审发现的取消/提交交错，以及真正流式传输的正式回归。"""

import asyncio
import threading
import time
from dataclasses import replace
import pytest
from settlement_scenarios import check_settled_reply, setup, _reply
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY
from nz_coder.runtime.execution import native_sdk


def test_cancel_plus_checkpoint_exception_remains_reenterable(monkeypatch, tmp_path):
    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            return _reply(kwargs, text="done")

    request, options = setup(monkeypatch, tmp_path, Provider())
    cancel = threading.Event()
    options = replace(options, cancellation=cancel)
    env = native_sdk.build_product_run_environment(
        request, options, config_snapshot=options.config_snapshot
    )
    runner = native_sdk.NativeSDKRunner(env)
    runtime = env.runtime_services.session_runtime
    checkpoint = runtime.checkpoint

    async def failed(context, status="running"):
        cancel.set()
        raise OSError("checkpoint disk failed concurrently with user cancellation")

    monkeypatch.setattr(runtime, "checkpoint", failed)
    try:
        with pytest.raises(OSError):
            asyncio.run(runner.run_result(request, options))
        owner = env.active_run_context
        assert owner.terminal_status.value == "cancelled"
        assert owner.finalized or env.has_pending_settlement
        cancel.clear()
        monkeypatch.setattr(runtime, "checkpoint", checkpoint)
        result = asyncio.run(runner.run_result(request, options))
        assert result.status.value == "completed_unverified"
    finally:
        env.close()


def enable_streaming(monkeypatch):
    original = native_sdk.resolve_model_runtime

    def runtime(selection):
        resolved = original(selection)
        return replace(
            resolved,
            capabilities=replace(resolved.capabilities, supports_streaming=True),
        )

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", runtime)
    monkeypatch.setattr(
        "nz_coder.runtime.execution.loop.resolve_model_runtime", runtime
    )


@pytest.mark.parametrize("kind", ["task", "event"])
def test_real_stream_cancel_preserves_observed_usage(monkeypatch, tmp_path, kind):
    from nz_coder.providers.normalized import chunk

    entered, release = threading.Event(), threading.Event()
    cancel = threading.Event()

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            assert kwargs["stream"] is True

            def generate():
                yield chunk(
                    content="partial",
                    usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                )
                entered.set()
                assert release.wait(3)
                yield chunk(content="late answer", finish_reason="stop")

            return generate()

    request, options = setup(monkeypatch, tmp_path, Provider())
    enable_streaming(monkeypatch)
    request = replace(request, stream=True)
    options = replace(options, cancellation=cancel)

    async def run():
        task = asyncio.create_task(
            native_sdk.NativeSDKRunner().run_result(request, options)
        )
        assert await asyncio.to_thread(entered.wait, 3)
        await asyncio.sleep(0.1)
        if kind == "task":
            task.cancel()
        else:
            cancel.set()
        release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status.value == "cancelled"
        assert result.usage.total_tokens == 10
        assert "late answer" not in result.final_text

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize("write_failure", [True, False])
def test_http_cancel_during_frozen_commit_does_not_overwrite_pending(
    monkeypatch, tmp_path, write_failure
):
    import json
    from nz_coder.http_service.manager import SessionManager
    from nz_coder.state import sessions
    from nz_coder.runtime.process.workdir import scoped_workdir
    from test_settlement_product_surfaces import product

    request, options, calls, fault = product(monkeypatch, tmp_path)
    fault[0] = write_failure
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(
        manager.create(workspace_id=manager.workspaces.id_for(tmp_path))["id"]
    )
    entered, release = threading.Event(), threading.Event()
    publisher = sessions._publish_session_json

    def blocked(prepared, path):
        data = json.loads(prepared.read_text())
        if path.name == session.session_id + ".json" and data.get("run_status") in {
            "completed",
            "completed_unverified",
        }:
            entered.set()
            assert release.wait(3)
        return publisher(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", blocked)
    try:
        session.start_run("Inspect the empty workspace.", model="offline/offline-model")
        assert entered.wait(3)
        runner = session.client._native_runner
        env = runner._pending_environments[runner._key(tmp_path, session.session_id)]
        owner = env.active_run_context
        assert owner.terminal_status.value in {"completed", "completed_unverified"}
        first = owner.transcript[-1]
        first_id = first[MESSAGE_ID_KEY]
        frozen = check_settled_reply(first, "first result")
        assert session.abort()
        time.sleep(0.05)
        release.set()
        assert session.wait(timeout=5)
        with scoped_workdir(tmp_path):
            data = sessions.load_session(session.session_id)
        fault[0] = False
        result = asyncio.run(
            session.client.retry_settlement(tmp_path, session.session_id)
        )
        if write_failure:
            assert result.state == "committed"
            with scoped_workdir(tmp_path):
                data = sessions.load_session(session.session_id)
        else:
            assert result.state == "idle"
        assert data["run_status"] == owner.terminal_status.value
        saved = data["messages"][-1]
        assert saved[MESSAGE_ID_KEY] == first_id
        assert check_settled_reply(saved, "first result") == frozen
        manager.close()
        restored = SessionManager(workspace_roots=[tmp_path], restore_saved=True)
        try:
            reopened = restored.get(session.session_id)
            info = reopened.info()
            assert info["status"] == owner.terminal_status.value
            assert info["last_result"]["answer"] == frozen
            assert "first result" in reopened.message_detail(1)
        finally:
            restored.close()
    finally:
        release.set()
        manager.close()


def test_cancel_arrives_while_checkpoint_writer_is_retiring(tmp_path):
    from types import SimpleNamespace
    from nz_coder.runtime.execution.runner import AgentRunner
    from nz_coder.runtime.session.runtime import SessionRuntime
    from nz_coder.runtime.session.store import EphemeralSessionStore
    from nz_coder.runtime.core.result import RunStatus
    from tests.runtime.test_native_product_runtime import _request

    runtime = SessionRuntime(EphemeralSessionStore())

    async def exercise():
        owner = await runtime.open(_request(tmp_path))
        entered, release = asyncio.Event(), asyncio.Event()

        async def retire():
            entered.set()
            await release.wait()

        lifecycle = SimpleNamespace(
            settle_session_checkpoints=retire, publish_terminal=lambda error=None: None
        )
        task = asyncio.create_task(
            AgentRunner._finalize_after_run_error(
                SimpleNamespace(session_runtime=runtime),
                owner,
                RunStatus.ERROR,
                None,
                OSError("original disk failure"),
                lifecycle,
            )
        )
        await entered.wait()
        owner.request_cancel()
        release.set()
        await task
        assert owner.finalized and owner.terminal_status is RunStatus.CANCELLED

    asyncio.run(exercise())
