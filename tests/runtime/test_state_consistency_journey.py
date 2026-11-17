"""真实工具/Native/HTTP/恢复组合流程，仅模型响应使用本地替身。"""
from __future__ import annotations

import copy
import json
import threading
import time
from types import SimpleNamespace

from nz_coder.foundation import config
from nz_coder.http_service import NZCoderClient, SessionHTTPService, SessionManager
from nz_coder.runtime.execution import native_sdk
from tests.runtime.test_native_product_runtime import _install_runtime


def _response(*calls, content=''):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=list(calls)),
                                 finish_reason='tool_calls' if calls else 'stop')],
        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
    )


def _call(index, name, **args):
    return SimpleNamespace(id=f'call-{index}', type='function',
                           function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def test_mutation_verification_cleanup_completion_and_http_restore(tmp_path, monkeypatch):
    workspace = tmp_path / 'project'
    (workspace / 'pkg').mkdir(parents=True)
    (workspace / 'tests').mkdir()
    (workspace / 'pkg/__init__.py').write_text('')
    (workspace / 'pkg/module.py').write_text('def value(): return 1\n')
    (workspace / 'replacement.py').write_text('def value(): return 3  # copied version\n')
    (workspace / 'tests/__init__.py').write_text('')
    (workspace / 'tests/test_probe.py').write_text(
        'import unittest\nfrom pkg.module import value\nclass T(unittest.TestCase):\n'
        ' def test_value(self): self.assertEqual(value(), 3)\n'
    )
    command = 'python -m pytest -q tests/test_probe.py'
    environments = []
    original_build = native_sdk.build_product_run_environment
    def capture_environment(*args, **kwargs):
        environment = original_build(*args, **kwargs)
        environments.append(environment)
        return environment
    monkeypatch.setattr(native_sdk, 'build_product_run_environment', capture_environment)

    class Provider:
        name = 'offline'
        calls = 0
        after_copy = None
        def create_completion(self, _client, **kwargs):
            self.calls += 1
            step = self.calls
            if step == 1:
                return _response(_call(step, 'write_file', path='pkg/module.py', content='def value(): return 2\n'))
            if step == 2:
                return _response(_call(step, 'bash', command='python -m py_compile pkg/module.py'))
            if step == 3:
                return _response(_call(step, 'bash', command='cp replacement.py pkg/module.py'))
            if step == 4:
                self.after_copy = copy.deepcopy(environments[0].verification_runtime.status())
                return _response(_call(step, 'write_file', path='.verify/probe.py', content='print("probe")\n'))
            if step == 5:
                return _response(_call(step, 'bash', command=command + ' 2>&1 | tail -5'))
            if step == 6:
                return _response(_call(step, 'bash', command='rm -rf .verify'))
            if step == 7:
                return _response(content='UNSUPPORTED_SUCCESS_DRAFT')
            if step == 8:
                assert any('Verification remains unconfirmed' in str(m.get('content')) for m in kwargs['messages'])
                return _response(_call('static', 'bash', command='python -m py_compile pkg/module.py'),
                                 _call('test', 'bash', command=command))
            assert step == 9, 'unexpected model request'
            return _response(content='VERIFIED_FINAL_DRAFT')

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'user-config'))
    for setting in ('PLANNING_ENABLED',):
        monkeypatch.setattr(config, setting, False)
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token='offline-state-journey')
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=3)
        wid = next(w['id'] for w in client.list_workspaces() if w['path'] == str(workspace))
        session_id = client.create_session('default', workspace_id=wid)['id']
        client.run(session_id, 'Fix value to 3, run tests, remove temporary helper and finish.', model='offline/offline-model')
        session = manager.get(session_id)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not session.wait(.02):
            snapshot = client.attach_snapshot(session_id)
            for request in snapshot['pending']['permissions']:
                assert request['permission'] in {'write_file', 'bash'}
                session.interactions.reply_permission(request['id'], 'once')
        assert session.wait(.1), session.info()
        info = client.get_session(session_id)
        assert info['status'] == 'completed', {'calls': provider.calls, 'result': info['last_result']}
        assert provider.calls == 9
        assert provider.after_copy['verification_needed'] is True
        assert provider.after_copy['verification_state'] != 'passed'
        assert not (workspace / '.verify').exists()
        result = info['last_result']
        assert result['metadata']['authored_content'] == 'VERIFIED_FINAL_DRAFT'
        assert result['answer'].startswith('VERIFIED_FINAL_DRAFT')
        verification = result['metadata']['verification']
        # 完整范围核对包含 shell 的副作用；清理之后的新鲜验证可以结清。
        assert verification['verification_needed'] is False
        assert verification['mutation_scope_unknown'] is False
        assert '范围未知' not in result['answer']
        assert all(item['status'] == 'passed' for item in verification['test_obligations'])
        assert all(item.get('scope') == str(workspace) for item in verification['test_obligations'])
        assert 'UNSUPPORTED_SUCCESS_DRAFT' not in result['answer']
    finally:
        service.shutdown()
        server.join(5)
        assert not server.is_alive()
    restored = SessionManager(workspace_roots=[workspace])
    try:
        recovered = restored.get(session_id).info()
        assert recovered['last_result'] == result
        assert recovered['status'] == 'completed'
    finally:
        restored.close()
