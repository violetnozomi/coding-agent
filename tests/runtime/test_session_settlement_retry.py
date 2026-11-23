"""SDK原结算重试：不启动模型、不重放命令，资源关闭后仍能保存原结果。"""

import asyncio
import copy
import json
import threading
from dataclasses import replace
import pytest

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY
from nz_coder.protocol.public_error import PublicRuntimeError
from nz_coder.runtime.core.result import TERMINAL_RESULT_KEY
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from settlement_scenarios import check_settled_reply, setup, payload, _reply


def scenario(monkeypatch, workspace, *, borrowed):
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            return _reply(
                kwargs, text="original answer" if len(calls) == 1 else "new answer"
            )

    request, options = setup(monkeypatch, workspace, Provider())
    request = replace(request, interaction_run_id="original-run")
    env = (
        native_sdk.build_product_run_environment(
            request, options, config_snapshot=options.config_snapshot
        )
        if borrowed
        else None
    )
    runner = native_sdk.NativeSDKRunner(env)
    publisher = sessions._publish_session_json
    fault = [True]
    attempts = []

    def publish(prepared, path):
        data = json.loads(prepared.read_text())
        if (
            path.name == request.session_id + ".json"
            and data.get("run_status") == "completed_unverified"
        ):
            attempts.append(data)
            if fault[0]:
                raise OSError("disk temporarily offline")
        return publisher(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", publish)
    return request, options, env, runner, calls, fault, attempts


@pytest.mark.parametrize("borrowed", [False, True])
def test_explicit_retry_keeps_original_result_after_resources_exit(
    monkeypatch, tmp_path, borrowed
):
    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=borrowed
    )
    try:
        with pytest.raises(OSError):
            asyncio.run(runner.run_result(request, options))
        original_env = (
            env
            or runner._pending_environments[runner._key(tmp_path, request.session_id)]
        )
        original = original_env.active_run_context
        assert original.terminal_status.value == "completed_unverified" and not original.finalized
        assert len(calls) == 1 and len(attempts) == 2
        first = attempts[0]["messages"][-1]
        frozen = check_settled_reply(first, "original answer")
        if not borrowed:
            assert runner.retry_cleanup(tmp_path, request.session_id)
            assert original_env.environment_cleanup_complete
            assert (
                runner._key(tmp_path, request.session_id)
                in runner._pending_environments
            )
        failed = asyncio.run(runner.retry_settlement(tmp_path, request.session_id))
        assert (
            failed.state == "pending" and failed.error.code == "session_commit_pending"
        )
        assert failed.error.retryable and not original.finalized and len(calls) == 1
        fault[0] = False
        fixed = asyncio.run(runner.retry_settlement(tmp_path, request.session_id))
        assert fixed.state == "committed" and fixed.interaction_run_id == "original-run"
        assert original.finalized and original.usage.total_tokens == 3
        data = payload(tmp_path, request)
        assert data["run_status"] == "completed_unverified"
        assert data["metadata"]["active_interaction_run_id"] == "original-run"
        latest = data["messages"][-1]
        assert latest[MESSAGE_ID_KEY] == first[MESSAGE_ID_KEY]
        assert check_settled_reply(latest, "original answer") == frozen == original.transcript[-1]["content"]
        assert data["metadata"][TERMINAL_RESULT_KEY]["last_result"]["answer"] == frozen
        assert data["metadata"]["_nz_session_snapshot"]["usage"]["input_tokens"] == 2
        assert len(calls) == 1
        assert (
            asyncio.run(runner.retry_settlement(tmp_path, request.session_id)).state
            == "idle"
        )
        if not borrowed:
            assert not runner._pending_environments
    finally:
        if env:
            env.close()


@pytest.mark.parametrize("borrowed", [False, True])
def test_new_activation_settles_old_then_runs_new_message(
    monkeypatch, tmp_path, borrowed
):
    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=borrowed
    )
    try:
        with pytest.raises(OSError):
            asyncio.run(runner.run_result(request, options))
        next_request = replace(
            request,
            messages=[{"role": "user", "content": "new actual task"}],
            interaction_run_id="new-run",
        )
        with pytest.raises(PublicRuntimeError) as caught:
            asyncio.run(runner.run_result(next_request, options))
        assert caught.value.public_error.code == "session_commit_pending"
        assert len(calls) == 1
        first = attempts[0]["messages"][-1]
        frozen = check_settled_reply(first, "original answer")
        fault[0] = False
        result = asyncio.run(runner.run_result(next_request, options))
        earlier, latest = [m for m in result.messages if m.get("role") == "assistant"]
        assert earlier[MESSAGE_ID_KEY] == first[MESSAGE_ID_KEY]
        assert check_settled_reply(earlier, "original answer") == frozen
        assert check_settled_reply(latest, "new answer") == result.final_text and len(calls) == 2
        assert any(m.get("role") == "assistant" and m.get("content") == frozen for m in calls[-1]["messages"])
        assert "original answer" not in repr(calls[-1]["messages"])
        assert any(m.get("content") == "new actual task" for m in calls[-1]["messages"])
        assert (
            payload(tmp_path, request)["metadata"]["_nz_session_snapshot"]["usage"][
                "input_tokens"
            ]
            == 4
        )
    finally:
        if env:
            env.close()


