from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.runtime.process import process_service as processes
from nz_coder.runtime.process.process_service import ProcessStateError
from tests.fixtures.process_lifecycle import ControlledBackend, wait_until

from nz_coder.runtime.process.process_service import (
    ProcessOwnershipError,
    ProcessService,
    ProcessStatus,
    close_workspace_process_service,
    dispose_session_processes,
    workspace_process_service,
)


def _python(script: Path, *args: str) -> str:
    argv = [sys.executable, "-u", str(script), *args]
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)


@pytest.mark.parametrize('tty', [False, True])
@pytest.mark.skipif(sys.platform != 'linux', reason='Linux same-group signal failure')
def test_final_group_signal_failure_keeps_original_process_owner(tmp_path, monkeypatch, tty):
    blocked, attempted = threading.Event(), threading.Event()
    blocked.set()
    real = os.killpg
    def killpg(pid, sig):
        attempted.set()
        if blocked.is_set():
            raise PermissionError('controlled group signal rejection')
        return real(pid, sig)
    monkeypatch.setattr(os, 'killpg', killpg)
    script = tmp_path / 'spawn.py'
    script.write_text('import subprocess,sys\np=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nopen("descendant.pid","w").write(str(p.pid))\n')
    service = ProcessService(tmp_path, kill_grace_seconds=.01)
    try:
        handle = service.start(_python(script), cwd=tmp_path, owner_session_id='child', owner_agent_id='child', tty=tty)
        record = service._records[handle.process_id]
        assert attempted.wait(2)
        assert not record.cleanup_done.wait(.1)
        assert record.waiter.is_alive() and record.capability_lease_id
        assert capability_leases().get(record.capability_lease_id) is not None
        with pytest.raises(ProcessStateError):
            service.close(deadline=time.monotonic())
        assert service._records[handle.process_id] is record
        blocked.clear()
        service.close(deadline=time.monotonic() + 3)
        assert record.cleanup_done.is_set() and not record.waiter.is_alive() and not record.reader.is_alive()
    finally:
        blocked.clear()
        service.close(deadline=time.monotonic() + 3)
        path = tmp_path / 'descendant.pid'
        if path.exists():
            pid = int(path.read_text())
            stat = Path(f'/proc/{pid}/stat')
            try:
                # 后代可能在读取状态或补充清理前已被系统回收。
                if not stat.read_text().split(') ', 1)[1].startswith('Z'):
                    os.kill(pid, 9)
            except (FileNotFoundError, ProcessLookupError):
                pass


def _wait_status(
    service: ProcessService,
    process_id: str,
    expected: set[str],
    *,
    session: str = "session-a",
    timeout: float = 5.0,
) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = service.get(process_id, owner_session_id=session).status
        if status in expected:
            return status
        time.sleep(0.02)
    raise AssertionError(f"process did not reach {expected}")


def test_process_service_repl_supports_incremental_write_and_cursor_reads(tmp_path):
    script = tmp_path / "repl.py"
    script.write_text(
        "import sys\n"
        "print('READY', flush=True)\n"
        "for line in sys.stdin:\n"
        "    value = line.strip()\n"
        "    if value == 'quit': break\n"
        "    print(eval(value, {'__builtins__': {}}), flush=True)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=True,
        )
        first = service.read(
            handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
        )
        assert "READY" in first.output

        service.write(handle.process_id, "1 + 1\n", owner_session_id="session-a")
        second = service.read(
            handle.process_id,
            owner_session_id="session-a",
            cursor=first.next_cursor,
            wait_seconds=2,
        )
        assert "2" in second.output
        assert "READY" not in second.output

        service.write(handle.process_id, "6 * 7\n", owner_session_id="session-a")
        third = service.read(
            handle.process_id,
            owner_session_id="session-a",
            cursor=second.next_cursor,
            wait_seconds=2,
        )
        assert "42" in third.output
        assert third.next_cursor > second.next_cursor
        killed = service.kill(handle.process_id, owner_session_id="session-a")
        assert killed.status == ProcessStatus.KILLED.value
    finally:
        service.close()
    assert not service.list(active_only=True)


def test_process_read_cancellation_stops_wait_without_killing_process(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        "import time\nprint('READY', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    handle = service.start(
        _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
    )
    first = service.read(
        handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
    )
    cancel = threading.Event()
    cancel.set()
    stopped = service.read(
        handle.process_id,
        owner_session_id="session-a",
        cursor=first.next_cursor,
        wait_seconds=10,
        cancel_event=cancel,
    )
    assert stopped.cancelled is True
    assert service.get(handle.process_id, owner_session_id="session-a").status == "running"
    service.close()


