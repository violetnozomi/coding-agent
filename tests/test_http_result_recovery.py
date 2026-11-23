"""HTTP 已冻结结果的持久化、恢复及新 Run 隔离行为。"""
from __future__ import annotations

import asyncio
import copy
import json
import queue
import threading

import pytest

from nz_coder.http_service.manager import SessionManager
from nz_coder.protocol.message_schema import attach_message_identity, ensure_message_identities
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import load_session, save_session

RECORD_KEY = '_nz_http_terminal_result'
NATIVE_RECORD_KEY = '_nz_terminal_result'


class RecoveryAgent:
    """确定性宿主；磁盘和管理器提交仍走产品实现。"""

    def __init__(self, session_id, mode):
        self.event_bus = SessionEventBus(session_id=session_id)
        self.model_id = 'recovery-probe'
        self.provider_id = 'offline'
        self.entered = threading.Event()

    async def run(self, messages, **kwargs):
        text = messages[-1]['content']
        self.entered.set()
        if text == 'wait':
            await asyncio.Event().wait()
        if text == 'raise':
            raise RuntimeError('private provider error')
        status = text if text in {'cancelled', 'error', 'interrupted', 'blocked', 'completed_unverified'} else 'completed'
        answer = '' if status == 'cancelled' else f'answer:{text}'
        messages.append({
            'role': 'assistant', 'content': answer,
            '_nz_interaction_run_id': self._requested_interaction_run_id,
        })
        return {
            'status': status, 'answer': answer, 'active_agent': 'actual-worker',
            'error': ({'schema': 'nz.public_error.v1', 'code': 'offline_failure',
                       'message': 'Safe offline failure', 'retryable': False, 'metadata': {}}
                      if status == 'error' else ''),
            'metadata': {
                'interaction_run_id': self._requested_interaction_run_id,
                'custom': {'values': [text]}, 'usage': {'input': 19, 'output': 3},
                'verification': {'verification_needed': status == 'completed_unverified'},
            },
        }

    def close(self):
        self.event_bus.close()


def _manager(workspace, **kwargs):
    return SessionManager(agent_factory=RecoveryAgent, workspace_roots=[workspace], **kwargs)


def _payload(workspace, session_id):
    with scoped_workdir(workspace):
        return load_session(session_id)


def _seed(workspace, *, status='completed', result=None, last_error=''):
    workspace.mkdir(exist_ok=True)
    session_id = 'recovery-session'
    messages = [
        {'role': 'user', 'content': 'old request'},
        {'role': 'assistant', 'content': 'OLD_ANSWER'},
    ]
    for index, message in enumerate(messages):
        attach_message_identity(message, f'msg-recovery-{index}', session_id=session_id)
        message['_nz_interaction_run_id'] = 'interaction-old'
    ensure_message_identities(messages, session_id)
    result = result if result is not None else {
        'status': 'completed', 'answer': 'OLD_ANSWER', 'active_agent': 'actual-worker',
        'error': '', 'metadata': {'interaction_run_id': 'interaction-old', 'custom': [1]},
    }
    metadata = {
        'native_setting': {'keep': [1, 2]},
        'active_interaction_run_id': 'interaction-old',
        RECORD_KEY: {'interaction_run_id': 'interaction-old', 'status': status,
                     'last_error': last_error, 'last_result': result},
    }
    with scoped_workdir(workspace):
        save_session(messages, session_id=session_id, mode='default', run_status=status,
                     session_metadata=metadata, activate=False, require_aliases=False,
                     model='recovery-probe')
    return session_id


