"""Thin process I/O backends owned by :mod:`process_service`.

The service owns lifecycle and events; these adapters only translate PTY,
ConPTY, and pipe operations into one small platform-neutral contract.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import re
import struct
import subprocess
import time
from typing import Callable, Protocol, runtime_checkable

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.runtime.process.platform_runtime import select_shell, terminate_process_tree


@runtime_checkable
class ProcessBackendSession(Protocol):
    """I/O and lifecycle contract implemented by each process transport."""

    pid: int
    tty: bool
    lifecycle_mode: str
    pipefail: bool

    def read_bytes(self, size: int) -> bytes: ...
    def write_bytes(self, data: bytes) -> None: ...
    def resize(self, *, rows: int, cols: int) -> None: ...
    def poll(self) -> int | None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate_tree(self, *, grace_seconds: float) -> None: ...
    def close(self) -> None: ...


def _size(rows: int, cols: int) -> tuple[int, int]:
    return max(1, min(int(rows), 1000)), max(1, min(int(cols), 1000))


class PipeBackend:
    """Portable binary pipe fallback with explicit shell invocation."""

    tty = False
    pipefail = False

    def __init__(
        self, process: subprocess.Popen, *, os_name: str, windows_job=None
    ) -> None:
        self.process = process
        self.pid = int(process.pid)
        self.os_name = os_name
        self._windows_job = windows_job
        self.lifecycle_mode = (
            "windows-job-object"
            if self._windows_job is not None
            else "windows-taskkill-fallback"
            if os_name == "nt"
            else "posix-process-group"
        )

    def read_bytes(self, size: int) -> bytes:
        if self.process.stdout is None:
            return b""
        return os.read(self.process.stdout.fileno(), size)

    def write_bytes(self, data: bytes) -> None:
        if self.process.stdin is None:
            raise OSError("process stdin is unavailable")
        self.process.stdin.write(data)
        self.process.stdin.flush()

    def resize(self, *, rows: int, cols: int) -> None:
        raise RuntimeError("terminal resize is unavailable for pipe processes")

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return int(self.process.wait(timeout=timeout))

    def terminate_tree(self, *, grace_seconds: float) -> None:
        if self._windows_job is not None and self._windows_job.terminate():
            return
        terminate_process_tree(self.process, os_name=self.os_name, force=False)
        if grace_seconds:
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass
        # 组长退出后仍可能有忽略 SIGTERM 的后代，最后一次终止覆盖整组。
        swept = terminate_process_tree(self.process, os_name=self.os_name, force=True)
        if self.os_name != "nt" and not swept:
            raise OSError("process group termination is still pending")

    def close(self) -> None:
        failures = []
        if self._windows_job is not None:
            try:
                self._windows_job.close()
            except Exception as exc:
                failures.append(exc)
            else:
                self._windows_job = None
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except Exception as exc:
                    failures.append(exc)
        if failures:
            raise failures[0]


class PosixPtyBackend:
    """POSIX PTY adapter."""

    tty = True
    pipefail = False

    def __init__(self, process: subprocess.Popen, master_fd: int) -> None:
        self.process = process
        self.master_fd = master_fd
        self.pid = int(process.pid)
        self.lifecycle_mode = "posix-process-group"

    def read_bytes(self, size: int) -> bytes:
        try:
            return os.read(self.master_fd, size)
        except OSError as exc:
            if exc.errno in {errno.EIO, errno.EBADF}:
                return b""
            raise

    def write_bytes(self, data: bytes) -> None:
        os.write(self.master_fd, data)

    def resize(self, *, rows: int, cols: int) -> None:
        _set_posix_terminal_size(self.master_fd, rows=rows, cols=cols)

    def poll(self) -> int | None:
        return self.process.poll()

    def wait(self, timeout: float | None = None) -> int:
        return int(self.process.wait(timeout=timeout))

    def terminate_tree(self, *, grace_seconds: float) -> None:
        terminate_process_tree(self.process, os_name="posix", force=False)
        if grace_seconds:
            try:
                self.process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass
        if not terminate_process_tree(self.process, os_name="posix", force=True):
            raise OSError("process group termination is still pending")

    def close(self) -> None:
        if self.master_fd is not None:
            os.close(self.master_fd)
            self.master_fd = None


class ConPtyBackend:
    """pywinpty adapter; no product state or events are owned here."""

    tty = True
    pipefail = False

    _TERMINAL_QUERY_RESPONSES = {
        "\x1b[c": "\x1b[?1;2c",
        "\x1b[0c": "\x1b[?1;2c",
        "\x1b[5n": "\x1b[0n",
        "\x1b[6n": "\x1b[1;1R",
    }
    _HEADLESS_HOST_SEQUENCES = (
        "\x1b[1t",
        "\x1b[c",
        "\x1b[0c",
        "\x1b[5n",
        "\x1b[6n",
        "\x1b[?1004h",
        "\x1b[?9001h",
    )

    def __init__(
        self,
        process,  # noqa: ANN001
        *,
        windows_job=None,  # noqa: ANN001
        rows: int = 24,
        cols: int = 80,
    ) -> None:
        self.process = process
        self.pid = int(process.pid)
        self._windows_job = windows_job
        self._rows, self._cols = _size(rows, cols)
        self._terminal_control_tail = ""
        self._closed = False
        self.lifecycle_mode = (
            "windows-job-object"
            if windows_job is not None
            else "windows-taskkill-fallback"
        )

    def read_bytes(self, size: int) -> bytes:
        while True:
            try:
                value = self.process.read(size)
            except EOFError:
                return b""
            text = (
                value.decode("utf-8", errors="replace")
                if isinstance(value, bytes)
                else str(value)
            )
            visible = self._consume_terminal_controls(text)
            if visible:
                return visible.encode("utf-8")

    def _consume_terminal_controls(self, value: str) -> str:
        """Answer and hide the VT startup handshake owned by a headless host."""
        combined = self._terminal_control_tail + value
        for query, response in self._TERMINAL_QUERY_RESPONSES.items():
            for _ in range(combined.count(query)):
                self.process.write(response)
        visible = combined
        for sequence in self._HEADLESS_HOST_SEQUENCES:
            visible = visible.replace(sequence, "")
        # PowerShell emits OSC window-title frames when hosted by ConPTY. They
        # are terminal metadata, not process output for the Agent transcript.
        visible = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", visible)
        osc_start = visible.rfind("\x1b]")
        if osc_start >= 0:
            self._terminal_control_tail = visible[osc_start:]
            visible = visible[:osc_start]
        else:
            self._terminal_control_tail = ""
        prefixes = {
            sequence[:length]
            for sequence in self._HEADLESS_HOST_SEQUENCES
            for length in range(1, len(sequence))
        }
        for length in range(min(len(visible), 7), 0, -1):
            suffix = visible[-length:]
            if suffix in prefixes:
                self._terminal_control_tail = suffix
                visible = visible[:-length]
                break
        return visible

    def write_bytes(self, data: bytes) -> None:
        value = data.decode("utf-8", errors="replace")
        value = value.replace("\r\n", "\n").replace("\n", "\r\n")
        self.process.write(value)

    def resize(self, *, rows: int, cols: int) -> None:
        selected_rows, selected_cols = _size(rows, cols)
        self._rows, self._cols = selected_rows, selected_cols
        self.process.setwinsize(selected_rows, selected_cols)

    def poll(self) -> int | None:
        if self.process.isalive() or self.process.exitstatus is None:
            return None
        return int(self.process.exitstatus)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            code = self.poll()
            if code is not None:
                return code
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired("ConPTY", timeout)
            time.sleep(0.02 if remaining is None else min(0.02, remaining))

    def terminate_tree(self, *, grace_seconds: float) -> None:
        if self._windows_job is not None and self._windows_job.terminate():
            deadline = time.monotonic() + max(0.0, grace_seconds)
            while self.process.isalive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not self.process.isalive():
                return
        if self._windows_job is None and _taskkill_pid_tree(self.pid):
            deadline = time.monotonic() + max(0.0, grace_seconds)
            while self.process.isalive() and time.monotonic() < deadline:
                time.sleep(0.02)
            if not self.process.isalive():
                return
        if self.process.isalive():
            self.process.terminate(force=False)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while self.process.isalive() and time.monotonic() < deadline:
            time.sleep(0.02)
        if self.process.isalive():
            self.process.terminate(force=True)

    def close(self) -> None:
        if self._windows_job is not None:
            self._windows_job.close()
            self._windows_job = None
        if not self._closed:
            self.process.close(force=self.process.isalive())
            self._closed = True


def _taskkill_pid_tree(pid: int) -> bool:
    """Terminate one owned Windows PID tree without process-name scans."""
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    return completed.returncode == 0


def create_process_backend(
    command: str,
    *,
    cwd: Path,
    tty: bool,
    rows: int,
    cols: int,
    os_name: str | None = None,
    which: Callable[[str], str | None] | None = None,
    winpty_module=None,
    environment=None,
) -> ProcessBackendSession:
    """Create the best backend available without changing service ownership."""
    import shutil

    selected_os = os.name if os_name is None else os_name
    shell = select_shell(os_name=selected_os, which=which or shutil.which)
    argv = shell.argv(command, login=environment is None)
    env = environment.as_dict() if environment else build_sanitized_subprocess_env()
    env.setdefault("PYTHONIOENCODING", "utf-8")
    selected_rows, selected_cols = _size(rows, cols)

    if tty and selected_os == "nt":
        module = winpty_module
        if module is None:
            try:
                import winpty as module  # type: ignore[import-not-found]
            except ImportError:
                module = False
        if module:
            process = module.PtyProcess.spawn(
                list(argv),
                cwd=str(cwd),
                env=env,
                dimensions=(selected_rows, selected_cols),
            )
            job = None
            try:
                job = _WindowsJob.bind(int(process.pid), os_name=selected_os)
                backend = ConPtyBackend(
                    process,
                    windows_job=job,
                    rows=selected_rows,
                    cols=selected_cols,
                )
                backend.pipefail = shell.kind.value == "bash"
                return backend
            except BaseException:
                _retire_failed_start(process, os_name=selected_os, conpty=True, job=job)
                raise

    if tty and selected_os != "nt":
        import pty

        master_fd, slave_fd = pty.openpty()
        process = None
        try:
            _set_posix_terminal_size(slave_fd, rows=selected_rows, cols=selected_cols)
            env.setdefault("TERM", "xterm-256color")
            process = subprocess.Popen(
                argv,
                shell=False,
                cwd=cwd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,
                env=env,
            )
            os.close(slave_fd)
            slave_fd = None
            backend = PosixPtyBackend(process, master_fd)
            backend.pipefail = shell.kind.value == "bash"
            return backend
        except BaseException:
            _retire_failed_start(
                process,
                os_name=selected_os,
                fds=[fd for fd in (master_fd, slave_fd) if fd is not None],
            )
            raise

    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        if selected_os == "nt"
        else 0
    )
    process = subprocess.Popen(
        argv,
        shell=False,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        start_new_session=(selected_os != "nt"),
        creationflags=creationflags,
        env=env,
    )
    job = None
    try:
        if selected_os == "nt":
            job = _WindowsJob.bind(int(process.pid), os_name=selected_os)
        backend = PipeBackend(process, os_name=selected_os, windows_job=job)
        backend.pipefail = shell.kind.value == "bash"
        return backend
    except BaseException:
        _retire_failed_start(process, os_name=selected_os, job=job)
        raise


def start_owned_pipe_command(argv, *, cwd: Path, env):
    """准备和检索命令的根进程、后代及两根输出管道由同一调用方管理。"""
    if os.name != "nt":
        return subprocess.Popen(
            argv, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
        ), None

    from nz_coder.runtime.process.windows_job_control import start_job_command

    job = _WindowsJob.create()
    try:
        return start_job_command(job, argv, cwd, env), job
    except BaseException:
        # 即使创建进程本身失败，尚未交给调用方的作业句柄也要释放。
        job.close_acquired_handle(job.handle)
        job.handle = None
        raise


def _retire_failed_start(process, *, os_name, conpty=False, job=None, fds=()) -> None:
    """包装失败仍由创建者持有资源；确认退出和句柄释放前不交还调用方。"""
    retry_delays = iter((0.05, 0.1, 0.2, 0.5))
    while process is not None:
        try:
            if job is not None:
                job.terminate()
            if conpty:
                if process.isalive():
                    process.terminate(force=True)
                if not process.isalive() and process.exitstatus is not None:
                    break
                time.sleep(0.05)
            else:
                terminate_process_tree(process, os_name=os_name, force=True)
                if process.wait(timeout=0.05) is not None:
                    break
        except subprocess.TimeoutExpired:
            continue
        except Exception:
            time.sleep(next(retry_delays, 1.0))
    pending = []
    if job is not None:
        pending.append(job.close)
    if process is not None:
        if conpty:
            pending.append(lambda: process.close(force=False))
        else:
            pending.extend(
                stream.close
                for stream in (process.stdin, process.stdout, process.stderr)
                if stream is not None
            )
    pending.extend(lambda fd=fd: os.close(fd) for fd in fds)
    while pending:
        failures = []
        for close in pending:
            try:
                close()
            except Exception:
                failures.append(close)
        pending = failures
        if pending:
            time.sleep(next(retry_delays, 1.0))


def _set_posix_terminal_size(fd: int, *, rows: int, cols: int) -> None:
    import fcntl
    import termios

    selected_rows, selected_cols = _size(rows, cols)
    fcntl.ioctl(
        fd,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", selected_rows, selected_cols, 0, 0),
    )


class _WindowsJob:
    """严格启动先创建作业；交互终端仍可尝试接管已经启动的进程。"""

    def __init__(self, handle, kernel32) -> None:
        self.handle = handle
        self.kernel32 = kernel32
        self._termination_requested = False

    @classmethod
    def create(cls, *, kernel32=None):
        import ctypes
        from ctypes import wintypes

        selected_kernel = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
        if kernel32 is None:
            # ctypes defaults function returns to 32-bit ``c_int``.  A
            # HANDLE is pointer-sized, so omitting these declarations
            # truncates real Job/process handles on 64-bit Windows while
            # injected Python fakes continue to work without ctypes APIs.
            selected_kernel.CreateJobObjectW.argtypes = [
                ctypes.c_void_p,
                wintypes.LPCWSTR,
            ]
            selected_kernel.CreateJobObjectW.restype = wintypes.HANDLE
            selected_kernel.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            selected_kernel.SetInformationJobObject.restype = wintypes.BOOL
            selected_kernel.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            selected_kernel.OpenProcess.restype = wintypes.HANDLE
            selected_kernel.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            selected_kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            selected_kernel.TerminateJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.UINT,
            ]
            selected_kernel.TerminateJobObject.restype = wintypes.BOOL
            selected_kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            selected_kernel.CloseHandle.restype = wintypes.BOOL
        ulong_ptr = ctypes.c_size_t

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ulong_ptr),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        handle = selected_kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError("Could not create a Windows command job")
        job = cls(handle, selected_kernel)
        try:
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not selected_kernel.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                raise OSError("Could not configure the Windows command job")
            return job
        except BaseException:
            job.close_acquired_handle(handle)
            raise

    @classmethod
    def bind(cls, pid: int, *, os_name: str | None = None, kernel32=None):
        if (os.name if os_name is None else os_name) != "nt":
            return None
        job = None
        process_handle = None
        assigned = False
        try:
            job = cls.create(kernel32=kernel32)
            process_handle = job.kernel32.OpenProcess(0x0101, False, int(pid))
            assigned = bool(process_handle and job.kernel32.AssignProcessToJobObject(job.handle, process_handle))
            return job if assigned else None
        except (AttributeError, OSError, ValueError):
            return None
        finally:
            if job is not None:
                job.close_acquired_handle(process_handle)
                if not assigned:
                    job.close_acquired_handle(job.handle)

    def close_acquired_handle(self, handle) -> None:
        # 启动方持有尚未移交的句柄。关闭遇到临时错误时不能先忘记该句柄。
        if handle:
            retry_delays = iter((0.05, 0.1, 0.2, 0.5))
            while not self.kernel32.CloseHandle(handle):
                time.sleep(next(retry_delays, 1.0))

    def terminate(self) -> bool:
        try:
            return bool(self.kernel32.TerminateJobObject(self.handle, 1))
        except (AttributeError, OSError, ValueError):
            return False

    def retire(self) -> bool:
        """即使直属进程先退出，job 仍能确认并回收属于这条命令的后代。"""
        from nz_coder.runtime.process.windows_job_control import active_job_processes

        if self.handle is None:
            return True
        if active_job_processes(self.kernel32, self.handle):
            if not self._termination_requested:
                if not self.terminate():
                    raise OSError("Windows command job termination was not accepted")
                self._termination_requested = True
            # 让所有者继续排空管道、检查总期限，再回来确认内核的实际成员数。
            return False
        self.close()
        return True

    def close(self) -> None:
        if self.handle is not None:
            if not self.kernel32.CloseHandle(self.handle):
                raise OSError("CloseHandle failed")
            self.handle = None


__all__ = [
    "ConPtyBackend",
    "PipeBackend",
    "PosixPtyBackend",
    "ProcessBackendSession",
    "create_process_backend",
]
