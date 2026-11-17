"""Parent-owned command authority, per-invocation resources and admission."""
from __future__ import annotations

from contextlib import nullcontext
from collections import OrderedDict, deque
from contextvars import Context
from dataclasses import dataclass, field, replace
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable

from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
from nz_coder.runtime.process.auxiliary import AuxiliaryCommandRunner
from nz_coder.foundation.capability_lease import capability_leases
from nz_coder.foundation.workspace_trust import ConfigSnapshot, scoped_config_snapshot
from nz_coder.protocol.public_error import PublicError, PublicInputError, PublicRuntimeError, format_public_error, to_public_error
from nz_coder.protocol.session_events import scoped_session_event_bus
from nz_coder.runtime.core.execution_context import (
    CommandExecutionPolicy, CommandPolicyRestriction, scoped_runtime_overrides, tighten_command_policy,
)
from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
from nz_coder.runtime.process.bash_execution import BashExecution
from nz_coder.runtime.process.command_channel import (
    ChildCommandEndpoint, CommandChannelLimits, CommandExecutionFact, ParentCommandPump,
)
from nz_coder.runtime.process.process_service import ProcessService, ProcessStateError
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import scoped_session


def command_directory_identity(path: Path) -> tuple[int, int]:
    """复用既有可信目录句柄身份，不从子方声明中取得。"""
    if os.name == "nt":
        from nz_coder.foundation.project_control import _windows_open, _windows_close, _windows_handle_info

        handle = _windows_open(path, directory=True)
        try:
            return _windows_handle_info(handle)
        finally:
            _windows_close(handle)
    handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(handle)
        return info.st_dev, info.st_ino
    finally:
        os.close(handle)


@dataclass(frozen=True)
class CommandAuthority:
    workspace: Path
    execution_root: Path
    execution_identity: tuple[int, int]
    parent_session_id: str
    child_session_id: str
    settlement_id: str
    agent_type: str
    read_only: bool
    allowed_tools: frozenset[str]
    config_snapshot: ConfigSnapshot | None
    run_settings: RunSettings
    execution_policy: CommandExecutionPolicy
    environment: object | None = None


@dataclass
class _CommandRequest:
    request_id: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    launch_done: threading.Event = field(default_factory=threading.Event)
    submitted: bool = False
    worker: threading.Thread | None = None
    auxiliary: AuxiliaryCommandRunner | None = None
    execution: BashExecution | None = None
    lease_id: str = ""
    reply_ready: bool = False
    reply_sent: bool = False
    reply: str | PublicError | None = None
    progress: tuple[str, dict] | None = None


_BASH_ARGUMENTS = frozenset({"command", "timeout", "workdir", "read_only"})
_PROCESS_ARGUMENTS = frozenset({
    "operation", "command", "process_id", "data", "cursor", "tail_bytes", "max_bytes",
    "wait_seconds", "workdir", "tty", "rows", "cols", "append_newline",
})


_RETRIEVAL_ARGUMENTS = {
    'grep_search': {'pattern': str, 'path': str, 'include': str, 'output_mode': str,
                    'head_limit': int, 'offset': int, 'context': int, 'case_insensitive': bool},
    'glob_search': {'pattern': str, 'path': str},
    'diff_status': {},
}


def _validate_retrieval(tool: str, arguments: dict) -> None:
    fields = _RETRIEVAL_ARGUMENTS[tool]
    if set(arguments) - fields.keys():
        raise PermissionError('unrecognized retrieval arguments')
    if tool != 'diff_status' and 'pattern' not in arguments:
        raise ValueError('pattern is required')
    for key, value in arguments.items():
        if value is None and tool == 'grep_search' and key in {'include', 'head_limit'}:
            continue
        if type(value) is not fields[key]:
            raise ValueError('invalid retrieval argument type')
    if tool == 'grep_search' and arguments.get('output_mode', 'content') not in {'content', 'files_with_matches', 'count'}:
        raise ValueError('invalid output mode')


