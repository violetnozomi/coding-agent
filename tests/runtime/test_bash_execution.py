"""Owned bash execution keeps real resources until their original owner exits."""
from __future__ import annotations

import io
import os
import shlex
import subprocess
import sys
import threading
import time

import pytest


def _execution(tmp_path, command, on_output=lambda _: True):
    from nz_coder.runtime.process.bash_execution import BashExecution

    return BashExecution((sys.executable, '-c', command), cwd=tmp_path,
                         env=dict(os.environ), deadline=time.monotonic() + 5,
                         kill_grace=0.02, on_output=on_output)


def _run(execution):
    outcome = []
    def owner():
        try:
            outcome.append(execution.run(cancel_event=None))
        except BaseException as exc:
            outcome.append(exc)
    thread = threading.Thread(target=owner)
    thread.start()
    return thread, outcome


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process groups')
def test_natural_leader_exit_sweeps_same_group_background_child(tmp_path):
    from pathlib import Path
    from tests.fixtures.process_lifecycle import wait_until

    chunks = []
    script = ('import subprocess, sys; '
              'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], '
              'stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid, flush=True)')
    execution = _execution(tmp_path, script, lambda data: chunks.append(data) or True)
    pid = None
    try:
        result = execution.run(cancel_event=None)
        pid = int(b''.join(chunks).strip())
        assert result.exit_code == 0 and result.stream_complete
        def stopped():
            stat = Path(f'/proc/{pid}/stat')
            return not stat.exists() or stat.read_text().split(') ', 1)[1].startswith('Z')
        wait_until(stopped, timeout=1)
        assert execution._process.stdout.closed
        assert not execution._reader.is_alive()
    finally:
        if pid is not None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


def test_record_exists_before_popen_and_reader_start(tmp_path, monkeypatch):
    from nz_coder.runtime.process import bash_execution as module
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.bash import execute_bash_local

    records = []
    real_popen = module.subprocess.Popen
    def popen(*args, **kwargs):
        assert len(records) == 1
        return real_popen(*args, **kwargs)
    def create(*args, **kwargs):
        execution = module.BashExecution(*args, **kwargs)
        records.append(execution)
        return execution
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    command = f'{shlex.quote(sys.executable)} -c {shlex.quote("print(123)")}'
    with scoped_workdir(tmp_path):
        result = execute_bash_local(command, create_execution=create)
    assert str(result) == '123'
    assert records[0].close(deadline=time.monotonic())


def test_cancel_does_not_close_stream_owned_by_live_reader(tmp_path, monkeypatch):
    from nz_coder.runtime.process import bash_execution as module

    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    class Stream:
        def read(self, size):
            entered.set()
            release.wait()
            return b''
        def close(self):
            assert release.is_set()
            closed.set()
    class Process:
        stdout = Stream()
        pid = 123
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired('controlled', timeout)
            return self.returncode
    process = Process()
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: process)
    def terminate(*args, **kwargs):
        process.returncode = -9
        return True
    monkeypatch.setattr(module, 'terminate_process_tree', terminate)
    execution = _execution(tmp_path, 'unused')
    owner, result = _run(execution)
    try:
        assert entered.wait(2)
        execution.request_stop()
        assert execution.close(deadline=time.monotonic()) is False
        assert not closed.is_set()
        assert owner.is_alive()
    finally:
        release.set()
        owner.join(2)
    assert not owner.is_alive()
    assert closed.is_set()
    assert execution.close(deadline=time.monotonic()) is True
    assert result[0].cancelled