@pytest.mark.parametrize('status', ['completed', 'cancelled', 'error', 'interrupted', 'blocked', 'completed_unverified'])
def test_http_terminal_result_survives_lazy_and_built_restore(tmp_path, status):
    """捕获只保存历史/状态，或只在一种恢复入口传递结果的缺陷。"""
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    first = _manager(workspace)
    try:
        first.start_run(session_id, status)
        session = first.get(session_id)
        assert session.wait(timeout=3)
        expected = session.info()['last_result']
        history = _payload(workspace, session_id)['messages']
        assert _payload(workspace, session_id)['metadata']['native_setting'] == {'keep': [1, 2]}
    finally:
        first.close()
    restored_manager = _manager(workspace)
    try:
        lazy = restored_manager.info(session_id)
        assert session_id not in restored_manager._sessions
        assert lazy['status'] == status
        assert lazy['last_result'] == expected
        restored = restored_manager.get(session_id)
        assert restored.info()['status'] == status
        assert restored.info()['last_result'] == expected
        assert restored.snapshot()['session']['last_result'] == expected
        assert restored.attach_snapshot()['session']['last_result'] == expected
        assert _payload(workspace, session_id)['messages'] == history
        lazy['last_result']['metadata']['custom']['values'].append('changed')
        snapshot = restored.snapshot()
        snapshot['session']['last_result']['metadata']['custom']['values'].append('changed')
        assert restored.info()['last_result'] == expected
    finally:
        restored_manager.close()


def test_http_acceptance_clears_old_result_on_disk(tmp_path):
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    manager = _manager(workspace)
    try:
        session = manager.get(session_id)
        session.start_run('wait')
        assert session.agent.entered.wait(timeout=2)
        accepted = _payload(workspace, session_id)
        assert accepted['run_status'] == 'running'
        assert RECORD_KEY not in accepted['metadata']
        assert accepted['metadata']['native_setting'] == {'keep': [1, 2]}
        interrupted_manager = _manager(workspace)
        try:
            interrupted = interrupted_manager.get(session_id).info()
            assert interrupted['status'] == 'interrupted'
            assert interrupted['last_result'] == {}
        finally:
            interrupted_manager.close()
    finally:
        manager.close()


def test_http_start_failure_rolls_back_history_and_clears_result(tmp_path, monkeypatch):
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    original_history = _payload(workspace, session_id)['messages']
    manager = _manager(workspace)
    try:
        session = manager.get(session_id)
        def fail_start(_thread):
            raise RuntimeError('cannot start worker')
        monkeypatch.setattr(threading.Thread, 'start', fail_start)
        with pytest.raises(RuntimeError, match='cannot start worker'):
            session.start_run('new request')
        saved = _payload(workspace, session_id)
        assert saved['messages'] == original_history
        assert saved['run_status'] == 'failed'
        assert RECORD_KEY not in saved['metadata']
        assert session.info()['last_result'] == {}
    finally:
        manager.close()


def test_http_manager_failure_preserves_only_actual_result_and_public_error(tmp_path):
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    manager = _manager(workspace)
    try:
        manager.start_run(session_id, 'raise')
        session = manager.get(session_id)
        assert session.wait(timeout=3)
        expected = session.info()
        assert expected['status'] == 'failed'
        assert expected['last_result'] == {}
        assert expected['last_error'] == 'An internal error occurred.'
    finally:
        manager.close()
    restored = _manager(workspace)
    try:
        info = restored.get(session_id).info()
        assert info['last_result'] == {}
        assert info['last_error'] == expected['last_error']
        assert info['status'] == 'failed'
    finally:
        restored.close()


@pytest.mark.parametrize('change', [
    'absent', 'not-dict', 'wrong-status', 'wrong-run', 'wrong-result-type',
    'wrong-answer-type', 'wrong-metadata-type', 'wrong-result-run', 'missing-user-id', 'running',
])
def test_http_invalid_record_does_not_hide_valid_history(tmp_path, change):
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    payload = _payload(workspace, session_id)
    metadata = payload['metadata']
    record = metadata[RECORD_KEY]
    if change == 'absent':
        metadata.pop(RECORD_KEY)
    elif change == 'not-dict':
        metadata[RECORD_KEY] = []
    elif change == 'wrong-status':
        record['status'] = 'cancelled'
    elif change == 'wrong-run':
        record['interaction_run_id'] = 'interaction-other'
    elif change == 'wrong-result-type':
        record['last_result'] = []
    elif change == 'wrong-answer-type':
        record['last_result']['answer'] = 1
    elif change == 'wrong-metadata-type':
        record['last_result']['metadata'] = []
    elif change == 'wrong-result-run':
        record['last_result']['metadata']['interaction_run_id'] = 'interaction-other'
    elif change == 'missing-user-id':
        payload['messages'][0].pop('_nz_interaction_run_id')
    elif change == 'running':
        payload['run_status'] = 'running'
    with scoped_workdir(workspace):
        save_session(payload['messages'], session_id=session_id, mode='default',
                     run_status=payload['run_status'], session_metadata=metadata,
                     activate=False, model='recovery-probe')
    history = _payload(workspace, session_id)['messages']
    manager = _manager(workspace)
    try:
        lazy = manager.info(session_id)
        info = manager.get(session_id).info()
        assert lazy['last_result'] == info['last_result'] == {}
        assert info['status'] == ('interrupted' if change == 'running' else 'completed')
        assert _payload(workspace, session_id)['messages'] == history
    finally:
        manager.close()


