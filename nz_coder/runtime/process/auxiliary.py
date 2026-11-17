"""请求内rg/Git资源归属；唯一owner负责创建、停止、回收及关流。"""

from __future__ import annotations

from contextvars import Context
from dataclasses import dataclass, field
import io
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from typing import TYPE_CHECKING, Callable, Mapping

from nz_coder.foundation.process_runner import LineCommandResult, ProcessInterrupted
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.runtime.process.platform_runtime import terminate_process_tree

if TYPE_CHECKING:
    from nz_coder.runtime.process.command_broker import CommandAuthority


def _failure(code: str, message: str) -> PublicRuntimeError:
    return PublicRuntimeError(PublicError(code, message))


def _identity(path: Path) -> tuple[int, int]:
    from nz_coder.runtime.process.command_broker import command_directory_identity

    return command_directory_identity(path)


@dataclass
class _Stream:
    handle: object
    chunks: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=64))
    thread: threading.Thread | None = None
    launch_done: threading.Event = field(default_factory=threading.Event)
    content: bytearray = field(default_factory=bytearray)
    closed: bool = False


@dataclass
class _Command:
    argv: tuple[str, ...]
    cwd: Path
    cwd_identity: tuple[int, int]
    timeout: float
    deadline: float
    lines: bool
    thread: threading.Thread | None = None
    launch_done: threading.Event = field(default_factory=threading.Event)
    native_done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    handoff: threading.Event = field(default_factory=threading.Event)
    process: object | None = None
    streams: list[_Stream] = field(default_factory=list)
    error: BaseException | None = None
    returncode: int | None = None
    output_bytes: int = 0
    early: bool = False


