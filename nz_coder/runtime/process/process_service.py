"""Workspace-scoped lifecycle for Agent-controlled persistent processes."""
from __future__ import annotations

import atexit
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
        self.buffer_limit = max(1024, int(buffer_limit))
        self.kill_grace_seconds = max(0.0, float(kill_grace_seconds))
        self.buffer = bytearray()
        self.buffer_start_cursor = 0
        self.cursor = 0
        self.condition = threading.Condition(threading.RLock())
        self.requested_terminal: ProcessStatus | None = None
        self.exit_event_published = False
        self.last_output_event_at = 0.0
        self.last_output_at = 0.0
        self.event_bus_ref = weakref.ref(event_bus) if event_bus is not None else None

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
        except RuntimeError:
            # A process may intentionally outlive the Run-owned event bus.
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
    ) -> ProcessHandle:
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
            if self._closed:
                raise ProcessStateError("process service is closed")
            self._prune_terminal_locked()
            active = sum(
                record.handle.status not in {item.value for item in _TERMINAL_STATUSES}
                for record in self._records.values()
            )
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
            try:
                backend = create_process_backend(
                    command,
                    cwd=resolved_cwd,
                    tty=bool(tty),
                    rows=rows,
                    cols=cols,
                )
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                record.handle = ProcessHandle(
                    **{
                        **starting.to_dict(),
                        "status": ProcessStatus.FAILED.value,
                        "exit_code": None,
                    }
                )
                self._append_output(record, f"Process failed to start: {exc}\n".encode())
                record.publish("process.failed", {
                    "process_id": process_id,
                    "error": str(exc),
                })
                return record.handle

            record.backend = backend
            record.handle = ProcessHandle(
                **{
                    **starting.to_dict(),
                    "status": ProcessStatus.RUNNING.value,
                    "pid": backend.pid,
                    "tty": backend.tty,
                }
            )
            if (
                tty
                and not backend.tty
                and getattr(backend, "os_name", "") == "nt"
            ):
                self._append_output(
                    record,
                    (
                        "Interactive terminal features unavailable; using pipe fallback. "
                        "Install Windows PTY support with: pip install pywinpty\n"
                    ).encode("utf-8"),
                )

        reader = threading.Thread(
            target=self._read_output,
            args=(record,),
            name=f"nz-process-read-{process_id[-6:]}",
            daemon=True,
        )
        waiter = threading.Thread(
            target=self._wait_for_exit,
            args=(record,),
            name=f"nz-process-wait-{process_id[-6:]}",
            daemon=True,
        )
        reader.start()
        waiter.start()
        record.publish("process.started", {"process": record.handle.to_dict()})
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
            if active_only and handle.status in {item.value for item in _TERMINAL_STATUSES}:
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
            saw_output = False
            while True:
                start = record.buffer_start_cursor
                end = record.cursor
                if requested is None:
                    tail = max(0, int(tail_bytes or 0))
                    selected = max(start, end - tail) if tail else start
                elif requested == -1:
                    selected = end
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
        with record.condition:
            if record.handle.status != ProcessStatus.RUNNING.value:
                raise ProcessStateError(
                    f"process is not running (status={record.handle.status})"
                )
            backend = record.backend
        if backend is None:
            raise ProcessStateError("process has no live backend")
        try:
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
        with record.condition:
            if record.handle.status != ProcessStatus.RUNNING.value:
                raise ProcessStateError(
                    f"process is not running (status={record.handle.status})"
                )
            backend = record.backend
            if backend is None or not backend.tty:
                raise ProcessStateError("terminal resize is unavailable for pipe processes")
            try:
                backend.resize(rows=rows, cols=cols)
            except (OSError, RuntimeError) as exc:
                raise ProcessStateError(f"terminal resize failed: {exc}") from exc
            return record.handle

    def kill(
        self,
        process_id: str,
        *,
        owner_session_id: str | None = None,
        reason: ProcessStatus = ProcessStatus.KILLED,
        event_bus=None,
    ) -> ProcessHandle:
        record = self._record(
            process_id,
            owner_session_id=owner_session_id,
            event_bus=event_bus,
        )
        with record.condition:
            if record.handle.status in {item.value for item in _TERMINAL_STATUSES}:
                return record.handle
            record.requested_terminal = reason
            backend = record.backend
        if backend is not None:
            backend.terminate_tree(grace_seconds=record.kill_grace_seconds)
            try:
                backend.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            self._mark_exit(record, backend.poll())
        with record.condition:
            handle = record.handle
        record.publish("process.killed", {
            "process_id": process_id,
            "reason": reason.value,
            "exit_code": handle.exit_code,
        })
        return handle

    def kill_session(self, session_id: str) -> int:
        handles = self.list(owner_session_id=str(session_id), active_only=True)
        for handle in handles:
            self.kill(
                handle.process_id,
                owner_session_id=str(session_id),
                reason=ProcessStatus.CANCELLED,
            )
        return len(handles)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handles = [
                record.handle
                for record in self._records.values()
                if record.handle.status not in {item.value for item in _TERMINAL_STATUSES}
            ]
        for handle in handles:
            try:
                self.kill(handle.process_id, reason=ProcessStatus.KILLED)
            except (ProcessNotFoundError, ProcessStateError):
                pass
        with self._lock:
            records = list(self._records.values())
        for record in records:
            self._close_record_streams(record)

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
                record for record in self._records.values()
                if record.handle.status in {item.value for item in _TERMINAL_STATUSES}
            ),
            key=lambda item: item.handle.started_at,
        )
        while len(self._records) >= self._effective_max_processes() * 2 and terminal:
            record = terminal.pop(0)
            self._records.pop(record.handle.process_id, None)
            self._close_record_streams(record)

    def _read_output(self, record: _ManagedProcess) -> None:
        backend = record.backend
        if backend is None:
            return
        try:
            while True:
                chunk = backend.read_bytes(64 * 1024)
                if not chunk:
                    break
                self._append_output(record, chunk)
        except OSError as exc:
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

    def _wait_for_exit(self, record: _ManagedProcess) -> None:
        backend = record.backend
        if backend is None:
            return
        try:
            exit_code = backend.wait()
        except BaseException:
            exit_code = backend.poll()
        if record.requested_terminal is None:
            # A shell leader can exit after daemonizing a child. The backend
            # still owns the group/job and sweeps it before status publication.
            backend.terminate_tree(grace_seconds=record.kill_grace_seconds)
        self._mark_exit(record, exit_code)

    def _mark_exit(self, record: _ManagedProcess, exit_code: int | None) -> None:
        with record.condition:
            current = record.handle
            if current.status in {item.value for item in _TERMINAL_STATUSES}:
                return
            status = record.requested_terminal or ProcessStatus.EXITED
            record.handle = ProcessHandle(
                **{
                    **current.to_dict(),
                    "status": status.value,
                    "exit_code": exit_code,
                }
            )
            should_publish = not record.exit_event_published
            record.exit_event_published = True
            record.condition.notify_all()
        if should_publish:
            record.publish("process.exited", {
                "process_id": current.process_id,
                "status": status.value,
                "exit_code": exit_code,
            })
    @staticmethod
    def _close_record_streams(record: _ManagedProcess) -> None:
        with record.condition:
            backend = record.backend
            record.backend = None
        if backend is not None:
            backend.close()


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


def close_workspace_process_service(workspace: Path) -> int:
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        service = _WORKSPACE_SERVICES.pop(key, None)
    if service is None:
        return 0
    count = len(service.list(active_only=True))
    service.close()
    return count


def dispose_session_processes(workspace: Path, session_id: str) -> int:
    key = Path(workspace).resolve()
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        service = _WORKSPACE_SERVICES.get(key)
    return service.kill_session(session_id) if service is not None else 0


def close_all_process_services() -> int:
    with _REGISTRY_LOCK:
        _ensure_registry_process()
        services = list(_WORKSPACE_SERVICES.values())
        _WORKSPACE_SERVICES.clear()
    count = sum(len(service.list(active_only=True)) for service in services)
    for service in services:
        service.close()
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
