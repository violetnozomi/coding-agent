"""HTTP快照在交互发布竞争下仍返回精确状态，失败隔离在短命子进程。"""
from __future__ import annotations

import asyncio
import multiprocessing
import os
import sys
import threading
import traceback

import pytest


def _race_worker(workspace, kind, mutation, output):
    from tests.test_http_service import FakeAgent
    from nz_coder.http_service import SessionManager

    go = threading.Event()
    decided = threading.Event()
    published = threading.Event()
    gated = threading.Event()
    agent_ready = threading.Event()
    actor_ids = set()
    events = []
    snapshots = []
    errors = []

    class Agent(FakeAgent):
        async def run(self, messages, **kwargs):
            if mutation == 'ask':
                actor_ids.add(threading.get_ident())
                agent_ready.set()
                assert go.wait(4)
            if kind == 'permission':
                self.permission_asker('bash', {'command': 'echo ok'})
            else:
                self.question_asker([])
            # 把取消保留在执行阶段，不让假模型提前冻结为完成。
            await asyncio.sleep(30)
            return {'status': 'completed'}

    manager = SessionManager(agent_factory=Agent, workspace_roots=[workspace], restore_saved=False)
    wid = next(w['id'] for w in manager.list_workspaces() if w['path'] == str(workspace))
    session = manager.get(manager.create('default', workspace_id=wid)['id'])
    original_lock = session.interactions._lock

    class ScheduledLock:
        def __enter__(self):
            if gated.is_set() and threading.get_ident() in actor_ids:
                if not original_lock.acquire(blocking=False):
                    # 正确锁序会把写方挡在broker外；允许快照完成后再发布。
                    decided.set()
                    original_lock.acquire()
            else:
                original_lock.acquire()
            return self

        def __exit__(self, *args):
            original_lock.release()

    session.interactions._lock = ScheduledLock()
    publish = session.interactions._publish
    publish_locked = session.event_bus._publish_locked

    def record(event_type, *args, **kwargs):
        event = publish_locked(event_type, *args, **kwargs)
        events.append((event.type, event.sequence))
        return event

    def publish_probe(name, properties):
        if gated.is_set() and mutation == 'ask' and name == kind + '.asked':
            decided.set()  # 旧路径此时持broker锁，随后会等待event锁。
        result = publish(name, properties)
        if name == kind + '.asked':
            published.set()
        return result

    session.event_bus._publish_locked = record
    session.interactions._publish = publish_probe
    checkpoint = session.event_bus.checkpoint_with_replay

    def checkpoint_probe(factory, **kwargs):
        if mutation == 'ask':
            def capture():
                go.set()
                assert decided.wait(4)
                return factory()
            return checkpoint(capture, **kwargs)
        go.set()
        assert decided.wait(4)
        return checkpoint(factory, **kwargs)

    session.event_bus.checkpoint_with_replay = checkpoint_probe
    if mutation == 'ask':
        gated.set()
    session.start_run('offline race')
    if mutation == 'ask':
        assert agent_ready.wait(4)
    else:
        assert published.wait(4)
        request = session.interactions.list()[0]

        def mutate():
            actor_ids.add(threading.get_ident())
            assert go.wait(4)
            if mutation == 'cancel':
                session.interactions.cancel_all('race', block_new=True)
            elif kind == 'permission':
                session.interactions.reply_permission(request['id'], 'once')
            else:
                session.interactions.reply_question(request['id'], [])
            # 超过旧256条窗口，不能再用该窗口证明pending准确。
            for index in range(260):
                session.event_bus.publish('message.part.delta', {'delta': str(index)})
            decided.set()
        writer = threading.Thread(target=mutate, daemon=True)
        gated.set()
        writer.start()

    def snapshot():
        try:
            snapshots.append(session.attach_snapshot())
        except BaseException as exc:
            errors.append(repr(exc))
    reader = threading.Thread(target=snapshot, daemon=True)
    reader.start()
    reader.join(2)
    result = {'returned': not reader.is_alive(), 'errors': errors}
    if reader.is_alive() or errors:
        result['stacks'] = {str(ident): ''.join(traceback.format_stack(frame)) for ident, frame in sys._current_frames().items()}
        output.put(result)
        output.close()
        output.join_thread()
        os._exit(0)
    assert published.wait(4)
    if mutation != 'ask':
        writer.join(4)
        assert not writer.is_alive()
    first = snapshots[0]
    event_type = kind + ('.asked' if mutation == 'ask' else ('.replied' if mutation == 'reply' else ('.replied' if kind == 'permission' else '.rejected')))
    sequence = next(seq for name, seq in events if name == event_type)
    pending = first['pending']['permissions' if kind == 'permission' else 'questions']
    result.update(snapshot=first, event_sequence=sequence, pending_count=len(pending))
    result['aborted'] = session.abort()
    result['settled'] = session.wait(4)
    result['pending_after_abort'] = session.interactions.list()
    manager.close(timeout=1)
    output.put(result)
    output.close()
    output.join_thread()