def test_silent_stop_failure_retries_without_status_poll(tmp_path, monkeypatch):
    from nz_coder.runtime.process import bash_execution as module

    signal_attempted = threading.Event()
    recover = threading.Event()
    actual_stop = module.terminate_process_tree
    created = threading.Event()
    actual_popen = module.subprocess.Popen
    def popen(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        created.set()
        return process
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    def stop(process, **kwargs):
        signal_attempted.set()
        if recover.is_set():
            return actual_stop(process, **kwargs)
        return False
    monkeypatch.setattr(module, 'terminate_process_tree', stop)
    execution = _execution(tmp_path, 'import time; time.sleep(30)')
    owner, result = _run(execution)
    try:
        assert created.wait(2)
        execution.request_stop()
        assert signal_attempted.wait(2)
        assert execution.close(deadline=time.monotonic()) is False
        recover.set()
        owner.join(2)
        assert not owner.is_alive()
        assert result[0].cancelled
    finally:
        recover.set()
        execution.request_stop()
        owner.join(2)


@pytest.mark.parametrize('failure', ['poll', 'wait', 'close'])
def test_poll_wait_or_close_failure_retains_original_owner(tmp_path, monkeypatch, failure):
    from nz_coder.runtime.process import bash_execution as module

    entered = threading.Event()
    recover = threading.Event()
    class Stream(io.BytesIO):
        def close(self):
            if failure == 'close' and not recover.is_set():
                entered.set()
                raise OSError('controlled close')
            super().close()
    class Process:
        stdout = Stream(b'final\n')
        pid = 123
        returncode = 0
        def poll(self):
            if failure == 'poll' and not recover.is_set():
                entered.set()
                raise OSError('controlled poll')
            return 0
        def wait(self, timeout=None):
            if failure == 'wait' and not recover.is_set():
                entered.set()
                raise OSError('controlled wait')
            return 0
    process = Process()
    monkeypatch.setattr(module.subprocess, 'Popen', lambda *a, **k: process)
    monkeypatch.setattr(module, 'terminate_process_tree', lambda *a, **k: True)
    execution = _execution(tmp_path, 'unused')
    owner, _ = _run(execution)
    try:
        assert entered.wait(2)
        assert not execution.close(deadline=time.monotonic())
        assert owner.is_alive()
    finally:
        recover.set()
        owner.join(2)
    assert not owner.is_alive()
    assert process.stdout.closed
    assert execution.close(deadline=time.monotonic())


def test_hard_limit_stops_retention_but_drains_pipe(tmp_path):
    chunks = []
    def output(chunk):
        if not chunks:
            chunks.append(chunk)
        return False
    execution = _execution(tmp_path, "import os\nwhile True: os.write(1,b'x'*8192)", output)
    outcome = execution.run(cancel_event=None)
    assert len(b''.join(chunks)) <= 8192
    assert outcome.output_limit_exceeded
    assert outcome.stream_complete
    assert execution.close(deadline=time.monotonic())


def test_reader_start_failure_reaps_created_process(tmp_path, monkeypatch):
    from nz_coder.runtime.process import bash_execution as module

    created = []
    actual_popen = module.subprocess.Popen
    def create(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        created.append(process)
        return process
    def fail_start(thread):
        raise RuntimeError('controlled Thread.start')
    monkeypatch.setattr(module.subprocess, 'Popen', create)
    monkeypatch.setattr(module.threading.Thread, 'start', fail_start)
    execution = _execution(tmp_path, 'import time; time.sleep(30)')
    with pytest.raises(RuntimeError, match='controlled'):
        execution.run(cancel_event=None)
    assert created[0].poll() is not None
    assert created[0].stdout.closed
    assert execution.close(deadline=time.monotonic())


def test_output_callback_exception_cleans_resources_before_propagating(tmp_path):
    def output(_):
        raise ValueError('controlled reporter')
    execution = _execution(tmp_path, "print('ready'); import time; time.sleep(.05)", output)
    with pytest.raises(ValueError, match='controlled reporter'):
        execution.run(cancel_event=None)
    assert execution.close(deadline=time.monotonic())


def test_reader_construction_failure_reaps_created_process(tmp_path, monkeypatch):
    from nz_coder.runtime.process import bash_execution as module

    created = []
    actual_popen = module.subprocess.Popen
    def create(*args, **kwargs):
        process = actual_popen(*args, **kwargs)
        created.append(process)
        return process
    def fail_thread(*args, **kwargs):
        raise RuntimeError('controlled Thread constructor')
    monkeypatch.setattr(module.subprocess, 'Popen', create)
    execution = _execution(tmp_path, 'import time; time.sleep(30)')
    monkeypatch.setattr(module.threading, 'Thread', fail_thread)
    try:
        with pytest.raises(RuntimeError, match='controlled Thread constructor'):
            execution.run(cancel_event=None)
        assert created[0].poll() is not None
        assert created[0].stdout.closed
        assert execution.close(deadline=time.monotonic())
    finally:
        if created and created[0].poll() is None:
            module.terminate_process_tree(created[0])
            created[0].wait(timeout=2)
            created[0].stdout.close()
