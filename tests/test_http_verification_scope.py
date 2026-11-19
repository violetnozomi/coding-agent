"""真实 HTTP/Native/文件/进程链路结清后，API、消息和落盘状态一致。"""
from __future__ import annotations

from dataclasses import replace

import copy
import json
import threading
import time
from types import SimpleNamespace

from nz_coder.foundation import config
from nz_coder.http_service import NZCoderClient, SessionHTTPService, SessionManager
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import load_session
from tests.runtime.test_native_product_runtime import _completion, _install_runtime


def test_http_cleanup_verified_completion_survives_snapshot_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'user-config'))
    for setting in ('PLANNING_ENABLED', 'REFLECTION_ENABLED'):
        monkeypatch.setattr(config, setting, False)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'app.py').write_text('value = 1\n')
    (workspace / 'tests').mkdir()
    (workspace / 'tests/test_app.py').write_text(
        'import unittest\nfrom app import value\n'
        'class TestApp(unittest.TestCase):\n def test_value(self): self.assertEqual(value, 2)\n')
    actions = [
        ('edit_file', {'path': 'app.py', 'old_text': 'value = 1', 'new_text': 'value = 2'}),
        ('bash', {'command': "mkdir data; printf x > data/state; printf 'temp = 1\n' > verify_http.py"}),
        ('bash', {'command': 'rm -rf data verify_http.py __pycache__ && echo cleaned'}),
        ('bash', {'command': 'python -m unittest discover -s tests -p test_app.py -v'}),
        ('bash', {'command': 'python -m py_compile app.py'}),
        'LOCAL_VERIFIED_DONE',
    ]

    class Provider:
        name = 'offline'
        calls = 0

        def create_completion(self, _client, **kwargs):
            names = [item['function']['name'] for item in kwargs.get('tools', [])]
            if names == ['emit_sidecar_verdict']:
                action = ('emit_sidecar_verdict', {'verdict': 'accept', 'reason': 'Controlled offline review', 'delivery_assessment': {'status': 'reviewed', 'issues': []}})
            else:
                assert self.calls < len(actions), 'unexpected extra model request'
                action = actions[self.calls]
                self.calls += 1
            calls = [] if isinstance(action, str) else [SimpleNamespace(
                id='call-' + str(self.calls), type='function',
                function=SimpleNamespace(name=action[0], arguments=json.dumps(action[1]))) ]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=action if isinstance(action, str) else '', tool_calls=calls),
                finish_reason='tool_calls' if calls else 'stop')],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3))

    provider = Provider()
    runtime = replace(_install_runtime(monkeypatch, provider), owns_client=True,
                      client=SimpleNamespace(close=lambda: None))
    for resolver in ('nz_coder.runtime.execution.native_sdk.resolve_model_runtime',
                     'nz_coder.runtime.execution.loop.resolve_model_runtime',
                     'nz_coder.runtime.model_gateway.resolve_model_runtime'):
        monkeypatch.setattr(resolver, lambda *_a, **_k: replace(runtime))
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token='scope-offline-token')
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=3)
        wid = next(item['id'] for item in client.list_workspaces() if item['path'] == str(workspace))
        session_id = client.create_session('default', workspace_id=wid)['id']
        session = manager.get(session_id)
        events = session.event_bus.subscribe()
        client.run(session_id, 'Change the application value to 2 and run focused local verification.',
                   model='offline/offline-model')
        deadline = time.monotonic() + 15
        approvals = []
        while time.monotonic() < deadline:
            event = events.get(timeout=max(.01, deadline - time.monotonic()))
            if event.type == 'permission.asked':
                approvals.append(event.properties['id'])
                assert client.reply_permission(session_id, event.properties['id'], 'once')
            if event.type == 'session.run.settled':
                break
        assert session.wait(1)
        snapshot = client.attach_snapshot(session_id)
        result = snapshot['session']['last_result']
        assert snapshot['settled'] is True
        assert snapshot['pending'] == {'permissions': [], 'questions': []}
        assert result['status'] == snapshot['session']['status'] == 'completed', result
        assert result['metadata']['verification']['verification_needed'] is False
        assert result['metadata']['verification']['mutation_scope_unknown'] is False
        assert len(approvals) == 5
        assert provider.calls == len(actions)
        with scoped_workdir(workspace):
            saved = load_session(session_id)
        assert saved['run_status'] == 'completed'
        assert saved['messages'][-1]['content'] == session.history[-1]['content'] == result['answer']
        restored = SessionManager(workspace_roots=[workspace], restore_saved=True).get(session_id)
        assert result['metadata']['changed_files'] == ['app.py']
        assert result['metadata']['environment_cleanup_complete'] is True
        frozen = copy.deepcopy(result)
        for key in ('changed_files', 'environment_cleanup_complete', 'environment_cleanup_failures'):
            frozen['metadata'].pop(key, None)
        assert restored.info()['last_result'] == frozen
        assert saved['metadata']['_nz_terminal_result']['last_result'] == frozen
        assert not (workspace / 'verify_http.py').exists() and not (workspace / 'data').exists()
    finally:
        service.shutdown()
        server.join(5)
        assert not server.is_alive()