def test_http_restore_keeps_frozen_outer_and_nested_status_distinct(tmp_path):
    workspace = tmp_path / 'project'
    session_id = _seed(workspace, status='cancelled')
    manager = _manager(workspace)
    try:
        info = manager.get(session_id).info()
        assert info['status'] == 'cancelled'
        assert info['last_result']['status'] == 'completed'
        assert info['last_result']['answer'] == 'OLD_ANSWER'
    finally:
        manager.close()


def test_http_failed_terminal_save_does_not_restore_previous_result(tmp_path, monkeypatch):
    from nz_coder.http_service import manager as manager_module
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    manager = _manager(workspace)
    original_save = manager_module.save_session
    def fail_terminal(*args, **kwargs):
        if kwargs.get('run_status') == 'completed':
            raise OSError('terminal commit unavailable')
        return original_save(*args, **kwargs)
    monkeypatch.setattr(manager_module, 'save_session', fail_terminal)
    try:
        session = manager.get(session_id)
        subscription = session.event_bus.subscribe()
        session.start_run('new answer')
        assert session.wait(timeout=3)
        events = []
        while True:
            try:
                events.append(subscription.get(timeout=0.01))
            except queue.Empty:
                break
        settled = next(event for event in events if event.type == 'session.run.settled')
        assert settled.properties == {'status': 'completed', 'persisted': False}
        saved = _payload(workspace, session_id)
        assert saved['run_status'] == 'running'
        assert RECORD_KEY not in saved['metadata']
    finally:
        manager.close()
    restored = _manager(workspace)
    try:
        assert restored.get(session_id).info()['status'] == 'interrupted'
        assert restored.get(session_id).info()['last_result'] == {}
    finally:
        restored.close()


