"""准备和检索命令在 Windows 上使用的进程启动与退出确认。"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import math
import os
from pathlib import Path
import subprocess
import time
from ctypes import wintypes
from functools import cmp_to_key


class _JobAccounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_longlong), ("kernel_time", ctypes.c_longlong),
        ("period_user_time", ctypes.c_longlong), ("period_kernel_time", ctypes.c_longlong),
        ("page_faults", wintypes.DWORD), ("total_processes", wintypes.DWORD),
        ("active_processes", wintypes.DWORD), ("terminated_processes", wintypes.DWORD),
    ]


def active_job_processes(kernel, handle: int) -> int:
    query = kernel.QueryInformationJobObject
    query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                      ctypes.POINTER(wintypes.DWORD)]
    query.restype = wintypes.BOOL
    info = _JobAccounting()
    if not query(handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.active_processes


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD), ("reserved", wintypes.LPWSTR),
        ("desktop", wintypes.LPWSTR), ("title", wintypes.LPWSTR),
        ("x", wintypes.DWORD), ("y", wintypes.DWORD),
        ("width", wintypes.DWORD), ("height", wintypes.DWORD),
        ("chars_x", wintypes.DWORD), ("chars_y", wintypes.DWORD),
        ("fill", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("show", wintypes.WORD), ("reserved_size", wintypes.WORD),
        ("reserved_bytes", ctypes.c_void_p), ("stdin", wintypes.HANDLE),
        ("stdout", wintypes.HANDLE), ("stderr", wintypes.HANDLE),
    ]


class _ExtendedStartupInfo(ctypes.Structure):
    _fields_ = [("startup", _StartupInfo), ("attributes", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", wintypes.HANDLE), ("thread", wintypes.HANDLE),
                ("pid", wintypes.DWORD), ("tid", wintypes.DWORD)]


@contextmanager
def _startup_attributes(kernel, job_handle, streams):
    initialize = kernel.InitializeProcThreadAttributeList
    initialize.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                           ctypes.POINTER(ctypes.c_size_t)]
    initialize.restype = wintypes.BOOL
    update = kernel.UpdateProcThreadAttribute
    update.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                       ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
    update.restype = wintypes.BOOL
    delete = kernel.DeleteProcThreadAttributeList
    delete.argtypes = [ctypes.c_void_p]
    delete.restype = None

    size = ctypes.c_size_t()
    initialize(None, 2, 0, ctypes.byref(size))  # Win32 首次调用返回所需字节数。
    storage = ctypes.create_string_buffer(size.value)
    if not initialize(storage, 2, 0, ctypes.byref(size)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        jobs = (wintypes.HANDLE * 1)(job_handle)
        handles = (wintypes.HANDLE * 3)(*streams)
        # JOB_LIST 只指定归属；HANDLE_LIST 才决定子进程能够继承哪些句柄。
        for attribute, values in ((0x0002000D, jobs), (0x00020002, handles)):
            if not update(storage, 0, attribute, values, ctypes.sizeof(values), None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        yield ctypes.cast(storage, ctypes.c_void_p)
    finally:
        delete(storage)


def _environment_block(kernel, env):
    if env is None:
        return None
    ordinal = kernel.CompareStringOrdinal
    ordinal.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL]
    ordinal.restype = ctypes.c_int

    def compare(left, right):
        order = ordinal(left, -1, right, -1, True)
        if not order:
            raise ctypes.WinError(ctypes.get_last_error())
        return order - 2  # CSTR_EQUAL 为 2。

    # 遵循 Windows 对变量名的排序。PATH 和 Path 同时出现时，保留最后设置的值。
    keys = sorted(env, key=cmp_to_key(compare))
    entries = []
    for index, key in enumerate(keys):
        value = env[key]
        if not key or "=" in key[1:] or "\0" in key or "\0" in value:
            raise ValueError("invalid Windows environment variable")
        if index + 1 == len(keys) or compare(key, keys[index + 1]) != 0:
            entries.append(f"{key}={value}")
    # create_unicode_buffer 再加一个 NUL，形成 Windows 要求的双结束符。
    return ctypes.create_unicode_buffer("\0".join(entries) + "\0")


def _create_suspended_process(job, argv, cwd, env, streams, info):
    kernel = job.kernel32
    create = kernel.CreateProcessW
    create.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
                       ctypes.POINTER(_ExtendedStartupInfo), ctypes.POINTER(_ProcessInfo)]
    create.restype = wintypes.BOOL
    command = subprocess.list2cmdline(argv)
    if "\0" in command or "\0" in os.fspath(cwd):
        raise ValueError("embedded null character")
    startup = _ExtendedStartupInfo()
    startup.startup.cb = ctypes.sizeof(startup)
    startup.startup.flags = 0x100  # STARTF_USESTDHANDLES
    startup.startup.stdin, startup.startup.stdout, startup.startup.stderr = streams
    with _startup_attributes(kernel, job.handle, streams) as attributes:
        startup.attributes = attributes
        # 扩展属性、Unicode 环境、挂起启动；创建成功的瞬间已归入 job。
        flags = 0x00080000 | 0x00000400 | 0x00000004
        if not create(None, ctypes.create_unicode_buffer(command), None, None, True, flags,
                      _environment_block(kernel, env), os.fspath(cwd), ctypes.byref(startup), ctypes.byref(info)):
            raise ctypes.WinError(ctypes.get_last_error())


def _resume_primary_thread(kernel, info):
    resume = kernel.ResumeThread
    resume.argtypes = [wintypes.HANDLE]
    resume.restype = wintypes.DWORD
    previous = resume(info.thread)
    if previous == 0xFFFFFFFF:
        raise ctypes.WinError(ctypes.get_last_error())
    if previous != 1:
        raise OSError("Unexpected suspend count for the command's primary thread")


class _WindowsPipeProcess:
    """两个命令所有者只从各自的回收线程等待同一进程句柄。"""

    def __init__(self, argv, info, stdout, stderr):
        self.args = argv
        self._info = info
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = None

    @property
    def pid(self) -> int:
        return self._info.pid

    def poll(self) -> int | None:
        try:
            return self.wait(0)
        except subprocess.TimeoutExpired:
            return None

    def wait(self, timeout: float | None = None) -> int:
        import _winapi

        if self.returncode is None:
            milliseconds = _winapi.INFINITE if timeout is None else max(0, math.ceil(timeout * 1000))
            handle = self._info.process
            if _winapi.WaitForSingleObject(handle, milliseconds) == _winapi.WAIT_TIMEOUT:
                raise subprocess.TimeoutExpired(self.args, timeout)
            code = _winapi.GetExitCodeProcess(handle)
            _winapi.CloseHandle(handle)
            self._info.process = None
            self.returncode = code
        return self.returncode


def start_job_command(job, argv, cwd: Path, env):
    import msvcrt

    info = _ProcessInfo()
    with ExitStack() as failed:
        try:
            # 子侧句柄在恢复线程前关闭；并发命令只能继承自己的三个标准流。
            with ExitStack() as child_ends:
                stdin = child_ends.enter_context(open(os.devnull, "rb", buffering=0))
                output = []
                child_handles = [msvcrt.get_osfhandle(stdin.fileno())]
                for _ in range(2):
                    read_fd, write_fd = os.pipe()
                    child_ends.callback(os.close, write_fd)
                    reader = failed.enter_context(os.fdopen(read_fd, "rb", buffering=0))
                    output.append(reader)
                    child_handles.append(msvcrt.get_osfhandle(write_fd))
                for handle in child_handles:
                    os.set_handle_inheritable(handle, True)
                process = _WindowsPipeProcess(argv, info, *output)
                _create_suspended_process(job, argv, cwd, env, child_handles, info)
            _resume_primary_thread(job.kernel32, info)
        except BaseException:
            if info.process:
                # 尚未移交。等作业中最后一个进程和直属进程实际退出后才释放读端。
                while True:
                    try:
                        if job.retire():
                            process.wait()
                            break
                    except OSError:
                        pass
                    time.sleep(0.05)
            raise
        finally:
            job.close_acquired_handle(info.thread)
        failed.pop_all()  # stdout、stderr 和进程句柄由命令所有者继续管理。
    return process
