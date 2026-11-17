"""Workspace-scoped lifecycle for Agent-controlled persistent processes."""
from __future__ import annotations

import atexit
from collections import deque
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import asdict, dataclass
from enum import Enum
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid
import weakref

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.runtime.process.platform_runtime import decode_process_output, is_within_workspace
from nz_coder.runtime.process.process_backends import (
    ProcessBackendSession,
    create_process_backend,
)
from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.protocol.public_error import to_public_error


class ProcessStatus(str, Enum):
    STARTING = "starting"
    RUNNING = "running"
    EXITED = "exited"
    FAILED = "failed"
    CANCELLED = "cancelled"
    KILLED = "killed"


_TERMINAL_STATUSES = frozenset({
    ProcessStatus.EXITED,
    ProcessStatus.FAILED,
    ProcessStatus.CANCELLED,
    ProcessStatus.KILLED,
})
@dataclass(frozen=True)
class ProcessHandle:
    """Serializable process identity exposed to tools instead of ``Popen``."""

    process_id: str
    command: str
    cwd: str
    started_at: float
    status: str
    exit_code: int | None
    pid: int | None
    tty: bool
    owner_session_id: str
    owner_agent_id: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProcessReadResult:
    process_id: str
    output: str
    cursor: int
    next_cursor: int
    buffer_start_cursor: int
    buffer_end_cursor: int
    truncated_before_cursor: bool
    has_more: bool
    status: str
    exit_code: int | None
    cancelled: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ProcessTerminalFact:
    """确认执行线程已退出后的服务事实，不依赖 read/status 调用。"""

    handle: ProcessHandle
    launch_id: str
    output: str
    output_complete: bool
    pipefail: bool = False


class ProcessNotFoundError(LookupError):
    pass


class ProcessOwnershipError(PermissionError):
    pass


class ProcessStateError(RuntimeError):
    pass


class _ManagedProcess:
    def __init__(
        self,
        handle: ProcessHandle,
        *,
        backend: ProcessBackendSession | None,
        event_bus=None,
        buffer_limit: int,
        kill_grace_seconds: float,
    ) -> None:
        self.handle = handle
        self.backend = backend
        self.environment = None
        self.pipefail = backend.pipefail if backend is not None else False
        self.buffer_limit = max(1024, int(buffer_limit))
        self.kill_grace_seconds = max(0.0, float(kill_grace_seconds))
        self.buffer = bytearray()
        self.buffer_start_cursor = 0
        self.output_eof = False
        self.cursor = 0
        self.condition = threading.Condition(threading.RLock())
        self.requested_terminal: ProcessStatus | None = None
        self.last_output_event_at = 0.0
        self.last_output_at = 0.0
        self.event_bus_ref = weakref.ref(event_bus) if event_bus is not None else None
        self.capability_lease_id = ""
        self.exit_confirmed = False
        self.actual_exit_code: int | None = None
        self.cleanup_done = threading.Event()
        self.startup_done = threading.Event()
        self.launch_done = threading.Event()
        self.reader: threading.Thread | None = None
        self.waiter: threading.Thread | None = None
        self.reader_started = False
        self.waiter_started = False
        self.inflight_io = 0
        self.write_lock = threading.Lock()
        self.rows = 24
        self.cols = 80
        self.launch_id = ""
        self.terminal_queued = False
        self.terminal_delivery_done = False
        self.pending_terminal_callbacks: set = set()

    def bind_event_bus(self, event_bus) -> None:
        if event_bus is None:
            return
        with self.condition:
            self.event_bus_ref = weakref.ref(event_bus)

    def publish(self, event_type: str, properties: dict) -> None:
        reference = self.event_bus_ref
        bus = reference() if reference is not None else None
        if bus is None:
            return
        try:
            bus.publish(event_type, properties)
        except Exception:
            # 事件回调不能阻止资源交接；关闭仍检查线程实际返回。
            pass


