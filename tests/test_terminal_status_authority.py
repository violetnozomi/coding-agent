"""终端以所属结果定稿；事件类别不覆盖类型化状态。"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from nz_coder.interface import remote
from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.protocol.run_view_reducer import RunViewReducer
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.core.result import RunStatus


class _Input:
    def __init__(self, **kwargs): pass
    async def close_async(self): pass


class _Backend:
    session_id = 'terminal-authority'
    client = SimpleNamespace(timeout=1)

    def __init__(self, runtime_status, settled_status, *, stale=False):
        self.bus = SessionEventBus(session_id=self.session_id)
        publisher = self.bus.for_interaction('current', agent_invocation_id='worker')
        event_type = 'session.run.cancelled' if runtime_status == 'interrupted' else 'session.run.completed'
        self.items = [publisher.publish(event_type, {'status': runtime_status}).to_dict()]
        if stale:
            old = self.bus.for_interaction('old', agent_invocation_id='worker')
            self.items.append(old.publish('session.run.settled', {'status': 'completed', 'persisted': True}).to_dict())
        self.items.append(publisher.publish('session.run.settled', {'status': settled_status, 'persisted': True}).to_dict())

    def attach_snapshot(self):
        return {'settled': False, 'session': {'running': True, 'status': 'running'},
                'run': {'interaction_run_id': 'current', 'status': 'running', 'messages': []},
                'active_interaction_run_id': 'current', 'pending': {}, 'events': [], 'cursor': {}}

    def events(self, **kwargs):
        yield from self.items


@pytest.mark.parametrize('runtime_status,settled_status', [
    ('completed', 'cancelled'), ('interrupted', 'interrupted'),
    ('completed_unverified', 'completed_unverified'), ('blocked', 'blocked'), ('max_turns', 'max_turns'),
])
@pytest.mark.parametrize('stale', [False, True])
def test_remote_follow_uses_current_settled_status(monkeypatch, runtime_status, settled_status, stale):
    monkeypatch.setattr(remote, 'TerminalInput', _Input)
    backend = _Backend(runtime_status, settled_status, stale=stale)
    output = io.StringIO()
    try:
        asyncio.run(remote._follow_run(backend, Console(file=output, force_terminal=False, width=120)))
    finally:
        backend.bus.close()
    rendered = output.getvalue()
    assert f'Run {settled_status.replace("_", " ")}' in rendered
    assert rendered.count('Run ') == 1


@pytest.mark.parametrize('status', [value.value for value in RunStatus])
def test_reducer_preserves_payload_terminal_status(status):
    reducer = RunViewReducer()
    reducer.replace_snapshot({'interaction_run_id': 'current', 'status': 'running'})
    for sequence, event_type in enumerate(['session.run.completed', 'session.run.settled'], 1):
        reducer.apply_event({'type': event_type, 'properties': {'status': status},
                             'meta': {'interaction_run_id': 'current', 'sequence': sequence}})
        assert reducer.state.status == status
        assert reducer.state.terminal['status'] == status


def test_local_final_result_overrides_runtime_candidate_without_waiting_for_http():
    from tests.test_run_renderer import FakeStreamingRenderer
    output = io.StringIO()
    view = TerminalRunRenderer(Console(file=output, force_terminal=False), FakeStreamingRenderer(), terminal_source='result')
    view.feed({'type': 'session.run.completed', 'properties': {'status': 'completed'}, 'meta': {}})
    assert 'Run completed' not in output.getvalue()
    view.finish({'status': 'cancelled'})
    assert 'Run cancelled' in output.getvalue()
