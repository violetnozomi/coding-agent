"""规范 Session 历史转换与真实终端、HTTP 交接回归。"""
import asyncio
import copy
import io
import json
from dataclasses import replace

import pytest
from rich.console import Console

from nz_coder.interface.commands.handlers import core
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.interface.session_controller import TerminalSessionController
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.state import sessions
from tests.test_session_revert import _history
from tests.test_http_result_recovery import _manager
from tests.runtime.test_native_product_runtime import _install_runtime, _request, _completion
from tests.fixtures.compaction_summary import summary_json


def _saved_http(tmp_path):
    identity = 'transition-session'
    with scoped_workdir(tmp_path):
        from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
        snapshots = WorkspaceSnapshotStore(tmp_path, sessions.session_snapshot_dir(identity))
        _, app, history = _history(tmp_path, store=snapshots, session_id=identity)
        sessions.save_session(history, session_id=identity, activate=False, require_aliases=False)
    manager = _manager(tmp_path)
    return manager, manager.get(identity), app


@pytest.mark.parametrize('operation', ['undo', 'redo'])
@pytest.mark.parametrize('after', [False, True])
def test_http_transition_confirms_canonical_save_and_compensates(tmp_path, monkeypatch, operation, after):
    manager, managed, app = _saved_http(tmp_path)
    try:
        if operation == 'redo':
            managed.undo()
        before_history = copy.deepcopy(managed.history)
        before_file = app.read_text()
        state_path = managed._reverter().state_path
        if operation == 'undo':
            sessions.write_session_runtime_json(state_path, {'previous': 'must survive failed undo'})
        before_state = state_path.read_bytes()
        with scoped_workdir(tmp_path):
            before = sessions.load_session(managed.session_id)
        publisher = sessions._publish_session_json
        def fail(prepared, path):
            if path.name == managed.session_id + '.json':
                if after:
                    publisher(prepared, path)
                raise OSError('injected canonical storage failure')
            return publisher(prepared, path)
        monkeypatch.setattr(sessions, '_publish_session_json', fail)
        if after:
            getattr(managed, operation)()
            assert app.read_text() != before_file
            assert managed.history != before_history
        else:
            with pytest.raises(Exception):
                getattr(managed, operation)()
            assert managed.history == before_history
            assert app.read_text() == before_file
            assert state_path.read_bytes() == before_state
            with scoped_workdir(tmp_path):
                assert sessions.load_session(managed.session_id) == before
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        if not after:
            getattr(managed, operation)()
        with scoped_workdir(tmp_path):
            assert sessions.load_session(managed.session_id)['messages'] == managed.history
    finally:
        manager.close()


def test_http_undo_respects_other_session_workspace_gate(tmp_path):
    manager, managed, app = _saved_http(tmp_path)
    other = None
    try:
        workspace_id = next(item['id'] for item in manager.list_workspaces() if item['path'] == str(tmp_path))
        other = manager.get(manager.create('default', workspace_id=workspace_id)['id'])
        other.start_run('wait')
        assert other.agent.entered.wait(3)
        before = copy.deepcopy(managed.history)
        with pytest.raises(Exception, match='workspace'):
            managed.undo()
        assert app.read_text() == 'after\n'
        assert managed.history == before
    finally:
        if other is not None:
            other.abort()
            assert other.wait(3)
        manager.close()


def _native(monkeypatch, tmp_path):
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from nz_coder.runtime.core.request import RunOptions
    class Provider:
        name = 'offline'
        calls = 0
        def create_completion(self, _client, **kwargs):
            self.calls += 1
            return _completion('Current answer')
    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = replace(_request(tmp_path), metadata={'permission_mode': 'auto', 'persist_session': True})
    options = RunOptions(permission_asker=lambda *_: True)
    env = build_product_run_environment(request, options)
    controller = TerminalSessionController(env)
    history = copy.deepcopy(list(request.messages))
    output = io.StringIO()
    ctx = CommandContext(history, {'id': request.session_id, 'agent': env, 'controller': controller},
                         'test', None, Console(file=output), lambda *a, **k: None)
    return provider, request, env, controller, ctx, output