@pytest.mark.parametrize('partial', [False, True], ids=['no-draft', 'with-draft'])
def test_http_native_two_runs_restore_cancel_and_retire_old_callbacks(tmp_path, monkeypatch, partial):
    """走默认 Native、真实 HTTP/审批/写盘，防止结果串轮和退休回调覆盖。"""
    from nz_coder.foundation import config
    from nz_coder.http_service import NZCoderClient, SessionHTTPService
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.adapters.runner import _LifecycleService
    from nz_coder.state import sessions
    from contextvars import copy_context
    from tests.runtime.test_native_product_runtime import _install_runtime
    from tests.runtime.test_run_result_text import _AnswerThenApprovalProvider

    entered, release, settle_started = threading.Event(), threading.Event(), threading.Event()
    original_settle = _LifecycleService.settle_session_checkpoints
    captured, release_callback = threading.Event(), threading.Event()
    writers, writer_errors = [], []
    provider = _AnswerThenApprovalProvider(partial)
    original_completion = provider.create_completion
    def completion(*args, **kwargs):
        if provider.calls == 0 and not partial:
            environment = environments[0]
            observer = environment._model_gateway_observer_for_run()
            scope = copy_context()
            def report():
                try:
                    captured.set()
                    assert release_callback.wait(timeout=5)
                    def scoped_report():
                        with scoped_workdir(workspace):
                            observer('model_call_retry', {
                                'purpose': 'coding', 'attempt': 1, 'wait_seconds': 0, 'error': 'offline retry',
                            })
                    scope.run(scoped_report)
                except BaseException as exc:
                    writer_errors.append(exc)
            writer = threading.Thread(target=report, name='entered-parent-progress-reporter')
            writers.append(writer)
            writer.start()
            assert captured.wait(timeout=3)
        return original_completion(*args, **kwargs)
    provider.create_completion = completion
    _install_runtime(monkeypatch, provider)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'user-config'))
    for setting in ('PLANNING_ENABLED',):
        monkeypatch.setattr(config, setting, False)
    environments, requests = [], []
    original_build = native_sdk.build_product_run_environment
    def capture_environment(request, *args, **kwargs):
        requests.append(copy.deepcopy(request.metadata))
        environment = original_build(request, *args, **kwargs)
        environments.append(environment)
        return environment
    monkeypatch.setattr(native_sdk, 'build_product_run_environment', capture_environment)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    original_write = sessions._publish_session_json
    def paused_write(prepared, path):
        json.loads(prepared.read_text(encoding="utf-8"))
        if threading.current_thread().name == 'entered-parent-progress-reporter' and path.name == f'{session_id}.json':
            entered.set()
            assert release.wait(timeout=5)
        return original_write(prepared, path)
    async def observe_settle(lifecycle):
        if environments and lifecycle.host is environments[0] and not partial:
            settle_started.set()
            release_callback.set()
            assert await asyncio.to_thread(entered.wait, 3)
        return await original_settle(lifecycle)
    monkeypatch.setattr(sessions, '_publish_session_json', paused_write)
    monkeypatch.setattr(_LifecycleService, 'settle_session_checkpoints', observe_settle)
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token='recovery-offline-token')
    server = threading.Thread(target=service.serve_forever, daemon=True)
    server.start()
    session_id = ''
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        workspace_id = next(item['id'] for item in manager.list_workspaces() if item['path'] == str(workspace))
        session_id = client.create_session('default', workspace_id=workspace_id)['id']
        client.run(session_id, 'Reply with the previous marker.', model='offline/offline-model')
        session = manager.get(session_id)
        if not partial:
            assert entered.wait(timeout=3)
            assert settle_started.is_set()
            assert not session.wait(timeout=0.05)
            assert client.get_session(session_id)['status'] == 'running'
            release.set()
            writers[0].join(timeout=3)
            assert not writers[0].is_alive()
            assert not writer_errors
        assert session.wait(timeout=10)
        previous = client.get_session(session_id)['last_result']
        assert previous['metadata']['authored_content'] == 'PREVIOUS_RUN_ANSWER'
        assert 'PREVIOUS_RUN_ANSWER' not in previous['answer']
        assert 'PREVIOUS_RUN_ANSWER' in client.message_detail(session_id, 1)
        old_environment = environments[0]
        old_messages = copy.deepcopy(session.history)
        subscription = session.event_bus.subscribe()
        client.run(session_id, 'Write never-created.txt containing CANCEL_ME.', model='offline/offline-model')
        while subscription.get(timeout=10).type != 'permission.asked':
            pass
        assert client.abort(session_id) == {'aborted': True}
        assert session.wait(timeout=10)
        info = client.get_session(session_id)
        assert info['status'] == info['last_result']['status'] == 'cancelled'
        assert info['last_result']['answer'] == ''
        assert info['last_result']['metadata']['authored_content'] == ''
        assert provider.calls == (3 if partial else 2)
        assert 'CURRENT_RUN_PARTIAL' not in json.dumps(client.messages(session_id))
        detail = client.message_detail(session_id, 2)
        assert 'CURRENT_RUN_PARTIAL' not in detail
        assert 'PREVIOUS_RUN_ANSWER' not in detail
        assert info['pending_interaction_count'] == 0
        assert not (workspace / 'never-created.txt').exists()
        saved = _payload(workspace, session_id)
        assert saved['run_status'] == 'cancelled'
        frozen_result = copy.deepcopy(info['last_result'])
        for key in ('changed_files', 'environment_cleanup_complete', 'environment_cleanup_failures'):
            frozen_result['metadata'].pop(key, None)
        assert saved['metadata'][NATIVE_RECORD_KEY]['last_result'] == frozen_result
        assert NATIVE_RECORD_KEY not in info['last_result']['metadata']
        assert all(NATIVE_RECORD_KEY not in request for request in requests)
        settled = session.event_bus.recent(1)[0]
        assert settled.type == 'session.run.settled'
        assert settled.properties == {'status': 'cancelled', 'persisted': True}
        # 后台线程保留旧环境及旧消息，必须不能把已提交终态降回 running。
        errors = []
        def late_reporter():
            try:
                with scoped_workdir(workspace):
                    old_environment._checkpoint_messages(old_messages, 'running')
            except BaseException as exc:
                errors.append(exc)
        reporter = threading.Thread(target=late_reporter, name='late-parent-progress-reporter')
        reporter.start()
        reporter.join(timeout=3)
        assert not reporter.is_alive()
        assert not errors
        assert _payload(workspace, session_id) == saved
    finally:
        release_callback.set()
        release.set()
        for writer in writers:
            writer.join(timeout=3)
        service.shutdown()
        server.join(timeout=3)
        assert not server.is_alive()
    restored_manager = SessionManager(workspace_roots=[workspace])
    try:
        lazy = restored_manager.info(session_id)
        restored = restored_manager.get(session_id)
        assert lazy['last_result'] == restored.info()['last_result'] == frozen_result
        assert restored.attach_snapshot()['session']['last_result'] == frozen_result
        assert restored.message_detail(2) == detail
        assert _payload(workspace, session_id)['messages'] == saved['messages']
        provider.create_completion = original_completion
        provider.calls = 0
        provider.partial = False
        subscription = restored.event_bus.subscribe()
        restored.start_run('go on', model='offline/offline-model')
        while subscription.get(timeout=10).type != 'permission.asked':
            pass
        assert restored.abort() is True
        assert restored.wait(timeout=10)
        final = _payload(workspace, session_id)
        assert final['run_status'] == 'cancelled'
        assert final['metadata'][NATIVE_RECORD_KEY]['interaction_run_id'] != saved['metadata'][NATIVE_RECORD_KEY]['interaction_run_id']
        assert all(NATIVE_RECORD_KEY not in request for request in requests)
        assert NATIVE_RECORD_KEY not in final['metadata'][NATIVE_RECORD_KEY]['last_result']['metadata']
        reporter = threading.Thread(target=late_reporter, name='old-parent-after-next-run')
        reporter.start()
        reporter.join(timeout=3)
        assert not reporter.is_alive()
        assert _payload(workspace, session_id) == final
        # 使用真实快照执行已有 undo/redo，不能继续暴露历史变更前的结果。
        restored.undo()
        assert restored.info()['last_result'] == {}
        assert NATIVE_RECORD_KEY not in _payload(workspace, session_id)['metadata']
        reloaded_manager = SessionManager(workspace_roots=[workspace])
        try:
            assert reloaded_manager.info(session_id)['last_result'] == {}
        finally:
            reloaded_manager.close()
        restored.redo()
        after_redo = _payload(workspace, session_id)
        assert after_redo['messages'] == final['messages']
        assert restored.info()['last_result'] == {}
        assert NATIVE_RECORD_KEY not in after_redo['metadata']
        assert after_redo['metadata'] == {key: value for key, value in final['metadata'].items() if key != NATIVE_RECORD_KEY}
    finally:
        restored_manager.close()


