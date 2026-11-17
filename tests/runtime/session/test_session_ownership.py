"""会话运行权覆盖加载到结算，独立客户端不能静默覆盖同一历史。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import subprocess
import sys

import pytest

from nz_coder.protocol.public_error import PublicRuntimeError
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import EphemeralSessionStore, LegacyJsonSessionStore


def request(root, *, session_id='shared'):
    return RunRequest(agent=AgentDefinition('worker', 'Inspect the project.'), profile=MAIN_PROFILE,
        messages=({'role': 'user', 'content': 'Investigate current files.'},), workspace=root,
        session_id=session_id, stream=False)


def test_distinct_json_owners_reject_overlap_before_history_can_be_overwritten(tmp_path):
    async def scenario():
        a, b = SessionRuntime(LegacyJsonSessionStore()), SessionRuntime(LegacyJsonSessionStore())
        context = await a.open(request(tmp_path))
        try:
            with pytest.raises(PublicRuntimeError, match='in progress|busy'):
                await b.open(request(tmp_path))
        finally:
            await a.finalize(context, RunStatus.CANCELLED)
        resumed = await b.open(request(tmp_path))
        await b.finalize(resumed, RunStatus.CANCELLED)
    asyncio.run(scenario())


def test_different_sessions_remain_parallel(tmp_path):
    async def scenario():
        a, b = SessionRuntime(LegacyJsonSessionStore()), SessionRuntime(LegacyJsonSessionStore())
        first = await a.open(request(tmp_path, session_id='one'))
        second = await b.open(request(tmp_path, session_id='two'))
        await asyncio.gather(a.finalize(first, RunStatus.CANCELLED), b.finalize(second, RunStatus.CANCELLED))
    asyncio.run(scenario())


def test_ephemeral_owners_follow_the_actual_shared_store(tmp_path):
    async def scenario():
        shared = EphemeralSessionStore()
        a, b, isolated = SessionRuntime(shared), SessionRuntime(shared), SessionRuntime(EphemeralSessionStore())
        first = await a.open(request(tmp_path))
        separate = await isolated.open(request(tmp_path))
        try:
            with pytest.raises(PublicRuntimeError):
                await b.open(request(tmp_path))
        finally:
            await a.finalize(first, RunStatus.CANCELLED)
            await isolated.finalize(separate, RunStatus.CANCELLED)
    asyncio.run(scenario())


def test_load_failure_releases_the_original_acquisition(tmp_path):
    class BrokenStore(LegacyJsonSessionStore):
        async def load(self, *_args):
            raise ValueError('corrupt original')
    async def scenario():
        with pytest.raises(ValueError, match='corrupt original'):
            await SessionRuntime(BrokenStore()).open(request(tmp_path))
        runtime = SessionRuntime(LegacyJsonSessionStore())
        context = await runtime.open(request(tmp_path))
        await runtime.finalize(context, RunStatus.CANCELLED)
    asyncio.run(scenario())


def test_failed_terminal_save_keeps_lease_for_original_retry(tmp_path):
    class FailingStore(LegacyJsonSessionStore):
        fail = True
        def _save_sync(self, session):
            if self.fail:
                raise OSError('controlled disk error')
            return super()._save_sync(session)
        async def prepare_terminal(self, session):
            write = await super().prepare_terminal(session)
            original = write.save
            async def save(current):
                if self.fail:
                    raise OSError('controlled disk error')
                await original(current)
            return replace(write, save=save)
    async def scenario():
        store = FailingStore()
        a, b = SessionRuntime(store), SessionRuntime(LegacyJsonSessionStore())
        context = await a.open(request(tmp_path))
        with pytest.raises(OSError, match='controlled disk'):
            await a.finalize(context, RunStatus.CANCELLED)
        try:
            with pytest.raises(PublicRuntimeError):
                await b.open(request(tmp_path))
        finally:
            store.fail = False
            await a.finalize(context, RunStatus.CANCELLED)
        resumed = await b.open(request(tmp_path))
        await b.finalize(resumed, RunStatus.CANCELLED)
    asyncio.run(scenario())


def test_outer_hold_survives_commit_and_closes_after_pending_retry(tmp_path):
    async def scenario():
        identity = SessionIdentity('shared')
        store = LegacyJsonSessionStore()
        lease = store.acquire(identity, tmp_path, outer_hold=True)
        runtime = SessionRuntime(store)
        context = await runtime.open(request(tmp_path), session_lease=lease)
        await runtime.finalize(context, RunStatus.CANCELLED)
        other = LegacyJsonSessionStore()
        with pytest.raises(PublicRuntimeError):
            other.acquire(identity, tmp_path)
        lease.close_hold()
        next_lease = other.acquire(identity, tmp_path)
        # 旧句柄再次关闭不能释放后来的 owner。
        lease.close_hold()
        with pytest.raises(PublicRuntimeError):
            store.acquire(identity, tmp_path)
        next_lease.close_hold()
    asyncio.run(scenario())


def test_same_explicit_token_cannot_be_claimed_by_two_runs(tmp_path):
    async def scenario():
        store = LegacyJsonSessionStore()
        lease = store.acquire(SessionIdentity('shared'), tmp_path, outer_hold=True)
        first = await SessionRuntime(store).open(request(tmp_path), session_lease=lease)
        try:
            with pytest.raises(PublicRuntimeError):
                await SessionRuntime(store).open(request(tmp_path), session_lease=lease)
        finally:
            await SessionRuntime(store).finalize(first, RunStatus.CANCELLED)
            lease.close_hold()
    asyncio.run(scenario())


def test_process_lease_blocks_and_os_exit_releases_it(tmp_path):
    store = LegacyJsonSessionStore()
    lease = store.acquire(SessionIdentity('shared'), tmp_path, outer_hold=True)
    code = """
