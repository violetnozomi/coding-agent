"""真实HTTP、Native执行器和本地shell；仅模型响应由确定性提供方替换。"""
from __future__ import annotations

from dataclasses import replace

import copy
import json
import threading
import time
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.http_service import NZCoderClient, SessionHTTPService, SessionManager
from tests.runtime.test_native_product_runtime import _install_runtime


def _completion(calls=()):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content='' if calls else 'LOCAL_COMMAND_DONE', tool_calls=list(calls)),
            finish_reason='tool_calls' if calls else 'stop',
        )],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
    )


def _bash(call_id, filename, timeout):
    return SimpleNamespace(id=call_id, type='function', function=SimpleNamespace(
        name='bash', arguments=json.dumps({
            'command': f'python3 -c "from pathlib import Path; Path(\'{filename}\').write_text(\'ok\')"',
            'timeout': timeout,
        }),
    ))


def test_native_mixed_invalid_timeout_snapshot_reply_and_abort(tmp_path, monkeypatch):
    class Provider:
        name = 'offline'

        def __init__(self):
            self.calls = 0
            self.after_tools = []

        def create_completion(self, _client, **kwargs):
            if any(item.get('function', {}).get('name') == 'emit_sidecar_verdict' for item in kwargs.get('tools', [])):
                return _completion((SimpleNamespace(id='review', type='function', function=SimpleNamespace(
                    name='emit_sidecar_verdict', arguments=json.dumps({'verdict': 'accept',
                    'reason': 'The response reports actual command outcomes.',
                    'delivery_assessment': {'status': 'reviewed', 'issues': []}}))),))
            self.calls += 1
            if self.calls == 1:
                return _completion((
                    _bash('invalid-timeout', 'invalid-must-not-exist.txt', 320),
                    _bash('allowed-command', 'allowed.txt', 2),
                ))
            if self.calls == 2:
                self.after_tools = copy.deepcopy(kwargs['messages'])
                return _completion()
            assert self.calls == 3, 'unexpected offline model request'
            return _completion((_bash('abort-command', 'abort-must-not-exist.txt', 2),))

    provider = Provider()
    runtime = replace(_install_runtime(monkeypatch, provider), owns_client=True,
                      client=SimpleNamespace(close=lambda: None))
    for resolver in ('nz_coder.runtime.execution.native_sdk.resolve_model_runtime',
                     'nz_coder.runtime.execution.loop.resolve_model_runtime',
                     'nz_coder.runtime.model_gateway.resolve_model_runtime'):
        monkeypatch.setattr(resolver, lambda *_a, **_k: replace(runtime))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'user-config'))
    for setting in ('PLANNING_ENABLED',):
        monkeypatch.setattr(config, setting, False)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token='snapshot-offline-token')
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=3)
        wid = next(item['id'] for item in client.list_workspaces() if item['path'] == str(workspace))
        session_id = client.create_session('default', workspace_id=wid)['id']
        session = manager.get(session_id)
        subscription = session.event_bus.subscribe()

        def wait_permission():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                event = subscription.get(timeout=max(0.01, deadline - time.monotonic()))
                if event.type == 'permission.asked':
                    return event
            raise AssertionError('permission request not published')

        client.run(session_id, 'Execute the supplied local shell checks.', model='offline/offline-model')
        asked = wait_permission()
        assert 'allowed.txt' in asked.properties['tool_input']['command']
        snapshot = client.attach_snapshot(session_id)
        assert [item['id'] for item in snapshot['pending']['permissions']] == [asked.properties['id']]
        assert snapshot['pending'] == snapshot['run']['pending']
        assert snapshot['session']['pending_interaction_count'] == 1
        assert snapshot['session']['runtime_status'] == snapshot['run']['status'] == 'waiting_permission'
        assert client.reply_permission(session_id, asked.properties['id'], 'once')
        assert session.wait(10)
        assert (workspace / 'allowed.txt').read_text() == 'ok'
        assert not (workspace / 'invalid-must-not-exist.txt').exists()
        tool_results = {message['tool_call_id']: message['content'] for message in provider.after_tools if message.get('role') == 'tool'}
        assert tool_results['invalid-timeout'].startswith('Error: timeout')
        first_asked = [event for event in session.event_bus.recent(1000) if event.type == 'permission.asked']
        assert len(first_asked) == 1
        assert client.attach_snapshot(session_id)['pending'] == {'permissions': [], 'questions': []}

        client.run(session_id, 'Execute another supplied shell check.', model='offline/offline-model')
        abort_asked = wait_permission()
        assert 'abort-must-not-exist.txt' in abort_asked.properties['tool_input']['command']
        assert client.attach_snapshot(session_id)['session']['pending_interaction_count'] == 1
        assert client.abort(session_id) == {'aborted': True}
        assert session.wait(10)
        settled = client.attach_snapshot(session_id)
        assert settled['settled']
        assert settled['pending'] == settled['run']['pending'] == {'permissions': [], 'questions': []}
        assert settled['session']['status'] == settled['session']['last_result']['status'] == 'cancelled'
        assert not (workspace / 'abort-must-not-exist.txt').exists()
        assert provider.calls == 3
    finally:
        service.shutdown()
        server.join(5)
        assert not server.is_alive()