class ProcessService:
    """Own persistent child processes for one resolved workspace.

    Process records survive individual Agent runs. Session deletion and service
    shutdown are explicit cleanup boundaries; cancelling a read is not.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        buffer_bytes: int | None = None,
        max_processes: int | None = None,
        kill_grace_seconds: float | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self._buffer_bytes_override = buffer_bytes
        self._max_processes_override = max_processes
        self._kill_grace_override = kill_grace_seconds
        self._lock = threading.RLock()
        self._records: dict[str, _ManagedProcess] = {}
        self._closed = False
        self._draining = False
        self._terminal_condition = threading.Condition(self._lock)
        self._terminal_callbacks: dict = {}
        self._terminal_queue = deque()
        self._terminal_thread: threading.Thread | None = None
        self._terminal_stopping = False
        self._terminal_starting = False
        self._terminal_launch_done = threading.Event()
        self._terminal_launch_done.set()

    def subscribe_terminal(self, callback, *, on_delivery_done=None):
        """每服务一个通知线程，条件唤醒后 join 真正执行线程；不定时轮询。"""
        with self._terminal_condition:
            if self._draining or self._closed:
                raise ProcessStateError("process service is draining or closed")
            self._terminal_callbacks[callback] = on_delivery_done
            launch = self._terminal_thread is None and not self._terminal_starting
            if launch:
                self._terminal_starting = True
                self._terminal_launch_done.clear()
        if launch:
            worker = None
            try:
                worker = threading.Thread(target=self._deliver_terminals,
                    name="nz-process-terminal-delivery", daemon=True)
                with self._terminal_condition:
                    self._terminal_thread = worker
                worker.start()
            except BaseException:
                with self._terminal_condition:
                    self._terminal_callbacks.pop(callback, None)
                    if worker is None or worker.ident is None:
                        self._terminal_thread = None
                raise
            finally:
                with self._terminal_condition:
                    self._terminal_starting = False
                    self._terminal_launch_done.set()
                    self._terminal_condition.notify_all()
        else:
            self._terminal_launch_done.wait()
            with self._terminal_condition:
                if self._terminal_thread is None:
                    self._terminal_callbacks.pop(callback, None)
                    raise ProcessStateError("process terminal delivery failed to start")

        def unsubscribe():
            with self._terminal_condition:
                self._terminal_callbacks.pop(callback, None)
                # 退订不冒充已投递；已经交接的历史需由 owner 显式 ACK。
                self._terminal_condition.notify_all()

        return unsubscribe

    def acknowledge_terminal(self, process_id: str, callback) -> None:
        with self._terminal_condition:
            record = self._records.get(process_id)
            if record is not None:
                record.pending_terminal_callbacks.discard(callback)
            self._terminal_condition.notify_all()

    def _queue_terminal(self, record: _ManagedProcess) -> None:
        with self._terminal_condition:
            if record.terminal_queued or not self._terminal_callbacks:
                return
            record.terminal_queued = True
            record.pending_terminal_callbacks.update(self._terminal_callbacks)
            self._terminal_queue.append(record)
            self._terminal_condition.notify_all()

    def _deliver_terminals(self) -> None:
        while True:
            with self._terminal_condition:
                self._terminal_condition.wait_for(lambda: self._terminal_queue or self._terminal_stopping)
                if not self._terminal_queue:
                    return
                record = self._terminal_queue.popleft()
            record.launch_done.wait()
            if record.waiter is not None and record.waiter.ident is not None:
                record.waiter.join()
            with record.condition:
                fact = ProcessTerminalFact(record.handle, record.launch_id,
                    decode_process_output(bytes(record.buffer)), record.output_eof and record.buffer_start_cursor == 0,
                    record.pipefail)
            with self._terminal_condition:
                callbacks = tuple((callback, self._terminal_callbacks.get(callback))
                                  for callback in record.pending_terminal_callbacks)
            for callback, _on_done in callbacks:
                try:
                    accepted = callback(fact) is not False
                except Exception:
                    accepted = False
                if accepted:
                    self.acknowledge_terminal(record.handle.process_id, callback)
            with self._terminal_condition:
                record.terminal_delivery_done = True
                self._terminal_condition.notify_all()
            for _callback, on_done in callbacks:
                if on_done is not None:
                    on_done()

    def terminal_delivery_pending(self) -> bool:
        with self._lock:
            return any(record.cleanup_done.is_set() and not record.terminal_delivery_done
                       for record in self._records.values() if record.terminal_queued or self._terminal_callbacks)

    def wait_terminal_delivery(self, *, deadline: float) -> bool:
        """只等待已退出进程的交接，不等待仍在运行的本体。"""
        with self._terminal_condition:
            return self._terminal_condition.wait_for(lambda: not self.terminal_delivery_pending(),
                max(0.0, deadline - time.monotonic()))

    def _lifecycle_target(self, record: _ManagedProcess) -> None:
        try:
            self._run_lifecycle(record)
        finally:
            # 队列仅表示可以 join；发布者必须等本线程实际返回才能交付终态。
            self._queue_terminal(record)

    def _effective_max_processes(self) -> int:
        return max(1, int(
            self._max_processes_override
            or current_run_settings().process_max_per_workspace
        ))

    def _effective_kill_grace(self) -> float:
        selected = (
            current_run_settings().process_kill_grace
            if self._kill_grace_override is None
            else self._kill_grace_override
        )
        return max(0.0, float(selected))

    def start(
        self,
        command: str,
        *,
        cwd: Path,
        owner_session_id: str,
        owner_agent_id: str = "",
        tty: bool = True,
        rows: int = 24,
        cols: int = 80,
        event_bus=None,
        launch_id: str = "",
        environment=None,
    ) -> ProcessHandle:
        from nz_coder.runtime.process.execution_environment import current_execution_environment
        selected_environment = environment or current_execution_environment()
        if selected_environment is not None:
            selected_environment.validate()
        settings = current_run_settings()
        buffer_bytes = max(1024, int(
            self._buffer_bytes_override or settings.process_buffer_bytes
        ))
        max_processes = max(1, int(
            self._max_processes_override or settings.process_max_per_workspace
        ))
        kill_grace_seconds = self._effective_kill_grace()
        command = str(command or "").strip()
        if not command:
            raise ValueError("command cannot be empty")
        resolved_cwd = Path(cwd).resolve()
        if not is_within_workspace(resolved_cwd, self.workspace):
            raise ValueError("process cwd escapes workspace")
        if not resolved_cwd.is_dir():
            raise ValueError(f"process cwd is not a directory: {resolved_cwd}")

        with self._lock:
            if self._draining or self._closed:
                raise ProcessStateError("process service is draining or closed")
            self._prune_terminal_locked()
            active = sum(not self._record_released(record) for record in self._records.values())
            if active >= max_processes:
                raise ProcessStateError(
                    f"workspace process limit reached ({max_processes})"
                )

            process_id = f"proc_{uuid.uuid4().hex[:12]}"
            started_at = time.time()
            starting = ProcessHandle(
                process_id=process_id,
                command=command,
                cwd=str(resolved_cwd),
                started_at=started_at,
                status=ProcessStatus.STARTING.value,
                exit_code=None,
                pid=None,
                tty=bool(tty),
                owner_session_id=str(owner_session_id or ""),
                owner_agent_id=str(owner_agent_id or ""),
            )
            record = _ManagedProcess(
                starting,
                backend=None,
                event_bus=event_bus,
                buffer_limit=buffer_bytes,
                kill_grace_seconds=kill_grace_seconds,
            )
            self._records[process_id] = record
            record.environment = selected_environment
            record.rows, record.cols = rows, cols
            record.launch_id = launch_id

        # 预留之后的外部操作不占服务锁，关闭可以及时进入 draining。
        context = copy_context()
        service_ref = weakref.ref(self)

        def revoke_process() -> None:
            service = service_ref()
            if service is None:
                raise ProcessStateError("process owner is unavailable")
            service._request_stop(record, ProcessStatus.CANCELLED)
            if not service._await_cleanup(
                record,
                service._deadline([record], None),
                join_worker=False,
            ):
                raise ProcessStateError("process resources are still pending")

        try:
            snapshot = settings.snapshot
            lease = capability_leases().create(
                kind="persistent-process",
                resource_id=process_id,
                workspace=self.workspace,
                control_fingerprint=(
                    snapshot.control_fingerprint if snapshot else "legacy"
                ),
                run_id=starting.owner_session_id,
                interaction_id=starting.owner_agent_id or starting.owner_session_id,
                owner_session=starting.owner_session_id,
                revoke=revoke_process,
            )
            record.capability_lease_id = lease.lease_id
            record.waiter = threading.Thread(
                target=context.run,
                args=(self._lifecycle_target, record),
                name=f"nz-process-wait-{process_id[-6:]}",
                daemon=True,
            )
            record.waiter.start()
            record.waiter_started = True
        except Exception:
            self._finish(record, ProcessStatus.FAILED)
            record.startup_done.set()
            self._queue_terminal(record)
            raise
        finally:
            record.launch_done.set()
        record.startup_done.wait()
        with record.condition:
            return record.handle

    def get(
        self,
        process_id: str,
        *,
        owner_session_id: str | None = None,
        event_bus=None,
    ) -> ProcessHandle:
        record = self._record(
            process_id,
            owner_session_id=owner_session_id,
            event_bus=event_bus,
        )
        with record.condition:
            return record.handle

    def list(
        self,
        *,
        owner_session_id: str | None = None,
        active_only: bool = False,
    ) -> list[ProcessHandle]:
        with self._lock:
            records = list(self._records.values())
        values: list[ProcessHandle] = []
        for record in records:
            with record.condition:
                handle = record.handle
            if owner_session_id is not None and handle.owner_session_id != owner_session_id:
                continue
            if active_only and record.cleanup_done.is_set():
                continue
            values.append(handle)
        return sorted(values, key=lambda item: (item.started_at, item.process_id))

    def read(
        self,
        process_id: str,
        *,
        owner_session_id: str | None = None,
        cursor: int | None = None,
        tail_bytes: int | None = None,
        max_bytes: int | None = None,
        wait_seconds: float = 0.0,
        cancel_event: threading.Event | None = None,
        event_bus=None,
    ) -> ProcessReadResult:
        record = self._record(
            process_id,
            owner_session_id=owner_session_id,
            event_bus=event_bus,
        )
        settings = current_run_settings()
        limit = max(1, min(
            int(max_bytes or settings.process_read_max_bytes),
            settings.process_read_max_bytes,
        ))
        wait_budget = max(0.0, min(float(wait_seconds), 30.0))
        deadline = time.monotonic() + wait_budget
        cancelled = False
        with record.condition:
            requested = cursor
            if requested is not None:
                requested = int(requested)
                if requested < -1:
                    raise ValueError("cursor must be -1 or a non-negative integer")
                # -1 固定为调用开始时的末尾，等待时不能随新输出向前移动。
                if requested == -1:
                    requested = record.cursor
            saw_output = False
            while True:
                start = record.buffer_start_cursor
                end = record.cursor
                if requested is None:
                    tail = max(0, int(tail_bytes or 0))
                    selected = max(start, end - tail) if tail else start
                else:
                    selected = requested
                terminal = record.handle.status in {
                    item.value for item in _TERMINAL_STATUSES
                }
                if selected < end:
                    saw_output = True
                # PTYs commonly echo stdin just before the application emits
                # its response. A waiting read coalesces a short quiet period
                # so one interaction is not split at that transport boundary.
                quiet = time.monotonic() - record.last_output_at
                if terminal or (
                    saw_output and (wait_budget <= 0 or quiet >= 0.05)
                ):
                    break
                if cancel_event is not None and cancel_event.is_set():
                    cancelled = True
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                record.condition.wait(timeout=min(0.1, remaining))

            start = record.buffer_start_cursor
            end = record.cursor
            truncated_before = selected < start
            actual = max(start, min(selected, end))
            offset = actual - start
            raw = bytes(record.buffer[offset:offset + limit])
            next_cursor = actual + len(raw)
            return ProcessReadResult(
                process_id=process_id,
                output=decode_process_output(
                    raw,
                    preferred_encoding=settings.process_output_encoding,
                ),
                cursor=selected,
                next_cursor=next_cursor,
                buffer_start_cursor=start,
                buffer_end_cursor=end,
                truncated_before_cursor=truncated_before,
                has_more=next_cursor < end,
                status=record.handle.status,
                exit_code=record.handle.exit_code,
                cancelled=cancelled,
            )

    def write(
        self,
        process_id: str,
        data: str,
        *,
        owner_session_id: str | None = None,
        event_bus=None,
    ) -> ProcessHandle:
        record = self._record(
            process_id,
            owner_session_id=owner_session_id,
            event_bus=event_bus,
        )
        payload = str(data).encode("utf-8")
        write_limit = current_run_settings().process_write_max_bytes
        if len(payload) > write_limit:
            raise ValueError(
                f"process write exceeds {write_limit} bytes"
            )
        try:
            with self._io(record) as backend:
                with record.write_lock:
                    backend.write_bytes(payload)
        except (BrokenPipeError, OSError) as exc:
            raise ProcessStateError(f"process write failed: {exc}") from exc

        record.publish("process.input", {
            "process_id": process_id,
            "bytes": len(payload),
        })
        return self.get(process_id, owner_session_id=owner_session_id)

    def resize(
        self,
        process_id: str,
        *,
        rows: int,
        cols: int,
        owner_session_id: str | None = None,
        event_bus=None,
    ) -> ProcessHandle:
        record = self._record(
            process_id,
            owner_session_id=owner_session_id,
            event_bus=event_bus,
        )
        with self._io(record) as backend:
            if not backend.tty:
                raise ProcessStateError("terminal resize is unavailable for pipe processes")
            try:
                backend.resize(rows=rows, cols=cols)
            except (OSError, RuntimeError) as exc:
                raise ProcessStateError(f"terminal resize failed: {exc}") from exc
        return self.get(process_id, owner_session_id=owner_session_id)

    @staticmethod
    @contextmanager
    def _io(record: _ManagedProcess):
        with record.condition:
            if (
                record.handle.status != ProcessStatus.RUNNING.value
                or record.requested_terminal is not None
                or record.exit_confirmed
            ):
                raise ProcessStateError(
                    f"process is not running (status={record.handle.status})"
                )
            backend = record.backend
            if backend is None:
                raise ProcessStateError("process has no live backend")
            record.inflight_io += 1
        try:
            yield backend
        finally:
            with record.condition:
                record.inflight_io -= 1
                record.condition.notify_all()

    @staticmethod
    def _deadline(records: list[_ManagedProcess], deadline: float | None) -> float:
        if deadline is not None:
            return deadline
        return time.monotonic() + (
            max(record.kill_grace_seconds for record in records) + 1.0
            if records
            else 0.0
        )

    @staticmethod
    def _request_stop(record: _ManagedProcess, reason: ProcessStatus) -> None:
        with record.condition:
            if not record.cleanup_done.is_set() and record.requested_terminal is None:
                record.requested_terminal = reason
                record.condition.notify_all()

    @staticmethod
    def _await_cleanup(
        record: _ManagedProcess, deadline: float, *, join_worker: bool
    ) -> bool:
        if not record.cleanup_done.wait(max(0.0, deadline - time.monotonic())):
            return False
        if not join_worker:
            return True
        if not record.launch_done.wait(max(0.0, deadline - time.monotonic())):
            return False
        for worker, started in (
            (record.reader, record.reader_started),
            (record.waiter, record.waiter_started),
        ):
            if started and worker is not None:
                if worker is threading.current_thread():
                    return False
                worker.join(max(0.0, deadline - time.monotonic()))
                if worker.is_alive():
                    return False
        return True

    def kill(
        self,
        process_id: str,
        *,
        owner_session_id: str | None = None,
        reason: ProcessStatus = ProcessStatus.KILLED,
        event_bus=None,
        deadline: float | None = None,
    ) -> ProcessHandle:
        record = self._record(
            process_id, owner_session_id=owner_session_id, event_bus=event_bus
        )
        deadline = self._deadline([record], deadline)
        self._request_stop(record, reason)
        self._await_cleanup(record, deadline, join_worker=False)
        with record.condition:
            return record.handle

    def kill_session(self, session_id: str, *, deadline: float | None = None) -> int:
        with self._lock:
            records = [
                record
                for record in self._records.values()
                if record.handle.owner_session_id == str(session_id)
            ]
        pending = [record for record in records if not self._record_released(record)]
        deadline = self._deadline(pending, deadline)
        for record in pending:
            self._request_stop(record, ProcessStatus.CANCELLED)
        if not all(
            [
                self._await_cleanup(record, deadline, join_worker=True)
                for record in pending
            ]
        ):
            raise ProcessStateError("session process resources are still pending")
        return len(pending)

    def _begin_close(self) -> list[_ManagedProcess]:
        with self._lock:
            self._draining = True
            records = list(self._records.values())
        return records

    def _close_records(self, records: list[_ManagedProcess], deadline: float) -> None:
        if not self._terminal_launch_done.wait(max(0.0, deadline - time.monotonic())):
            raise ProcessStateError("process terminal delivery startup is still pending")
        complete = [
            self._await_cleanup(record, deadline, join_worker=True)
            for record in records
        ]
        if not all(complete):
            raise ProcessStateError("process service cleanup is still pending")
        with self._terminal_condition:
            if not self._terminal_condition.wait_for(
                lambda: not any(record.pending_terminal_callbacks for record in records),
                max(0.0, deadline - time.monotonic()),
            ):
                raise ProcessStateError("process terminal facts are still pending")
            self._terminal_stopping = True
            self._terminal_condition.notify_all()
        worker = self._terminal_thread
        if worker is not None and worker.ident is not None:
            worker.join(max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                raise ProcessStateError("process terminal delivery is still pending")
        with self._lock:
            self._closed = True

    def request_close(self) -> None:
        """只登记draining和停止，让外部关闭不必先等待native I/O。"""
        for record in self._begin_close():
            self._request_stop(record, ProcessStatus.KILLED)

    def close(self, *, deadline: float | None = None) -> None:
        self.request_close()
        records = self._begin_close()
        self._close_records(records, self._deadline(records, deadline))

    @staticmethod
    def _record_released(record: _ManagedProcess) -> bool:
        return (
            record.cleanup_done.is_set()
            and record.launch_done.is_set()
            and not record.pending_terminal_callbacks
            and not any(
                worker is not None and started and worker.is_alive()
                for worker, started in (
                    (record.reader, record.reader_started),
                    (record.waiter, record.waiter_started),
                )
            )
        )

    def _record(
        self,
        process_id: str,
        *,
        owner_session_id: str | None,
        event_bus,
    ) -> _ManagedProcess:
        with self._lock:
            record = self._records.get(str(process_id or ""))
        if record is None:
            raise ProcessNotFoundError(f"unknown process_id: {process_id}")
        with record.condition:
            owner = record.handle.owner_session_id
        if owner_session_id is not None and owner != str(owner_session_id):
            raise ProcessOwnershipError("process belongs to another session")
        record.bind_event_bus(event_bus)
        return record

    def _prune_terminal_locked(self) -> None:
        terminal = sorted(
            (
                record
                for record in self._records.values()
                if self._record_released(record)
            ),
            key=lambda item: item.handle.started_at,
        )
        while len(self._records) >= self._effective_max_processes() * 2 and terminal:
            record = terminal.pop(0)
            self._records.pop(record.handle.process_id, None)

    def _read_output(self, record: _ManagedProcess) -> None:
        backend = record.backend
        if backend is None:
            return
        try:
            while True:
                chunk = backend.read_bytes(64 * 1024)
                if not chunk:
                    with record.condition:
                        record.output_eof = True
                    break
                self._append_output(record, chunk)
        except Exception as exc:
            self._append_output(record, f"\n[process output error: {exc}]\n".encode())
        finally:
            with record.condition:
                record.condition.notify_all()

    def _append_output(self, record: _ManagedProcess, chunk: bytes) -> None:
        with record.condition:
            record.buffer.extend(chunk)
            record.cursor += len(chunk)
            record.last_output_at = time.monotonic()
            excess = len(record.buffer) - record.buffer_limit
            if excess > 0:
                del record.buffer[:excess]
                record.buffer_start_cursor += excess
            now = time.monotonic()
            publish_output = now - record.last_output_event_at >= 0.1
            if publish_output:
                record.last_output_event_at = now
            cursor = record.cursor
            record.condition.notify_all()
        if publish_output:
            record.publish("process.output", {
                "process_id": record.handle.process_id,
                "bytes": len(chunk),
                "cursor": cursor,
            })

    def _run_lifecycle(self, record: _ManagedProcess) -> None:
        with record.condition:
            stopped = record.requested_terminal is not None
        if stopped:
            self._finish(record, ProcessStatus.CANCELLED)
            record.startup_done.set()
            return
        requested_tty = record.handle.tty
        try:
            backend = create_process_backend(
                record.handle.command,
                cwd=Path(record.handle.cwd),
                tty=requested_tty,
                rows=record.rows,
                cols=record.cols,
                environment=record.environment,
            )
        except Exception as exc:
            public = to_public_error(exc)
            self._append_output(
                record, f"Process failed to start: {public.message}\n".encode()
            )
            self._finish(record, ProcessStatus.FAILED)
            record.startup_done.set()
            return
        with record.condition:
            record.backend = backend
            record.pipefail = backend.pipefail
            record.handle = ProcessHandle(
                **{
                    **record.handle.to_dict(),
                    "status": ProcessStatus.RUNNING.value,
                    "pid": backend.pid,
                    "tty": backend.tty,
                }
            )
        if (
            requested_tty
            and not backend.tty
            and getattr(backend, "os_name", "") == "nt"
        ):
            self._append_output(
                record,
                (
                    "Interactive terminal features unavailable; using pipe fallback. "
                    "Install Windows PTY support with: pip install pywinpty\n"
                ).encode(),
            )
        try:
            record.reader = threading.Thread(
                target=self._read_output,
                args=(record,),
                name=f"nz-process-read-{record.handle.process_id[-6:]}",
                daemon=True,
            )
            record.reader.start()
            record.reader_started = True
        except Exception:
            self._request_stop(record, ProcessStatus.FAILED)
        record.startup_done.set()
        record.publish("process.started", {"process": record.handle.to_dict()})

        next_stop_at = 0.0
        stop_retry_delays = iter((0.05, 0.1, 0.2, 0.5))
        swept = False
        retry_delays = iter((0.05, 0.1, 0.2, 0.5))
        while True:
            try:
                with record.condition:
                    stopping = record.requested_terminal is not None
                if (
                    stopping
                    and not record.exit_confirmed
                    and time.monotonic() >= next_stop_at
                ):
                    # 发送停止后仍须观察本体；静默失败不能令原 owner 永久放弃重试。
                    backend.terminate_tree(grace_seconds=record.kill_grace_seconds)
                    next_stop_at = time.monotonic() + next(stop_retry_delays, 1.0)
                if not record.exit_confirmed:
                    observed_at = time.monotonic()
                    try:
                        code = backend.wait(timeout=0.05)
                    except subprocess.TimeoutExpired:
                        code = backend.poll()
                    except Exception:
                        code = backend.poll()
                        if code is None:
                            raise
                    if code is None:
                        with record.condition:
                            record.condition.wait(
                                timeout=max(
                                    0.0, 0.05 - (time.monotonic() - observed_at)
                                )
                            )
                        continue
                    with record.condition:
                        record.actual_exit_code = code
                        record.exit_confirmed = True
                if not swept:
                    # 本体退出后仍清扫同组后代，使 reader 有机会收到真正 EOF。
                    backend.terminate_tree(grace_seconds=record.kill_grace_seconds)
                    swept = True
                if record.reader_started and record.reader is not None:
                    record.reader.join(0.05)
                    if record.reader.is_alive():
                        continue
                with record.condition:
                    if record.inflight_io:
                        record.condition.wait(timeout=0.05)
                        continue
                backend.close()
                break
            except Exception:
                time.sleep(next(retry_delays, 1.0))
        self._finish(record, ProcessStatus.EXITED)

    @staticmethod
    def _finish(record: _ManagedProcess, fallback: ProcessStatus) -> None:
        with record.condition:
            if record.cleanup_done.is_set():
                return
            status = record.requested_terminal or fallback
            record.handle = ProcessHandle(
                **{
                    **record.handle.to_dict(),
                    "status": status.value,
                    "exit_code": record.actual_exit_code,
                }
            )
            record.backend = None
            if record.capability_lease_id:
                capability_leases().release(record.capability_lease_id)
                record.capability_lease_id = ""
            record.cleanup_done.set()
            record.condition.notify_all()
        properties = {
            "process_id": record.handle.process_id,
            "status": status.value,
            "exit_code": record.actual_exit_code,
        }
        record.publish("process.exited", properties)
        if status in {ProcessStatus.KILLED, ProcessStatus.CANCELLED}:
            record.publish("process.killed", {**properties, "reason": status.value})
        elif fallback == ProcessStatus.FAILED:
            record.publish("process.failed", properties)


_REGISTRY_LOCK = threading.RLock()
_REGISTRY_PID = os.getpid()
_WORKSPACE_SERVICES: dict[Path, ProcessService] = {}


def _ensure_registry_process() -> None:
    global _REGISTRY_PID
    current = os.getpid()
    if current == _REGISTRY_PID:
        return
    # A forked child must never kill handles owned by its parent process.
    _WORKSPACE_SERVICES.clear()
    _REGISTRY_PID = current


def workspace_process_service(workspace: Path) -> ProcessService:
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        service = _WORKSPACE_SERVICES.get(key)
        if service is None:
            service = ProcessService(key)
            _WORKSPACE_SERVICES[key] = service
        return service


def close_workspace_process_service(
    workspace: Path, *, deadline: float | None = None
) -> int:
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        service = _WORKSPACE_SERVICES.get(key)
    if service is None:
        return 0
    count = len(service.list(active_only=True))
    service.close(deadline=deadline)
    with _REGISTRY_LOCK:
        if _WORKSPACE_SERVICES.get(key) is service:
            del _WORKSPACE_SERVICES[key]
    return count


def dispose_session_processes(
    workspace: Path,
    session_id: str,
    *,
    deadline: float | None = None,
) -> int:
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        service = _WORKSPACE_SERVICES.get(key)
    return (
        service.kill_session(session_id, deadline=deadline)
        if service is not None
        else 0
    )


def close_all_process_services(*, deadline: float | None = None) -> int:
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        # 入口快照同时封住每个已登记服务的新启动；这里只做短锁和请求。
        selected = [
            (key, service, service._begin_close())
            for key, service in _WORKSPACE_SERVICES.items()
        ]
    records = [record for _, _, batch in selected for record in batch]
    deadline = ProcessService._deadline(records, deadline)
    count = sum(not record.cleanup_done.is_set() for record in records)
    for record in records:
        ProcessService._request_stop(record, ProcessStatus.KILLED)
    failures = []
    for key, service, batch in selected:
        try:
            service._close_records(batch, deadline)
        except Exception as exc:
            failures.append(f"{key}: {exc}")
        else:
            with _REGISTRY_LOCK:
                if _WORKSPACE_SERVICES.get(key) is service:
                    del _WORKSPACE_SERVICES[key]
    if failures:
        raise ProcessStateError(
            "process services cleanup pending: " + "; ".join(failures)
        )
    return count


atexit.register(close_all_process_services)


__all__ = [
    "ProcessHandle",
    "ProcessNotFoundError",
    "ProcessOwnershipError",
    "ProcessReadResult",
    "ProcessService",
    "ProcessStateError",
    "ProcessStatus",
    "close_all_process_services",
    "close_workspace_process_service",
    "dispose_session_processes",
    "workspace_process_service",
]
