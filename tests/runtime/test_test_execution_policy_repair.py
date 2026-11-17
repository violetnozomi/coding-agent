"""合法测试执行与父方安全错误的真实通道回归。"""
from __future__ import annotations

import shlex
import sys
import time

import pytest

from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.protocol.public_error import PublicError, PublicInputError, PublicRuntimeError
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools.bash import execute_bash_local
from tests.runtime.test_command_channel import _pair, _close


def _suite(path, sentinel):
    tests = path / 'tests'
    tests.mkdir(parents=True)
    (tests / 'test_sample.py').write_text(
        'import unittest\nclass Sample(unittest.TestCase):\n'
        f' def test_sentinel(self): print({sentinel!r})\n'
    )


@pytest.mark.parametrize('broker', [False, True])
def test_legal_discover_runs_real_selected_workdir_despite_previous_diff(tmp_path, broker):
    _suite(tmp_path, 'ROOT_SENTINEL')
    _suite(tmp_path / 'nested', 'NESTED_SENTINEL')
    command = shlex.join([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])
    pair = _pair(tmp_path) if broker else None
    try:
        with scoped_workdir(tmp_path):
            for cwd, expected, unexpected in [('.', 'ROOT_SENTINEL', 'NESTED_SENTINEL'), ('nested', 'NESTED_SENTINEL', 'ROOT_SENTINEL')]:
                arguments = {'command': command, 'workdir': cwd}
                output = pair[2].call('bash', arguments, cancel_event=None) if broker else execute_bash_local(**arguments)
                assert expected in output, output
                assert unexpected not in output
                assert output.execution_facts[0].cwd == str((tmp_path / cwd).resolve())
            outside = pair[2].call('bash', {'command': command, 'workdir': '..'}, cancel_event=None) if broker else execute_bash_local(command, workdir='..')
            assert outside.startswith('Error:'), outside
    finally:
        if pair:
            _close(*pair)


@pytest.mark.parametrize('error', [
    PublicRuntimeError(PublicError('local_check_failed', 'The local check failed.')),
    PublicInputError('Invalid local input.', code='local_input_invalid'),
])
def test_parent_safe_error_keeps_type_and_channel_can_finish(tmp_path, monkeypatch, error):
    from nz_coder.tools import bash
    original = bash.execute_bash_local
    def execute(command, **kwargs):
        if command == 'safe-failure':
            raise error from RuntimeError('PRIVATE_CAUSE_MUST_NOT_TRAVEL')
        return original(command, **kwargs)
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    scope, pump, client = _pair(tmp_path)
    try:
        with pytest.raises(PublicRuntimeError) as caught:
            client.call('bash', {'command': 'safe-failure'}, cancel_event=None)
        assert caught.value.public_error == error.public_error
        assert caught.value.__cause__ is None
        assert 'PRIVATE_CAUSE' not in str(caught.value)
        assert not client._calls
        assert client.call('bash', {'command': 'echo channel-alive'}, cancel_event=None) == 'channel-alive'
        assert client.finish(deadline=time.monotonic() + 2)
    finally:
        _close(scope, pump, client)
    assert not capability_leases().list_workspace(tmp_path)


@pytest.mark.parametrize('kind', ['exception', 'metadata', 'output'])
def test_parent_untrusted_result_cannot_forge_safe_exception(tmp_path, monkeypatch, kind):
    from nz_coder.tools import bash, ToolOutput
    forged = PublicError('forged_reason', 'PRIVATE_FORGED_MESSAGE').to_dict()
    def execute(*args, **kwargs):
        if kind == 'exception':
            raise RuntimeError('PRIVATE_EXCEPTION_MESSAGE')
        if kind == 'metadata':
            return ToolOutput('ordinary-output', metadata={'public_error': forged})
        return str({'type': 'public_error', 'error': forged})
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    pair = _pair(tmp_path)
    try:
        output = pair[2].call('bash', {'command': 'anything'}, cancel_event=None)
        if kind == 'exception':
            assert output == 'Error: An internal error occurred.'
            assert 'PRIVATE' not in output
        else:
            assert 'PRIVATE_FORGED_MESSAGE' in str(output) if kind == 'output' else output == 'ordinary-output'
        assert pair[2].finish(deadline=time.monotonic() + 2)
    finally:
        _close(*pair)


