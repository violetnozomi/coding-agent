"""真实类型投影经 HTTP 管理器落盘与恢复，保留同一安全错误。"""
from __future__ import annotations

import json
import threading

import pytest

from nz_coder.http_service import NZCoderClient, SessionHTTPService
from nz_coder.http_service.manager import SessionManager
from nz_coder.protocol.public_error import PublicError
from nz_coder.runtime.core.result import RunStatus, freeze_terminal_result
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.execution.runner import _typed_result
from tests.test_http_result_recovery import RecoveryAgent


def test_native_http_provider_failure_keeps_the_same_public_error_in_the_answer(monkeypatch, tmp_path):
    from nz_coder.providers.http import ProviderHTTPError
    from tests.runtime.test_native_product_runtime import _install_runtime

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            raise ProviderHTTPError(401, {}, "PRIVATE_PROVIDER_RESPONSE")

    _install_runtime(monkeypatch, Provider())
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token="offline-http-error-token")
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        workspace = next(item["id"] for item in client.list_workspaces() if item["path"] == str(tmp_path))
        session_id = client.create_session("default", workspace)["id"]
        client.run(session_id, "Answer a question.", model="offline/offline-model")
        assert manager.get(session_id).wait(timeout=10)
        snapshot = client.snapshot(session_id)
        info = snapshot["session"]
        result = info["last_result"]
        wire = next(m["info"]["error"]["data"]["public_error"] for m in snapshot["messages"]
                    if m["info"].get("error"))
        assert wire["code"] == "provider_error" and wire["metadata"]["status_code"] == 401
        assert info["status"] == result["status"] == "error"
        assert info["last_error"] == result["error"] == wire["message"]
        assert result["metadata"]["public_error"] == wire
        assert wire["message"] in result["answer"]
        assert client.messages(session_id)[-1]["content"] == result["answer"]
        assert "PRIVATE_PROVIDER_RESPONSE" not in json.dumps(snapshot)
    finally:
        service.shutdown()
        server.join(timeout=2)
    restored = SessionManager(workspace_roots=[tmp_path])
    try:
        recovered = restored.get(session_id).info()["last_result"]
        assert recovered["metadata"]["public_error"] == wire
        assert recovered["answer"] == result["answer"]
    finally:
        restored.close()


@pytest.mark.parametrize('backend', ['native', 'agent'])
def test_error_wire_survives_http_disk_restore_then_clears_on_success(monkeypatch, tmp_path, backend):
    wire = PublicError("local_failure", "The operation failed safely.").to_dict()

    class Client:
        async def run(self, request, **_kwargs):
            failed = request.messages[-1]["content"] == "fail"
            status = RunStatus.ERROR if failed else RunStatus.COMPLETED
            context = RunContext(request, Session.create(request.session_id, request.messages, workspace=request.workspace),
                                 "worker", metadata=dict(request.metadata))
            context.freeze_execution(status)
            freeze_terminal_result(context, {"status": status.value, "public_error": wire})
            return _typed_result(request, context, {"status": status.value, "public_error": wire})

    monkeypatch.setattr("nz_coder.http_service.manager.AgentClient", Client)
    class Agent(RecoveryAgent):
        async def run(self, messages, **kwargs):
            current = next(message['content'] for message in reversed(messages) if message['role'] == 'user')
            result = await super().run(messages, **kwargs)
            if current == 'error':
                result.update(status='error', public_error=wire, last_error=wire['message'], error='')
            return result

    factory = {'agent_factory': Agent} if backend == 'agent' else {}
    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False, **factory)
    try:
        session = manager.get(manager.create()["id"])
        session_id = session.session_id
        manager.start_run(session_id, "error" if backend == 'agent' else 'fail')
        assert session.wait(timeout=5)
        info = session.info()
        assert info["status"] == "error"
        assert info["last_error"] == info["last_result"]["error"] == wire["message"]
        assert info["last_result"]["metadata"]["public_error"] == wire
    finally:
        manager.close()
    restored = SessionManager(workspace_roots=[workspace], restore_saved=True, **factory)
    try:
        session = restored.get(session_id)
        info = session.info()
        assert info["last_error"] == info["last_result"]["error"] == wire["message"]
        restored.start_run(session_id, "succeed")
        assert session.wait(timeout=5)
        info = session.info()
        assert info["status"] == "completed"
        assert info["last_error"] == "" and info["last_result"]["error"] == ""
        assert "public_error" not in info["last_result"]["metadata"]
    finally:
        restored.close()


def test_http_accepted_cancellation_cannot_keep_a_returned_error_wire(tmp_path):
    wire = PublicError('failure', 'Safe cancelled-operation failure').to_dict()
    holder = {}
    class Agent(RecoveryAgent):
        async def run(self, messages, **kwargs):
            result = await super().run(messages, **kwargs)
            assert holder['session'].abort()
            result.update(status='error', public_error=wire, last_error=wire['message'],
                          metadata={'public_error': wire})
            return result

    manager = SessionManager(agent_factory=Agent, workspace_roots=[tmp_path], restore_saved=False)
    try:
        session = manager.get(manager.create()['id'])
        holder['session'] = session
        session.start_run('error')
        assert session.wait(timeout=5)
        info = session.info()
        assert info['status'] == info['last_result']['status'] == 'cancelled'
        assert info['last_error'] == info['last_result']['error'] == ''
        assert not info['last_result'].get('last_error')
        assert 'public_error' not in info['last_result']
        assert 'public_error' not in info['last_result']['metadata']
    finally:
        manager.close()