@pytest.mark.parametrize('after', [False, True, None])
def test_compact_commits_once_and_next_real_run_continues(tmp_path, monkeypatch, after):
    provider, request, env, controller, ctx, output = _native(monkeypatch, tmp_path)
    async def run():
        await controller.run(ctx.history, stream=False)
        coding = provider.create_completion
        provider.create_completion = lambda _client, **kwargs: _completion(summary_json())
        original = copy.deepcopy(ctx.history)
        publisher = sessions._publish_session_json
        writes = []
        def fail(prepared, path):
            if path.name == request.session_id + '.json':
                writes.append(json.loads(prepared.read_text()))
                if after is True:
                    publisher(prepared, path)
                if after is not None:
                    raise OSError('compact save failure')
            return publisher(prepared, path)
        monkeypatch.setattr(sessions, '_publish_session_json', fail)
        if after is False:
            with pytest.raises(Exception):
                await core.handle_compact(ctx)
            assert ctx.history == original
            assert 'Context compacted' not in output.getvalue()
        else:
            await core.handle_compact(ctx)
            assert '_nz_compaction' in ctx.history[0]
            assert 'Context compacted' in output.getvalue()
            assert len(writes) == 1
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        stored = await LegacyJsonSessionStore().load(SessionIdentity(request.session_id), tmp_path)
        assert stored.transcript == ctx.history
        provider.create_completion = coding
        ctx.history.append({'role': 'user', 'content': 'continue the task'})
        await controller.run(ctx.history, stream=False)
        assert provider.calls == 2
    try:
        with scoped_workdir(tmp_path):
            asyncio.run(run())
    finally:
        env.close()


@pytest.mark.parametrize('command', ['resume', 'fork', 'model', 'connect', 'variants', 'replace', 'candidate'])
def test_pending_real_controller_rejects_replacement_before_side_effects(tmp_path, monkeypatch, command):
    provider, request, env, controller, ctx, output = _native(monkeypatch, tmp_path)
    from nz_coder.protocol.public_error import PublicRuntimeError
    publisher = sessions._publish_session_json
    def fail(prepared, path):
        data = json.loads(prepared.read_text())
        if path.name == request.session_id + '.json' and data.get('run_status') in {'completed', 'completed_unverified'}:
            raise OSError('terminal unavailable')
        return publisher(prepared, path)
    monkeypatch.setattr(sessions, '_publish_session_json', fail)
    try:
        with scoped_workdir(tmp_path):
            with pytest.raises(OSError):
                asyncio.run(controller.run(ctx.history, stream=False))
            assert env.has_pending_settlement
            old_client = controller._client
            history = copy.deepcopy(ctx.history)
            sessions.save_session([{'role': 'user', 'content': 'other'}], session_id='other', activate=False)
            before = sessions.load_session(request.session_id)
            ctx.args = {'resume': 'other', 'fork': '1', 'model': 'openai/gpt-test', 'variants': 'default'}.get(command, '')
            candidate = None
            if command == 'candidate':
                from nz_coder.runtime.execution.native_sdk import build_product_run_environment
                from nz_coder.runtime.core.request import RunOptions
                candidate = build_product_run_environment(replace(request, session_id='candidate'), RunOptions(),
                                                          activate_session_on_build=False)
            with pytest.raises(PublicRuntimeError, match='waiting for Session storage'):
                if command == 'candidate':
                    ctx.replace_agent('candidate', candidate)
                elif command == 'replace':
                    controller.replace_environment(env)
                else:
                    value = getattr(core, 'handle_' + command)(ctx)
                    if asyncio.iscoroutine(value):
                        asyncio.run(value)
            if candidate is not None:
                assert candidate.environment_cleanup_complete
            assert ctx.agent is env and controller._client is old_client
            assert ctx.history == history
            assert sessions.load_session(request.session_id) == before
            monkeypatch.setattr(sessions, '_publish_session_json', publisher)
            retry = asyncio.run(controller._client.retry_settlement(tmp_path, request.session_id))
            assert retry.state == 'committed'
            assert provider.calls == 1
    finally:
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        asyncio.run(controller._client.retry_settlement(tmp_path, request.session_id))
        env.close()


