"""从真实终端控制器和HTTP管理器激活，确认宿主不会覆盖未结算原件。"""

import asyncio
import copy
import json
import pytest

from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.protocol.public_error import PublicRuntimeError
from nz_coder.state import sessions
from settlement_scenarios import setup, _reply


def product(monkeypatch, tmp_path):
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            return _reply(
                kwargs, text="first result" if len(calls) == 1 else "second result"
            )

    request, options = setup(monkeypatch, tmp_path, Provider())
    for name, value in {
        "NZ_PLANNING_ENABLED": "0",
        "RUNTIME_STATE_PERSIST": "1",
        "NZ_WORKING_MEMORY_REMINDER_STEPS": "0",
    }.items():
        monkeypatch.setenv(name, value)
    original = sessions._publish_session_json
    fault = [True]

    def publish(prepared, path):
        data = json.loads(prepared.read_text())
        if (
            path.name == data.get("session_id", "") + ".json"
            and data.get("run_status") in {"completed", "completed_unverified"}
            and fault[0]
        ):
            raise OSError("disk offline")
        return original(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", publish)
    return request, options, calls, fault


def test_terminal_controller_recovers_original_before_stale_history(
    monkeypatch, tmp_path
):
    from nz_coder.interface.session_controller import TerminalSessionController

    request, options, calls, fault = product(monkeypatch, tmp_path)
    env = native_sdk.build_product_run_environment(
        request, options, config_snapshot=options.config_snapshot
    )
    controller = TerminalSessionController(env)
    history = copy.deepcopy(list(request.messages))
    try:
        with pytest.raises(OSError):
            asyncio.run(
                controller.run(
                    history, stream=False, config_snapshot=options.config_snapshot
                )
            )
        with scoped_workdir(tmp_path):
            history[:] = sessions.load_session(request.session_id)["messages"]
        history.append({"role": "user", "content": "Inspect again."})
        fault[0] = False
        result = asyncio.run(
            controller.run(
                history, stream=False, config_snapshot=options.config_snapshot
            )
        )
        assert result["status"] in {"completed", "completed_unverified"}
        assert len(calls) == 2
        assert any(
            isinstance(m.get("content"), str)
            and m["content"].startswith("first result")
            for m in calls[1]["messages"]
        )
        assert history[-1]["content"].startswith("second result")
    finally:
        env.close()


@pytest.mark.parametrize("explicit_retry", [False, True])
def test_http_preserves_pending_and_refreshes_canonical(
    monkeypatch, tmp_path, explicit_retry
):
    from nz_coder.http_service.manager import SessionManager

    request, options, calls, fault = product(monkeypatch, tmp_path)
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(
        manager.create(workspace_id=manager.workspaces.id_for(tmp_path))["id"]
    )
    try:
        session.start_run("Inspect the empty workspace.", model="offline/offline-model")
        assert session.wait(timeout=6)
        assert len(calls) == 1
        runner = session.client._native_runner
        assert (
            runner._key(tmp_path, session.session_id) in runner._pending_environments
        ), session.info()
        env = runner._pending_environments[runner._key(tmp_path, session.session_id)]
        owner = env.active_run_context
        with scoped_workdir(tmp_path):
            path = sessions.session_dir() / (session.session_id + ".json")
        before = path.read_bytes()
        assert json.loads(before)["run_status"] == "running"
        with pytest.raises(PublicRuntimeError) as caught:
            session.start_run("Inspect again.", model="offline/offline-model")
        assert caught.value.public_error.code == "session_commit_pending"
        assert path.read_bytes() == before and len(calls) == 1
        fault[0] = False
        if explicit_retry:
            restored = asyncio.run(
                session.client.retry_settlement(tmp_path, session.session_id)
            )
            assert restored.state == "committed"
            info = session.info()
            assert (
                info["status"] == owner.terminal_status.value and not info["last_error"]
            )
            assert info["last_result"]["answer"].startswith("first result")
            snapshot = session.snapshot()
            assert snapshot["session"]["status"] == owner.terminal_status.value
        session.start_run("Inspect again.", model="offline/offline-model")
        assert session.wait(timeout=6)
        assert len(calls) == 2 and not session.last_error
        assert any(
            isinstance(m.get("content"), str)
            and m["content"].startswith("first result")
            for m in calls[1]["messages"]
        )
        assert owner.finalized
    finally:
        manager.close()