@pytest.mark.parametrize('kind', ['permission', 'question'])
@pytest.mark.parametrize('mutation', ['ask', 'reply', 'cancel'])
def test_attach_and_interaction_changes_serialize_without_deadlock(tmp_path, kind, mutation):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    process = context.Process(target=_race_worker, args=(workspace, kind, mutation, output))
    process.start()
    try:
        result = output.get(timeout=15)
        process.join(5)
        assert not process.is_alive()
        assert result['returned'], '\n'.join(result.get('stacks', {}).values())
        assert result['errors'] == []
        snapshot = result['snapshot']
        after_cursor = result['event_sequence'] > snapshot['cursor']['sequence']
        expected_pending = (not after_cursor) if mutation == 'ask' else after_cursor
        assert result['pending_count'] == int(expected_pending)
        assert snapshot['pending'] == snapshot['run']['pending']
        assert snapshot['session']['pending_interaction_count'] == int(expected_pending)
        status = 'waiting_' + kind if expected_pending else 'running'
        assert snapshot['session']['runtime_status'] == snapshot['run']['status'] == status
        assert result['aborted'] and result['settled']
        assert result['pending_after_abort'] == []
    finally:
        if process.is_alive():
            process.terminate()
            process.join(3)
        output.close()


def test_historical_asked_event_does_not_recreate_live_pending(tmp_path):
    from tests.test_http_service import FakeAgent
    from nz_coder.http_service import SessionManager
    manager = SessionManager(agent_factory=FakeAgent, workspace_roots=[tmp_path], restore_saved=False)
    wid = next(w['id'] for w in manager.list_workspaces() if w['path'] == str(tmp_path))
    session = manager.get(manager.create('default', workspace_id=wid)['id'])
    try:
        session.start_run('wait')
        session.event_bus.publish('permission.asked', {'id': 'old-unsettled', 'created_at': 1, 'kind': 'permission'})
        snapshot = session.attach_snapshot()
        assert snapshot['pending'] == snapshot['run']['pending'] == {'permissions': [], 'questions': []}
        assert snapshot['session']['pending_interaction_count'] == 0
        assert snapshot['session']['runtime_status'] == snapshot['run']['status'] == 'running'
    finally:
        session.abort()
        assert session.wait(3)
        manager.close()


@pytest.mark.parametrize('method', ['snapshot', 'attach_snapshot'])
@pytest.mark.parametrize('failure', ['factory', 'closed-bus'])
def test_failed_checkpoint_releases_interaction_lock(tmp_path, monkeypatch, method, failure):
    from tests.test_http_service import FakeAgent
    from nz_coder.http_service import SessionManager

    manager = SessionManager(agent_factory=FakeAgent, workspace_roots=[tmp_path], restore_saved=False)
    session = manager.get(manager.create()['id'])
    checkpoint_name = 'checkpoint' if method == 'snapshot' else 'checkpoint_with_replay'
    if failure == 'closed-bus':
        session.event_bus.close()
    else:
        checkpoint = getattr(session.event_bus, checkpoint_name)

        def failing_checkpoint(factory, **kwargs):
            def fail():
                raise ValueError('snapshot probe')
            return checkpoint(fail, **kwargs)
        monkeypatch.setattr(session.event_bus, checkpoint_name, failing_checkpoint)

    returned = threading.Event()
    try:
        with pytest.raises(RuntimeError if failure == 'closed-bus' else ValueError):
            getattr(session, method)()

        def read_pending():
            session.interactions.list()
            returned.set()
        reader = threading.Thread(target=read_pending, daemon=True)
        reader.start()
        assert returned.wait(2), '快照失败后其他线程无法读取交互清单'
        reader.join(2)
    finally:
        manager.close()


@pytest.mark.parametrize('kind', ['permission', 'question'])
def test_expired_interaction_is_absent_from_snapshot_after_replay_overflow(tmp_path, kind):
    from tests.test_http_service import FakeAgent
    from nz_coder.http_service import SessionManager

    expired = threading.Event()

    class Agent(FakeAgent):
        async def run(self, messages, **kwargs):
            if kind == 'permission':
                self.permission_asker('bash', {'command': 'echo ok'})
            else:
                self.question_asker([])
            expired.set()
            await asyncio.Event().wait()

    manager = SessionManager(
        agent_factory=Agent, workspace_roots=[tmp_path], restore_saved=False,
        interaction_timeout_seconds=0.05,
    )
    session = manager.get(manager.create()['id'])
    try:
        session.start_run('offline timeout')
        assert expired.wait(3)
        terminal = next(event for event in session.event_bus.recent(100) if event.properties.get('reason') == 'timeout')
        for index in range(260):
            session.event_bus.publish('message.part.delta', {'delta': str(index)})
        snapshot = session.attach_snapshot()
        assert snapshot['cursor']['sequence'] > terminal.sequence
        assert snapshot['pending'] == snapshot['run']['pending'] == {'permissions': [], 'questions': []}
        assert snapshot['session']['pending_interaction_count'] == 0
        assert snapshot['session']['runtime_status'] == snapshot['run']['status'] == 'running'
    finally:
        session.abort()
        assert session.wait(3)
        manager.close()