def test_http_batch_approvals_are_visible_before_tool_results_are_saved(tmp_path, monkeypatch):
    paths = ("one.txt", "two.txt", "three.txt")

    class Provider:
        name = "offline"
        started = False

        def create_completion(self, _client, **_kwargs):
            if self.started:
                return _completion("Wrote three files")
            self.started = True
            response = _completion(tool="write_file")
            response.choices[0].message.tool_calls = [
                SimpleNamespace(
                    id=f"call-{index}", type="function",
                    function=SimpleNamespace(
                        name="write_file", arguments=json.dumps({"path": path, "content": path}),
                    ),
                )
                for index, path in enumerate(paths)
            ]
            return response

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "user-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "user-cache"))
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(tmp_path / "user-config" / "config.env"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "user-config" / "workspace-trust.json"))
    monkeypatch.setenv("NZ_PLANNING_ENABLED", "0")
    _install_runtime(monkeypatch, Provider())
    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token="batch-offline-token")
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        workspace_id = next(item["id"] for item in client.list_workspaces() if item["path"] == str(workspace))
        session_id = client.create_session("default", workspace_id)["id"]
        session = manager.get(session_id)
        stream = session.event_bus.subscribe()
        started = client.run(session_id, "Write the three requested files.", model="offline/offline-model")
        interaction_id = started["active_interaction_run_id"]
        assert interaction_id
        ids = set()
        for path in paths:
            while (event := stream.get(timeout=5)).type != "permission.asked":
                pass
            current, = client.pending_permissions(session_id)
            assert current["tool_input"]["path"] == path
            assert event.run_id == interaction_id
            assert current["id"] == event.properties["id"] and current["id"] not in ids
            ids.add(current["id"])
            snapshot = client.attach_snapshot(session_id)
            assert snapshot["run"]["pending"]["permissions"] == [current]
            assert not any(
                part.get("type") == "tool" and part.get("state", {}).get("status") == "completed"
                for part in snapshot["run"]["parts"]
            )
            with scoped_workdir(workspace):
                saved_messages = load_session(session_id)["messages"]
                assert not any(message["role"] == "tool" for message in saved_messages)
            assert client.reply_permission(session_id, current["id"], "once")
        assert session.wait(timeout=15), session.info()
        settled = next(event for event in session.event_bus.recent(256) if event.type == "session.run.settled")
        info = client.get_session(session_id)
        assert settled.run_id == info["active_interaction_run_id"] == interaction_id
        assert settled.properties["persisted"] is True
        assert client.snapshot(session_id)["session"]["last_result"] == info["last_result"]
        assert [(workspace / path).read_text() for path in paths] == list(paths)
        with scoped_workdir(workspace):
            saved = load_session(session_id)
        terminal = saved["metadata"]["_nz_terminal_result"]
        assert terminal["interaction_run_id"] == terminal["last_result"]["metadata"]["interaction_run_id"] == interaction_id
        assert terminal["status"] == saved["run_status"] == settled.properties["status"]
        assert terminal["last_result"]["answer"] == info["last_result"]["answer"]
        saved_calls = [message["tool_call_id"] for message in saved["messages"] if message["role"] == "tool"]
        assert saved_calls == ["call-0", "call-1", "call-2"]
    finally:
        service.shutdown()
        server.join(5)
        assert not server.is_alive()
