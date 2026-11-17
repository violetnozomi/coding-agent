"""Real spawn proves parent ownership, policy initialization and handoff causes."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from tests.fixtures import subagent_process_targets as targets
from tests.fixtures.process_lifecycle import wait_until, make_command_authorizer


def _python_command(script):
    return shlex.join((sys.executable, '-u', '-c', script))


def _general_state():
    from nz_coder.runtime.agent.subagent import _new_subagent_state
    return _new_subagent_state('parent', 'general-purpose', ['bash'])


def test_real_child_hard_verification_repair_reuses_identical_policy(tmp_path, monkeypatch):
    supervisor, scope, _, _, errors = prepare_case(
        tmp_path, monkeypatch, targets.production_probe,
        prompt='produce a complete report', probe_mode='verification',
        verification={'enforcement': 'hard', 'min_final_text_chars': 20},
    )
    supervisor.start()
    try:
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 10, stop_grace=.02)
        facts = json.loads((tmp_path / '.nz-coder' / 'model-probe.json').read_text())
        assert facts['model_calls'] == 2, outcome.result
        assert outcome.canonical['status'] == 'completed_unverified', outcome.result
        assert outcome.canonical.get('public_error') is None
        assert 'sufficiently detailed' in outcome.canonical['final_text']
        assert outcome.canonical['postconditions']['ok'] is True
        assert errors == []
    finally:
        cleanup_case(supervisor, scope)


def _pid_running(pid):
    stat = Path(f'/proc/{pid}/stat')
    return stat.exists() and not stat.read_text().split(') ', 1)[1].startswith('Z')


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux native ownership observation')
def test_parent_owns_bash_after_agent_is_force_killed(tmp_path, monkeypatch):
    created = []
    real = subprocess.Popen
    def popen(*args, **kwargs):
        process = real(*args, **kwargs)
        created.append(process)
        return process
    monkeypatch.setattr(subprocess, 'Popen', popen)
    script = ('import subprocess,sys,os,json,time; '
              'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
              'open("command-pids.json","w").write(json.dumps([os.getpid(),p.pid])); time.sleep(30)')
    (tmp_path / 'descriptor_probe.py').write_text(script)
    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.parent_owned_probe, state=_general_state(), command_tool='bash', command=f'{shlex.quote(sys.executable)} descriptor_probe.py')
    outcomes = []
    supervisor.start()
    waiter = threading.Thread(target=lambda: outcomes.append(supervisor.wait(threading.Event(), deadline=time.monotonic() + 10, stop_grace=.02)))
    waiter.start()
    pids = []
    try:
        wait_until(lambda: (tmp_path / 'command-pids.json').exists(), timeout=6)
        pids = json.loads((tmp_path / 'command-pids.json').read_text())
        records = list(scope._requests.values())
        record = next(record for record in records if record.execution is not None)
        command = record.execution._process
        assert command in created
        parent_pid = int(Path(f'/proc/{command.pid}/status').read_text().split('PPid:', 1)[1].split()[0])
        assert parent_pid == os.getpid() and parent_pid != supervisor.process.pid
        supervisor.process.kill()
        waiter.join(3)
        assert not waiter.is_alive() and supervisor.exited
        assert scope.close(deadline=time.monotonic() + 3)
        assert record.execution._done.is_set() and not record.worker.is_alive()
        assert not record.execution._reader.is_alive()
        assert command.returncode is not None and command.stdout.closed
        wait_until(lambda: all(not _pid_running(pid) for pid in pids))
    finally:
        cleanup_case(supervisor, scope, waiter)
        for pid in pids:
            if _pid_running(pid):
                os.kill(pid, 9)


@pytest.mark.parametrize('tty', [False, True])
@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux PIPE and POSIX PTY')
def test_parent_owns_pipe_and_posix_pty_after_agent_exit(tmp_path, monkeypatch, tty):
    from nz_coder.runtime.agent import subagent

    state = subagent._new_subagent_state('parent', 'general-purpose', ['process'])
    script = 'import os,sys,time; print("ready",flush=True); x=sys.stdin.readline(); print(x.strip()+"尾部",flush=True); time.sleep(30)'
    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.parent_owned_probe, state=state, command_tool='process', command=_python_command(script), tty=tty, command_authorizer=make_command_authorizer(tmp_path))
    outcomes = []
    supervisor.start()
    waiter = threading.Thread(target=lambda: outcomes.append(supervisor.wait(threading.Event(), deadline=time.monotonic() + 10, stop_grace=.02)))
    waiter.start()
    try:
        wait_until(lambda: (tmp_path / 'process-probe.json').exists(), timeout=6)
        facts = json.loads((tmp_path / 'process-probe.json').read_text())
        output = ''.join(read['output'] for read in facts['reads'])
        assert '你好尾部' in output and '尾部' in facts['tail']['output']
        assert facts['write']['bytes_written'] == len('你好\n'.encode())
        cursors = [read['next_cursor'] for read in facts['reads']]
        assert cursors == sorted(cursors) and cursors[-1] > 0
        if tty:
            assert facts['resize']['rows'] == 31 and facts['resize']['cols'] == 99
        record = next(iter(scope._service._records.values()))
        backend = record.backend
        assert record.waiter.is_alive() and record.reader.is_alive()
        supervisor.process.kill()
        waiter.join(3)
        assert not waiter.is_alive() and supervisor.exited
        assert scope.close(deadline=time.monotonic() + 3)
        assert not record.reader.is_alive() and not record.waiter.is_alive()
        assert not _pid_running(facts['started']['process']['pid'])
        assert record.backend is None and backend.process.returncode is not None
        if tty:
            assert backend.master_fd is None
        else:
            assert backend.process.stdin.closed and backend.process.stdout.closed
    finally:
        cleanup_case(supervisor, scope, waiter)


@pytest.mark.skipif(not sys.platform.startswith('linux'), reason='Linux descriptor identity')
def test_shell_environment_and_descriptors_exclude_credentials_and_channel(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'offline-secret-sentinel')
    monkeypatch.setenv('GITHUB_TOKEN', 'offline-token-sentinel')
    script = ('import os,json; fds={}; '
              '\nfor fd in os.listdir("/proc/self/fd"):\n'
              ' try: fds[fd]=os.readlink("/proc/self/fd/"+fd)\n'
              ' except FileNotFoundError: pass\n'
              'print(json.dumps({"env":dict(os.environ),"fds":fds}))')
    (tmp_path / 'descriptor_probe.py').write_text(script)
    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.parent_owned_probe, state=_general_state(), command_tool='bash', command=f'{shlex.quote(sys.executable)} descriptor_probe.py')
    channel_identity = os.readlink(f'/proc/self/fd/{scope._server_socket.fileno()}')
    child_identity = os.readlink(f'/proc/self/fd/{scope._endpoint.socket.fileno()}')
    supervisor.start()
    try:
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 8, stop_grace=.02)
        assert outcome.reason == 'exit', outcome.result
        facts = json.loads(json.loads((tmp_path / 'command-output.json').read_text()))
        assert 'OPENAI_API_KEY' not in facts['env'] and 'GITHUB_TOKEN' not in facts['env']
        assert 'offline-secret-sentinel' not in json.dumps(facts)
        assert channel_identity not in facts['fds'].values() and child_identity not in facts['fds'].values()
    finally:
        cleanup_case(supervisor, scope)


def test_lost_reply_never_replays_side_effect_or_starts_locally(tmp_path, monkeypatch):
    command = _python_command('from pathlib import Path; p=Path("counter"); p.write_text(p.read_text()+"x" if p.exists() else "x"); print("done")')
    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.parent_owned_probe, state=_general_state(), command_tool='bash', command=command)
    original = scope.peek_reply
    replies = []
    def drop():
        result = original()
        if result is not None:
            replies.append(result)
            raise ConnectionError('controlled loss after side effect before final reply')
        return None
    monkeypatch.setattr(scope, 'peek_reply', drop)
    supervisor.start()
    try:
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 8, stop_grace=.02)
        assert outcome.reason == 'error' and len(replies) == 1
        assert (tmp_path / 'counter').read_text() == 'x'
        assert scope.close(deadline=time.monotonic() + 2)
        assert len(scope._requests) <= 1
    finally:
        cleanup_case(supervisor, scope)


@pytest.mark.parametrize('message_mib,body_mib,oversized', [(16, 15, False), (16, 17, True), (32, 18, False)])
def test_large_tooloutput_round_trip_is_complete_with_interleaved_calls(tmp_path, monkeypatch, message_mib, body_mib, oversized):
    import hashlib
    import socket
    from tests.fixtures.process_lifecycle import make_command_scope
    from nz_coder.protocol.attachments import make_image_attachment
    from nz_coder.runtime.process.command_channel import ChildCommandClient, ChildCommandEndpoint, CommandChannelLimits, ParentCommandPump
    from nz_coder.tools import ToolOutput
    import nz_coder.tools.bash as bash

    scope = make_command_scope(tmp_path, ready=False)
    configured = replace(scope.authority.run_settings, process_buffer_bytes=4 * 1024 * 1024)
    assert CommandChannelLimits.from_settings(configured).max_message_bytes > 32 * 1024 * 1024
    limits = CommandChannelLimits(max_message_bytes=message_mib * 1024 * 1024)
    parent, child = socket.socketpair()
    pump = ParentCommandPump(parent, handler=scope, limits=limits)
    pump.start()
    client = ChildCommandClient(ChildCommandEndpoint(child, scope.authority.settlement_id), limits=limits, deadline=time.monotonic() + 30)
    client.set_fact_receiver(lambda _fact: None)
    assert client.policy_ready(scope.authority.execution_policy, deadline=time.monotonic() + 2)
    both = threading.Barrier(2)
    values = {name: ToolOutput(name * (body_mib * 1024 * 1024), title='标题' + name * 1000,
                              metadata={'name': name, 'nested': {'payload': '界' * 20000}},
                              attachments=[make_image_attachment(b'\x89PNG\r\n\x1a\n' + name.encode() * 50000, 'image/png')]) for name in ('a', 'b')}
    def execute(command, **kwargs):
        both.wait(timeout=2)
        return values[command]
    monkeypatch.setattr(bash, 'execute_bash_local', execute)
    results = {}
    threads = [threading.Thread(target=lambda name=name: results.update({name: client.call('bash', {'command': name}, cancel_event=None)})) for name in values]
    def digest(value):
        payload = [str(value), value.title, value.metadata, value.attachments]
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
            assert not thread.is_alive()
        assert set(results) == set(values)
        for name, value in results.items():
            if oversized:
                assert str(value) == 'Error: Command message limit exceeded'
            else:
                assert isinstance(value, ToolOutput)
                assert digest(value) == digest(values[name])
        assert client.finish(deadline=time.monotonic() + 2)
    finally:
        assert client.close(deadline=time.monotonic() + 2)
        assert pump.close(deadline=time.monotonic() + 2)
        assert scope.close(deadline=time.monotonic() + 2)
        for thread in threads:
            thread.join(2)
            assert not thread.is_alive()


def prepare_case(tmp_path, monkeypatch, target, *, prompt='offline', settings=None, state=None, config_snapshot=None, command_authorizer=None, **extras):
    from nz_coder.runtime.agent import process_supervisor as supervision, subagent
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.agent.child_process_result import allocate_child_process_result
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.command_broker import ParentCommandScope
    from nz_coder.runtime.process.command_channel import CommandChannelLimits
    from nz_coder.runtime.process.workdir import scoped_workdir

    deadline = time.monotonic() + 12
    settings = settings or replace(RunSettings.from_legacy_globals(), subagent_worktree_enabled=False, subagent_max_turns=4, subagent_timeout_seconds=15, process_kill_grace=.02)
    with scoped_workdir(tmp_path), scoped_run_settings(settings), scoped_config_snapshot(config_snapshot) if config_snapshot is not None else nullcontext(), subagent.scoped_parent_context(session_id='parent', config_snapshot=config_snapshot):
        state = state or subagent._new_subagent_state('parent', 'explore', ['bash'])
        state['settlement_id'] = 'round-1'
        from tests.fixtures.process_lifecycle import make_preparation_scope, complete_prepared_evidence
        preparation = make_preparation_scope(tmp_path, child=state['session_id'], settings=settings, deadline=deadline, settlement_id='round-1')
        try:
            prepared, authority = subagent.prepare_subagent_command_context(prompt, state, workspace=tmp_path, config_snapshot=config_snapshot, settings=settings, settlement_id='round-1', preparation=preparation)
            prepared = complete_prepared_evidence(prepared, state, preparation)
            preparation.seal()
        finally:
            assert preparation.close(deadline=time.monotonic() + 2)
        subagent._save_subagent_state('parent', state, tmp_path)
    holder = {}
    errors = []
    def failed(error):
        errors.append(error)
        if 'supervisor' in holder:
            holder['supervisor'].fail(error)
    scope = ParentCommandScope(authority, deadline=deadline, child_publisher=None, on_channel_error=failed, command_authorizer=command_authorizer)
    endpoint = scope.start()
    payload = {
        'workspace': str(tmp_path), 'parent_session_id': 'parent', 'config_snapshot': config_snapshot,
        'parent_model_id': subagent._subagent_model('general-purpose', config_snapshot),
        'memory_scope': state['memory_scope'],
        'session_id': state['session_id'], 'settlement_id': 'round-1',
        'agent_type': state['agent_type'], 'allowed_tools': state['allowed_tools'],
        'prompt': prompt, 'prepared_workspace': prepared,
        'command_limits': CommandChannelLimits.from_settings(authority.run_settings),
        'command_settings': authority.run_settings, 'command_deadline': deadline,
        **extras,
    }
    monkeypatch.setattr(supervision, '_run_subagent_process', target)
    supervisor = supervision.SubagentProcessSupervisor(payload, allocate_child_process_result(tmp_path), command_endpoint=endpoint, on_stop=scope.request_stop)
    holder['supervisor'] = supervisor
    return supervisor, scope, prepared, payload, errors


def cleanup_case(supervisor, scope, waiter=None):
    scope.request_stop()
    process = supervisor.process
    if process is not None:
        try:
            if process.is_alive():
                process.kill()
            process.join(3)
        except ValueError:
            pass
    if waiter is not None:
        waiter.join(3)
        assert not waiter.is_alive()
    if process is not None and not supervisor.exited:
        cancelled = threading.Event()
        cancelled.set()
        supervisor.wait(cancelled, deadline=time.monotonic(), stop_grace=.02)
    assert supervisor.close(deadline=time.monotonic() + 1)
    assert scope.close(deadline=time.monotonic() + 2)


def test_normal_finish_eof_before_a1_write_does_not_kill_child(tmp_path, monkeypatch):
    supervisor, scope, _, _, errors = prepare_case(tmp_path, monkeypatch, targets.finish_then_result)
    outcomes = []
    supervisor.start()
    waiter = threading.Thread(target=lambda: outcomes.append(supervisor.wait(threading.Event(), deadline=time.monotonic() + 8, stop_grace=.02)))
    waiter.start()
    try:
        wait_until(lambda: (tmp_path / 'finished-channel').exists(), timeout=6)
        assert supervisor.process.is_alive()
        assert errors == []
        (tmp_path / 'release-result').touch()
        waiter.join(3)
        assert not waiter.is_alive()
        assert outcomes[0].reason == 'exit'
        assert outcomes[0].canonical['cost'] == .125
        assert outcomes[0].canonical['usage']['total'] == 18
    finally:
        (tmp_path / 'release-result').touch()
        cleanup_case(supervisor, scope, waiter)


@pytest.mark.parametrize('mode', ['eof', 'protocol'])
def test_unexpected_eof_or_protocol_error_stops_model_body_as_error(tmp_path, monkeypatch, mode):
    supervisor, scope, _, _, errors = prepare_case(tmp_path, monkeypatch, targets.drop_command_channel, mode=mode)
    cancelled = threading.Event()
    supervisor.start()
    try:
        outcome = supervisor.wait(cancelled, deadline=time.monotonic() + 8, stop_grace=.02)
        assert outcome.reason == 'error'
        assert outcome.public_error.code == 'child_command_channel_error'
        assert not cancelled.is_set()
        assert len(errors) == 1
        assert not supervisor.process.is_alive()
    finally:
        cleanup_case(supervisor, scope)


@pytest.mark.parametrize('cause', ['infra_first', 'deadline_first', 'user_cancel', 'infra_before_later_deadline'])
def test_timeout_infra_and_user_cancel_preserve_handoff_precedence(tmp_path, monkeypatch, cause):
    from nz_coder.protocol.public_error import PublicError

    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.blocking)
    cancelled = threading.Event()
    supervisor.start()
    try:
        deadline = time.monotonic() - .01 if cause in {'deadline_first', 'user_cancel'} else time.monotonic() + 4
        supervisor.fail(PublicError('child_command_channel_error', 'Offline channel failure.'))
        if cause == 'infra_before_later_deadline':
            deadline = time.monotonic() + .02
            threading.Event().wait(.03)
        if cause == 'user_cancel':
            cancelled.set()
        outcome = supervisor.wait(cancelled, deadline=deadline, stop_grace=.02)
        assert outcome.reason == ('cancelled' if cause == 'user_cancel' else 'timeout' if cause == 'deadline_first' else 'error')
        if outcome.reason == 'error':
            assert outcome.public_error.code == 'child_command_channel_error'
        assert not supervisor.process.is_alive()
    finally:
        cleanup_case(supervisor, scope)


@pytest.mark.parametrize('partial', [False, True])
def test_spawn_success_and_failure_retire_same_parent_child_endpoint(tmp_path, monkeypatch, partial):
    import multiprocessing

    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.blocking)
    endpoint = scope._endpoint
    original = multiprocessing.get_context('spawn').Process.start
    def start(process):
        if partial:
            original(process)
        raise RuntimeError('controlled spawn failure')
    with monkeypatch.context() as fault:
        fault.setattr(multiprocessing.get_context('spawn').Process, 'start', start)
        with pytest.raises(RuntimeError, match='controlled spawn'):
            supervisor.start()
    try:
        assert scope._endpoint is endpoint
        assert endpoint.socket.fileno() == -1
        assert supervisor.exited
    finally:
        cleanup_case(supervisor, scope)


@pytest.mark.parametrize("replace_identity", [False, True])
def test_parent_prepares_real_worktree_once_and_child_uses_bound_identity(tmp_path, monkeypatch, replace_identity):
    from nz_coder.runtime.core.run_settings import RunSettings

    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    (tmp_path / 'sample.py').write_text('original\n')
    subprocess.run(['git', 'add', 'sample.py'], cwd=tmp_path, check=True)
    subprocess.run(['git', '-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid', 'commit', '-qm', 'fixture'], cwd=tmp_path, check=True)
    settings = replace(RunSettings.from_legacy_globals(), subagent_worktree_enabled=True, subagent_max_turns=3, subagent_timeout_seconds=15)
    supervisor, scope, prepared, _, errors = prepare_case(tmp_path, monkeypatch, targets.production_probe, settings=settings, probe_mode='worktree')
    assert prepared.execution_root != tmp_path
    backup = prepared.execution_root.with_name(prepared.execution_root.name + '-original')
    if replace_identity:
        prepared.execution_root.rename(backup)
        prepared.execution_root.mkdir()
    supervisor.start()
    try:
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 10, stop_grace=.02)
        if replace_identity:
            assert not (tmp_path / '.nz-coder' / 'model-probe.json').exists()
            assert outcome.reason == 'error'
            return
        assert (tmp_path / '.nz-coder' / 'model-probe.json').exists(), outcome.result
        facts = json.loads((tmp_path / '.nz-coder' / 'model-probe.json').read_text())
        assert facts['cwd'] == str(prepared.execution_root)
        assert facts['model_calls'] >= 2
        assert str(prepared.execution_root) in facts['tool_outputs']
        assert errors == []
        assert scope._endpoint.socket.fileno() == -1
    finally:
        cleanup_case(supervisor, scope)
        if replace_identity:
            prepared.execution_root.rmdir()
            backup.rename(prepared.execution_root)


def test_actual_restored_policy_matches_parent_before_first_model_step(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.model import Session, SessionStatus
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    from nz_coder.state.sessions import session_runtime_state_path

    settings = replace(RunSettings.from_legacy_globals(), subagent_worktree_enabled=False, subagent_max_turns=4, subagent_timeout_seconds=15, runtime_state_persist=True)
    with scoped_workdir(tmp_path), scoped_run_settings(settings):
        state = subagent._new_subagent_state('parent', 'general-purpose', ['bash'])
        session = Session.create(state['session_id'], [
            {'role': 'user', 'content': 'pytest tests/unit'},
            {'role': 'assistant', 'content': 'unfinished', '_nz_continuation': {'version': 1, 'status': 'max_turns', 'summary': 'unfinished'}},
        ], workspace=tmp_path, parent_session_id='parent')
        session.begin_run()
        session.record_status(SessionStatus.MAX_TURNS)
        asyncio.run(LegacyJsonSessionStore().save(session))
        checkpoint = RuntimeState()
        checkpoint.reset(max_turns=4)
        checkpoint.initial_task_text = 'pytest tests/unit'
        checkpoint.has_diff = True
        checkpoint.save(session_runtime_state_path(state['session_id']), active=False)
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_sample.py').write_text(
        'import unittest\nclass Restored(unittest.TestCase):\n'
        ' def test_restored(self): print("RESTORED_TEST_SENTINEL")\n'
    )
    supervisor, scope, _, _, errors = prepare_case(tmp_path, monkeypatch, targets.production_probe, prompt='go on', settings=settings, state=state, probe_mode='restore')
    try:
        assert scope.authority.execution_policy.strict_local_tools is False
        assert scope.authority.execution_policy.fingerprint
        supervisor.start()
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 10, stop_grace=.02)
        assert (tmp_path / '.nz-coder' / 'model-probe.json').exists(), outcome.result
        facts = json.loads((tmp_path / '.nz-coder' / 'model-probe.json').read_text())
        assert facts['restored'] is True
        assert facts['initial_strict'] is False
        assert sum('RESTORED_TEST_SENTINEL' in item['text'] and 'Ran 1 test' in item['text'] for item in facts['tool_replies']) == 2
        assert all('Broad test runner blocked' not in output for output in facts['tool_outputs'])
        assert facts['strict_after_tools'] is False
        assert errors == []
    finally:
        cleanup_case(supervisor, scope)


@pytest.mark.parametrize('captured_snapshot', [True, False])
def test_persist_disabled_is_bound_in_child_without_restoring_checkpoint(tmp_path, monkeypatch, captured_snapshot):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.model import Session, SessionStatus
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    from nz_coder.state.sessions import session_runtime_state_path

    from nz_coder.foundation.workspace_trust import load_config_snapshot

    if not captured_snapshot:
        monkeypatch.setenv('RUNTIME_STATE_PERSIST', '1')
    snapshot = load_config_snapshot(tmp_path, environ={
        'SUBAGENT_WORKTREE_ENABLED': '0', 'SUBAGENT_MAX_TURNS': '3',
        'SUBAGENT_TIMEOUT_SECONDS': '15', 'RUNTIME_STATE_PERSIST': '0',
        'REFLECTION_ENABLED': '0', 'API_KEY': 'offline-fixture',
    }, user_config_path=tmp_path.parent / 'absent-user-config') if captured_snapshot else None
    settings = RunSettings.from_snapshot(snapshot) if snapshot is not None else replace(RunSettings.from_legacy_globals(), subagent_worktree_enabled=False, subagent_max_turns=3, runtime_state_persist=False)

    with scoped_workdir(tmp_path), scoped_run_settings(settings):
        state = subagent._new_subagent_state('parent', 'general-purpose', ['bash'])
        session = Session.create(state['session_id'], [
            {'role': 'user', 'content': 'pytest tests/current'},
            {'role': 'assistant', 'content': 'unfinished', '_nz_continuation': {'version': 1, 'status': 'max_turns', 'summary': 'unfinished'}},
        ], workspace=tmp_path, parent_session_id='parent')
        session.begin_run()
        session.record_status(SessionStatus.MAX_TURNS)
        asyncio.run(LegacyJsonSessionStore().save(session))
        checkpoint = RuntimeState()
        checkpoint.reset()
        checkpoint.initial_task_text = 'pytest tests/private'
        checkpoint.save(session_runtime_state_path(state['session_id']), active=False)
    supervisor, scope, _, _, errors = prepare_case(tmp_path, monkeypatch, targets.production_probe, prompt='continue and run pytest tests/current', settings=settings, state=state, config_snapshot=snapshot, probe_mode='no_restore')
    try:
        assert scope.authority.execution_policy.strict_local_tools is False
        assert scope.authority.execution_policy.fingerprint
        supervisor.start()
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 10, stop_grace=.02)
        if not captured_snapshot:
            assert outcome.reason == 'error'
            assert outcome.public_error.code == 'child_command_channel_error'
            assert not (tmp_path / '.nz-coder' / 'model-probe.json').exists()
            return
        assert (tmp_path / '.nz-coder' / 'model-probe.json').exists(), outcome.result
        facts = json.loads((tmp_path / '.nz-coder' / 'model-probe.json').read_text())
        assert facts['model_calls'] >= 1
        assert facts['restored'] is False
        assert errors == []
    finally:
        cleanup_case(supervisor, scope)


@pytest.mark.parametrize('partial', [False, True])
def test_scope_start_failure_retires_untransferred_endpoint(tmp_path, monkeypatch, partial):
    from tests.fixtures.process_lifecycle import make_command_scope

    scope = make_command_scope(tmp_path, ready=False)
    original = threading.Thread.start
    def start(thread):
        if thread.name == 'nz-command-parent-pump':
            if partial:
                original(thread)
            raise RuntimeError('controlled parent pump start')
        return original(thread)
    with monkeypatch.context() as fault:
        fault.setattr(threading.Thread, 'start', start)
        with pytest.raises(RuntimeError, match='controlled parent'):
            scope.start()
    assert scope.close(deadline=time.monotonic() + 2)
    assert scope._endpoint.socket.fileno() == -1
    assert scope._server_socket.fileno() == -1


def test_late_channel_fault_does_not_override_confirmed_outcome(tmp_path, monkeypatch):
    from nz_coder.protocol.public_error import PublicError

    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.finish_then_result)
    (tmp_path / 'release-result').touch()
    supervisor.start()
    try:
        first = supervisor.wait(threading.Event(), deadline=time.monotonic() + 8, stop_grace=.02)
        assert first.reason == 'exit' and first.canonical['cost'] == .125
        supervisor.fail(PublicError('child_command_channel_error', 'Late channel fault.'))
        later = supervisor.wait(threading.Event(), deadline=time.monotonic(), stop_grace=.02)
        assert later is first
        assert later.public_error is None
    finally:
        cleanup_case(supervisor, scope)


def test_channel_failure_preserves_a1_canonical_usage_and_cost(tmp_path, monkeypatch):
    supervisor, scope, _, _, _ = prepare_case(tmp_path, monkeypatch, targets.canonical_then_broken_channel)
    supervisor.start()
    try:
        outcome = supervisor.wait(threading.Event(), deadline=time.monotonic() + 8, stop_grace=.02)
        assert outcome.reason == 'error'
        assert outcome.public_error.code == 'child_command_channel_error'
        assert outcome.canonical['usage']['total'] == 18
        assert outcome.canonical['cost'] == .125
        assert outcome.canonical_settlement_id == 'round-1'
    finally:
        cleanup_case(supervisor, scope)


def test_parent_broker_unknown_curl_cannot_execute_without_authorization(tmp_path,monkeypatch):
    command='''B=http://127.0.0.1:8123; curl -o '#1' "$B/{../escaped,safe}"'''
    supervisor,scope,_,_,_=prepare_case(tmp_path,monkeypatch,targets.parent_owned_probe,
        state=_general_state(),command_tool='bash',command=command)
    supervisor.start()
    try:
        outcome=supervisor.wait(threading.Event(),deadline=time.monotonic()+8,stop_grace=.02)
        assert outcome.reason=='exit'
        output=json.loads((tmp_path/'command-output.json').read_text())
        assert 'authorization' in output.lower()
        assert all(record.execution is None for record in scope._requests.values())
    finally:
        cleanup_case(supervisor,scope)
