"""Backend selection and injected ConPTY adapter tests."""
from __future__ import annotations

from pathlib import Path
import subprocess

from nz_coder.runtime.process_backends import (
    ConPtyBackend,
    PosixPtyBackend,
    _WindowsJob,
    create_process_backend,
)


class _FakePtyProcess:
    pid = 123

    def __init__(self) -> None:
        self.reads = ["READY\r\n", ""]
        self.writes = []
        self.sizes = []
        self.alive = True
        self.exitstatus = 0
        self.closed = False

    def read(self, _size):
        return self.reads.pop(0)

    def write(self, value):
        self.writes.append(value)

    def setwinsize(self, rows, cols):
        self.sizes.append((rows, cols))

    def isalive(self):
        return self.alive

    def terminate(self, force=False):
        self.alive = False

    def close(self, force=False):
        self.closed = True


class _FakeFactory:
    spawned = None
    process = _FakePtyProcess()

    @classmethod
    def spawn(cls, command, **kwargs):
        cls.spawned = (command, kwargs)
        return cls.process


class _FakeWinPty:
    PtyProcess = _FakeFactory


def _which(name):
    return {"pwsh.exe": r"C:\PowerShell\pwsh.exe"}.get(name)


def test_conpty_adapter_start_read_write_resize_and_ctrl_c(tmp_path: Path):
    backend = create_process_backend(
        "python -i",
        cwd=tmp_path,
        tty=True,
        rows=24,
        cols=80,
        os_name="nt",
        which=_which,
        winpty_module=_FakeWinPty,
    )

    assert isinstance(backend, ConPtyBackend)
    assert backend.tty is True
    assert _FakeFactory.spawned[0] == [
        r"C:\PowerShell\pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        "python -i",
    ]
    assert _FakeFactory.spawned[1]["dimensions"] == (24, 80)
    assert backend.read_bytes(1024) == b"READY\r\n"
    backend.write_bytes("中文\n".encode())
    backend.write_bytes(b"already\r\n")
    backend.write_bytes(b"\x03")
    backend.resize(rows=40, cols=120)
    backend.resize(rows=60, cols=200)
    assert _FakeFactory.process.writes[-3:] == ["中文\r\n", "already\r\n", "\x03"]
    assert _FakeFactory.process.sizes == [(40, 120), (60, 200)]
    backend.terminate_tree(grace_seconds=0)
    backend.close()
    assert backend.poll() == 0
    assert _FakeFactory.process.closed is True


def test_conpty_answers_terminal_device_queries_before_next_read():
    process = _FakePtyProcess()
    process.reads = ["\x1b[c\x1b[?1004h\x1b[?9001h", "READY"]
    backend = ConPtyBackend(process, rows=24, cols=80)

    assert backend.read_bytes(1024) == b"READY"
    assert process.writes == ["\x1b[?1;2c"]


def test_conpty_closed_pty_is_a_normal_end_of_stream():
    process = _FakePtyProcess()
    process.reads = []

    def closed(_size):
        raise EOFError("Pty is closed")

    process.read = closed
    backend = ConPtyBackend(process)

    assert backend.read_bytes(1024) == b""


def test_windows_tty_falls_back_to_pipe_when_winpty_is_missing(tmp_path, monkeypatch):
    class _Pipe:
        pid = 7
        stdin = None
        stdout = None
        stderr = None

        def poll(self): return 0
        def wait(self, timeout=None): return 0
        def terminate(self): pass
        def kill(self): pass

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: _Pipe())
    backend = create_process_backend(
        "echo ok", cwd=tmp_path, tty=True, rows=24, cols=80,
        os_name="nt", which=_which, winpty_module=False,
    )
    assert backend.tty is False
    assert backend.lifecycle_mode == "windows-taskkill-fallback"


class _FakeKernel32:
    def __init__(self) -> None:
        self.calls = []
        self.limit_flags = 0

    def CreateJobObjectW(self, _security, _name):
        self.calls.append("create")
        return 101

    def SetInformationJobObject(self, handle, info_class, info, _size):
        self.calls.append(("configure", int(handle), int(info_class)))
        self.limit_flags = int(info._obj.BasicLimitInformation.LimitFlags)
        return 1

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("open", int(access), bool(inherit), int(pid)))
        return 202

    def AssignProcessToJobObject(self, job, process):
        self.calls.append(("assign", int(job), int(process)))
        return 1

    def CloseHandle(self, handle):
        self.calls.append(("close", int(handle)))
        return 1

    def TerminateJobObject(self, handle, code):
        self.calls.append(("terminate", int(handle), int(code)))
        return 1


def test_windows_job_sets_kill_on_close_before_binding_owned_pid():
    kernel32 = _FakeKernel32()

    job = _WindowsJob.bind(4242, os_name="nt", kernel32=kernel32)

    assert job is not None
    assert kernel32.limit_flags & 0x00002000
    configure = next(i for i, call in enumerate(kernel32.calls) if call[0] == "configure")
    assign = next(i for i, call in enumerate(kernel32.calls) if call[0] == "assign")
    assert configure < assign
    assert ("open", 0x0101, False, 4242) in kernel32.calls
    job.close()


def test_conpty_uses_job_for_tree_termination(tmp_path, monkeypatch):
    class _Job:
        def __init__(self):
            self.terminated = False
            self.closed = False

        def terminate(self):
            self.terminated = True
            _FakeFactory.process.alive = False
            return True

        def close(self):
            self.closed = True

    job = _Job()
    monkeypatch.setattr(
        _WindowsJob,
        "bind",
        classmethod(lambda cls, pid, **kwargs: job),
    )
    _FakeFactory.process = _FakePtyProcess()

    backend = create_process_backend(
        "python -i",
        cwd=tmp_path,
        tty=True,
        rows=24,
        cols=80,
        os_name="nt",
        which=_which,
        winpty_module=_FakeWinPty,
    )
    assert backend.lifecycle_mode == "windows-job-object"
    backend.terminate_tree(grace_seconds=0)
    backend.close()

    assert job.terminated is True
    assert job.closed is True


def test_posix_backend_reports_process_group_lifecycle():
    class Process:
        pid = 71

    backend = PosixPtyBackend(Process(), -1)

    assert backend.lifecycle_mode == "posix-process-group"


def test_conpty_without_job_uses_pid_scoped_taskkill_fallback(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        _WindowsJob,
        "bind",
        classmethod(lambda cls, pid, **kwargs: None),
    )
    monkeypatch.setattr(
        "nz_coder.runtime.process_backends.subprocess.run",
        lambda argv, **kwargs: (
            calls.append((argv, kwargs))
            or subprocess.CompletedProcess(argv, 0)
        ),
    )
    _FakeFactory.process = _FakePtyProcess()
    backend = create_process_backend(
        "python -i", cwd=tmp_path, tty=True, rows=24, cols=80,
        os_name="nt", which=_which, winpty_module=_FakeWinPty,
    )

    backend.terminate_tree(grace_seconds=0)

    assert backend.lifecycle_mode == "windows-taskkill-fallback"
    assert calls[0][0] == ["taskkill", "/PID", "123", "/T", "/F"]