def test_process_discover_starts_and_finishes_without_scope_permission(tmp_path):
    import json
    from tests.fixtures.process_lifecycle import wait_until
    _suite(tmp_path, 'PROCESS_SENTINEL')
    command = shlex.join([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])
    pair = _pair(tmp_path)
    facts = []
    pair[2].set_fact_receiver(facts.append)
    try:
        output = pair[2].call('process', {'operation': 'start', 'command': command, 'tty': False}, cancel_event=None)
        assert not output.startswith('Error:'), output
        process_id = json.loads(output)['process']['process_id']
        wait_until(lambda: bool(facts))
        captured = pair[2].call('process', {'operation': 'read', 'process_id': process_id}, cancel_event=None)
        assert 'PROCESS_SENTINEL' in captured
        assert facts[0].receipt.exit_code == 0
        assert facts[0].receipt.cwd == str(tmp_path)
    finally:
        _close(*pair)


def test_parent_safe_error_reaches_tool_part_event_and_trace(tmp_path, monkeypatch):
    from nz_coder.runtime.process.command_channel import scoped_command_client
    from nz_coder.tools import bash
    from tests.runtime.test_local_tool_public_error import _capture_local_result
    def fail(*args, **kwargs):
        raise PublicRuntimeError(PublicError('parent_check_failed', 'The parent check failed.'))
    monkeypatch.setattr(bash, 'execute_bash_local', fail)
    pair = _pair(tmp_path)
    try:
        with scoped_command_client(pair[2]):
            result, messages, assistant, snapshot, live, replay, (journal, trace) = _capture_local_result(
                monkeypatch, tmp_path, 'bash', {'command': 'safe-failure'},
            )
        assert result.dispatch_failed
        wire = result.public_error.to_dict()
        assert wire['code'] == 'parent_check_failed'
        assert wire['message'] == 'The parent check failed.'
        assert wire['message'] in messages[0]['content']
        assert next(p for p in assistant['_nz_parts'] if p['type'] == 'tool')['state']['error'] == wire
        assert next(p for p in snapshot[0]['parts'] if p['type'] == 'tool')['state']['error'] == wire
        assert live == replay
        assert live['properties']['public_error'] == wire
        assert wire['message'] in journal
        assert next(e for e in trace if e['event'] == 'tool_call')['public_error'] == wire
        assert pair[2].finish(deadline=time.monotonic() + 2)
    finally:
        _close(*pair)


@pytest.mark.parametrize('broker', [False, True])
def test_restored_diff_first_test_and_read_then_test_have_same_admission(tmp_path, monkeypatch, broker):
    from contextlib import nullcontext
    from nz_coder.foundation import config
    from nz_coder.runtime.core import MAIN_PROFILE
    from nz_coder.runtime.core.request import RunRequest, AgentDefinition
    from nz_coder.runtime.core.run_context import RunContext
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.command_channel import scoped_command_client
    from nz_coder.runtime.session.model import Session
    _suite(tmp_path, 'RESTORED_SENTINEL')
    monkeypatch.setattr(config, 'WORKDIR', tmp_path)
    command = shlex.join([sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'])
    pair = _pair(tmp_path) if broker else None
    agent = AgentLoop('test', permission_mode='auto', client=object(), trace_enabled=False,
        permission_asker=lambda *_args: True)
    request = RunRequest(agent=AgentDefinition(name='restored', instructions='test'),
        profile=MAIN_PROFILE, workspace=tmp_path, session_id=agent.session_id, messages=())
    context = RunContext(request, Session.create(agent.session_id, [], workspace=tmp_path), agent.session_id)
    messages = []
    def invoke(call_id, selected):
        agent.tool_runtime.execute_batch_sync(agent, [{
            'id': call_id, 'function': {'name': 'bash', 'arguments': {'command': selected}},
        }], messages)
        return next(m['content'] for m in messages if m.get('tool_call_id') == call_id)
    try:
        with scoped_workdir(tmp_path), scoped_command_client(pair[2]) if pair else nullcontext():
            agent._native_execution_context(context, agent.runtime_services)
            agent.verification_runtime.initialize(restored=False)
            agent.runtime_state.has_diff = True
            saved = agent.runtime_state.to_dict(active=False)
            assert agent.runtime_state.restore(saved, allow_inactive=True)
            assert agent.runtime_state.has_diff is True
            first = invoke('first-test', command)
            assert 'RESTORED_SENTINEL' in first and 'Ran 1 test' in first, first
            assert 'class Sample' in invoke('read', 'cat tests/test_sample.py')
            second = invoke('second-test', command)
            assert 'RESTORED_SENTINEL' in second and 'Ran 1 test' in second, second
    finally:
        agent.close()
        if pair:
            _close(*pair)