def test_http_result_record_commits_at_existing_freeze_barrier(tmp_path, monkeypatch):
    from nz_coder.state import sessions
    workspace = tmp_path / 'project'
    session_id = _seed(workspace)
    entered, release = threading.Event(), threading.Event()
    original_write = sessions._publish_session_json
    def paused_write(prepared, path):
        payload = json.loads(prepared.read_text(encoding="utf-8"))
        if path.name == f'{session_id}.json' and payload.get('run_status') == 'completed':
            entered.set()
            assert release.wait(timeout=3)
        return original_write(prepared, path)
    monkeypatch.setattr(sessions, '_publish_session_json', paused_write)
    manager = _manager(workspace)
    try:
        session = manager.get(session_id)
        session.start_run('frozen answer')
        assert entered.wait(timeout=3)
        assert session.info()['status'] == 'running'
        assert session.abort() is False
        accepted = _payload(workspace, session_id)
        assert accepted['run_status'] == 'running'
        assert RECORD_KEY not in accepted['metadata']
        release.set()
        assert session.wait(timeout=3)
        terminal = _payload(workspace, session_id)
        assert terminal['run_status'] == 'completed'
        assert terminal['metadata'][RECORD_KEY]['last_result'] == session.info()['last_result']
        assert terminal['messages'] == session.history
    finally:
        release.set()
        manager.close()