def test_fork_parent_save_failure_keeps_source_and_discards_prepared_children(tmp_path, monkeypatch):
    from tests.runtime.test_child_lifecycle_regressions import _native_source, _reference
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from nz_coder.protocol.message_schema import attach_message_identity, ensure_message_identities
    provider, request, env, controller, ctx, output = _native(monkeypatch, tmp_path)
    source, tree = _native_source(tmp_path)
    env.session_id = 'old-parent'
    ctx.session_state['id'] = 'old-parent'
    ctx.history[:] = [{'role': 'user', 'content': 'delegate task'}, _reference(source['session_id'])]
    attach_message_identity(ctx.history[1], 'msg-parent-task', session_id='old-parent')
    ctx.history[1]['_nz_parts'][0].update(id='part-task', message_id='msg-parent-task')
    ensure_message_identities(ctx.history, 'old-parent')
    original = copy.deepcopy(ctx.history)
    built = []
    def build(*args, **kwargs):
        from nz_coder.runtime.core.request import RunOptions
        candidate = build_product_run_environment(replace(request, session_id=args[2]), RunOptions(),
                                                   activate_session_on_build=kwargs['activate_session_on_build'])
        built.append(candidate)
        return candidate
    ctx.build_agent = build
    ctx.args = '1'
    publisher = sessions._publish_session_json
    def fail(prepared, path):
        if path.name.startswith('fork-'):
            raise OSError('fork parent save failed')
        return publisher(prepared, path)
    monkeypatch.setattr(sessions, '_publish_session_json', fail)
    try:
        with scoped_workdir(tmp_path):
            sessions.activate_session('old-parent')
            core.handle_fork(ctx)
            assert ctx.agent is env and ctx.session_id == 'old-parent'
            assert ctx.history == original
            assert sessions.active_session_id() == 'old-parent'
            assert len(built) == 1, output.getvalue()
            target = built[0].session_id
            assert not sessions._session_path(target).exists()
            assert not sessions.session_artifact_dir(target).exists()
            assert not subagent.list_subagent_sessions(target, tmp_path)
            assert tree.exists() and (tree / 'note.txt').read_text() == 'original'
            assert LegacyJsonSessionStore().load_session(SessionIdentity(source['session_id']), tree).transcript
            assert 'Forked turn' not in output.getvalue()
    finally:
        for candidate in built:
            candidate.close()
        env.close()


def test_serve_accepts_selected_provider_specific_key(tmp_path, monkeypatch):
    from nz_coder.http_service import cli
    from nz_coder.foundation import config
    from nz_coder.providers.models import save_model_selection
    from nz_coder.providers.configuration import provider_connection
    from nz_coder.http_service.server import SessionHTTPService
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path.parent / 'task6-user-config'))
    monkeypatch.setattr(config, 'API_KEY', '')
    monkeypatch.setattr(config, 'ANTHROPIC_API_KEY', 'offline-dummy')
    def stop(self):
        raise KeyboardInterrupt
    monkeypatch.setattr(SessionHTTPService, 'serve_forever', stop)
    with scoped_workdir(tmp_path):
        save_model_selection('anthropic', 'claude-test')
        assert provider_connection('anthropic').configured
        assert cli.serve_main(['--port', '0', '--token', 'offline-test-token']) == 0


@pytest.mark.parametrize('committed', [False, True])
def test_unknown_transition_retains_receipt_blocks_new_run_then_resolves(tmp_path, monkeypatch, committed):
    from nz_coder.runtime.session.store import SessionHistoryUnconfirmed
    manager, managed, app = _saved_http(tmp_path)
    publisher, reader = sessions._publish_session_json, sessions._read_session_payload
    unavailable = [False]
    def fail_publish(prepared, path):
        if path.name == managed.session_id + '.json':
            if committed:
                publisher(prepared, path)
            unavailable[0] = True
            raise OSError('save outcome unknown')
        return publisher(prepared, path)
    def fail_read(path, **kwargs):
        if unavailable[0] and path.name == managed.session_id + '.json':
            raise OSError('read back unavailable')
        return reader(path, **kwargs)
    monkeypatch.setattr(sessions, '_publish_session_json', fail_publish)
    monkeypatch.setattr(sessions, '_read_session_payload', fail_read)
    try:
        with pytest.raises(SessionHistoryUnconfirmed) as caught:
            managed.undo()
        assert caught.value.transition.rollback is not None
        assert app.read_text() == 'before\n'
        assert not managed._run_gate.locked()
        with pytest.raises(Exception):
            managed.start_run('must not execute')
        assert not managed._run_gate.locked()
        unavailable[0] = False
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        store = LegacyJsonSessionStore()
        lease = store.acquire(SessionIdentity(managed.session_id), tmp_path)
        lease.close_hold()
        restored = store.load_session(SessionIdentity(managed.session_id), tmp_path)
        assert len(restored.transcript) == (0 if committed else 2)
        assert app.read_text() == ('before\n' if committed else 'after\n')
        (managed.redo if committed else managed.undo)()
    finally:
        unavailable[0] = False
        manager.close()


