"""Thin process I/O backends owned by :mod:`process_service`.

The service owns lifecycle and events; these adapters only translate PTY,
ConPTY, and pipe operations into one small platform-neutral contract.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import struct
import subprocess
import time
from typing import Callable, Protocol, runtime_checkable

from nz_coder.runtime.platform_runtime import select_shell, terminate_process_tree


@runtime_checkable
class ProcessBackendSession(Protocol):
    """I/O and lifecycle contract implemented by each process transport."""

    pid: int
    tty: bool
    lifecycle_mode: str

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

    def __init__(self, process: subprocess.Popen, *, os_name: str) -> None:
        self.process = process
        self.pid = int(process.pid)
        self.os_name = os_name
        self._windows_job = (
            _WindowsJob.bind(self.pid, os_name=os_name) if os_name == "nt" else None
        )
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
        if self.process.poll() is None or self.os_name == "nt":
            terminate_process_tree(self.process, os_name=self.os_name, force=True)

    def close(self) -> None:
        if self._windows_job is not None:
            self._windows_job.close()
            self._windows_job = None
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


class PosixPtyBackend:
    """POSIX PTY adapter."""

    tty = True

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
        terminate_process_tree(self.process, os_name="posix", force=True)

    def close(self) -> None:
        try:
            os.close(self.master_fd)
        except OSError:
            pass


class ConPtyBackend:
    """pywinpty adapter; no product state or events are owned here."""

    tty = True

    def __init__(self, process, *, windows_job=None) -> None:  # noqa: ANN001
        self.process = process
        self.pid = int(process.pid)
        self._windows_job = windows_job
        self.lifecycle_mode = (
            "windows-job-object"
            if windows_job is not None
            else "windows-taskkill-fallback"
        )

    def read_bytes(self, size: int) -> bytes:
        value = self.process.read(size)
        return value if isinstance(value, bytes) else str(value).encode("utf-8")

    def write_bytes(self, data: bytes) -> None:
        self.process.write(data.decode("utf-8", errors="replace"))

    def resize(self, *, rows: int, cols: int) -> None:
        selected_rows, selected_cols = _size(rows, cols)
        self.process.setwinsize(selected_rows, selected_cols)

    def poll(self) -> int | None:
        return None if self.process.isalive() else int(self.process.exitstatus or 0)

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while self.process.isalive():
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("ConPTY", timeout)
            time.sleep(0.02)
        return int(self.process.exitstatus or 0)

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
        try:
            self.process.close(force=self.process.isalive())
        except (OSError, RuntimeError):
            pass


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
) -> ProcessBackendSession:
    """Create the best backend available without changing service ownership."""
    import shutil

    selected_os = os.name if os_name is None else os_name
    shell = select_shell(os_name=selected_os, which=which or shutil.which)
    argv = shell.argv(command)
    env = dict(os.environ)
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
            return ConPtyBackend(
                process,
                windows_job=_WindowsJob.bind(int(process.pid), os_name=selected_os),
            )

    if tty and selected_os != "nt":
        import pty

        master_fd, slave_fd = pty.openpty()
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
        except BaseException:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return PosixPtyBackend(process, master_fd)

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
    return PipeBackend(process, os_name=selected_os)


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
    """Best-effort Job Object binding; unavailable platforms return ``None``."""

    def __init__(self, handle, kernel32) -> None:
        self.handle = handle
        self.kernel32 = kernel32

    @classmethod
    def bind(cls, pid: int, *, os_name: str | None = None, kernel32=None):
        selected_os = os.name if os_name is None else os_name
        if selected_os != "nt":
            return None
        try:
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
                return None
            limits = _ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            configured = selected_kernel.SetInformationJobObject(
                handle,
                9,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            if not configured:
                selected_kernel.CloseHandle(handle)
                return None
            process_handle = selected_kernel.OpenProcess(0x0101, False, int(pid))
            if not process_handle:
                selected_kernel.CloseHandle(handle)
                return None
            assigned = selected_kernel.AssignProcessToJobObject(handle, process_handle)
            selected_kernel.CloseHandle(process_handle)
            if not assigned:
                selected_kernel.CloseHandle(handle)
                return None
            return cls(handle, selected_kernel)
        except (AttributeError, OSError, ValueError):
            return None

    def terminate(self) -> bool:
        try:
            return bool(self.kernel32.TerminateJobObject(self.handle, 1))
        except (AttributeError, OSError, ValueError):
            return False

    def close(self) -> None:
        try:
            self.kernel32.CloseHandle(self.handle)
        except (AttributeError, OSError, ValueError):
            pass


__all__ = [
    "ConPtyBackend",
    "PipeBackend",
    "PosixPtyBackend",
    "ProcessBackendSession",
    "create_process_backend",
]