class AuxiliaryCommandRunner:
    def __init__(
        self,
        authority: CommandAuthority,
        *,
        request_id: int,
        cancel_event: threading.Event,
        deadline: float,
        environment: Mapping[str, str],
    ):
        self.authority = authority
        self.request_id = request_id
        self._cancel = cancel_event
        self._deadline = deadline
        self._environment = dict(environment)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._sealed = False
        self._record: _Command | None = None

    @property
    def search_path(self) -> str:
        return self._environment.get("PATH", os.defpath)

    @property
    def retired(self) -> bool:
        with self._lock:
            return self._sealed and self._record is None

    def _checkpoint(self) -> None:
        if self._cancel.is_set() or self._stop.is_set():
            raise ProcessInterrupted("cancelled")
        if time.monotonic() >= self._deadline:
            raise ProcessInterrupted("timeout")

    def _check_cwd(self, cwd: Path, identity=None) -> tuple[int, int]:
        self._checkpoint()
        try:
            root = self.authority.execution_root
            if _identity(root) != self.authority.execution_identity:
                raise ValueError("root changed")
            if cwd.is_symlink() or not cwd.resolve().is_relative_to(root):
                raise ValueError("cwd outside root")
            actual = _identity(cwd)
            if identity is not None and actual != identity:
                raise ValueError("cwd changed")
            return actual
        except (OSError, ValueError, PublicRuntimeError) as exc:
            raise _failure(
                "auxiliary_directory_changed", "Command directory identity changed."
            ) from exc

    def _error(self, record: _Command, error: BaseException) -> None:
        with self._lock:
            if record.error is None:
                record.error = error
            record.stop.set()
            record.handoff.set()

    def _start(self, argv, *, cwd, timeout, lines) -> _Command:
        cwd = Path(cwd).absolute()
        identity = self._check_cwd(cwd)
        record = _Command(
            tuple(argv),
            cwd,
            identity,
            timeout,
            min(self._deadline, time.monotonic() + timeout),
            lines,
        )
        with self._lock:
            self._checkpoint()
            if self._sealed or self._record is not None:
                raise ValueError("Auxiliary command runner is sealed or busy.")
            self._record = record
        try:
            record.thread = threading.Thread(
                target=Context().run,
                args=(self._execute, record),
                name=f"nz-auxiliary-{self.authority.child_session_id}-{self.request_id}",
                daemon=True,
            )
            record.thread.start()
        except BaseException as exc:
            self._error(record, exc)
            if record.thread is None or record.thread.ident is None:
                record.native_done.set()
        finally:
            record.launch_done.set()
        return record

    def _read(self, record: _Command, stream: _Stream) -> None:
        try:
            while chunk := stream.handle.read(8192):
                with self._lock:
                    if record.handoff.is_set():
                        continue
                    record.output_bytes += len(chunk)
                    if (
                        record.output_bytes
                        > self.authority.run_settings.bash_output_hard_limit_bytes
                    ):
                        self._error(
                            record,
                            _failure(
                                "auxiliary_output_limit",
                                "Command output exceeded its limit.",
                            ),
                        )
                        continue
                # stderr在提前停止前已接纳的块仍交给原owner；之后的新字节只排空。
                while not record.handoff.is_set() or (
                    record.early and record.error is None and stream is record.streams[1]
                ):
                    try:
                        stream.chunks.put(chunk, timeout=0.05)
                        break
                    except queue.Full:
                        continue
        except BaseException:
            self._error(
                record,
                _failure("auxiliary_io_error", "Command output could not be read."),
            )

    def _drain(self, record: _Command) -> None:
        for index, stream in enumerate(record.streams):
            if index == 0 and record.lines and not record.handoff.is_set():
                continue
            while True:
                try:
                    chunk = stream.chunks.get_nowait()
                except queue.Empty:
                    break
                if not record.handoff.is_set() or (
                    index == 1 and record.early and record.error is None
                ):
                    stream.content.extend(chunk)

    def _execute(self, record: _Command) -> None:
        try:
            self._check_cwd(record.cwd, record.cwd_identity)
            environment = dict(self._environment)
            if Path(record.argv[0]).name in {"rg", "rg.exe"}:
                environment.pop("RIPGREP_CONFIG_PATH", None)
            else:
                environment.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
            record.process = subprocess.Popen(
                record.argv,
                cwd=str(record.cwd),
                env=environment,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=os.name != "nt",
                bufsize=0,
            )
            record.streams = [
                _Stream(record.process.stdout),
                _Stream(record.process.stderr),
            ]
            for index, stream in enumerate(record.streams):
                try:
                    stream.thread = threading.Thread(
                        target=Context().run,
                        args=(self._read, record, stream),
                        name=f"nz-auxiliary-reader-{self.authority.child_session_id}-{self.request_id}-{index}",
                        daemon=True,
                    )
                    stream.thread.start()
                except BaseException as exc:
                    self._error(record, exc)
                finally:
                    stream.launch_done.set()
        except BaseException as exc:
            self._error(record, exc)
        if record.process is not None:
            for stream in record.streams:
                stream.launch_done.set()
            self._drive(record)
        record.native_done.set()

    def _drive(self, record: _Command) -> None:
        process = record.process
        waited = swept = False
        stopping_at = None
        while True:
            self._drain(record)
            now = time.monotonic()
            try:
                self._checkpoint()
            except ProcessInterrupted as exc:
                self._error(record, exc)
            if now >= record.deadline and record.error is None and not record.early:
                self._error(
                    record, subprocess.TimeoutExpired(record.argv, record.timeout)
                )
            if record.stop.is_set():
                if stopping_at is None:
                    stopping_at = now
                try:
                    terminate_process_tree(
                        process,
                        force=now - stopping_at
                        >= self.authority.run_settings.process_kill_grace,
                    )
                except (OSError, subprocess.SubprocessError):
                    self._error(
                        record,
                        _failure(
                            "auxiliary_io_error",
                            "Command stopping could not be confirmed.",
                        ),
                    )
            if not waited:
                try:
                    if process.poll() is not None:
                        record.returncode = process.wait(timeout=0)
                        waited = True
                except (OSError, subprocess.SubprocessError):
                    self._error(
                        record,
                        _failure(
                            "auxiliary_io_error", "Command exit could not be confirmed."
                        ),
                    )
            if waited and not swept:
                try:
                    swept = terminate_process_tree(process, force=True)
                except (OSError, subprocess.SubprocessError):
                    swept = False
                if not swept:
                    self._error(
                        record,
                        _failure(
                            "auxiliary_io_error",
                            "Command group cleanup could not be confirmed.",
                        ),
                    )
            if waited and swept:
                for stream in record.streams:
                    thread = stream.thread
                    if (
                        thread is not None
                        and thread.ident is not None
                        and thread.is_alive()
                    ):
                        continue
                    if not stream.closed:
                        try:
                            stream.handle.close()
                            stream.closed = True
                        except (OSError, ValueError):
                            self._error(
                                record,
                                _failure(
                                    "auxiliary_io_error",
                                    "Command output could not be closed.",
                                ),
                            )
                self._drain(record)
                if all(s.closed and s.chunks.empty() for s in record.streams):
                    return
            time.sleep(0.01)

    def _wait_record(self, record: _Command, *, deadline: float) -> bool:
        if not record.launch_done.wait(
            max(0.0, min(0.05, deadline - time.monotonic()))
        ):
            return False
        thread = record.thread
        if thread is not None and thread.ident is not None:
            thread.join(max(0.0, min(0.05, deadline - time.monotonic())))
            if thread.is_alive():
                return False
        if not record.native_done.is_set():
            return False
        with self._lock:
            if self._record is record:
                self._record = None
        return True

    def _wait(self, record: _Command) -> None:
        while not self._wait_record(record, deadline=time.monotonic() + 0.05):
            self._checkpoint()
        self._checkpoint()
        if record.error is not None:
            raise record.error

    @staticmethod
    def _text(content: bytearray) -> str:
        with io.TextIOWrapper(
            io.BytesIO(content), encoding="utf-8", errors="replace"
        ) as stream:
            return stream.read()

    def capture(
        self, argv: tuple[str, ...], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        record = self._start(argv, cwd=cwd, timeout=timeout, lines=False)
        self._wait(record)
        return subprocess.CompletedProcess(
            record.argv,
            record.returncode,
            self._text(record.streams[0].content),
            self._text(record.streams[1].content),
        )

    def lines(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        timeout: float,
        on_line: Callable[[str], bool],
    ) -> LineCommandResult:
        record = self._start(argv, cwd=cwd, timeout=timeout, lines=True)
        pending = bytearray()
        try:
            while True:
                self._checkpoint()
                if record.error is not None:
                    break
                if not record.streams:
                    if record.native_done.is_set():
                        break
                    time.sleep(0.01)
                    continue
                stream = record.streams[0]
                try:
                    chunk = stream.chunks.get(timeout=0.05)
                except queue.Empty:
                    if not stream.launch_done.is_set() or (
                        stream.thread is not None and stream.thread.is_alive()
                    ):
                        continue
                    chunk = b""
                pending.extend(chunk)
                while b"\n" in pending or (not chunk and pending):
                    if b"\n" in pending:
                        line, _, tail = pending.partition(b"\n")
                        pending = bytearray(tail)
                    else:
                        line, pending = pending, bytearray()
                    text = line.decode("utf-8", errors="replace").strip()
                    if text and on_line(text):
                        with self._lock:
                            self._checkpoint()
                            if record.error is None:
                                record.early = True
                                record.handoff.set()
                                record.stop.set()
                        break
                if record.early or not chunk:
                    break
        except BaseException as exc:
            self._error(record, exc)
        self._wait(record)
        return LineCommandResult(
            record.returncode,
            record.streams[1].content.decode("utf-8", errors="replace").strip(),
            record.early,
        )

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def request_stop(self) -> None:
        self._stop.set()
        self.seal()
        with self._lock:
            record = self._record
            if record is not None:
                record.stop.set()
                record.handoff.set()

    def close(self, *, deadline: float) -> bool:
        self.request_stop()
        while True:
            with self._lock:
                record = self._record
            if record is None or self._wait_record(record, deadline=deadline):
                return True
            if time.monotonic() >= deadline:
                return False
