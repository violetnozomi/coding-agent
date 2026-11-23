"""父方准备命令的归属、合作中断及实际资源退出确认。"""
from __future__ import annotations

import io
import os
import queue
import re
import subprocess
import threading
import time
from contextvars import Context
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
from nz_coder.runtime.process.command_broker import command_directory_identity
from nz_coder.runtime.process.platform_runtime import terminate_process_tree
from nz_coder.runtime.process.process_backends import _WindowsJob, start_owned_pipe_command
from nz_coder.runtime.worktree.models import Worktree

if TYPE_CHECKING:
    from nz_coder.foundation.workspace_trust import ConfigSnapshot
    from nz_coder.runtime.core.run_settings import RunSettings


class PreparationInterrupted(Exception):
    def __init__(self, reason: Literal["cancelled", "timeout"], *, cleanup_unconfirmed: bool = False):
        self.reason = reason
        message = "Preparation cancelled." if reason == "cancelled" else "Timed out during child preparation."
        if reason == "timeout" and cleanup_unconfirmed:
            message += " The command exited, but process group cleanup could not be confirmed."
        super().__init__(message)


class PreparationFailure(PublicRuntimeError):
    pass


def _failure(code: str, message: str) -> PreparationFailure:
    return PreparationFailure(PublicError(code, message))


def _default_encoding() -> str:
    with io.TextIOWrapper(io.BytesIO()) as stream:
        return stream.encoding


@dataclass
class _Stream:
    handle: object
    chunks: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=64))
    thread: threading.Thread | None = None
    launch_done: threading.Event = field(default_factory=threading.Event)
    closed: bool = False
    content: bytearray = field(default_factory=bytearray)


@dataclass
class _Command:
    argv: tuple[str, ...]
    cwd: Path
    timeout: float
    deadline: float
    encoding: str
    environment: dict[str, str] | None = None
    filesystem_output: bool = False
    thread: threading.Thread | None = None
    launch_done: threading.Event = field(default_factory=threading.Event)
    native_done: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    process: object | None = None
    windows_job: _WindowsJob | None = None
    streams: list[_Stream] = field(default_factory=list)
    error: BaseException | None = None
    returncode: int | None = None
    cleanup_unconfirmed: bool = False
    output_bytes: int = 0