def test_process_service_bounded_buffer_reports_expired_cursor(tmp_path):
    script = tmp_path / "output.py"
    script.write_text("print('x' * 5000, flush=True)\n", encoding="utf-8")
    service = ProcessService(tmp_path, buffer_bytes=1024)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        _wait_status(service, handle.process_id, {"exited"})
        result = service.read(
            handle.process_id,
            owner_session_id="session-a",
            cursor=0,
            max_bytes=4096,
        )
        assert result.truncated_before_cursor is True
        assert result.buffer_start_cursor > 0
        assert len(result.output.encode()) <= 1024
        assert result.next_cursor == result.buffer_end_cursor
    finally:
        service.close()


def test_process_service_crash_status_and_output_are_retained(tmp_path):
    script = tmp_path / "crash.py"
    script.write_text(
        "import sys\nprint('CRASHING', flush=True)\nsys.exit(7)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        assert _wait_status(service, handle.process_id, {"exited"}) == "exited"
        status = service.get(handle.process_id, owner_session_id="session-a")
        output = service.read(handle.process_id, owner_session_id="session-a")
        assert status.exit_code == 7
        assert "CRASHING" in output.output
    finally:
        service.close()


def test_process_service_decodes_configured_legacy_output(tmp_path, monkeypatch):
    from nz_coder.foundation import config

    payload = "中文错误".encode("gbk")
    script = tmp_path / "legacy.py"
    script.write_text(
        f"import os\nos.write(1, {payload!r})\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROCESS_OUTPUT_ENCODING", "cp936", raising=False)
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        assert _wait_status(service, handle.process_id, {"exited"}) == "exited"
        result = service.read(handle.process_id, owner_session_id="session-a")
        assert result.output == "中文错误"
    finally:
        service.close()


def test_windows_conpty_unavailable_uses_actionable_pipe_fallback(tmp_path, monkeypatch):
    class PipeFallback:
        pid = 4321
        tty = False
        pipefail = False
        os_name = "nt"

        def read_bytes(self, _size):
            return b""

        def write_bytes(self, _data):
            return None

        def resize(self, **_kwargs):
            raise RuntimeError("unavailable")

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate_tree(self, *, grace_seconds):
            return None

        def close(self):
            return None

    monkeypatch.setattr(
        "nz_coder.runtime.process.process_service.create_process_backend",
        lambda *_args, **_kwargs: PipeFallback(),
    )
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            "python server.py",
            cwd=tmp_path,
            owner_session_id="session-a",
            tty=True,
        )
        result = service.read(handle.process_id, owner_session_id="session-a")

        assert handle.tty is False
        assert "Interactive terminal features unavailable" in result.output
        assert "pip install pywinpty" in result.output
    finally:
        service.close()


