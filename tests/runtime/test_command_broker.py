"""Private parent scopes preserve ownership, admission and native completion."""
from __future__ import annotations

import json
import shlex
import sys
import threading
import time

import pytest

from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.runtime.core.execution_context import CommandPolicyRestriction
from tests.fixtures.process_lifecycle import ControlledBackend, wait_until, make_command_scope as make_scope, make_command_authorizer



def call(scope, request_id, tool, arguments):
    scope.begin_request(request_id)
    scope.submit_request(request_id, tool, arguments, CommandPolicyRestriction())
    wait_until(lambda: scope.peek_reply() is not None)
    response_id, result = scope.peek_reply()
    assert response_id == request_id
    scope.reply_sent(request_id)
    return result


def python_command(script):
    return shlex.join((sys.executable, '-u', '-c', script))


@pytest.mark.parametrize('command', ['pytest', 'pytest tests/other', 'pytest tests/unit/test_sample.py'])
def test_process_start_does_not_restrict_test_selection(tmp_path, monkeypatch, command):
    from nz_coder.runtime.process import process_service

    backend = ControlledBackend()
    backend.auto_stop = True
    created = []
    def create(command, **kwargs):
        created.append(command)
        return backend
    monkeypatch.setattr(process_service, 'create_process_backend', create)
    scope = make_scope(tmp_path)
    try:
        result = call(scope, 1, 'process', {'operation': 'start', 'command': command, 'tty': False})
        assert not result.startswith('Error:'), result
        assert created == [command]
    finally:
        backend.recover()
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux same-group failure observation')
def test_failed_final_group_signal_retains_bash_owner_and_lease(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    blocked, attempted = threading.Event(), threading.Event()
    blocked.set()
    real = os.killpg
    def killpg(pid, sig):
        attempted.set()
        if blocked.is_set():
            raise PermissionError('controlled group signal rejection')
        return real(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    scope = make_scope(tmp_path)
    script = ('import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"], '
              'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); open("descendant.pid","w").write(str(p.pid))')
    record = None
    try:
        scope.begin_request(1)
        scope.submit_request(1, 'bash', {'command': python_command(script)}, CommandPolicyRestriction())
        record = scope._requests[1]
        assert attempted.wait(2)
        assert not record.execution._done.wait(.1)
        assert record.worker.is_alive() and record.lease_id
        assert scope.peek_reply() is None
        assert not scope.close(deadline=time.monotonic())
        assert capability_leases().get(record.lease_id) is not None
        blocked.clear()
        assert scope.close(deadline=time.monotonic() + 3)
        assert not record.worker.is_alive() and not record.execution._reader.is_alive()
        assert record.execution._process.returncode == 0 and record.execution._process.stdout.closed
    finally:
        blocked.clear()
        assert scope.close(deadline=time.monotonic() + 3)
        path = tmp_path / 'descendant.pid'
        if path.exists():
            pid = int(path.read_text())
            stat = Path(f'/proc/{pid}/stat')
            if stat.exists() and not stat.read_text().split(') ', 1)[1].startswith('Z'):
                os.kill(pid, 9)


def test_same_root_siblings_have_independent_services(tmp_path):
    from nz_coder.runtime.process.process_service import close_workspace_process_service, workspace_process_service

    a, b = make_scope(tmp_path, 'a', command_authorizer=make_command_authorizer(tmp_path)), make_scope(tmp_path, 'b', command_authorizer=make_command_authorizer(tmp_path))
    try:
        one = json.loads(call(a, 1, 'process', {'operation': 'start', 'tty': False, 'command': python_command('import time; time.sleep(30)')}))
        two = json.loads(call(b, 1, 'process', {'operation': 'start', 'tty': False, 'command': python_command("import sys\nfor x in sys.stdin: print(x.strip(), flush=True)")}))
        assert one['process']['pid'] != two['process']['pid']
        assert a.close(deadline=time.monotonic() + 2)
        process_id = two['process']['process_id']
        call(b, 2, 'process', {'operation': 'write', 'process_id': process_id, 'data': 'sibling-alive\n'})
        read = json.loads(call(b, 3, 'process', {'operation': 'read', 'process_id': process_id, 'wait_seconds': 1}))
        assert 'sibling-alive' in read['output']
        assert workspace_process_service(tmp_path).list(owner_session_id='b') == []
    finally:
        assert a.close(deadline=time.monotonic() + 2)
        assert b.close(deadline=time.monotonic() + 2)
        close_workspace_process_service(tmp_path)


def test_foreign_process_id_and_client_owner_are_rejected(tmp_path):
    a, b = make_scope(tmp_path, 'a'), make_scope(tmp_path, 'b')
    try:
        started = json.loads(call(a, 1, 'process', {'operation': 'start', 'tty': False, 'command': python_command('import time; time.sleep(30)')}))
        result = call(b, 1, 'process', {'operation': 'kill', 'process_id': started['process']['process_id']})
        assert result.startswith('Error:')
        assert json.loads(call(b, 2, 'process', {'operation': 'list'}))['processes'] == []
        b.begin_request(3)
        with pytest.raises(PermissionError):
            b.submit_request(3, 'bash', {'command': 'echo bad', 'owner_session_id': 'a'}, CommandPolicyRestriction())
    finally:
        assert a.close(deadline=time.monotonic() + 2)
        assert b.close(deadline=time.monotonic() + 2)


def test_begin_reservation_survives_cancelled_blocked_io(tmp_path, monkeypatch):
    from nz_coder.runtime.process import process_service

    backend = ControlledBackend()
    backend.io_release.clear()
    monkeypatch.setattr(process_service, 'create_process_backend', lambda *a, **k: backend)
    scope = make_scope(tmp_path, max_requests=2)
    try:
        started = json.loads(call(scope, 1, 'process', {'operation': 'start', 'command': 'controlled'}))
        process_id = started['process']['process_id']
        for request_id, operation in [(2, 'write'), (3, 'resize')]:
            scope.begin_request(request_id)
            scope.submit_request(request_id, 'process', {'operation': operation, 'process_id': process_id, 'data': 'x'}, CommandPolicyRestriction())
        assert backend.io_entered.wait(2)
        for request_id in [2, 3]:
            scope.cancel_request(request_id)
        assert scope.peek_reply() is None
        with pytest.raises(ValueError, match='limit'):
            scope.begin_request(4)
        assert not scope.close(deadline=time.monotonic())
        assert capability_leases().list_workspace(tmp_path)
    finally:
        backend.recover()
        assert scope.close(deadline=time.monotonic() + 2)
    assert backend.closed
    assert not capability_leases().list_workspace(tmp_path)


def test_stop_closes_admission_before_starting_snapshot(tmp_path, monkeypatch):
    from nz_coder.runtime.process import process_service

    entered, release = threading.Event(), threading.Event()
    backend = ControlledBackend()
    def create(*args, **kwargs):
        entered.set()
        release.wait()
        return backend
    monkeypatch.setattr(process_service, 'create_process_backend', create)
    scope = make_scope(tmp_path)
    scope.begin_request(1)
    scope.submit_request(1, 'process', {'operation': 'start', 'command': 'controlled'}, CommandPolicyRestriction())
    try:
        assert entered.wait(2)
        scope.request_stop()
        with pytest.raises(ValueError, match='draining'):
            scope.begin_request(2)
        assert not scope.close(deadline=time.monotonic())
    finally:
        backend.recover()
        release.set()
        assert scope.close(deadline=time.monotonic() + 2)
    assert backend.closed


def test_bash_lease_precedes_create_and_revoke_retains_pending_owner(tmp_path, monkeypatch):
    from nz_coder.runtime.process import bash_execution

    read_entered, release = threading.Event(), threading.Event()
    actual_popen = bash_execution.subprocess.Popen
    def create(*args, **kwargs):
        leases = capability_leases().list_workspace(tmp_path)
        assert any(lease.kind == 'bash-command' for lease in leases)
        process = actual_popen(*args, **kwargs)
        original = process.stdout
        class Stream:
            def read(self, size):
                read_entered.set()
                release.wait()
                return original.read(size)
            def close(self):
                assert release.is_set()
                original.close()
        process.stdout = Stream()
        return process
    monkeypatch.setattr(bash_execution.subprocess, 'Popen', create)
    scope = make_scope(tmp_path)
    scope.begin_request(1)
    scope.submit_request(1, 'bash', {'command': python_command('import time; time.sleep(30)')}, CommandPolicyRestriction())
    try:
        assert read_entered.wait(2)
        revoked = capability_leases().revoke_workspace(tmp_path)
        assert revoked.failed == 1 and revoked.revoked == 0
        assert capability_leases().list_workspace(tmp_path)
        assert not scope.close(deadline=time.monotonic())
    finally:
        release.set()
        assert scope.close(deadline=time.monotonic() + 2)
    assert capability_leases().list_workspace(tmp_path) == []


@pytest.mark.parametrize('failure', ['lease', 'popen', 'worker_start'])
def test_creation_failure_leaves_no_unowned_resource(tmp_path, monkeypatch, failure):
    scope = make_scope(tmp_path)
    if failure == 'lease':
        monkeypatch.setattr(capability_leases(), 'create', lambda **kwargs: (_ for _ in ()).throw(OSError('controlled lease')))
    elif failure == 'popen':
        from nz_coder.runtime.process import bash_execution
        monkeypatch.setattr(bash_execution.subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(OSError('controlled popen')))
    else:
        original = threading.Thread.start
        def start(thread):
            if thread.name.startswith('nz-command-'):
                raise RuntimeError('controlled worker start')
            return original(thread)
        monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        result = call(scope, 1, 'bash', {'command': 'echo should-not-run'})
        assert result.startswith('Error:')
    finally:
        assert scope.close(deadline=time.monotonic() + 2)
    assert not capability_leases().list_workspace(tmp_path)


def test_policy_ready_is_required_and_only_initializes_once(tmp_path):
    scope = make_scope(tmp_path, ready=False)
    try:
        with pytest.raises(ValueError, match='initialized'):
            scope.begin_request(1)
        with pytest.raises(ValueError, match='policy'):
            scope.policy_ready('different')
        assert scope.close(deadline=time.monotonic() + 2)
        scope = make_scope(tmp_path)
        with pytest.raises(ValueError, match='initialized'):
            scope.policy_ready('initial')
        assert str(call(scope, 1, 'bash', {'command': 'echo ready'})) == 'ready'
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_policy_mismatch_reports_single_channel_error_even_while_draining(tmp_path):
    from nz_coder.protocol.public_error import PublicError

    errors = []
    scope = make_scope(tmp_path, ready=False, errors=errors)
    try:
        with pytest.raises(ValueError, match='policy'):
            scope.policy_ready('wrong')
        error = PublicError('child_command_channel_error', 'Command channel initialization failed.')
        scope.connection_lost(error)
        scope.connection_lost(error)
        assert errors == [error]
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_cancel_of_blocked_rpc_keeps_final_response_pending(tmp_path, monkeypatch):
    from nz_coder.runtime.process import process_service

    backend = ControlledBackend()
    backend.io_release.clear()
    monkeypatch.setattr(process_service, 'create_process_backend', lambda *a, **k: backend)
    scope = make_scope(tmp_path, max_requests=1, command_authorizer=make_command_authorizer(tmp_path))
    try:
        started = json.loads(call(scope, 1, 'process', {'operation': 'start', 'command': 'controlled'}))
        scope.begin_request(2)
        scope.submit_request(2, 'process', {'operation': 'write', 'process_id': started['process']['process_id'], 'data': 'x'}, CommandPolicyRestriction())
        assert backend.io_entered.wait(2)
        scope.cancel_request(2)
        assert scope.peek_reply() is None
        with pytest.raises(ValueError, match='limit'):
            scope.begin_request(3)
        backend.recover()
        wait_until(lambda: scope.peek_reply() is not None)
        scope.reply_sent(2)
    finally:
        backend.recover()
        assert scope.close(deadline=time.monotonic() + 2)


def retrieval_scope(path, child='child'):
    from dataclasses import replace
    scope = make_scope(path, child)
    scope.authority = replace(scope.authority, allowed_tools=frozenset({'grep_search','glob_search','diff_status'}))
    return scope


def start_retrieval(scope, request_id=1):
    scope.begin_request(request_id)
    scope.submit_request(request_id, 'grep_search', {'pattern':'needle'}, CommandPolicyRestriction())
    return scope._requests[request_id]


@pytest.mark.parametrize('tool,args', [
    ('smart_search',{}), ('diff_status', {'cwd':'.'}),
    ('grep_search', {'pattern':'x','argv':[]}), ('glob_search', {'pattern':'x','env':{}}),
    ('grep_search', {'pattern':'x','offset':True}), ('grep_search', {'pattern':'x','head_limit':1.5}),
    ('grep_search', {'pattern':'x','case_insensitive':'yes'}), ('grep_search', {'pattern':'x','include':{}}),
    ('glob_search', {'pattern':3}), ('grep_search', {'pattern':'x','owner':'parent'}),
])
def test_retrieval_request_rejects_extra_fields_and_unlisted_tools(tmp_path, tool, args):
    scope = retrieval_scope(tmp_path)
    try:
        scope.begin_request(1)
        with pytest.raises((PermissionError, ValueError)):
            scope.submit_request(1, tool, args, CommandPolicyRestriction())
        assert scope._requests[1].worker is None
    finally:
        assert scope.close(deadline=time.monotonic()+2)


def test_peek_does_not_stop_active_retrieval(tmp_path, monkeypatch):
    from nz_coder.tools import search
    from tests.runtime.test_auxiliary_execution import argv
    scope = retrieval_scope(tmp_path)
    def handler(*args, runner, **kw):
        return runner.capture(argv('import time; print("ready",flush=True); time.sleep(30)'), cwd=tmp_path, timeout=3).stdout
    monkeypatch.setattr(search, 'execute_grep_search_local', handler)
    try:
        record = start_retrieval(scope)
        wait_until(lambda: record.auxiliary._record is not None and record.auxiliary._record.process is not None)
        native = record.auxiliary._record
        for _ in range(10):
            assert scope.peek_reply() is None
        assert not record.cancel_event.is_set() and native.process.poll() is None
    finally:
        assert scope.close(deadline=time.monotonic()+3)


def test_reply_waits_for_auxiliary_and_worker_physical_exit(tmp_path, monkeypatch):
    from nz_coder.runtime.process.auxiliary import AuxiliaryCommandRunner
    from nz_coder.tools import search
    from tests.runtime.test_auxiliary_execution import argv
    entered, release = threading.Event(), threading.Event()
    real = AuxiliaryCommandRunner._execute
    def execute(self, record):
        real(self, record)
        entered.set()
        release.wait(3)
    monkeypatch.setattr(AuxiliaryCommandRunner, '_execute', execute)
    monkeypatch.setattr(search, 'execute_grep_search_local', lambda *a, runner, **kw: runner.capture(argv('print("ok")'), cwd=tmp_path, timeout=2).stdout)
    scope = retrieval_scope(tmp_path)
    try:
        record = start_retrieval(scope)
        assert entered.wait(2)
        scope.cancel_request(1)
        wait_until(lambda: record.reply_ready)
        assert record.auxiliary._record.native_done.is_set()
        assert scope.peek_reply() is None
        assert not scope.close(deadline=time.monotonic())
        assert 1 in scope._requests
    finally:
        release.set()
        assert scope.close(deadline=time.monotonic()+3)


def test_retrieval_close_uses_one_deadline_for_every_record(tmp_path, monkeypatch):
    from nz_coder.runtime.process.auxiliary import AuxiliaryCommandRunner
    from nz_coder.tools import search
    from tests.runtime.test_auxiliary_execution import argv
    scope = retrieval_scope(tmp_path)
    monkeypatch.setattr(search, 'execute_grep_search_local', lambda *a, runner, **kw: runner.capture(argv('import time; time.sleep(30)'), cwd=tmp_path, timeout=3).stdout)
    records = []
    try:
        records = [start_retrieval(scope, i) for i in (1,2)]
        wait_until(lambda: all(r.auxiliary._record is not None for r in records))
        real, observed = AuxiliaryCommandRunner.close, []
        def close(self, *, deadline):
            observed.append((self.request_id, deadline))
            return real(self, deadline=deadline)
        with monkeypatch.context() as patch:
            patch.setattr(AuxiliaryCommandRunner, 'close', close)
            deadline = time.monotonic()
            scope.close(deadline=deadline)
        assert {i for i,d in observed if d==deadline} == {1,2}
        assert all(r.cancel_event.is_set() for r in records)
    finally:
        assert scope.close(deadline=time.monotonic()+3)


def test_lost_reply_or_eof_keeps_original_auxiliary_owner(tmp_path, monkeypatch):
    from nz_coder.runtime.process import auxiliary
    from nz_coder.tools import search
    from tests.runtime.test_auxiliary_execution import argv
    blocked = threading.Event()
    blocked.set()
    real = auxiliary.terminate_process_tree
    monkeypatch.setattr(auxiliary, 'terminate_process_tree', lambda *a, **kw: False if blocked.is_set() else real(*a, **kw))
    monkeypatch.setattr(search, 'execute_grep_search_local', lambda *a, runner, **kw: runner.capture(argv('print("done")'), cwd=tmp_path, timeout=2).stdout)
    scope = retrieval_scope(tmp_path)
    try:
        record = start_retrieval(scope)
        wait_until(lambda: record.auxiliary._record is not None and record.auxiliary._record.returncode is not None)
        native = record.auxiliary._record
        scope.connection_lost(None)
        assert not scope.close(deadline=time.monotonic())
        assert record.auxiliary._record is native and scope._requests[1] is record
    finally:
        blocked.clear()
        assert scope.close(deadline=time.monotonic()+3)


def test_retrieval_siblings_capture_context_and_cancel_independently(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import current_workdir
    from nz_coder.state.sessions import active_session_id
    from nz_coder.tools import search, current_tool_cancel_event
    entered = threading.Event()
    release = threading.Event()
    scopes = []
    def handler(*a, runner, **kw):
        root = current_workdir()
        assert active_session_id() == root.name
        assert current_tool_cancel_event() is runner._cancel
        if root.name == 'a':
            entered.set()
            release.wait(3)
        return root.name
    monkeypatch.setattr(search, 'execute_grep_search_local', handler)
    for name in ('a','b'):
        path=tmp_path/name
        path.mkdir()
        scopes.append(retrieval_scope(path, name))
    try:
        a=start_retrieval(scopes[0])
        assert entered.wait(2)
        scopes[0].cancel_request(1)
        assert call(scopes[1], 1, 'grep_search', {'pattern':'x'}) == 'b'
        assert a.cancel_event.is_set() and not scopes[1]._draining
    finally:
        release.set()
        for scope in scopes:
            assert scope.close(deadline=time.monotonic()+3)