class ParentCommandScope:
    """Own one invocation without registering its service in the workspace map."""

    def __init__(
        self, authority: CommandAuthority, *, deadline: float, child_publisher: object | None,
        on_channel_error: Callable[[PublicError], None],
        command_authorizer=None,
    ) -> None:
        self.authority = authority
        self._command_authorizer = command_authorizer
        self._deadline = deadline
        self._environment = authority.environment.as_dict() if authority.environment else build_sanitized_subprocess_env(profile="workspace-command")
        self._publisher = child_publisher
        self._on_channel_error = on_channel_error
        settings = authority.run_settings
        self._service = ProcessService(
            authority.execution_root, buffer_bytes=settings.process_buffer_bytes,
            max_processes=settings.process_max_per_workspace,
            kill_grace_seconds=settings.process_kill_grace,
        )
        self._lock = threading.RLock()
        # 只安装authority声明的上下文，不能继承父会话的事务或tracker。
        self._context = Context()
        self._requests: dict[int, _CommandRequest] = {}
        self._high_water = 0
        self._policy: CommandExecutionPolicy | None = None
        self._draining = False
        self._finished = False
        self._connection_closed = False
        self._channel_error: PublicError | None = None
        self._pump: ParentCommandPump | None = None
        self._endpoint: ChildCommandEndpoint | None = None
        self._server_socket: socket.socket | None = None
        self._start_requested = False
        self._start_done = threading.Event()
        self._facts: OrderedDict[int, tuple[CommandExecutionFact, str, bool]] = OrderedDict()
        self._fact_high_water = 0
        self._facts_sealed_through = 0
        self._historical_facts: list[CommandExecutionFact] = []
        self._terminal_callback = self._on_terminal
        self._terminal_unsubscribe = None
        self._retirement_condition = threading.Condition(self._lock)
        self._retirements = deque()
        self._retirement_stopping = False
        self._retirement_thread: threading.Thread | None = None
        self._retirement_launch_done = threading.Event()
        self._retirement_launch_done.set()

    def bind_pump(self, pump) -> None:
        with self._lock:
            if self._pump is not None and self._pump is not pump:
                raise ValueError("command scope already has a pump")
            self._pump = pump
            self._retirement_launch_done.clear()
        try:
            worker = threading.Thread(target=self._notify_retirements,
                name="nz-command-retirement-notifier", daemon=True)
            with self._lock:
                self._retirement_thread = worker
            worker.start()
        finally:
            self._retirement_launch_done.set()

    def _notify_retirements(self) -> None:
        while True:
            with self._retirement_condition:
                self._retirement_condition.wait_for(lambda: self._retirements or self._retirement_stopping)
                if not self._retirements:
                    return
                record = self._retirements.popleft()
            record.launch_done.wait()
            if record.worker is not None and record.worker.ident is not None:
                record.worker.join()
            self._pump.wake()

    def _on_terminal(self, terminal):
        from nz_coder.runtime.adapters.verification_facts import command_receipt

        handle = terminal.handle
        if handle.owner_session_id != self.authority.child_session_id:
            raise ValueError("process fact owner mismatch")
        receipt = command_receipt("process", handle.command, handle.cwd,
            outcome=("cancelled" if handle.status in {"cancelled", "killed"} else
                     "failed" if handle.status == "failed" or handle.exit_code != 0 else "succeeded"),
            exit_code=handle.exit_code, output=terminal.output, output_complete=terminal.output_complete,
            pipefail=terminal.pipefail)
        receipt = replace(receipt, execution_id=f"process:{self.authority.settlement_id}:{handle.process_id}")
        fact = CommandExecutionFact(self.authority.settlement_id, int(terminal.launch_id), receipt)
        with self._lock:
            if fact.launch_request_id <= self._facts_sealed_through or self._draining or self._pump is None:
                self._historical_facts.append(fact)
                return True
            self._fact_high_water += 1
            self._facts[self._fact_high_water] = (fact, handle.process_id, False)
        self._pump.wake()
        return False

    def peek_fact(self):
        with self._lock:
            return next(((seq, item[0]) for seq, item in self._facts.items() if not item[2]), None)

    def fact_sent(self, seq: int) -> None:
        with self._lock:
            if seq in self._facts:
                fact, process_id, _sent = self._facts[seq]
                self._facts[seq] = (fact, process_id, True)

    def acknowledge_fact(self, seq: int) -> None:
        with self._lock:
            if not 0 < seq <= self._fact_high_water:
                raise ValueError("unknown execution fact acknowledgement")
            pending = self._facts.pop(seq, None)
        if pending is not None:
            self._service.acknowledge_terminal(pending[1], self._terminal_callback)

    def fact_barrier_watermark(self) -> int | None:
        if self._service.terminal_delivery_pending():
            return None
        with self._lock:
            return self._fact_high_water

    def fact_watermark_acknowledged(self, watermark: int) -> bool:
        with self._lock:
            return not any(seq <= watermark for seq in self._facts)

    def seal_facts(self, watermark: int) -> None:
        with self._lock:
            if watermark > self._fact_high_water or any(seq <= watermark for seq in self._facts):
                raise ValueError("cannot seal unacknowledged execution facts")
        self._retire_fact_receiver()

    def _retire_fact_receiver(self) -> None:
        """接收端退休后由父方历史接管事实；不再等待已退出子进程回复。"""
        with self._lock:
            # 密封旧启动，不封死同一子任务后置修复轮的新启动。
            self._facts_sealed_through = self._high_water
            pending = tuple(self._facts.values())
            self._historical_facts.extend(item[0] for item in pending)
            self._facts.clear()
        for _fact, process_id, _sent in pending:
            self._service.acknowledge_terminal(process_id, self._terminal_callback)

    def start(self) -> ChildCommandEndpoint:
        with self._lock:
            if self._start_requested or self._draining:
                raise ValueError("command scope already started or draining")
            self._start_requested = True
        try:
            server, child = socket.socketpair()
            with self._lock:
                self._server_socket = server
                self._endpoint = ChildCommandEndpoint(child, self.authority.settlement_id)
            pump = ParentCommandPump(
                server, handler=self,
                limits=CommandChannelLimits.from_settings(self.authority.run_settings),
            )
            with self._lock:
                self._pump = pump
            pump.start()
            if self._draining:
                pump.request_stop()
            return self._endpoint
        except BaseException:
            self.request_stop()
            if self._endpoint is not None:
                self._endpoint.close()
            if self._pump is None and self._server_socket is not None:
                self._server_socket.close()
            raise
        finally:
            self._start_done.set()

    def policy_ready(self, fingerprint: str) -> None:
        with self._lock:
            if self._policy is not None:
                raise ValueError("command policy is already initialized")
            if self._draining or fingerprint != self.authority.execution_policy.fingerprint:
                self._draining = True
                raise ValueError("command policy initialization mismatch")
            self._policy = self.authority.execution_policy

    def _prune_locked(self) -> None:
        for request_id, record in list(self._requests.items()):
            if record.submitted and not record.launch_done.is_set():
                continue
            if record.worker is not None and record.worker.is_alive():
                continue
            if record.execution is not None and not record.execution.close(deadline=time.monotonic()):
                continue
            if record.auxiliary is not None and not record.auxiliary.retired:
                continue
            if record.lease_id:
                capability_leases().release(record.lease_id)
                record.lease_id = ""
            if record.reply_sent or self._connection_closed:
                del self._requests[request_id]

    def begin_request(self, request_id: int) -> None:
        with self._lock:
            if self._policy is None:
                raise ValueError("command policy is not initialized")
            if self._draining:
                raise ValueError("command scope is draining")
            if type(request_id) is not int or request_id <= self._high_water:
                raise ValueError("command request id must strictly increase")
            self._prune_locked()
            if len(self._requests) >= self.authority.run_settings.max_parallel_tasks:
                raise ValueError("command request limit reached")
            self._high_water = request_id
            self._requests[request_id] = _CommandRequest(request_id)

    def submit_request(
        self, request_id: int, tool: str, arguments: dict,
        restriction: CommandPolicyRestriction,
    ) -> None:
        with self._lock:
            record = self._requests.get(request_id)
            if record is None or record.submitted:
                raise ValueError("command request is not reserved")
            if self._draining:
                raise ValueError("command scope is draining")
            assert self._policy is not None
            self._policy = policy = tighten_command_policy(self._policy, restriction)
            authority = self.authority
            if tool not in {"bash", "process", *_RETRIEVAL_ARGUMENTS} or tool not in authority.allowed_tools:
                raise PermissionError("tool is not available to this child")
            if tool == "process" and authority.read_only:
                raise PermissionError("process is not available to a read-only child")
            if time.monotonic() >= self._deadline:
                raise ValueError("command deadline exceeded")
            if not isinstance(arguments, dict):
                raise PermissionError("unrecognized command arguments")
            if tool in _RETRIEVAL_ARGUMENTS:
                _validate_retrieval(tool, arguments)
                record.auxiliary = AuxiliaryCommandRunner(
                    authority, request_id=request_id, cancel_event=record.cancel_event,
                    deadline=self._deadline, environment=self._environment,
                )
            else:
                allowed = _BASH_ARGUMENTS if tool == "bash" else _PROCESS_ARGUMENTS
                if set(arguments) - allowed:
                    raise PermissionError("unrecognized command arguments")
            record.submitted = True
        # 构造/start自身也可能阻塞或部分成功；launch_done之前不能退役记录。
        try:
            worker = threading.Thread(
                target=self._context.copy().run,
                args=(self._execute, record, tool, dict(arguments), policy),
                name=f"nz-command-{request_id}", daemon=True,
            )
            with self._lock:
                record.worker = worker
            worker.start()
        except BaseException as exc:
            self._set_reply(record, format_public_error(exc))
        finally:
            record.launch_done.set()

    def _set_reply(self, record: _CommandRequest, reply: str | PublicError) -> None:
        with self._lock:
            if not record.reply_ready:
                record.reply_ready = True
                record.reply = reply
                if self._pump is not None:
                    self._retirements.append(record)
                    self._retirement_condition.notify_all()

    def _register_bash(self, record: _CommandRequest, *args, **kwargs) -> BashExecution:
        authority = self.authority
        def revoke() -> None:
            record.cancel_event.set()
            if record.execution is not None:
                record.execution.request_stop()
                record.execution.close(deadline=time.monotonic() + authority.run_settings.process_kill_grace)
            if record.worker is not None and record.worker.is_alive():
                raise ProcessStateError("bash command resources are still pending")
        snapshot = authority.config_snapshot
        lease = capability_leases().create(
            kind="bash-command", resource_id=f"{authority.settlement_id}:{record.request_id}",
            workspace=authority.execution_root,
            control_fingerprint=snapshot.control_fingerprint if snapshot else "legacy",
            run_id=authority.child_session_id,
            interaction_id=getattr(self._publisher, "interaction_run_id", authority.child_session_id),
            owner_session=authority.child_session_id, revoke=revoke,
        )
        with self._lock:
            record.lease_id = lease.lease_id
        execution = BashExecution(*args, **kwargs)
        with self._lock:
            record.execution = execution
            if self._draining or record.cancel_event.is_set():
                execution.request_stop()
        return execution

    def _execute(
        self, record: _CommandRequest, tool: str, arguments: dict,
        policy: CommandExecutionPolicy,
    ) -> None:
        from nz_coder.tools import scoped_tool_cancellation, scoped_tool_metadata_reporter
        from nz_coder.runtime.process.execution_environment import scoped_execution_environment
        from nz_coder.tools.bash import execute_bash_local
        from nz_coder.tools.process import execute_process_local
        from nz_coder.tools.search import execute_grep_search_local, execute_glob_search_local
        from nz_coder.tools.repo_intel import execute_diff_status_local
        from nz_coder.tool_platform.permissioning.command_authorization import (
            authorization_failure, command_authorization_identity, scoped_command_authorization,
        )

        authority = self.authority
        def progress(title: str, metadata: dict) -> None:
            item = dict(metadata)
            if "output" in item:
                item["output"] = str(item["output"])[:4000]
            with self._lock:
                if not record.reply_sent:
                    record.progress = (title, item)
            if self._pump is not None:
                self._pump.wake()
        try:
            with (
                scoped_execution_environment(authority.environment),
                scoped_workdir(authority.execution_root),
                scoped_run_settings(authority.run_settings),
                scoped_config_snapshot(authority.config_snapshot) if authority.config_snapshot is not None else nullcontext(),
                scoped_runtime_overrides(strict_local_tools=policy.strict_local_tools),
                scoped_session(authority.child_session_id),
                scoped_session_event_bus(self._publisher),
                scoped_tool_cancellation(record.cancel_event),
                scoped_tool_metadata_reporter(progress),
            ):
                if command_directory_identity(authority.execution_root) != authority.execution_identity:
                    raise PermissionError("command execution directory identity changed")
                admission_error = None
                authorization = nullcontext()
                if tool in {"bash", "process"} and self._command_authorizer is not None:
                    checked_arguments = dict(arguments)
                    if tool == "bash":
                        checked_arguments["read_only"] = authority.read_only
                    expected = command_authorization_identity(tool, checked_arguments)
                    admission_error = self._command_authorizer.authorize(tool, checked_arguments,
                        workspace=authority.execution_root, read_only=authority.read_only,
                        cancel_event=record.cancel_event, deadline=self._deadline)
                    if admission_error is None:
                        authorization = scoped_command_authorization(tool, checked_arguments, expected_identity=expected)
                elif tool == "process" and str(arguments.get("operation") or "").strip().lower() == "write":
                    admission_error = authorization_failure("Permission approval interface is not available.",
                        source="interaction_failure", reason="unavailable")
                if admission_error is not None:
                    result = admission_error
                elif record.cancel_event.is_set():
                    result = "Error: Command cancelled"
                elif time.monotonic() >= self._deadline:
                    result = "Error: Command deadline exceeded"
                elif command_directory_identity(authority.execution_root) != authority.execution_identity:
                    result = authorization_failure("Denied: Command execution directory identity changed")
                elif tool == "bash":
                    with authorization:
                        result = execute_bash_local(
                            arguments.get("command", ""), read_only=authority.read_only,
                            timeout=arguments.get("timeout"), workdir=arguments.get("workdir"),
                            create_execution=lambda *a, **k: self._register_bash(record, *a, **k),
                            environment=authority.environment,
                        )
                elif tool in _RETRIEVAL_ARGUMENTS:
                    handler = {'grep_search': execute_grep_search_local,
                               'glob_search': execute_glob_search_local,
                               'diff_status': execute_diff_status_local}[tool]
                    result = handler(**arguments, runner=record.auxiliary)
                else:
                    if str(arguments.get("operation") or "").lower() == "start":
                        with self._lock:
                            if self._terminal_unsubscribe is None:
                                self._terminal_unsubscribe = self._service.subscribe_terminal(
                                    self._terminal_callback,
                                    on_delivery_done=self._pump.wake if self._pump is not None else None,
                                )
                    with authorization:
                        result = execute_process_local(
                            arguments, service=self._service,
                            owner_session_id=authority.child_session_id,
                            owner_agent_id=getattr(self._publisher, "agent_id", authority.child_session_id),
                            event_bus=self._publisher,
                            launch_id=str(record.request_id),
                        )
        except (PublicRuntimeError, PublicInputError) as exc:
            result = to_public_error(exc)
        except BaseException as exc:
            result = format_public_error(exc)
        finally:
            if record.auxiliary is not None:
                record.auxiliary.seal()
        self._set_reply(record, result)

    def cancel_request(self, request_id: int) -> None:
        with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                # 回复与取消可在双向通道上交错；旧请求退役后的取消是幂等操作。
                if 0 < request_id <= self._high_water:
                    return
                raise ValueError("unknown command request id")
            record.cancel_event.set()
            if record.execution is not None:
                record.execution.request_stop()
            if record.auxiliary is not None:
                record.auxiliary.request_stop()
            if not record.submitted:
                self._set_reply(record, "Error: Command cancelled")

    def peek_reply(self) -> tuple[int, str | PublicError] | None:
        with self._lock:
            self._prune_locked()
            candidates = [record for record in self._requests.values()
                          if record.reply_ready and not record.reply_sent
                          and (not record.submitted or record.launch_done.is_set())
                          and (record.worker is None or not record.worker.is_alive())]
        for record in candidates:
            # close带停止含义，只对已完成worker的候选做锁外物理确认。
            if record.auxiliary is not None and not record.auxiliary.close(deadline=time.monotonic()):
                continue
            if record.execution is not None and not record.execution.close(deadline=time.monotonic()):
                continue
            with self._lock:
                if self._requests.get(record.request_id) is not record or record.reply_sent:
                    continue
                if record.auxiliary is not None and not record.auxiliary.retired:
                    continue
                assert record.reply is not None
                return record.request_id, record.reply
        return None

    def reply_sent(self, request_id: int) -> None:
        with self._lock:
            record = self._requests[request_id]
            record.reply_sent = True
            record.reply = None
            record.progress = None
            self._prune_locked()

    def take_progress(self) -> tuple[int, str, dict] | None:
        with self._lock:
            for record in self._requests.values():
                if record.progress is not None:
                    title, metadata = record.progress
                    record.progress = None
                    return record.request_id, title, metadata
        return None

    def finish(self) -> None:
        with self._lock:
            if any(not r.reply_sent for r in self._requests.values()):
                raise ValueError("command RPCs are still pending")
            self._finished = True
        self.request_stop()

    def connection_lost(self, error: PublicError | None) -> None:
        with self._lock:
            expected = self._finished or self._draining
            self._connection_closed = True
            if (error is not None or not expected) and self._channel_error is None:
                self._channel_error = error or PublicError(
                    "child_command_channel_error", "Child command channel closed unexpectedly.",
                )
                notify = self._channel_error
            else:
                notify = None
        self.request_stop()
        if notify is not None:
            self._on_channel_error(notify)

    def request_stop(self) -> None:
        with self._lock:
            self._draining = True
            for record in self._requests.values():
                record.cancel_event.set()
                if record.execution is not None:
                    record.execution.request_stop()
                if record.auxiliary is not None:
                    record.auxiliary.request_stop()
        self._service.request_close()

    def close(self, *, deadline: float) -> bool:
        self.request_stop()
        start_done = not self._start_requested or self._start_done.wait(max(0.0, deadline - time.monotonic()))
        if self._pump is not None:
            self._pump.request_stop()
        self._retire_fact_receiver()
        try:
            self._service.close(deadline=deadline)
            service_done = True
        except ProcessStateError:
            service_done = False
        with self._lock:
            records = list(self._requests.values())
        workers_done = True
        for record in records:
            if record.submitted:
                record.launch_done.wait(max(0.0, deadline - time.monotonic()))
                if not record.launch_done.is_set():
                    workers_done = False
            worker = record.worker
            if worker is not None and worker.ident is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
                if worker.is_alive():
                    workers_done = False
            if record.execution is not None and not record.execution.close(deadline=deadline):
                workers_done = False
            if record.auxiliary is not None and not record.auxiliary.close(deadline=deadline):
                workers_done = False
        with self._lock:
            if self._pump is None:
                self._connection_closed = True
            self._prune_locked()
        channel_done = start_done
        if self._pump is not None:
            channel_done = self._pump.close(deadline=deadline) and channel_done
        if start_done:
            try:
                if self._endpoint is not None:
                    self._endpoint.close()
                if self._pump is None and self._server_socket is not None:
                    self._server_socket.close()
            except OSError:
                channel_done = False
        with self._retirement_condition:
            self._retirement_stopping = True
            self._retirement_condition.notify_all()
        retirement_done = self._retirement_launch_done.wait(max(0.0, deadline - time.monotonic()))
        if self._retirement_thread is not None and self._retirement_thread.ident is not None:
            self._retirement_thread.join(max(0.0, deadline - time.monotonic()))
            retirement_done = not self._retirement_thread.is_alive() and retirement_done
        if service_done and self._terminal_unsubscribe is not None:
            self._terminal_unsubscribe()
        return service_done and workers_done and channel_done and retirement_done