def test_unknown_transition_conflict_never_overwrites_external_history_or_files(tmp_path, monkeypatch):
    from nz_coder.runtime.session.store import SessionHistoryUnconfirmed, _pending_history
    manager, managed, app = _saved_http(tmp_path)
    publisher = sessions._publish_session_json
    def conflict(prepared, path):
        if path.name == managed.session_id + '.json':
            payload = json.loads(prepared.read_text())
            payload['messages'] = [{'role': 'user', 'content': 'external winner'}]
            prepared.write_text(json.dumps(payload))
            publisher(prepared, path)
            raise OSError('other writer')
        return publisher(prepared, path)
    monkeypatch.setattr(sessions, '_publish_session_json', conflict)
    store = LegacyJsonSessionStore()
    identity = SessionIdentity(managed.session_id)
    key = store.ownership_key(identity, tmp_path)
    try:
        with pytest.raises(SessionHistoryUnconfirmed):
            managed.undo()
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        app.write_text('user changes after unknown save')
        with pytest.raises(SessionHistoryUnconfirmed):
            managed.undo()
        assert app.read_text() == 'user changes after unknown save'
        assert store.load_session(identity, tmp_path).transcript[0]['content'] == 'external winner'
        assert key in _pending_history
        assert not managed._run_gate.locked()
    finally:
        _pending_history.pop(key, None)
        manager.close()


def test_cli_history_transition_uses_session_lease(tmp_path, monkeypatch):
    from nz_coder.protocol.public_error import PublicRuntimeError
    _, request, env, controller, ctx, _ = _native(monkeypatch, tmp_path)
    _, app, history = _history(tmp_path, store=env.session_reverter.store, session_id=request.session_id)
    ctx.history[:] = history
    store = env.runtime_services.session_runtime.store
    try:
        with scoped_workdir(tmp_path):
            sessions.save_session(history, session_id=request.session_id, activate=False)
            lease = store.acquire(SessionIdentity(request.session_id), tmp_path)
            try:
                with pytest.raises(PublicRuntimeError) as caught:
                    core.handle_undo(ctx)
                assert caught.value.public_error.code == 'session_busy'
                before = sessions.load_session(request.session_id)
                with pytest.raises(PublicRuntimeError) as save_error:
                    core.handle_save_session(ctx)
                assert save_error.value.public_error.code == 'session_busy'
                assert sessions.load_session(request.session_id) == before
                assert app.read_text() == 'after\n'
                assert ctx.history == history
            finally:
                lease.close_hold()
            core.handle_undo(ctx)
            assert ctx.history == [] and app.read_text() == 'before\n'
            core.handle_redo(ctx)
            assert ctx.history == history and app.read_text() == 'after\n'
    finally:
        env.close()


def test_compact_first_unsaved_history_commits_and_reloads(tmp_path, monkeypatch):
    from nz_coder.state import context
    provider, request, env, controller, ctx, _ = _native(monkeypatch, tmp_path)
    monkeypatch.setattr(context, '_create_compaction_completion', lambda *a, **k: summary_json())
    try:
        with scoped_workdir(tmp_path):
            ctx.history.append({'role': 'assistant', 'content': 'first local note'})
            asyncio.run(core.handle_compact(ctx))
            saved = LegacyJsonSessionStore().load_session(SessionIdentity(request.session_id), tmp_path)
            assert saved.transcript == ctx.history
            assert '_nz_compaction' in saved.transcript[0]
            assert not saved.metadata.get('active_interaction_run_id')
            ctx.history.append({'role': 'user', 'content': 'next task'})
            asyncio.run(controller.run(ctx.history, stream=False))
            assert provider.calls == 1
    finally:
        env.close()