def test_multiple_processes_are_isolated_by_id_and_session(tmp_path):
    script = tmp_path / "labeled.py"
    script.write_text(
        "import sys, time\nprint(sys.argv[1], flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        first = service.start(
            _python(script, "SERVICE_A"), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        second = service.start(
            _python(script, "SERVICE_B"), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        first_output = service.read(
            first.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
        )
        second_output = service.read(
            second.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
        )
        assert "SERVICE_A" in first_output.output and "SERVICE_B" not in first_output.output
        assert "SERVICE_B" in second_output.output and "SERVICE_A" not in second_output.output
        with pytest.raises(ProcessOwnershipError):
            service.get(first.process_id, owner_session_id="session-b")
        assert len(service.list(owner_session_id="session-a", active_only=True)) == 2
    finally:
        service.close()
    assert not service.list(active_only=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY resize contract")
def test_posix_pty_resize_is_supported(tmp_path):
    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=True,
        )
        resized = service.resize(
            handle.process_id, rows=40, cols=120, owner_session_id="session-a",
        )
        assert resized.tty is True
    finally:
        service.close()


def test_workspace_registry_shares_service_and_session_cleanup_kills_only_owner(tmp_path):
    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    first = workspace_process_service(tmp_path)
    second = workspace_process_service(tmp_path / ".")
    assert first is second
    a = first.start(
        _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
    )
    b = first.start(
        _python(script), cwd=tmp_path, owner_session_id="session-b", tty=False,
    )
    try:
        assert dispose_session_processes(tmp_path, "session-a") == 1
        assert first.get(a.process_id, owner_session_id="session-a").status == "cancelled"
        assert first.get(b.process_id, owner_session_id="session-b").status == "running"
    finally:
        assert close_workspace_process_service(tmp_path) == 1
    assert not first.list(active_only=True)


def test_process_events_do_not_include_output_contents(tmp_path):
    from nz_coder.protocol.session_events import SessionEventBus

    script = tmp_path / "events.py"
    script.write_text("print('secret-value', flush=True)\n", encoding="utf-8")
    bus = SessionEventBus(session_id="session-a")
    service = ProcessService(tmp_path)
    try:
        handle = service.start(
            _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False,
            event_bus=bus,
        )
        _wait_status(service, handle.process_id, {"exited"})
        events = [event.to_dict() for event in bus.recent(20)]
        assert any(event["type"] == "process.started" for event in events)
        assert any(event["type"] == "process.output" for event in events)
        assert any(event["type"] == "process.exited" for event in events)
        assert "secret-value" not in json.dumps(events)
    finally:
        service.close()
        bus.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_service_close_kills_spawned_descendant_process_group(tmp_path):
    parent = tmp_path / "parent.py"
    pid_file = tmp_path / "child.pid"
    parent.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "print('CHILD_READY', flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    handle = service.start(
        _python(parent), cwd=tmp_path, owner_session_id="session-a", tty=False,
    )
    output = service.read(
        handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=2,
    )
    assert "CHILD_READY" in output.output
    child_pid = int(pid_file.read_text(encoding="utf-8"))
    service.close()

    deadline = time.monotonic() + 2
    state = ""
    while time.monotonic() < deadline:
        status_path = Path(f"/proc/{child_pid}/status")
        if not status_path.exists():
            break
        try:
            status_text = status_path.read_text(errors="replace")
        except FileNotFoundError:
            break
        state = next(
            (
                line for line in status_text.splitlines()
                if line.startswith("State:")
            ),
            "",
        )
        if "Z (zombie)" in state:
            break
        time.sleep(0.02)
    assert not Path(f"/proc/{child_pid}").exists() or "Z (zombie)" in state


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux process ownership probe")
def test_kill_sweeps_term_ignoring_child_after_group_leader_exits(tmp_path):
    """组长响应 SIGTERM 先退出时，同组后代仍必须被终止并回收。"""
    import ctypes
    import signal

    libc = ctypes.CDLL(None)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    child_script = tmp_path / "ignores_term.py"
    child_script.write_text(
        "import os, pathlib, signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path('child.pid').write_text(str(os.getpid()))\n"
        "time.sleep(30)\n",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, 'ignores_term.py'])\n"
        "time.sleep(30)\n",
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.03)
    handle = None
    child_pid = None
    try:
        handle = service.start(
            _python(parent), cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        deadline = time.monotonic() + 2
        while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        child_pid = int((tmp_path / "child.pid").read_text())
        assert os.getpgid(child_pid) == handle.pid
        stopped = service.kill(handle.process_id, owner_session_id="session-a")
        assert stopped.status == "killed"
        deadline = time.monotonic() + 1
        reaped = 0
        while not reaped and time.monotonic() < deadline:
            reaped, _status = os.waitpid(child_pid, os.WNOHANG)
            if not reaped:
                time.sleep(0.01)
        assert reaped == child_pid, "command reported killed while its child remained alive"
        child_pid = None
    finally:
        if handle is not None:
            try:
                os.killpg(handle.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        service.close()
        if child_pid is not None:
            os.waitpid(child_pid, 0)
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group contract")
def test_naturally_exited_shell_sweeps_daemonized_child(tmp_path):
    pid_file = tmp_path / "daemon.pid"
    command = (
        f"{shlex.quote(sys.executable)} -c \"import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path('daemon.pid').write_text(str(child.pid))\""
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(
            command, cwd=tmp_path, owner_session_id="session-a", tty=False,
        )
        assert _wait_status(service, handle.process_id, {"exited"}) == "exited"
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        state = ""
        while time.monotonic() < deadline:
            status_path = Path(f"/proc/{child_pid}/status")
            if not status_path.exists():
                break
            try:
                status_text = status_path.read_text(errors="replace")
            except FileNotFoundError:
                break
            state = next(
                (line for line in status_text.splitlines() if line.startswith("State:")),
                "",
            )
            if "Z (zombie)" in state:
                break
            time.sleep(0.02)
        assert not Path(f"/proc/{child_pid}").exists() or "Z (zombie)" in state
    finally:
        service.close()


@pytest.fixture
def controlled_process(tmp_path, monkeypatch):
    backend = ControlledBackend()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = ProcessService(tmp_path, max_processes=1, kill_grace_seconds=0)
    handle = service.start("controlled", cwd=tmp_path, owner_session_id="session-a")
    try:
        yield service, handle, backend
    finally:
        backend.recover()
        service.close()


def test_unconfirmed_kill_keeps_capacity_lease_and_owner(controlled_process):
    service, handle, backend = controlled_process
    record = service._records[handle.process_id]
    lease_id = record.capability_lease_id
    pending = service.kill(handle.process_id, deadline=time.monotonic() + 0.03)
    assert pending.status == "running"
    assert pending.exit_code is None
    assert not record.cleanup_done.is_set()
    assert capability_leases().get(lease_id) is not None
    assert len(service.list(active_only=True)) == 1
    with pytest.raises(ProcessStateError, match="limit"):
        service.start("second", cwd=service.workspace, owner_session_id="session-a")
    backend.recover()
    assert record.cleanup_done.wait(2)
    assert capability_leases().get(lease_id) is None
    assert {ident for _, ident in backend.controls} == {record.waiter.ident}


def test_wait_and_poll_failure_retry_without_query_driver(
    controlled_process, monkeypatch
):
    service, handle, backend = controlled_process
    backend.control_fault.set()
    record = service._records[handle.process_id]
    wait_until(lambda: len(backend.controls) >= 4)
    other = ControlledBackend()
    other.auto_stop = True
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: other)
    another = ProcessService(service.workspace, kill_grace_seconds=0)
    try:
        new = another.start(
            "another", cwd=service.workspace, owner_session_id="session-b"
        )
        assert another.kill(new.process_id).status == "killed"
        assert record.waiter.is_alive()
        backend.recover()
        assert record.cleanup_done.wait(2)
        assert backend.closed
    finally:
        another.close()


def test_body_exit_waits_for_real_reader_and_tail_output(controlled_process):
    service, handle, backend = controlled_process
    record = service._records[handle.process_id]
    lease_id = record.capability_lease_id
    backend.body_exit.set()
    assert backend.stop_seen.wait(1)
    assert service.get(handle.process_id).status == "running"
    assert not record.cleanup_done.is_set()
    assert capability_leases().get(lease_id)
    backend.tail = b"last output\n"
    backend.eof.set()
    assert record.cleanup_done.wait(2)
    assert service.read(handle.process_id).output == "last output\n"
    assert backend.closed


@pytest.mark.parametrize("operation", ["write", "resize"])
@pytest.mark.parametrize("failure", ["exception", "blocking"])
def test_backend_close_failure_and_inflight_io_keep_record_live(
    controlled_process, operation, failure
):
    service, handle, backend = controlled_process
    record = service._records[handle.process_id]
    backend.io_release.clear()
    if failure == "exception":
        backend.close_fault.set()
    else:
        backend.close_release.clear()
    io = threading.Thread(
        target=lambda: (
            service.write(handle.process_id, "input")
            if operation == "write"
            else service.resize(handle.process_id, rows=40, cols=120)
        )
    )
    io.start()
    try:
        assert backend.io_entered.wait(1)
        assert (
            service.kill(handle.process_id, deadline=time.monotonic() + 0.03).status
            == "running"
        )
        backend.body_exit.set()
        backend.eof.set()
        assert not backend.close_entered.wait(0.06)
        with pytest.raises(ProcessStateError):
            service.write(handle.process_id, "late")
        backend.io_release.set()
        io.join(1)
        assert not io.is_alive()
        assert backend.close_entered.wait(1)
        assert not record.cleanup_done.is_set()
        assert record.backend is backend
        backend.close_fault.clear()
        backend.close_release.set()
        assert record.cleanup_done.wait(2)
        assert backend.closed
    finally:
        backend.recover()
        io.join(1)


@pytest.mark.parametrize("failure", ["lease", "worker", "reader"])
def test_start_failure_keeps_resource_owner(tmp_path, monkeypatch, failure):
    backend = ControlledBackend()
    backend.auto_stop = True
    created = []

    def create(*a, **kw):
        created.append(threading.get_ident())
        return backend

    monkeypatch.setattr(processes, "create_process_backend", create)
    if failure == "lease":
        monkeypatch.setattr(
            capability_leases(),
            "create",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("lease unavailable")),
        )
    else:
        original = threading.Thread.start

        def start(thread):
            prefix = "nz-process-wait-" if failure == "worker" else "nz-process-read-"
            if thread.name.startswith(prefix):
                raise RuntimeError("thread unavailable")
            return original(thread)

        monkeypatch.setattr(threading.Thread, "start", start)
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    try:
        if failure in {"lease", "worker"}:
            with pytest.raises(RuntimeError, match="unavailable"):
                service.start("controlled", cwd=tmp_path, owner_session_id="session-a")
            assert not created
        else:
            handle = service.start(
                "controlled", cwd=tmp_path, owner_session_id="session-a"
            )
            record = service._records[handle.process_id]
            assert record.cleanup_done.wait(2)
            assert backend.closed
            assert created == [record.waiter.ident]
            assert {ident for _, ident in backend.controls} == {record.waiter.ident}
    finally:
        backend.recover()
        service.close()


def test_starting_backend_is_retained_during_close(tmp_path, monkeypatch):
    backend = ControlledBackend()
    backend.auto_stop = True
    entered, release = threading.Event(), threading.Event()

    def create(*a, **kw):
        entered.set()
        release.wait()
        return backend

    monkeypatch.setattr(processes, "create_process_backend", create)
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    starter = threading.Thread(
        target=lambda: service.start(
            "controlled", cwd=tmp_path, owner_session_id="session-a"
        )
    )
    starter.start()
    try:
        assert entered.wait(1)
        with pytest.raises(ProcessStateError):
            service.close(deadline=time.monotonic() + 0.03)
        with pytest.raises(ProcessStateError, match="draining|closed"):
            service.start("late", cwd=tmp_path, owner_session_id="session-a")
    finally:
        release.set()
        starter.join(2)
        backend.recover()
        service.close()
    assert backend.closed


@pytest.mark.parametrize("stop_first", [True, False])
def test_stop_acceptance_and_natural_exit_are_linearized(
    controlled_process, stop_first
):
    service, handle, backend = controlled_process
    record = service._records[handle.process_id]
    if stop_first:
        service.kill(
            handle.process_id, reason=ProcessStatus.CANCELLED, deadline=time.monotonic()
        )
        service.kill(handle.process_id, deadline=time.monotonic())
    backend.recover()
    assert record.cleanup_done.wait(2)
    expected = "cancelled" if stop_first else "exited"
    assert service.kill(handle.process_id).status == expected


def test_lifecycle_callback_failure_does_not_hold_resources(tmp_path, monkeypatch):
    class Bus:
        def publish(self, *a, **kw):
            raise ValueError("callback failed")

    backend = ControlledBackend()
    backend.auto_stop = True
    bus = Bus()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    try:
        handle = service.start(
            "controlled", cwd=tmp_path, owner_session_id="session-a", event_bus=bus
        )
        lease_id = service._records[handle.process_id].capability_lease_id
        assert service.kill(handle.process_id).status == "killed"
        assert capability_leases().get(lease_id) is None
    finally:
        backend.recover()
        service.close()


def test_kill_deadline_returns_real_running_handle(controlled_process):
    service, handle, backend = controlled_process
    assert (
        service.kill(handle.process_id, deadline=time.monotonic() - 1).status
        == "running"
    )
    assert backend.stop_seen.wait(1)
    assert backend.controls[0][1] != threading.get_ident()


def test_close_timeout_keeps_draining_registry_and_native_worker(tmp_path, monkeypatch):
    backend = ControlledBackend()
    backend.auto_stop = True
    entered, release = threading.Event(), threading.Event()

    class Bus:
        def publish(self, kind, properties):
            if kind == "process.exited":
                entered.set()
                release.wait()

    bus = Bus()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = workspace_process_service(tmp_path)
    service._max_processes_override = 1
    handle = service.start(
        "owned", cwd=tmp_path, owner_session_id="session-a", event_bus=bus
    )
    record = service._records[handle.process_id]
    try:
        service.kill(handle.process_id, deadline=time.monotonic() + 1)
        assert entered.wait(1)
        assert record.cleanup_done.is_set()
        assert record.waiter.is_alive()
        # 构造容量清理阈值；未真正退出的记录仍不可 prune。
        with service._lock:
            service._records["threshold"] = record
            service._prune_terminal_locked()
            assert handle.process_id in service._records
            del service._records["threshold"]
        with pytest.raises(ProcessStateError):
            close_workspace_process_service(tmp_path, deadline=time.monotonic() + 0.03)
        assert workspace_process_service(tmp_path) is service
        with pytest.raises(ProcessStateError):
            service.start("late", cwd=tmp_path, owner_session_id="session-a")
        with pytest.raises(ProcessStateError):
            service.kill_session("session-a", deadline=time.monotonic())
    finally:
        release.set()
        backend.recover()
        close_workspace_process_service(tmp_path)
    assert not record.waiter.is_alive()


@pytest.mark.parametrize("entry", ["session", "service", "all"])
@pytest.mark.parametrize("budget", [0, 0.04])
def test_batch_deadline_requests_all_before_waiting(
    tmp_path, monkeypatch, entry, budget
):
    backends = [ControlledBackend() for _ in range(3)]
    iterator = iter(backends)
    monkeypatch.setattr(
        processes, "create_process_backend", lambda *a, **kw: next(iterator)
    )
    services = []
    for i in range(3 if entry == "all" else 1):
        workspace = tmp_path / str(i)
        workspace.mkdir()
        service = workspace_process_service(workspace)
        services.append(service)
        for _ in range(1 if entry == "all" else 3):
            service.start("owned", cwd=workspace, owner_session_id="session-a")
    target_records = {id(record) for service in services for record in service._records.values()}
    seen = []
    original = ProcessService._await_cleanup

    def await_cleanup(record, deadline, *, join_worker):
        # 第一次等待前，每个目标都已登记请求，不依赖 worker 调度。
        assert all(
            r.requested_terminal is not None
            for s in services
            for r in s._records.values()
        )
        if id(record) in target_records:
            seen.append(deadline)
        return original(record, deadline, join_worker=join_worker)

    monkeypatch.setattr(ProcessService, "_await_cleanup", staticmethod(await_cleanup))
    deadline = time.monotonic() + budget
    try:
        with pytest.raises(ProcessStateError):
            if entry == "session":
                services[0].kill_session("session-a", deadline=deadline)
            elif entry == "service":
                services[0].close(deadline=deadline)
            else:
                processes.close_all_process_services(deadline=deadline)
        assert seen == [deadline] * 3
        assert time.monotonic() < deadline + 0.2
        for service in services:
            assert workspace_process_service(service.workspace) is service
    finally:
        for backend in backends:
            backend.recover()
        for service in services:
            close_workspace_process_service(service.workspace)


def test_registry_close_failure_retries_same_owner(tmp_path, monkeypatch):
    backend = ControlledBackend()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = workspace_process_service(tmp_path)
    handle = service.start("owned", cwd=tmp_path, owner_session_id="session-a")
    record = service._records[handle.process_id]
    try:
        with pytest.raises(ProcessStateError):
            close_workspace_process_service(tmp_path, deadline=time.monotonic())
        assert workspace_process_service(tmp_path) is service
        assert record.waiter.is_alive()
        assert not record.cleanup_done.is_set()
    finally:
        backend.recover()
        close_workspace_process_service(tmp_path)
    assert tmp_path.resolve() not in processes._WORKSPACE_SERVICES


def test_close_all_continues_and_aggregates_failures(tmp_path, monkeypatch):
    backends = [ControlledBackend(), ControlledBackend(), ControlledBackend()]
    iterator = iter(backends)
    monkeypatch.setattr(
        processes, "create_process_backend", lambda *a, **kw: next(iterator)
    )
    services = []
    for i in range(3):
        workspace = tmp_path / str(i)
        workspace.mkdir()
        service = workspace_process_service(workspace)
        service.start("owned", cwd=workspace, owner_session_id="session-a")
        services.append(service)
    backends[1].recover()
    middle = next(iter(services[1]._records.values()))
    assert middle.cleanup_done.wait(2)
    middle.waiter.join(1)
    try:
        with pytest.raises(ProcessStateError) as failure:
            processes.close_all_process_services(deadline=time.monotonic() + 0.03)
        assert str(services[0].workspace) in str(failure.value)
        assert str(services[2].workspace) in str(failure.value)
        assert services[1].workspace not in processes._WORKSPACE_SERVICES
        for i in (0, 2):
            assert workspace_process_service(services[i].workspace) is services[i]
    finally:
        for backend in backends:
            backend.recover()
        for service in services:
            close_workspace_process_service(service.workspace)


def test_old_close_cannot_remove_replacement_service(tmp_path, monkeypatch):
    old = workspace_process_service(tmp_path)
    replacement = ProcessService(tmp_path)

    def replace(*, deadline=None):
        with processes._REGISTRY_LOCK:
            processes._WORKSPACE_SERVICES[tmp_path.resolve()] = replacement

    monkeypatch.setattr(old, "close", replace)
    close_workspace_process_service(tmp_path)
    assert workspace_process_service(tmp_path) is replacement
    close_workspace_process_service(tmp_path)


def test_lifecycle_control_faults_use_capped_backoff(tmp_path, monkeypatch):
    from types import SimpleNamespace

    backend = ControlledBackend()
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        if len(pauses) == 5:
            backend.recover()

    monkeypatch.setattr(
        processes,
        "time",
        SimpleNamespace(
            monotonic=time.monotonic,
            time=time.time,
            sleep=pause,
        ),
    )
    backend.control_fault.set()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    try:
        handle = service.start("owned", cwd=tmp_path, owner_session_id="session-a")
        assert service._records[handle.process_id].cleanup_done.wait(2)
        assert pauses == [0.05, 0.1, 0.2, 0.5, 1.0]
    finally:
        backend.recover()
        service.close()


def test_reserved_start_is_cancelled_before_backend_creation(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = capability_leases().create
    created = []

    def lease(**kwargs):
        entered.set()
        release.wait()
        return original(**kwargs)

    monkeypatch.setattr(capability_leases(), "create", lease)
    monkeypatch.setattr(
        processes, "create_process_backend", lambda *a, **kw: created.append(True)
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    results = []
    starter = threading.Thread(
        target=lambda: results.append(
            service.start("reserved", cwd=tmp_path, owner_session_id="session-a")
        )
    )
    starter.start()
    try:
        assert entered.wait(1)
        with pytest.raises(ProcessStateError):
            service.close(deadline=time.monotonic() + 0.02)
        assert len(service.list(active_only=True)) == 1
    finally:
        release.set()
        starter.join(2)
        service.close()
    assert not created
    assert results[0].status == "killed"
    assert capability_leases().list_workspace(tmp_path) == []


def test_lifecycle_inherits_start_context_and_captured_grace(tmp_path, monkeypatch):
    from dataclasses import replace

    from nz_coder.runtime.core.run_settings import (
        current_run_settings,
        scoped_run_settings,
    )

    backend = ControlledBackend()
    backend.auto_stop = True
    observations = []
    bound = replace(current_run_settings(), process_kill_grace=0.07)

    def create(*a, **kw):
        observations.append(current_run_settings())
        return backend

    monkeypatch.setattr(processes, "create_process_backend", create)
    service = ProcessService(tmp_path)
    with scoped_run_settings(bound):
        handle = service.start("owned", cwd=tmp_path, owner_session_id="session-a")
    try:
        assert observations == [bound]
        record = service._records[handle.process_id]
        assert record.kill_grace_seconds == 0.07
        assert service.kill(handle.process_id).status == "killed"
    finally:
        backend.recover()
        service.close()


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux escaped stdout writer ownership"
)
def test_escaped_stdout_writer_retains_owner_until_real_eof(tmp_path):
    import ctypes
    import signal

    libc = ctypes.CDLL(None)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    child = tmp_path / "escaped.py"
    child.write_text(
        "import os, pathlib, time\n"
        "pathlib.Path('escaped.pid').write_text(str(os.getpid()))\n"
        "while not pathlib.Path('release').exists(): time.sleep(.01)\n"
        "print('escaped tail', flush=True)\n"
    )
    parent = tmp_path / "launcher.py"
    parent.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-u', 'escaped.py'], start_new_session=True)\n"
    )
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    child_pid = None
    child_reaped = False
    pidfile = tmp_path / "escaped.pid"
    try:
        handle = service.start(
            _python(parent), cwd=tmp_path, owner_session_id="session-a", tty=False
        )
        wait_until(pidfile.exists)
        child_pid = int(pidfile.read_text())
        assert os.getpgid(child_pid) != handle.pid
        record = service._records[handle.process_id]
        lease_id = record.capability_lease_id
        wait_until(lambda: record.exit_confirmed)
        assert (
            service.kill(handle.process_id, deadline=time.monotonic() + 0.03).status
            == "running"
        )
        assert record.reader.is_alive() and record.waiter.is_alive()
        assert capability_leases().get(lease_id) is not None
        with pytest.raises(ProcessStateError):
            service.close(deadline=time.monotonic() + 0.03)
        (tmp_path / "release").touch()
        assert record.cleanup_done.wait(2)
        assert "escaped tail" in service.read(handle.process_id).output
        os.waitpid(child_pid, 0)
        child_reaped = True
    finally:
        (tmp_path / "release").touch()
        try:
            service.close()
        finally:
            if not child_reaped and pidfile.exists():
                child_pid = int(pidfile.read_text())
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(child_pid, 0)
            assert libc.prctl(36, previous.value, 0, 0, 0) == 0


def test_stop_retries_after_silent_termination_failure(controlled_process):
    service, handle, backend = controlled_process
    record = service._records[handle.process_id]
    lease_id = record.capability_lease_id
    assert (
        service.kill(handle.process_id, deadline=time.monotonic() + 0.03).status
        == "running"
    )
    assert backend.stop_seen.wait(1)
    assert capability_leases().get(lease_id) is not None
    backend.auto_stop = True
    # 无需再次 status/kill；恢复后仍由原 owner 重试停止。
    assert record.cleanup_done.wait(2)
    assert service.get(handle.process_id).status == "killed"
    assert sum(kind == "terminate" for kind, _ in backend.controls) >= 2
    assert {ident for _, ident in backend.controls} == {record.waiter.ident}


@pytest.mark.skipif(
    os.name == "nt", reason="real POSIX retry after silent signal failure"
)
def test_real_pipe_stop_recovers_after_silent_signal_failure(tmp_path, monkeypatch):
    from nz_coder.runtime.process import process_backends as backends

    script = tmp_path / "wait.py"
    script.write_text("import time\ntime.sleep(60)\n")
    suppress = threading.Event()
    suppress.set()
    actual = backends.terminate_process_tree
    attempted = threading.Event()
    attempts = []

    def signal(process, **kwargs):
        if suppress.is_set():
            attempts.append(kwargs["force"])
            if len(attempts) == 2:
                attempted.set()
            return False
        return actual(process, **kwargs)

    monkeypatch.setattr(backends, "terminate_process_tree", signal)
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    handle = service.start(
        _python(script), cwd=tmp_path, owner_session_id="session-a", tty=False
    )
    record = service._records[handle.process_id]
    owned_backend = record.backend
    try:
        assert (
            service.kill(handle.process_id, deadline=time.monotonic() + 0.03).status
            == "running"
        )
        assert attempted.wait(1)
        suppress.clear()
        assert record.cleanup_done.wait(2)
        assert service.get(handle.process_id).status == "killed"
    finally:
        suppress.clear()
        actual(owned_backend.process, os_name="posix", force=True)
        service.close()


def test_windows_explicit_pipe_preserves_output_without_tty_fallback_hint(
    tmp_path, monkeypatch
):
    backend = ControlledBackend()
    backend.tty = False
    backend.os_name = "nt"
    backend.tail = b"actual output"
    backend.recover()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = ProcessService(tmp_path, kill_grace_seconds=0)
    try:
        handle = service.start(
            "owned", cwd=tmp_path, owner_session_id="session-a", tty=False
        )
        record = service._records[handle.process_id]
        assert record.cleanup_done.wait(2)
        result = service.read(handle.process_id)
        assert result.output == "actual output"
        assert result.buffer_end_cursor == len(b"actual output")
    finally:
        backend.recover()
        service.close()


def test_process_read_from_end_returns_output_arriving_while_waiting(tmp_path):
    script = tmp_path / "delayed.py"
    script.write_text("import time\nprint('READY', flush=True)\ntime.sleep(0.3)\nprint('NEW_OUTPUT', flush=True)\n")
    service = ProcessService(tmp_path, kill_grace_seconds=0.05)
    try:
        handle = service.start(_python(script), cwd=tmp_path, owner_session_id="session-a")
        ready = service.read(handle.process_id, owner_session_id="session-a", cursor=0, wait_seconds=1)
        assert "READY" in ready.output and "NEW_OUTPUT" not in ready.output
        result = service.read(handle.process_id, owner_session_id="session-a", cursor=-1, wait_seconds=2)
        assert "NEW_OUTPUT" in result.output
        assert result.cursor == ready.next_cursor
        assert result.next_cursor > result.cursor
    finally:
        service.close()