@pytest.mark.parametrize('decision', ['reject', 'timeout'])
def test_native_synthetic_permission_uses_same_public_request_and_stops(tmp_path, monkeypatch, decision):
    """真实HTTP跨过文件写入，到runtime补编译审批；拒绝不伪完成。"""

    class Provider:
        name = 'offline'
        calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            assert self.calls <= 2, 'approval stop requested another model completion'
            if self.calls == 1:
                return _completion([SimpleNamespace(id='files', type='function', function=SimpleNamespace(
                    name='write_files_batch', arguments=json.dumps({'files': [
                        {'path': 'a.py', 'content': 'a=1\n'}, {'path': 'b.py', 'content': 'b=1\n'},
                    ]}),
                ))])
            return _completion()

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'user-config'))
    for setting in ('PLANNING_ENABLED',):
        monkeypatch.setattr(config, setting, False)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False,
                             interaction_timeout_seconds=1 if decision == 'timeout' else 10)
    service = SessionHTTPService(port=0, manager=manager, token='approval-offline-token')
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=3)
        wid = next(w['id'] for w in client.list_workspaces() if w['path'] == str(workspace))
        sid = client.create_session('default', workspace_id=wid)['id']
        session = manager.get(sid)
        subscription = session.event_bus.subscribe()

        def wait_permission():
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                event = subscription.get(timeout=max(.01, deadline-time.monotonic()))
                if event.type == 'permission.asked':
                    return event
            raise AssertionError('no permission request')

        client.run(sid, '创建并验证 a.py、b.py。验收命令：`python -m py_compile a.py b.py`。', model='offline/offline-model')
        first = wait_permission()
        assert first.properties['permission'] == 'write_files_batch'
        client.reply_permission(sid, first.properties['id'], 'once')
        check = wait_permission()
        assert check.properties['permission'] == 'bash'
        pending = client.pending_permissions(sid)
        assert len(pending) == 1 and pending[0]['id'] == check.properties['id']
        assert pending[0]['tool_input'] == check.properties['tool_input']
        assert '_nz_runtime_verification_stage' not in pending[0]['tool_input']
        if decision == 'reject':
            client.reply_permission(sid, check.properties['id'], 'reject')
        assert session.wait(8)
        settled = client.attach_snapshot(sid)
        assert settled['session']['last_result']['status'] == settled['run']['status'] == 'blocked'
        assert settled['pending'] == {'permissions': [], 'questions': []}
        final_text = settled['session']['last_result']['answer']
        assert ('rejected' if decision == 'reject' else 'timed out') in final_text.lower()
        assert 'LOCAL_COMMAND_DONE' not in final_text
        assert provider.calls == 2
        assert len([e for e in session.event_bus.recent(1000) if e.type == 'permission.asked']) == 2
        assert not list(workspace.rglob('__pycache__'))
        assert (workspace / 'a.py').read_text() == 'a=1\n'
    finally:
        service.shutdown()
        server.join(5)
        assert not server.is_alive()