@pytest.mark.parametrize('after', [False, True])
def test_resume_alias_failure_never_retires_old_owner_before_confirmation(tmp_path, monkeypatch, after):
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from nz_coder.runtime.core.request import RunOptions
    _, request, env, controller, ctx, _ = _native(monkeypatch, tmp_path)
    candidate = build_product_run_environment(replace(request, session_id='resume-target'), RunOptions(),
                                              activate_session_on_build=False)
    ctx.build_agent = lambda *a, **k: candidate
    ctx.args = 'resume-target'
    publisher = sessions._publish_session_json
    try:
        with scoped_workdir(tmp_path):
            sessions.save_session([{'role':'user','content':'resumed task'}], session_id='resume-target', activate=False)
            sessions.activate_session(request.session_id)
            before = sessions._active_path().read_bytes()
            def fail(prepared, path):
                if path.name == 'active.json':
                    if after:
                        publisher(prepared, path)
                    raise OSError('alias publication failed')
                return publisher(prepared, path)
            monkeypatch.setattr(sessions, '_publish_session_json', fail)
            if after:
                core.handle_resume(ctx)
                assert ctx.agent is candidate and controller.environment is candidate
                assert sessions.active_session_id() == 'resume-target'
                assert env.environment_cleanup_complete
            else:
                with pytest.raises(OSError):
                    core.handle_resume(ctx)
                assert ctx.agent is env and controller.environment is env
                assert not env.environment_cleanup_complete
                assert candidate.environment_cleanup_complete
                assert sessions._active_path().read_bytes() == before
                assert sessions.active_session_id() == request.session_id
    finally:
        candidate.close()
        env.close()


@pytest.mark.parametrize('blocked_command', ['model', 'fork', 'save'])
def test_compact_unknown_commit_recovers_ui_before_next_controller_run(tmp_path, monkeypatch, blocked_command):
    from nz_coder.state import context
    from nz_coder.runtime.session.store import SessionHistoryUnconfirmed
    provider, request, env, controller, ctx, _ = _native(monkeypatch, tmp_path)
    summaries = []
    def summarize(*a, **k):
        summaries.append(True)
        return summary_json()
    monkeypatch.setattr(context, '_create_compaction_completion', summarize)
    publisher, reader = sessions._publish_session_json, sessions._read_session_payload
    unreadable = [False]
    def fail_publish(prepared, path):
        publisher(prepared, path)
        if path.name == request.session_id + '.json':
            unreadable[0] = True
            raise OSError('committed but storage confirmation unavailable')
    def fail_read(path, **kwargs):
        if unreadable[0] and path.name == request.session_id + '.json':
            raise OSError('read back unavailable')
        return reader(path, **kwargs)
    async def run():
        await controller.run(ctx.history, stream=False)
        before = sessions.load_session(request.session_id)
        monkeypatch.setattr(sessions, '_publish_session_json', fail_publish)
        monkeypatch.setattr(sessions, '_read_session_payload', fail_read)
        with pytest.raises(SessionHistoryUnconfirmed):
            await core.handle_compact(ctx)
        old_history = copy.deepcopy(ctx.history)
        ctx.args = 'openai/other' if blocked_command == 'model' else ''
        action = {'model': core.handle_model, 'fork': core.handle_fork,
                  'save': core.handle_save_session}[blocked_command]
        with pytest.raises(SessionHistoryUnconfirmed):
            action(ctx)
        assert ctx.agent is env and controller.environment is env
        assert ctx.history == old_history
        unreadable[0] = False
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        saved = sessions.load_session(request.session_id)
        assert saved['metadata']['_nz_terminal_result'] == before['metadata']['_nz_terminal_result']
        assert saved['metadata']['_nz_session_snapshot']['usage'] == before['metadata']['_nz_session_snapshot']['usage']
        ctx.history.append({'role': 'user', 'content': 'continue'})
        await controller.run(ctx.history, stream=False)
        assert provider.calls == 2
        assert summaries == [True]
        assert '_nz_compaction' in ctx.history[0]
        assert sum(m.get('content') == 'continue' for m in ctx.history) == 1
    try:
        with scoped_workdir(tmp_path):
            asyncio.run(run())
    finally:
        unreadable[0] = False
        env.close()