class PreparationScope:
    def __init__(
        self, workspace: Path, *, parent_session_id: str, child_session_id: str,
        settlement_id: str, config_snapshot: ConfigSnapshot | None,
        run_settings: RunSettings, cancel_event: threading.Event, deadline: float,
        on_target: Callable[[Worktree], None],
        on_state: Callable[[dict], None] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.parent_session_id = parent_session_id
        self.child_session_id = child_session_id
        self.settlement_id = settlement_id
        self.config_snapshot = config_snapshot
        self._settings = run_settings
        self._cancel = cancel_event
        self._deadline = deadline
        self._on_target = on_target
        self._on_state = on_state
        self._identity = command_directory_identity(self.workspace)
        self._env = build_sanitized_subprocess_env(profile="workspace-command")
        self._env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="")
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._sealed = False
        self._record: _Command | None = None
        self._target: Path | None = None
        self._target_identity: tuple[int, int] | None = None

    @property
    def remaining_seconds(self) -> float:
        return max(0.001, self._deadline - time.monotonic())

    @property
    def deadline(self) -> float:
        return self._deadline

    @property
    def target(self) -> Path | None:
        return self._target

    def publish_state(self, state: dict) -> bool:
        if self._on_state is None:
            return False
        self._on_state(state)
        return True

    def checkpoint(self) -> None:
        if self._cancel.is_set() or self._stop.is_set():
            raise PreparationInterrupted("cancelled")
        if time.monotonic() >= self._deadline:
            with self._lock:
                unresolved = self._record is not None and self._record.cleanup_unconfirmed
            raise PreparationInterrupted("timeout", cleanup_unconfirmed=unresolved)
        try:
            if command_directory_identity(self.workspace) != self._identity:
                raise ValueError("parent replaced")
            target = self._target
            if target is not None:
                if target.is_symlink():
                    raise ValueError("target replaced")
                if target.exists():
                    identity = command_directory_identity(target)
                    if self._target_identity is None:
                        self._target_identity = identity
                    elif identity != self._target_identity:
                        raise ValueError("target replaced")
                elif self._target_identity is not None:
                    raise ValueError("target disappeared")
        except (OSError, ValueError, PublicRuntimeError) as exc:
            raise _failure("preparation_directory_changed", "Preparation directory identity changed.") from exc

    def record_target(self, worktree: Worktree) -> None:
        self.checkpoint()
        managed = prepare_user_storage(self.workspace).workspace_cache / "worktrees"
        name = re.sub(r"-+", "-", "".join(c.lower() if c.isalnum() else "-" for c in self.child_session_id)).strip("-") or "subagent"
        target = managed / name
        if (worktree.mode not in {"git", "copy"} or worktree.id != name
                or Path(worktree.path) != target or target.is_symlink()):
            raise _failure("preparation_target_invalid", "Preparation target is not owned by this child.")
        with self._lock:
            if self._sealed or self._stop.is_set():
                raise _failure("preparation_closed", "Preparation no longer accepts work.")
            if self._target is not None and self._target != target:
                raise _failure("preparation_target_invalid", "Preparation target cannot be rebound.")
            self._target = target
        # 保存是父方私有调用者的职责；失败时尚未允许目标变更。
        self._on_target(replace(worktree))
        self.checkpoint()

    def _check_cwd(self, cwd: Path) -> None:
        self.checkpoint()
        if cwd != self.workspace and (self._target is None or cwd != self._target):
            raise _failure("preparation_target_invalid", "Preparation command directory is not owned.")

    def run_git(self, args: tuple[str, ...], *, cwd: Path, timeout: float) -> subprocess.CompletedProcess[str]:
        return self.run(("git", *args), cwd=cwd, timeout=timeout, environment=self._env, filesystem_output=True)

    def run(self, argv: tuple[str, ...], *, cwd: Path, timeout: float, environment=None, filesystem_output=False) -> subprocess.CompletedProcess[str]:
        cwd = Path(cwd).absolute()
        self._check_cwd(cwd)
        record = _Command(tuple(argv), cwd, timeout,
                          min(self._deadline, time.monotonic() + timeout), _default_encoding(),
                          environment=environment, filesystem_output=filesystem_output)
        with self._lock:
            if self._sealed or self._stop.is_set():
                raise _failure("preparation_closed", "Preparation no longer accepts work.")
            if self._record is not None:
                raise _failure("preparation_busy", "The previous preparation command has not retired.")
            self._record = record
        try:
            record.thread = threading.Thread(
                target=Context().run, args=(self._execute, record),
                name=f"nz-preparation-{self.child_session_id}", daemon=True,
            )
            record.thread.start()
        except BaseException as exc:
            self._set_error(record, exc)
            record.stop.set()
            if record.thread is None or record.thread.ident is None:
                record.native_done.set()
        finally:
            record.launch_done.set()
        while not self._wait_record(record, deadline=time.monotonic() + .05):
            self.checkpoint()
        self.checkpoint()
        if record.error is not None:
            raise record.error
        outputs = []
        for stream in record.streams:
            # Git 的 NUL/CR 路径与文件名字节不能经文本换行归一化。
            if record.filesystem_output:
                outputs.append(os.fsdecode(bytes(stream.content)))
            else:
                with io.TextIOWrapper(io.BytesIO(stream.content), encoding=record.encoding) as text:
                    outputs.append(text.read())
        return subprocess.CompletedProcess(record.argv, record.returncode, *outputs)

    def _set_error(self, record: _Command, error: BaseException) -> None:
        with self._lock:
            if record.error is None:
                record.error = error

    def _read(self, record: _Command, stream: _Stream) -> None:
        try:
            while chunk := stream.handle.read(8192):
                while True:
                    try:
                        stream.chunks.put(chunk, timeout=.05)
                        break
                    except queue.Full:
                        continue
        except BaseException:
            self._set_error(record, _failure("preparation_io_error", "Preparation output could not be read."))

    def _drain(self, record: _Command) -> None:
        for stream in record.streams:
            while True:
                try:
                    chunk = stream.chunks.get_nowait()
                except queue.Empty:
                    break
                record.output_bytes += len(chunk)
                if record.output_bytes > self._settings.bash_output_hard_limit_bytes:
                    self._set_error(record, _failure("preparation_output_limit", "Preparation output exceeded its limit."))
                elif record.error is None:
                    stream.content.extend(chunk)

    def _execute(self, record: _Command) -> None:
        try:
            self._check_cwd(record.cwd)
            record.process, record.windows_job = start_owned_pipe_command(
                record.argv, cwd=record.cwd,
                env=record.environment if record.environment is not None else self._env,
            )
            record.streams = [_Stream(record.process.stdout), _Stream(record.process.stderr)]
            for index, stream in enumerate(record.streams):
                try:
                    stream.thread = threading.Thread(
                        target=self._read, args=(record, stream),
                        name=f"nz-preparation-reader-{self.child_session_id}-{index}", daemon=True,
                    )
                    stream.thread.start()
                except BaseException as exc:
                    self._set_error(record, exc)
                finally:
                    stream.launch_done.set()
        except BaseException as exc:
            self._set_error(record, exc)
        if record.process is not None:
            # reader创建失败时，尚未启动的流也必须有明确的启动结束事实。
            for stream in record.streams:
                stream.launch_done.set()
            self._drive(record)
        record.native_done.set()

    def _drive(self, record: _Command) -> None:
        process = record.process
        waited = False
        swept = False
        stopping_at: float | None = None
        while True:
            self._drain(record)
            now = time.monotonic()
            stopping = (record.stop.is_set() or self._stop.is_set() or self._cancel.is_set()
                        or now >= record.deadline or record.error is not None)
            if now >= record.deadline and record.error is None:
                self._set_error(record, subprocess.TimeoutExpired(record.argv, record.timeout))
            if stopping:
                if stopping_at is None:
                    stopping_at = now
                try:
                    terminate_process_tree(
                        process, force=now - stopping_at >= self._settings.process_kill_grace,
                        windows_job=record.windows_job,
                    )
                except (OSError, subprocess.SubprocessError):
                    self._set_error(record, _failure("preparation_io_error", "Preparation process cleanup could not be confirmed."))
            if not waited:
                try:
                    if process.poll() is not None:
                        record.returncode = process.wait(timeout=0)
                        waited = True
                except (OSError, subprocess.SubprocessError):
                    self._set_error(record, _failure("preparation_io_error", "Preparation process exit could not be confirmed."))
            if waited and not swept:
                try:
                    swept = terminate_process_tree(process, force=True, windows_job=record.windows_job)
                    # Windows job 返回 False 时，成员可能正在正常退出。
                    unconfirmed = not swept and record.windows_job is None
                except (OSError, subprocess.SubprocessError):
                    swept = False
                    unconfirmed = True
                with self._lock:
                    record.cleanup_unconfirmed = unconfirmed
            if waited and swept:
                for stream in record.streams:
                    thread = stream.thread
                    if thread is not None and thread.ident is not None:
                        thread.join(0)
                        if thread.is_alive():
                            continue
                    if not stream.closed:
                        try:
                            stream.handle.close()
                            stream.closed = True
                        except (OSError, ValueError):
                            self._set_error(record, _failure("preparation_io_error", "Preparation output could not be closed."))
                self._drain(record)
                if all(stream.closed and stream.chunks.empty() for stream in record.streams):
                    return
            time.sleep(.01)

    def _wait_record(self, record: _Command, *, deadline: float) -> bool:
        if not record.launch_done.wait(max(0., deadline - time.monotonic())):
            return False
        thread = record.thread
        if thread is not None and thread.ident is not None:
            thread.join(max(0., deadline - time.monotonic()))
            if thread.is_alive():
                return False
        if not record.native_done.is_set():
            return False
        with self._lock:
            if self._record is record:
                self._record = None
        return True

    def seal(self) -> None:
        with self._lock:
            self._sealed = True

    def request_stop(self) -> None:
        self._stop.set()

    def close(self, *, deadline: float) -> bool:
        self.request_stop()
        with self._lock:
            record = self._record
        return record is None or self._wait_record(record, deadline=deadline)