def test_native_http_unverified_snapshot_survives_terminal_commit_and_reload(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace
    from tests.runtime.test_native_product_runtime import _install_runtime, _completion
    from nz_coder.runtime.session.model import SessionIdentity
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    tests=tmp_path/'tests'
    tests.mkdir()
    (tests/'test_probe.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n')
    class Provider:
        name='offline'
        calls=0
        def create_completion(self,_client,**kwargs):
            self.calls+=1
            result=_completion('Actual verification run finished')
            if self.calls==1:
                result.choices[0].message=SimpleNamespace(content='',tool_calls=[SimpleNamespace(
                    id='call-http-test',type='function',function=SimpleNamespace(name='bash',arguments=json.dumps({
                        'command':'python -m unittest discover -s tests 2>&1 | tail -8'})))])
                result.choices[0].finish_reason='tool_calls'
            return result
    provider=Provider()
    _install_runtime(monkeypatch,provider)
    manager=SessionManager(workspace_roots=[tmp_path],restore_saved=False)
    try:
        workspace_id=next(w['id'] for w in manager.list_workspaces() if w['path']==str(tmp_path))
        identity=manager.create('default',workspace_id=workspace_id)['id']
        managed=manager.get(identity)
        subscription=managed.event_bus.subscribe()
        manager.start_run(identity,'Run the tests',model='offline/offline-model')
        import time
        deadline=time.monotonic()+10
        while not managed.wait(timeout=0.02) and time.monotonic()<deadline:
            for pending in managed.interactions.list('permission'):
                assert pending['permission']=='bash'
                assert pending['tool_input']['command']=='python -m unittest discover -s tests 2>&1 | tail -8'
                managed.interactions.reply_permission(pending['id'],'once')
        assert managed.wait(timeout=0.1), managed.info()
        info=managed.info()
        assert info['status']=='completed_unverified'
        result=info['last_result']
        assert result['status']=='completed_unverified'
        assert result['metadata']['authored_content'] == 'Actual verification run finished'
        assert '1 次测试命令通过' in result['answer']
        assert '退出码 0' in result['answer']
        assert result['metadata']['authored_content'] not in result['answer']
        detail = managed.message_detail(1)
        assert '模型说明（执行声明以本轮回执为准）' in detail
        assert result['metadata']['authored_content'] in detail
        snapshot=result['metadata']['verification']
        assert snapshot['verification_needed'] is False
        assert snapshot['tests_passed'] is None
        assert snapshot['review_pending'] is True
        assert snapshot['last_verification']['status'] == 'passed'
        assert '已采信检查' in result['answer']
        assert snapshot['last_verification']['command'] in result['answer']
        assert snapshot['last_verification']['cwd'] == str(tmp_path)
        assert str(tmp_path) in result['answer']
        events=[]
        while True:
            try:
                events.append(subscription.get(timeout=0.01))
            except queue.Empty:
                break
        settled=next(e for e in events if e.type=='session.run.settled')
        assert settled.properties=={'status':'completed_unverified','persisted':True}
        durable=asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(identity),tmp_path))
        assert durable.status.value=='completed_unverified'
        assert durable.metadata['verification']==snapshot
        assert durable.metadata['authored_content'] == result['metadata']['authored_content']
        assert durable.transcript[-1]['content']==result['answer']
        calls=provider.calls
    finally:
        manager.close()
    restored=SessionManager(workspace_roots=[tmp_path])
    try:
        reloaded=restored.get(identity).info()
        assert reloaded['status']=='completed_unverified'
        frozen = copy.deepcopy(result)
        for key in ('changed_files', 'environment_cleanup_complete', 'environment_cleanup_failures'):
            frozen['metadata'].pop(key, None)
        assert reloaded['last_result']==frozen
        assert restored.get(identity).message_detail(1) == detail
        assert provider.calls==calls
    finally:
        restored.close()