def test_compact_cancel_during_commit_keeps_confirmed_history_and_next_input(tmp_path, monkeypatch):
    import threading
    from nz_coder.state import context
    provider, request, env, controller, ctx, output = _native(monkeypatch, tmp_path)
    monkeypatch.setattr(context, '_create_compaction_completion', lambda *a, **k: summary_json())
    entered, release = threading.Event(), threading.Event()
    publisher = sessions._publish_session_json
    def pause(prepared, path):
        if path.name == request.session_id + '.json':
            entered.set()
            assert release.wait(3)
        return publisher(prepared, path)
    async def run():
        await controller.run(ctx.history, stream=False)
        monkeypatch.setattr(sessions, '_publish_session_json', pause)
        operation = asyncio.create_task(core.handle_compact(ctx))
        assert await asyncio.to_thread(entered.wait, 3)
        assert controller.cancel() is True
        release.set()
        await operation
        assert 'Context compacted' in output.getvalue()
        assert '_nz_compaction' in ctx.history[0]
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        ctx.history.append({'role':'user','content':'after cancel at commit'})
        await controller.run(ctx.history, stream=False)
        assert provider.calls == 2
    try:
        with scoped_workdir(tmp_path):
            asyncio.run(run())
    finally:
        release.set()
        env.close()


def test_compact_cancel_during_summary_keeps_original_session(tmp_path, monkeypatch):
    import threading
    provider, request, env, controller, ctx, output = _native(monkeypatch, tmp_path)
    entered, release = threading.Event(), threading.Event()
    def summarize(_client, **kwargs):
        entered.set()
        assert release.wait(3)
        return _completion(summary_json())
    async def run():
        await controller.run(ctx.history, stream=False)
        baseline = sessions.load_session(request.session_id)
        history = copy.deepcopy(ctx.history)
        provider.create_completion = summarize
        operation = asyncio.create_task(core.handle_compact(ctx))
        if not await asyncio.to_thread(entered.wait, 3):
            await operation
            pytest.fail('compaction did not reach the real model gateway')
        cancel_event = env._active_compaction_cancel_event
        assert controller.cancel() is True
        for _ in range(100):
            if cancel_event.is_set():
                break
            await asyncio.sleep(0.001)
        assert cancel_event.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert controller.cancel() is False
        assert ctx.history == history
        assert sessions.load_session(request.session_id) == baseline
        assert 'Context compacted' not in output.getvalue()
        lease = env.runtime_services.session_runtime.store.acquire(SessionIdentity(request.session_id), tmp_path)
        lease.close_hold()
    try:
        with scoped_workdir(tmp_path):
            asyncio.run(run())
    finally:
        release.set()
        env.close()


def test_cli_missing_step_snapshot_refuses_without_legacy_file_undo(tmp_path, monkeypatch):
    _, request, env, _, ctx, output = _native(monkeypatch, tmp_path)
    app = tmp_path / 'app.py'
    app.write_text('user current edit')
    try:
        with scoped_workdir(tmp_path):
            sessions.save_session(ctx.history, session_id=request.session_id, activate=False)
            before = sessions.load_session(request.session_id)
            core.handle_undo(ctx)
            assert 'Refused to undo' in output.getvalue()
            assert app.read_text() == 'user current edit'
            assert sessions.load_session(request.session_id) == before
    finally:
        env.close()


