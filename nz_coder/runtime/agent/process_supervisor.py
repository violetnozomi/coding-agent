"""Parent-owned supervision of one explicitly isolated child Agent process."""
from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from dataclasses import dataclass
import multiprocessing
from multiprocessing.process import BaseProcess
import threading
import time
from typing import Callable, Literal

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError, to_public_error
from nz_coder.runtime.process.command_channel import ChildCommandClient, ChildCommandEndpoint, scoped_command_client
from nz_coder.runtime.agent.child_process_result import (
    CHILD_PROCESS_RESULT_SCHEMA,
    ChildProcessResultFile,
    MAX_RESULT_CHARACTERS,
    decode_child_process_result,
    read_child_process_result,
    write_child_process_result,
)


def _run_subagent_process(
    result_path: ChildProcessResultFile, payload: dict, command_endpoint: ChildCommandEndpoint,
) -> None:
    """Actual commands stay in the parent; A1 still owns the result envelope."""
    client = None
    try:
        try:
            from nz_coder.runtime.agent.subagent import (
                run_subagent, scoped_parent_context, scoped_prepared_subagent_workspace,
            )
            from nz_coder.runtime.process.workdir import scoped_workdir
            from nz_coder.runtime.core.run_settings import scoped_run_settings
            from nz_coder.foundation.workspace_trust import scoped_config_snapshot
            from nz_coder.state.memory_scope import MemoryScope, scoped_memory_scope

            from nz_coder.runtime.agent.prepared_evidence import bind_diff_lookup
            prepared = payload["prepared_workspace"]
            bind_diff_lookup(
                prepared.diff_evidence, workspace=Path(payload["workspace"]),
                parent_session_id=payload["parent_session_id"], child_session_id=payload["session_id"],
                settlement_id=payload["settlement_id"], refs=payload.get("evidence_refs") or [],
            )
            snapshot = payload.get("config_snapshot")
            memory_scope = MemoryScope.from_dict(payload["memory_scope"])
            client = ChildCommandClient(
                command_endpoint, limits=payload["command_limits"],
                deadline=payload["command_deadline"],
            )
            with (
                scoped_workdir(payload["workspace"]),
                scoped_memory_scope(memory_scope),
                scoped_config_snapshot(snapshot) if snapshot is not None else nullcontext(),
                scoped_run_settings(payload["command_settings"]),
                scoped_prepared_subagent_workspace(payload["prepared_workspace"]),
                scoped_command_client(client),
                scoped_parent_context(session_id=payload["parent_session_id"],
                                      model_id=payload["parent_model_id"], config_snapshot=snapshot,
                                      memory_scope=memory_scope.to_dict()),
            ):
                result = run_subagent(
                    payload["prompt"], agent_type=payload["agent_type"],
                    session_id=payload["session_id"], allowed_tools=payload.get("allowed_tools"),
                    target_paths=payload.get("target_paths"), output_schema=payload.get("output_schema"),
                    model_hint=payload.get("model_hint"), evidence_refs=payload.get("evidence_refs"),
                    verification=payload.get("verification"), cancel_event=threading.Event(),
                )
            envelope = {"schema": CHILD_PROCESS_RESULT_SCHEMA, "ok": True, "result": str(result)[:MAX_RESULT_CHARACTERS]}
            metadata = getattr(result, "metadata", {})
            if payload.get("settlement_id") and isinstance(metadata, dict) and "child_result" in metadata:
                envelope["child_result"] = metadata["child_result"]
                envelope["child_result_settlement_id"] = metadata.get("child_result_settlement_id")
        except BaseException as exc:
            envelope = {"schema": CHILD_PROCESS_RESULT_SCHEMA, "ok": False, "public_error": to_public_error(exc).to_dict()}
        write_child_process_result(result_path, envelope)
        if client is not None:
            client.finish(deadline=payload["command_deadline"])
    finally:
        if client is not None:
            # 不能清空等待表冒充退出；父方仍能按自己的期限终止本体。
            while not client.close(deadline=time.monotonic() + 0.05):
                pass
        else:
            command_endpoint.close()


