"""仅位于审查工件目录的真实恢复交接探针。"""
import hashlib
import json
import socket
import threading

import pytest

from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
from nz_coder.state.sessions import session_runtime_state_path, session_dir


def test_missing_domain_retains_current_strict_policy(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain
    from nz_coder.runtime.verification.domain.models import RunScope, TaskScope, WorkspaceVersion
    scope = RunScope(TaskScope('s', 't', str(tmp_path)), 'r', 'a')
    domain = restore_domain(None, lock=threading.RLock(), scope=scope,
                            version=WorkspaceVersion(1, 1, 1), require_targeted=True)
    assert domain.requires_targeted
    assert domain.snapshot().targeted_pending


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    for name in ('XDG_STATE_HOME', 'XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'LOCALAPPDATA'):
        monkeypatch.setenv(name, str(tmp_path.parent / (tmp_path.name + '-' + name.lower())))
    for name in ('REFLECTION_ENABLED', 'PLANNING_ENABLED', 'LSP_WRITE_DIAGNOSTICS_ENABLED'):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(socket.socket, 'connect', lambda *_a, **_k: (_ for _ in ()).throw(AssertionError('network forbidden')))


@pytest.mark.parametrize('legacy,damage', [(False, 'json'), (True, 'schema')])
def test_disk_restore_does_not_finalize_or_replace(tmp_path, monkeypatch, legacy, damage):
    from test_node_verification_runtime import _run
    evidence, finalized = {}, []
    original_finalize = SessionRuntime.finalize
    async def observe_finalize(self, context, status):
        finalized.append(status.value)
        return await original_finalize(self, context, status)
    monkeypatch.setattr(SessionRuntime, 'finalize', observe_finalize)
    build = native_sdk.build_product_run_environment
    def seed(request, options):
        environment = build(request, options)
        with scoped_workdir(request.workspace):
            path = session_runtime_state_path(request.session_id)
        environment.runtime_state.save(path, active=True)
        if damage == 'json':
            path.write_text('{"verification_domain":')
        else:
            raw = json.loads(path.read_text())
            raw['verification_domain'] = {'schema': 'broken'}
            path.write_text(json.dumps(raw))
        evidence.update(path=path, before=hashlib.sha256(path.read_bytes()).hexdigest())
        return environment
    monkeypatch.setattr(native_sdk, 'build_product_run_environment', seed)
    if legacy:
        async def legacy_run(self, request, options):
            return await self._environment.run(list(request.messages), stream=False)
        monkeypatch.setattr(native_sdk.NativeSDKRunner, 'run_result', legacy_run)
    caught = None
    try:
        _run(monkeypatch, tmp_path, [], task='Continue the previous task.', limit=1)
    except BaseException as exc:
        caught = exc
    assert 'path' in evidence, repr(caught)
    same = hashlib.sha256(evidence['path'].read_bytes()).hexdigest() == evidence['before']
    print(json.dumps({'legacy': legacy, 'damage': damage, 'exception': type(caught).__name__, 'message': str(caught), 'finalized': finalized, 'runtime_unchanged': same}))
    assert isinstance(caught, CheckpointRestoreError)
    assert not finalized
    assert same


def test_http_managed_activation_preserves_session_on_bad_checkpoint(tmp_path, monkeypatch):
    from nz_coder.http_service.manager import SessionManager
    import nz_coder.http_service.manager as manager_module
    from test_native_product_runtime import _install_runtime
    class Provider:
        name = 'offline'
        calls = 0
        def create_completion(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError('model must not run')
    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    monkeypatch.setattr('nz_coder.runtime.model_gateway.resolve_model_runtime', lambda *_a, **_k: runtime)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    session_id = manager.create('default', workspace_id=manager.workspaces.id_for(workspace))['id']
    session = manager.get(session_id)
    from nz_coder.state.sessions import save_session
    session.history = [{'role': 'user', 'content': 'Previous task.'}]
    with scoped_workdir(workspace):
        save_session(session.history, session_id=session_id, activate=False, require_aliases=False)
        authority = session_dir() / (session_id + '.json')
        runtime_path = session_runtime_state_path(session_id)
    state = RuntimeState()
    state.save(runtime_path, active=True)
    raw = json.loads(runtime_path.read_text())
    raw['verification_domain'] = {'schema': 'broken'}
    runtime_path.write_text(json.dumps(raw))
    before = authority.read_bytes()
    runtime_before = runtime_path.read_bytes()
    saves = []
    save = manager_module.save_session
    def observe_save(*args, **kwargs):
        saves.append(kwargs.get('run_status'))
        return save(*args, **kwargs)
    monkeypatch.setattr(manager_module, 'save_session', observe_save)
    try:
        from nz_coder.protocol.public_error import to_public_error
        with pytest.raises(CheckpointRestoreError) as caught:
            session.start_run('Continue the previous task.', model='offline/offline-model')
        assert to_public_error(caught.value).code == 'verification_restore_failed'
        assert not saves
        assert provider.calls == 0
        assert authority.read_bytes() == before
        assert runtime_path.read_bytes() == runtime_before
    finally:
        session.dispose(force=True)


@pytest.mark.parametrize('raw', [b'{', b'\xff', b'[]', b'{}', b'{"active":"yes"}'])
def test_invalid_runtime_envelope_is_not_missing(tmp_path, raw):
    path = tmp_path / 'runtime.json'
    path.write_bytes(raw)
    with pytest.raises(CheckpointRestoreError):
        RuntimeState().load(path)
    assert path.read_bytes() == raw


def test_public_recovery_error_has_safe_cli_projection():
    from nz_coder.protocol.public_error import to_public_error, format_public_error
    error = CheckpointRestoreError('private filesystem details')
    assert to_public_error(error).code == 'verification_restore_failed'
    assert 'private filesystem details' not in format_public_error(error)
    assert 'verification' in format_public_error(error).lower()


def test_http_file_changes_after_preflight_do_not_save_failed_session(tmp_path, monkeypatch):
    from nz_coder.http_service.manager import SessionManager
    import nz_coder.http_service.manager as manager_module
    import nz_coder.runtime.adapters.verification_runtime as adapter
    from test_native_product_runtime import _install_runtime
    class Provider:
        name = 'offline'
        calls = 0
        def create_completion(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError('model must not run')
    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    monkeypatch.setattr('nz_coder.runtime.model_gateway.resolve_model_runtime', lambda *_a, **_k: runtime)
    workspace = tmp_path / 'project'
    workspace.mkdir()
    manager = SessionManager(workspace_roots=[workspace], restore_saved=False)
    session_id = manager.create('default', workspace_id=manager.workspaces.id_for(workspace))['id']
    session = manager.get(session_id)
    session.history = [{'role': 'user', 'content': 'Previous task.'}]
    with scoped_workdir(workspace):
        path = session_runtime_state_path(session_id)
    RuntimeState().save(path, active=True)
    preflight = adapter.preflight_runtime_restore
    def change_after_preflight(**kwargs):
        preflight(**kwargs)
        raw = json.loads(path.read_text())
        raw['verification_domain'] = {'schema': 'broken'}
        path.write_text(json.dumps(raw))
    monkeypatch.setattr(adapter, 'preflight_runtime_restore', change_after_preflight)
    saves = []
    save = manager_module.save_session
    def observe_save(*args, **kwargs):
        saves.append(kwargs.get('run_status'))
        return save(*args, **kwargs)
    monkeypatch.setattr(manager_module, 'save_session', observe_save)
    try:
        session.start_run('Continue the previous task.', model='offline/offline-model')
        assert session.wait(10)
        assert provider.calls == 0
        assert saves == ['running']
        assert session.last_error == 'Verification checkpoint restore failed.'
        assert session.history[-1]['content'] == 'Continue the previous task.'
    finally:
        session.dispose(force=True)


def test_runtime_load_missing_inactive_and_unreadable(tmp_path):
    path = tmp_path / 'runtime.json'
    state = RuntimeState()
    assert not state.load(path)
    state.save(path, active=False)
    assert not RuntimeState().load(path)
    assert RuntimeState().load(path, allow_inactive=True)
    path.unlink()
    path.mkdir()
    with pytest.raises(CheckpointRestoreError):
        RuntimeState().load(path)


def test_preflight_respects_replacement_and_inactive_activation(tmp_path):
    from types import SimpleNamespace
    from nz_coder.runtime.adapters.verification_runtime import preflight_runtime_restore
    from nz_coder.runtime.core.execution_context import command_activation
    session_id = 'activation-policy'
    with scoped_workdir(tmp_path):
        path = session_runtime_state_path(session_id)
    state = RuntimeState()
    state.save(path, active=False)
    raw = json.loads(path.read_text())
    raw['verification_domain'] = {'schema': 'broken'}
    path.write_text(json.dumps(raw))
    settings = SimpleNamespace(runtime_state_persist=True)
    first = command_activation([{'role': 'user', 'content': 'Initial task.'}])
    preflight_runtime_restore(session_id=session_id, workspace=tmp_path, activation=first, settings=settings)
    replacement = command_activation([{'role': 'user', 'content': 'Old task.'},
        {'role': 'assistant', 'content': 'Old response.'}, {'role': 'user', 'content': 'New task: inspect this.'}])
    preflight_runtime_restore(session_id=session_id, workspace=tmp_path, activation=replacement, settings=settings)
    followup = command_activation([{'role': 'user', 'content': 'Old task.'},
        {'role': 'assistant', 'content': 'Old response.'}, {'role': 'user', 'content': 'Continue that work.'}])
    with pytest.raises(CheckpointRestoreError):
        preflight_runtime_restore(session_id=session_id, workspace=tmp_path, activation=followup, settings=settings)
    settings.runtime_state_persist = False
    preflight_runtime_restore(session_id=session_id, workspace=tmp_path, activation=followup, settings=settings)


def test_recovery_error_http_and_cli_public_rendering():
    from types import SimpleNamespace
    from nz_coder.http_service.server import _SessionRequestHandler
    from nz_coder.interface.run_renderer import TerminalRunRenderer
    error = CheckpointRestoreError('private path')
    rendered = []
    handler = _SessionRequestHandler.__new__(_SessionRequestHandler)
    handler.path = '/session/example/run'
    handler._authorize = lambda: None
    handler._route_session = lambda *_args: (_ for _ in ()).throw(error)
    handler._error = lambda status, code, message: rendered.append((status, code, message))
    handler._dispatch('POST')
    assert rendered[0][1:] == ('verification_restore_failed', 'Verification checkpoint restore failed.')
    cli = SimpleNamespace(_terminal_seen=False, drain=lambda: None, close=lambda: None,
        _with_stream_paused=lambda callback: callback(),
        _render_run_end=lambda status, message: rendered.append((status, message)))
    TerminalRunRenderer.fail(cli, error)
    assert rendered[-1] == ('failed', 'Verification checkpoint restore failed.')
