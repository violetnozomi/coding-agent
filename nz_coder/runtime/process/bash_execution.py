"""Single-owner bash process, bounded output reader and confirmed cleanup."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import Callable

from nz_coder.runtime.process.platform_runtime import terminate_process_tree


@dataclass(frozen=True)
class BashExecutionResult:
    exit_code: int | None
    cancelled: bool
    timed_out: bool
    stream_complete: bool
    output_limit_exceeded: bool


class BashExecution:
    """run 的原调用线程独占 native 操作；close 只等待，不跨线程关流。"""

    def __init__(
        self, argv: tuple[str, ...], *, cwd: Path, env: dict[str, str],
        deadline: float, kill_grace: float, on_output: Callable[[bytes], bool],
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = dict(env)
        self._deadline = deadline
        self._kill_grace = kill_grace
        self._on_output = on_output
        self._stop = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._started = False
        self._process: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self._stream_complete = False
        self._reader_error: BaseException | None = None

    def request_stop(self) -> None:
        self._stop.set()

    def close(self, *, deadline: float) -> bool:
        self.request_stop()
        self._done.wait(max(0.0, deadline - time.monotonic()))
        return self._done.is_set() and not (
            self._reader is not None and self._reader.is_alive()
        )

    def _read_output(self) -> None:
        try:
            assert self._process is not None and self._process.stdout is not None
            while True:
                chunk = self._process.stdout.read(8192)
                if not chunk:
                    self._stream_complete = True
                    return
                # 保留既有注入文本流的编码契约；真实Popen始终使用binary。
                payload = chunk if isinstance(chunk, bytes) else chunk.encode("utf-8")
                self._queue.put(payload)
        except BaseException as exc:
            self._reader_error = exc

    def run(self, *, cancel_event: threading.Event | None) -> BashExecutionResult:
        with self._lock:
            if self._started:
                raise RuntimeError("bash execution already has an owner")
            self._started = True
        cancelled = self._stop.is_set() or (cancel_event is not None and cancel_event.is_set())
        timed_out = not cancelled and time.monotonic() >= self._deadline
        if cancelled or timed_out:
            self._done.set()
            return BashExecutionResult(None, cancelled, timed_out, False, False)
        try:
            process = subprocess.Popen(
                self._argv, shell=False, cwd=self._cwd, env=self._env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0),
            )
        except BaseException:
            self._done.set()
            raise
        self._process = process
        reader = None
        error: BaseException | None = None
        try:
            reader = self._reader = threading.Thread(
                target=self._read_output, name="nz-bash-output", daemon=True,
            )
            reader.start()
        except BaseException as exc:
            error = exc
        output_limit = False
        stop_started: float | None = None
        next_signal = 0.0
        reaped = False
        stdout_closed = False
        while True:
            now = time.monotonic()
            if self._reader_error is not None and error is None:
                error = self._reader_error
            if stop_started is None:
                cancelled = self._stop.is_set() or (cancel_event is not None and cancel_event.is_set())
                timed_out = not cancelled and now >= self._deadline
                if cancelled or timed_out or output_limit or error is not None:
                    stop_started = now
            if stop_started is not None and now >= next_signal:
                terminate_process_tree(process, force=now - stop_started >= self._kill_grace)
                next_signal = now + 0.05
            try:
                chunk = self._queue.get(timeout=0.05)
            except queue.Empty:
                chunk = None
            if chunk is not None and error is None:
                try:
                    if not self._on_output(chunk):
                        output_limit = True
                except BaseException as exc:
                    error = exc
            if not reaped:
                try:
                    if process.poll() is not None:
                        process.wait(timeout=0)
                        reaped = True
                except subprocess.TimeoutExpired:
                    pass
                except BaseException as exc:
                    if error is None:
                        error = exc
            # Thread.start可能先启动再抛错，ident比“start已返回”更接近事实。
            reader_alive = reader is not None and reader.is_alive()
            if reaped and not reader_alive and self._queue.empty():
                # 领进程退出和EOF不能证明同组后台进程也已结束。
                if os.name != "nt":
                    if not terminate_process_tree(process, force=True):
                        continue
                if reader is not None and reader.ident is not None:
                    reader.join(timeout=0)
                if not stdout_closed:
                    try:
                        assert process.stdout is not None
                        process.stdout.close()
                        stdout_closed = True
                    except BaseException as exc:
                        if error is None:
                            error = exc
                        self._stop.wait(0.05)
                        continue
                self._done.set()
                if error is not None:
                    raise error
                return BashExecutionResult(
                    process.returncode, cancelled, timed_out,
                    self._stream_complete, output_limit,
                )
