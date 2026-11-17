"""公开 SDK 与 HTTP 的原件写入共用会话运行权。"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
import threading

import pytest

from nz_coder.http_service import SessionManager
from nz_coder.protocol.public_error import PublicRuntimeError
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.state.sessions import _session_path, load_session
from nz_coder.state.workdir import scoped_workdir
from tests.runtime.session.test_session_ownership import request
from tests.test_http_service import FakeAgent


def test_busy_http_start_never_rewrites_the_other_owners_checkpoint(tmp_path):
    manager = SessionManager(agent_factory=FakeAgent, workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))['id'])
    runtime = SessionRuntime(LegacyJsonSessionStore())
    context = asyncio.run(runtime.open(request(tmp_path, session_id=session.session_id)))
    asyncio.run(runtime.checkpoint(context))
    with scoped_workdir(tmp_path):
        path = _session_path(session.session_id)
        before = path.read_bytes()
    try:
        with pytest.raises(PublicRuntimeError, match='in progress|busy'):
            session.start_run('must not overwrite')
        assert path.read_bytes() == before
    finally:
        asyncio.run(runtime.finalize(context, RunStatus.CANCELLED))
        manager.close()


def test_http_reloads_completed_external_history_before_appending_input(tmp_path):
    manager = SessionManager(agent_factory=FakeAgent, workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))['id'])
    runtime = SessionRuntime(LegacyJsonSessionStore())
    context = asyncio.run(runtime.open(request(tmp_path, session_id=session.session_id)))
    context.transcript.append({'role': 'assistant', 'content': 'external answer must survive'})
    asyncio.run(runtime.finalize(context, RunStatus.COMPLETED_UNVERIFIED))
    try:
        session.start_run('next request')
        assert session.wait(5)
        with scoped_workdir(tmp_path):
            saved = load_session(session.session_id)
        assert any(m['content'] == 'external answer must survive' for m in saved['messages'])
        assert saved['messages'][-1]['content'] == 'answer:next request'
    finally:
        manager.close()


def _offline_provider(monkeypatch, provider):
    from tests.runtime.test_native_product_runtime import _runtime
    from nz_coder.foundation import config

    def resolve(*_args, **_kwargs):
        return replace(_runtime(), provider=provider)
    monkeypatch.setattr('nz_coder.runtime.execution.native_sdk.resolve_model_runtime', resolve)
    monkeypatch.setattr('nz_coder.runtime.execution.loop.resolve_model_runtime', resolve)
    monkeypatch.setattr(config, 'PLANNING_ENABLED', False)


def test_two_default_sdk_clients_reject_second_before_model_and_preserve_answer(tmp_path, monkeypatch):
    from nz_coder.sdk import AgentClient
    from tests.runtime.test_native_product_runtime import _completion

    entered, release = threading.Event(), threading.Event()
    calls = []
    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            calls.append(copy.deepcopy(kwargs['messages']))
            entered.set()
            assert release.wait(5)
            return _completion('original answer')
    _offline_provider(monkeypatch, Provider())
    req = replace(request(tmp_path), provider='offline', model='offline-model',
        metadata={'permission_mode': 'auto', 'persist_session': True})
    async def scenario():
        first = asyncio.create_task(AgentClient().run(req))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            with pytest.raises(PublicRuntimeError, match='in progress|busy'):
                await AgentClient().run(req)
        finally:
            release.set()
            result = await first
        assert 'original answer' in result.final_text
        assert len(calls) == 1
        with scoped_workdir(tmp_path):
            assert 'original answer' in load_session(req.session_id)['messages'][-1]['content']
    asyncio.run(scenario())


def test_failure_before_execution_context_factory_releases_session(tmp_path, monkeypatch):
    from nz_coder.sdk import AgentClient
    from nz_coder.tools.scratchpad import Scratchpad

    _offline_provider(monkeypatch, object())
    def fail(*_args, **_kwargs):
        raise ValueError('scratchpad preparation failed')
    monkeypatch.setattr(Scratchpad, 'for_user_message', fail)
    req = replace(request(tmp_path), provider='offline', model='offline-model',
        metadata={'permission_mode': 'auto', 'persist_session': True})
    with pytest.raises(ValueError, match='scratchpad preparation failed'):
        asyncio.run(AgentClient().run(req))
    lease = LegacyJsonSessionStore().acquire(SessionIdentity(req.session_id), tmp_path)
    lease.close_hold()


def test_native_http_commits_once_and_returns_storage_busy_as_conflict(tmp_path, monkeypatch):
    from nz_coder.http_service import SessionHTTPService, NZCoderClient, NZCoderHTTPError
    from nz_coder.http_service import manager as manager_module
    from tests.runtime.test_native_product_runtime import _completion

    class Provider:
        name = 'offline'
        def create_completion(self, _client, **_kwargs):
            return _completion('native answer')
    _offline_provider(monkeypatch, Provider())
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))['id'])
    saves = []
    save = manager_module.save_session
    def record(*args, **kwargs):
        saves.append(kwargs.get('run_status'))
        return save(*args, **kwargs)
    monkeypatch.setattr(manager_module, 'save_session', record)
    try:
        session.start_run('inspect', model='offline/offline-model')
        assert session.wait(5)
        assert session.status == 'completed_unverified'
        assert saves == ['running'], '包装层不能在原件提交后再写整份历史'
        with scoped_workdir(tmp_path):
            path = _session_path(session.session_id)
        before = path.read_bytes()
        lease = LegacyJsonSessionStore().acquire(SessionIdentity(session.session_id), tmp_path)
        try:
            service = SessionHTTPService(port=0, manager=manager)
            thread = threading.Thread(target=service.serve_forever)
            thread.start()
            try:
                client = NZCoderClient(service.base_url, service.token, timeout=3)
                with pytest.raises(NZCoderHTTPError) as caught:
                    client.run(session.session_id, 'busy request')
                assert caught.value.status == 409
                assert path.read_bytes() == before
            finally:
                service.shutdown()
                thread.join(3)
        finally:
            lease.close_hold()
    finally:
        manager.close()


def test_explicit_sdk_factory_passes_outer_lease_to_actual_facade(tmp_path, monkeypatch):
    from nz_coder.sdk import AgentClient
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from tests.runtime.test_native_product_runtime import _completion

    class Provider:
        name = 'offline'
        def create_completion(self, _client, **_kwargs):
            return _completion('factory answer')
    _offline_provider(monkeypatch, Provider())
    req = replace(request(tmp_path), provider='offline', model='offline-model',
        metadata={'permission_mode': 'auto', 'persist_session': True})
    environment = build_product_run_environment(req, RunOptions())
    lease = LegacyJsonSessionStore().acquire(SessionIdentity(req.session_id), tmp_path, outer_hold=True)
    try:
        result = asyncio.run(AgentClient(agent_factory=lambda _request: environment).run(req, session_lease=lease))
        assert 'factory answer' in result.final_text
        assert lease.active
    finally:
        lease.close_hold()
        environment.close()


def test_http_thread_setup_failure_releases_unclaimed_hold_and_workspace_gate(tmp_path, monkeypatch):
    from contextlib import contextmanager
    from nz_coder.foundation import workspace_trust

    original = workspace_trust.scoped_config_snapshot
    @contextmanager
    def fail_thread_setup(snapshot):
        if threading.current_thread().name.startswith('nz-http-'):
            raise ValueError('controlled thread preparation failure')
        with original(snapshot):
            yield
    manager = SessionManager(agent_factory=FakeAgent, workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))['id'])
    monkeypatch.setattr(workspace_trust, 'scoped_config_snapshot', fail_thread_setup)
    try:
        session.start_run('fail setup')
        assert session.wait(3)
        assert session.status == 'failed'
        lease = LegacyJsonSessionStore().acquire(SessionIdentity(session.session_id), tmp_path)
        lease.close_hold()
        monkeypatch.setattr(workspace_trust, 'scoped_config_snapshot', original)
        session.start_run('retry normally')
        assert session.wait(3)
        assert session.last_result['answer'] == 'answer:retry normally'
    finally:
        manager.close()


def test_legacy_http_pending_storage_preserves_original_and_retries_before_next_run(tmp_path, monkeypatch):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution.native_sdk import build_product_run_environment
    from tests.runtime.test_native_product_runtime import _completion

    calls = []
    class Provider:
        name = 'offline'
        def create_completion(self, _client, **_kwargs):
            calls.append('called')
            return _completion('frozen original answer')
    _offline_provider(monkeypatch, Provider())
    prepare = LegacyJsonSessionStore.prepare_terminal
    fail = True
    async def fail_save(self, session):
        write = await prepare(self, session)
        async def save(candidate):
            if fail:
                raise OSError('controlled terminal storage failure')
            await write.save(candidate)
        return replace(write, save=save)
    monkeypatch.setattr(LegacyJsonSessionStore, 'prepare_terminal', fail_save)
    def factory(session_id, mode):
        req = replace(request(tmp_path), session_id=session_id, provider='offline', model='offline-model',
            metadata={'permission_mode': mode, 'persist_session': True})
        return build_product_run_environment(req, RunOptions())
    manager = SessionManager(agent_factory=factory, workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))['id'])
    try:
        session.start_run('inspect')
        assert session.wait(5)
        assert session.agent.has_pending_settlement
        assert session._client_settlement_pending()
        with scoped_workdir(tmp_path):
            canonical = load_session(session.session_id)
        assert canonical['run_status'] == 'running'
        assert '_nz_http_terminal_result' not in canonical['metadata']
        fail = False
        session._settle_previous_result()
        assert not session.agent.has_pending_settlement
        assert session.status == 'completed_unverified'
        assert len(calls) == 1, '保存重试不能重新调用模型'
        session.start_run('next request')
        assert session.wait(5)
        assert len(calls) == 2
        assert session.status == 'completed_unverified'
    finally:
        # 失败复现也应允许测试环境退出；不改写原候选。
        fail = False
        asyncio.run(session.agent.retry_settlement())
        writer = session.agent._session_checkpoint_writer
        if writer is not None:
            writer.owner._session_lease.finish()
        manager.close()


def test_repeated_cancel_at_writer_retirement_keeps_explicit_settlement_owner(tmp_path, monkeypatch):
    import queue
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion

    class Provider:
        name = 'offline'
        def create_completion(self, _client, **_kwargs):
            return _completion('retiring answer')
    _offline_provider(monkeypatch, Provider())
    original_build = native_sdk.build_product_run_environment
    entered, releases, captured = queue.Queue(), [], []
    def build(*args, **kwargs):
        env = original_build(*args, **kwargs)
        captured.append(env)
        retire = env._retire_session_checkpoints
        def pause(writer):
            release = threading.Event()
            releases.append(release)
            entered.put(release)
            assert release.wait(5)
            return retire(writer)
        env._retire_session_checkpoints = pause
        return env
    monkeypatch.setattr(native_sdk, 'build_product_run_environment', build)
    req = replace(request(tmp_path), provider='offline', model='offline-model',
        metadata={'permission_mode': 'auto', 'persist_session': True})
    runner = native_sdk.NativeSDKRunner()
    async def scenario():
        task = asyncio.create_task(runner.run_result(req))
        try:
            for _ in range(3):
                release = await asyncio.to_thread(entered.get, True, 3)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            owner = captured[0]._session_checkpoint_writer.owner
            if not owner.finalized:
                assert captured[0].has_pending_settlement
                assert runner._pending_environments
                assert (await runner.retry_settlement(tmp_path, req.session_id)).state == 'committed'
            assert owner.finalized
            assert not owner._session_lease.active
            next_owner = LegacyJsonSessionStore().acquire(SessionIdentity(req.session_id), tmp_path)
            next_owner.close_hold()
        finally:
            for release in releases:
                release.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if captured and captured[0]._session_checkpoint_writer:
                captured[0]._session_checkpoint_writer.owner._session_lease.finish()
    asyncio.run(scenario())