import sys
from pathlib import Path
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.protocol.public_error import PublicRuntimeError
try:
    held = LegacyJsonSessionStore().acquire(SessionIdentity('shared'), Path(sys.argv[1]))
except PublicRuntimeError:
    print('busy')
else:
    print('acquired')
"""
    try:
        first = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True, timeout=10)
        assert first.returncode == 0, first.stderr
        assert first.stdout.strip() == 'busy'
    finally:
        lease.close_hold()
    second = subprocess.run([sys.executable, '-c', code, str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == 'acquired'
    after_exit = store.acquire(SessionIdentity('shared'), tmp_path)
    after_exit.close_hold()


def test_cancelled_rollback_waits_for_writer_retirement_then_releases(tmp_path):
    import threading
    from types import SimpleNamespace
    from nz_coder.runtime.execution.runner import AgentRunner

    async def scenario():
        runtime = SessionRuntime(LegacyJsonSessionStore())
        context = await runtime.open(request(tmp_path))
        entered, release = threading.Event(), threading.Event()
        def retire(_context):
            entered.set()
            assert release.wait(5)
        runner = AgentRunner(SimpleNamespace(session_runtime=runtime), context_factory_abort=retire)
        cleanup = asyncio.create_task(runner._abort_preparation(context))
        assert await asyncio.to_thread(entered.wait, 2)
        cleanup.cancel()
        await asyncio.sleep(0)
        cleanup.cancel()
        with pytest.raises(PublicRuntimeError):
            LegacyJsonSessionStore().acquire(SessionIdentity('shared'), tmp_path)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        next_owner = LegacyJsonSessionStore().acquire(SessionIdentity('shared'), tmp_path)
        next_owner.close_hold()
    asyncio.run(scenario())


def test_outer_hold_closed_before_failed_commit_retry_does_not_leak(tmp_path):
    class Store(LegacyJsonSessionStore):
        fail = True
        async def prepare_terminal(self, session):
            write = await super().prepare_terminal(session)
            async def save(candidate):
                if self.fail:
                    raise OSError('temporary write failure')
                await write.save(candidate)
            return replace(write, save=save)
    async def scenario():
        store = Store()
        lease = store.acquire(SessionIdentity('shared'), tmp_path, outer_hold=True)
        runtime = SessionRuntime(store)
        context = await runtime.open(request(tmp_path), session_lease=lease)
        with pytest.raises(OSError):
            await runtime.finalize(context, RunStatus.CANCELLED)
        lease.close_hold()
        with pytest.raises(PublicRuntimeError):
            LegacyJsonSessionStore().acquire(SessionIdentity('shared'), tmp_path)
        store.fail = False
        await runtime.finalize(context, RunStatus.CANCELLED)
        next_owner = LegacyJsonSessionStore().acquire(SessionIdentity('shared'), tmp_path)
        next_owner.close_hold()
    asyncio.run(scenario())