def test_redo_revert_record_delete_failure_restores_files_history_and_record(tmp_path, monkeypatch):
    from pathlib import Path
    manager, managed, app = _saved_http(tmp_path)
    try:
        managed.undo()
        state = managed._reverter().state_path
        previous = state.read_bytes()
        unlink = Path.unlink
        failed = [False]
        def fail(path, *args, **kwargs):
            if path == state and not failed[0]:
                failed[0] = True
                raise PermissionError('cannot delete revert record')
            return unlink(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', fail)
        with pytest.raises(Exception, match='rolled back'):
            managed.redo()
        assert state.read_bytes() == previous
        assert app.read_text() == 'before\n'
        assert managed.history == []
        managed.redo()
        assert app.read_text() == 'after\n'
        assert len(managed.history) == 2
    finally:
        manager.close()


def test_cancel_during_history_recovery_adopts_confirmed_compact_before_retry(tmp_path, monkeypatch):
    import threading
    from nz_coder.state import context
    from nz_coder.runtime.session.store import SessionHistoryUnconfirmed, _pending_history
    provider, request, env, controller, ctx, _ = _native(monkeypatch, tmp_path)
    summaries = []
    def summarize(*args, **kwargs):
        summaries.append(True)
        return summary_json()
    monkeypatch.setattr(context, '_create_compaction_completion', summarize)
    publisher, reader = sessions._publish_session_json, sessions._read_session_payload
    unreadable = [False]
    blocking = [False]
    entered, release = threading.Event(), threading.Event()
    def fail_publish(prepared, path):
        publisher(prepared, path)
        if path.name == request.session_id + '.json':
            unreadable[0] = True
            raise OSError('commit confirmation unavailable')
    def read(path, **kwargs):
        if path.name == request.session_id + '.json':
            if unreadable[0]:
                raise OSError('confirmation unavailable')
            if blocking[0]:
                entered.set()
                assert release.wait(3)
        return reader(path, **kwargs)
    async def run():
        await controller.run(ctx.history, stream=False)
        monkeypatch.setattr(sessions, '_publish_session_json', fail_publish)
        monkeypatch.setattr(sessions, '_read_session_payload', read)
        with pytest.raises(SessionHistoryUnconfirmed):
            await controller.compact(ctx.history)
        monkeypatch.setattr(sessions, '_publish_session_json', publisher)
        unreadable[0], blocking[0] = False, True
        ctx.history.append({'role': 'user', 'content': 'keep the new request'})
        operation = asyncio.create_task(controller.run(ctx.history, stream=False))
        assert await asyncio.to_thread(entered.wait, 3)
        operation.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        blocking[0] = False
        assert '_nz_compaction' in ctx.history[0]
        assert sum(row.get('content') == 'keep the new request' for row in ctx.history) == 1
        assert provider.calls == 1
        await controller.run(ctx.history, stream=False)
        assert provider.calls == 2 and summaries == [True]
    try:
        with scoped_workdir(tmp_path):
            asyncio.run(run())
    finally:
        release.set()
        _pending_history.pop(env.runtime_services.session_runtime.store.ownership_key(
            SessionIdentity(request.session_id), tmp_path), None)
        env.close()


@pytest.mark.parametrize('after', [False, True])
def test_model_alias_publication_keeps_selection_and_environment_consistent(tmp_path, monkeypatch, after):
    from nz_coder.providers.models import active_model_selection, save_model_selection
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from nz_coder.runtime.core.request import RunOptions
    _, request, env, controller, ctx, output = _native(monkeypatch, tmp_path)
    candidates = []
    template = env.model_runtime
    def resolve(selection):
        return replace(template, provider_id=selection.provider_name, model_id=selection.model_id,
                       request_model_id=selection.model_id)
    monkeypatch.setattr('nz_coder.runtime.execution.native_sdk.resolve_model_runtime', resolve)
    monkeypatch.setattr('nz_coder.runtime.execution.loop.resolve_model_runtime', resolve)
    def build(*args, **kwargs):
        selection = active_model_selection()
        candidate = build_product_run_environment(
            replace(request, provider=selection.provider, model=selection.model_id), RunOptions(),
            activate_session_on_build=kwargs['activate_session_on_build'])
        candidates.append(candidate)
        return candidate
    ctx.build_agent, ctx.args = build, 'openai/next-model'
    publisher = sessions._publish_session_json
    try:
        with scoped_workdir(tmp_path):
            save_model_selection('openai', 'offline-model')
            previous = active_model_selection()
            # 原 alias 是构造时的空投影，候选发布则读取新保存的规范历史。
            sessions.save_session(ctx.history, session_id=request.session_id, activate=False,
                                  require_aliases=False)
            before = sessions._active_path().read_bytes()
            def fail(prepared, path):
                if path.name == 'active.json':
                    if after:
                        publisher(prepared, path)
                    raise OSError('model alias publication failed')
                return publisher(prepared, path)
            monkeypatch.setattr(sessions, '_publish_session_json', fail)
            core.handle_model(ctx)
            if after:
                assert ctx.agent is candidates[0] and controller.environment is candidates[0]
                assert active_model_selection().model_id == candidates[0].model_id == 'next-model'
                assert env.environment_cleanup_complete
            else:
                assert active_model_selection() == previous
                assert ctx.agent is env and controller.environment is env
                assert candidates[0].environment_cleanup_complete
                assert not env.environment_cleanup_complete
                assert sessions._active_path().read_bytes() == before
                assert 'Switched model' not in output.getvalue()
            assert sessions.active_session_id() == request.session_id
    finally:
        for candidate in candidates:
            candidate.close()
        env.close()