def test_conflict_reports_stable_error_without_overwrite(monkeypatch, tmp_path):
    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=False
    )
    with pytest.raises(OSError):
        asyncio.run(runner.run_result(request, options))
    with scoped_workdir(tmp_path):
        path = sessions.session_dir() / (request.session_id + ".json")
    data = json.loads(path.read_text())
    data["messages"][0]["content"] = "external edit"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    fault[0] = False
    result = asyncio.run(runner.retry_settlement(tmp_path, request.session_id))
    assert result.state == "conflict" and result.error.code == "session_commit_conflict"
    assert (
        not result.error.retryable and path.read_bytes() == before and len(calls) == 1
    )
    assert runner._pending_environments


def test_idle_and_agent_client_forwarding(tmp_path):
    from nz_coder.sdk import AgentClient

    runner = native_sdk.NativeSDKRunner()
    client = AgentClient(runner=runner)
    result = asyncio.run(client.retry_settlement(tmp_path, "never-opened"))
    assert (
        result.state == "idle"
        and result.interaction_run_id is None
        and result.error is None
    )


def test_concurrent_cross_loop_retries_publish_once(monkeypatch, tmp_path):
    import concurrent.futures
    from nz_coder.runtime.session.runtime import SessionRuntime

    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=True
    )
    try:
        with pytest.raises(OSError):
            asyncio.run(runner.run_result(request, options))
        original = env.active_run_context
        entered, release, joined = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )
        publisher = sessions._publish_session_json

        def publish(prepared, path):
            if (
                path.name == request.session_id + ".json"
                and json.loads(prepared.read_text()).get("run_status") == "completed_unverified"
            ):
                entered.set()
                assert release.wait(5)
            return publisher(prepared, path)

        monkeypatch.setattr(sessions, "_publish_session_json", publish)
        finalize = SessionRuntime.finalize
        count = [0]
        lock = threading.Lock()

        async def observe(self, owner, status):
            if owner is original:
                with lock:
                    count[0] += 1
                    if count[0] == 2:
                        joined.set()
            return await finalize(self, owner, status)

        monkeypatch.setattr(SessionRuntime, "finalize", observe)
        fault[0] = False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                asyncio.run, runner.retry_settlement(tmp_path, request.session_id)
            )
            assert entered.wait(3)
            second = pool.submit(
                asyncio.run, runner.retry_settlement(tmp_path, request.session_id)
            )
            try:
                assert joined.wait(3)
            finally:
                release.set()
            assert first.result(5).state == second.result(5).state == "committed"
        assert len(calls) == 1 and len(attempts) == 3
        terminal = [
            e for e in env.event_bus.recent(1000) if e.type == "session.run.completed"
        ]
        assert len(terminal) == 1 and terminal[0].run_id == "original-run"
        assert original.session.usage == original.usage
    finally:
        env.close()


def test_cancel_retry_after_disk_publish_keeps_original_frozen_result(
    monkeypatch, tmp_path
):
    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=True
    )
    try:
        with pytest.raises(OSError):
            asyncio.run(runner.run_result(request, options))
        original = env.active_run_context
        entered, release = threading.Event(), threading.Event()
        publisher = sessions._publish_session_json

        def publish(prepared, path):
            if (
                path.name == request.session_id + ".json"
                and json.loads(prepared.read_text()).get("run_status") == "completed_unverified"
            ):
                entered.set()
                assert release.wait(5)
            return publisher(prepared, path)

        monkeypatch.setattr(sessions, "_publish_session_json", publish)
        fault[0] = False

        async def cancel():
            task = asyncio.create_task(
                runner.retry_settlement(tmp_path, request.session_id)
            )
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(cancel())
        assert original.terminal_status.value == "completed_unverified" and not original.finalized
        result = asyncio.run(runner.retry_settlement(tmp_path, request.session_id))
        assert result.state == "committed" and len(attempts) == 3 and len(calls) == 1
    finally:
        env.close()


def test_capacity_failure_keeps_specific_nonretryable_cause(monkeypatch, tmp_path):
    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=True
    )
    try:
        with pytest.raises(OSError):
            asyncio.run(runner.run_result(request, options))
        monkeypatch.setattr(sessions, "_session_byte_limit", lambda: 100)
        result = asyncio.run(runner.retry_settlement(tmp_path, request.session_id))
        assert (
            result.state == "pending"
            and result.error.code == "resource_limit"
            and not result.error.retryable
        )
        assert len(calls) == 1 and env.has_pending_settlement
    finally:
        env.close()


def test_other_session_can_run_while_first_is_pending(monkeypatch, tmp_path):
    request, options, env, runner, calls, fault, attempts = scenario(
        monkeypatch, tmp_path, borrowed=False
    )
    with pytest.raises(OSError):
        asyncio.run(runner.run_result(request, options))
    other = replace(request, session_id="other-session", interaction_run_id="other-run")
    result = asyncio.run(runner.run_result(other, options))
    assert result.status.value == "completed_unverified" and len(calls) == 2
    assert runner._has_pending_settlement(tmp_path, request.session_id)