@dataclass(frozen=True)
class ProcessOutcome:
    """Agent-body exit facts; business completion remains the manager's decision."""

    result: str
    public_error: PublicError | None
    reason: Literal["exit", "cancelled", "timeout", "error"]
    exit_code: int
    canonical: dict | None = None
    canonical_settlement_id: str = ""


class SubagentProcessSupervisor:
    """Own the exact child handle; only wait performs termination and result I/O."""

    def __init__(
        self, payload: dict, result_path: ChildProcessResultFile, *,
        command_endpoint: ChildCommandEndpoint, on_stop: Callable[[], None],
    ) -> None:
        self._payload = dict(payload)
        self._result_path = result_path
        self._command_endpoint = command_endpoint
        self._on_stop = on_stop
        self._stop_notified = False
        self._failure: PublicError | None = None
        self._failure_at: float | None = None
        self._internal_stop_at: float | None = None
        self._wake = threading.Event()
        self._process: BaseProcess | None = None
        self._lock = threading.Lock()
        self._wait_lock = threading.Lock()
        self._stop = threading.Event()
        self._exited = threading.Event()
        self._wait_finished = threading.Event()
        self._outcome: ProcessOutcome | None = None
        self._handle_closed = False

    @property
    def process(self) -> BaseProcess | None:
        return self._process

    @property
    def exited(self) -> bool:
        return self._exited.is_set()

    def fail(self, error: PublicError) -> None:
        """只登记一次infra原因；调用线程不操作本体，不伪造用户取消。"""
        with self._lock:
            if self._failure is None and not self._exited.is_set():
                self._failure = error
                self._failure_at = time.monotonic()
                self._wake.set()

    def _request_internal_stop(self) -> None:
        with self._lock:
            if self._internal_stop_at is None:
                self._internal_stop_at = time.monotonic()
            self._stop.set()
            self._wake.set()

    def _notify_stop(self) -> None:
        with self._lock:
            if self._stop_notified:
                return
            self._stop_notified = True
        self._on_stop()

    def start(self) -> None:
        process = None
        try:
            with self._lock:
                if self._process is not None or self._stop.is_set():
                    raise RuntimeError("Child process supervisor cannot be started again")
                if self._failure is not None:
                    raise PublicRuntimeError(self._failure)
                process = multiprocessing.get_context("spawn").Process(
                    target=_run_subagent_process,
                    args=(self._result_path, self._payload, self._command_endpoint),
                    name=f"nz-subagent-process-{self._payload.get('session_id', 'child')}",
                    daemon=True,
                )
                self._process = process
            process.start()
        except BaseException:
            self._request_internal_stop()
            self._notify_stop()
            if process is not None and process.pid is not None:
                self.wait(threading.Event(), deadline=time.monotonic(), stop_grace=0.1)
            else:
                self._exited.set()
                self._wait_finished.set()
            self.close(deadline=time.monotonic())
            raise
        finally:
            # 传递的是同一端点的spawn副本；父进程副本必须在所有分支退役。
            self._command_endpoint.close()

    def _stop_reason(self, cancel_event: threading.Event, deadline: float):
        if cancel_event.is_set():
            return "cancelled"
        now = time.monotonic()
        with self._lock:
            causes = [(deadline, "timeout")] if now >= deadline else []
            if self._failure_at is not None:
                causes.append((self._failure_at, "error"))
            if self._internal_stop_at is not None:
                causes.append((self._internal_stop_at, "cancelled"))
        return min(causes, key=lambda item: item[0])[1] if causes else None

    @staticmethod
    def _signal(process: BaseProcess, *, force: bool) -> None:
        try:
            if force:
                process.kill()
            else:
                process.terminate()
        except OSError:
            # OS 拒绝终止不等于退出；保持句柄并在监督循环中继续核查。
            pass

    @staticmethod
    def _has_exited(process: BaseProcess) -> bool:
        return process.exitcode is not None

    def _stop_until_exited(self, process: BaseProcess, grace: float) -> None:
        if self._has_exited(process):
            return
        self._signal(process, force=False)
        process.join(timeout=grace)
        if not self._has_exited(process):
            self._signal(process, force=True)
            process.join(timeout=grace)
        while not self._has_exited(process):
            self._signal(process, force=True)
            process.join(timeout=0)
            self._exited.wait(0.05)

    def wait(self, cancel_event: threading.Event, *, deadline: float, stop_grace: float) -> ProcessOutcome:
        with self._lock:
            if self._outcome is not None:
                return self._outcome
        if not self._wait_lock.acquire(blocking=False):
            raise RuntimeError("Child process already has a supervisor waiter")
        try:
            process = self._process
            if process is None or process.pid is None:
                raise RuntimeError("Child process has not started")
            reason = self._stop_reason(cancel_event, deadline)
            while reason is None and not self._has_exited(process):
                self._wake.wait(min(0.05, max(0.0, deadline - time.monotonic())))
                reason = self._stop_reason(cancel_event, deadline)
            if reason is not None:
                self._notify_stop()
                self._stop_until_exited(process, max(0.0, stop_grace))
            process.join(timeout=0)
            exit_code = process.exitcode
            assert exit_code is not None
            self._exited.set()
            self._notify_stop()
            outcome = self._read_outcome(exit_code)
            if reason is None:
                # 读取/校验也占用同一期限，晚到成功不能覆盖已发生的取消或超时。
                reason = self._stop_reason(cancel_event, deadline)
            if reason is not None:
                public_error = self._failure if reason == "error" else None
                content = public_error.message if public_error is not None else (
                    "Cancelled; isolated child process terminated." if reason == "cancelled"
                    else "Timed out; isolated child process terminated."
                )
                outcome = ProcessOutcome(content, public_error, reason, exit_code, outcome.canonical, outcome.canonical_settlement_id)
            with self._lock:
                self._outcome = outcome
            return outcome
        finally:
            self._wait_finished.set()
            self._wait_lock.release()

    def _read_outcome(self, exit_code: int) -> ProcessOutcome:
        canonical = None
        identity = ""
        try:
            envelope = read_child_process_result(self._result_path)
            candidate = envelope.get("child_result")
            if candidate is not None:
                identity = str(envelope["child_result_settlement_id"])
                if (
                    identity != self._payload.get("settlement_id")
                    or candidate.get("task_id") != self._payload.get("session_id")
                    or candidate.get("session_id") != self._payload.get("session_id")
                    or candidate.get("parent_session_id") != self._payload.get("parent_session_id")
                ):
                    raise ValueError("Canonical child result belongs to a different invocation")
                canonical = candidate
        except (OSError, ValueError):
            envelope = None
            canonical = None
            identity = ""
        if exit_code != 0:
            error = PublicError("child_process_exit", "The isolated child process exited unexpectedly.", metadata={"exit_code": exit_code})
            return ProcessOutcome(error.message, error, "error", exit_code, canonical, identity)
        content, error = decode_child_process_result(envelope)
        return ProcessOutcome(content, error, "error" if error else "exit", exit_code, canonical, identity)

    def close(self, *, deadline: float) -> bool:
        self._request_internal_stop()
        self._notify_stop()
        with self._lock:
            if self._process is None:
                self._exited.set()
                self._wait_finished.set()
        exited = self._exited.wait(max(0.0, deadline - time.monotonic()))
        if exited and self._wait_finished.is_set():
            with self._lock:
                if self._process is not None and not self._handle_closed:
                    self._process.close()
                    self._handle_closed = True
        return exited
