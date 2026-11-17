"""宿主私有的批次批准、精确命令授权和父方命令准入。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.runtime.process.workdir import current_workdir, scoped_workdir
from nz_coder.state.sessions import active_session_id
from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError
from nz_coder.tools import ToolCancellationScope, current_tool_cancel_event, scoped_tool_cancellation


_BASH_DEFAULTS = {"command": "", "read_only": False, "timeout": None, "workdir": None}
_PROCESS_DEFAULTS = {
    "operation": "", "command": None, "process_id": None, "data": None,
    "cursor": None, "tail_bytes": None, "max_bytes": None, "wait_seconds": 0.0,
    "workdir": None, "tty": True, "rows": 24, "cols": 80, "append_newline": False,
}


def _action_identity(tool: str, arguments: dict, *, workspace: Path | None = None, session_id: str | None = None) -> str:
    """缺省值与实际 cwd 统一；目录身份变化会使先前批准失效。"""
    from nz_coder.runtime.process.command_broker import command_directory_identity
    from nz_coder.tools import get_dynamic_tool_binding_identity

    workspace = Path(workspace or current_workdir()).resolve()
    if session_id is None:
        if tool == "process":
            from nz_coder.tools.process import _owner
            session_id = _owner()[0]
        else:
            session_id = active_session_id() or "direct-tool"
    args = dict(arguments)
    action = {
        "tool": tool, "workspace": str(workspace),
        "workspace_identity": command_directory_identity(workspace),
        "session": session_id,
        "binding": get_dynamic_tool_binding_identity(tool),
    }
    if tool in {"bash", "process"}:
        from nz_coder.tools.bash import _resolve_bash_workdir, normalize_bash_command

        defaults = _BASH_DEFAULTS if tool == "bash" else _PROCESS_DEFAULTS
        args = {key: arguments.get(key, default) for key, default in defaults.items()}
        cwd, error = _resolve_bash_workdir(args.get("workdir"), workspace=workspace)
        if error:
            raise ValueError(error)
        assert cwd is not None
        args["workdir"] = str(cwd)
        action["cwd_identity"] = command_directory_identity(cwd)
        if tool == "bash":
            args["timeout"] = current_run_settings().bash_timeout if args["timeout"] is None else args["timeout"]
            action["effective_command"] = normalize_bash_command(str(args["command"]))
        else:
            args["operation"] = str(args["operation"] or "").strip().lower()
            action["effective_command"] = str(args["command"] or "").strip()
    action["arguments"] = args
    return json.dumps(action, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


_COMMAND_AUTHORIZATION: ContextVar[str | None] = ContextVar("command_authorization", default=None)


@contextmanager
def scoped_command_authorization(tool: str, arguments: dict, *, expected_identity: str | None = None):
    """仅由已完成准入的宿主调用，不接受模型或 socket 中的批准声明。"""
    token = _COMMAND_AUTHORIZATION.set(expected_identity or _action_identity(tool, arguments))
    try:
        yield
    finally:
        _COMMAND_AUTHORIZATION.reset(token)


def command_is_authorized(tool: str, arguments: dict, *, workspace: Path | None = None, session_id: str | None = None) -> bool:
    approved = _COMMAND_AUTHORIZATION.get()
    if approved is None:
        return False
    try:
        return approved == _action_identity(tool, arguments, workspace=workspace, session_id=session_id)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False


def command_authorization_present() -> bool:
    return _COMMAND_AUTHORIZATION.get() is not None


def command_authorization_identity(tool: str, arguments: dict) -> str:
    return _action_identity(tool, arguments)


@dataclass
class _PermissionApproval:
    nonce: object
    index: int
    owner: object
    action: str
    source: str
    consumed: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_COLLECTING: ContextVar[tuple | None] = ContextVar("command_approval_collector", default=None)
_EXECUTING: ContextVar[_PermissionApproval | None] = ContextVar("command_approval_execution", default=None)


class CommandApprovalBatch:
    """批准只归当前批次所有，未分发的批次不向长期执行器留下状态。"""

    def __init__(self) -> None:
        self._nonce = object()
        self._records: dict[int, _PermissionApproval] = {}
        self._dispatched: set[int] = set()
        self._closed = False
        self._lock = threading.Lock()

    @contextmanager
    def collecting(self, index: int):
        token = _COLLECTING.set((self, self._nonce, index))
        try:
            yield
        finally:
            _COLLECTING.reset(token)

    @contextmanager
    def executing(self, index: int, call: dict):
        with self._lock:
            # 首次没有批准记录仍可走正常权限检查；关闭或重复分发必须拒绝。
            reentered = self._closed or index in self._dispatched
            self._dispatched.add(index)
            record = self._records.pop(index, None)
            if reentered:
                record = _PermissionApproval(self._nonce, index, None, "invalidated", "")
        function = call.get("function", {})
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                arguments = None
        try:
            valid = (record is not None and record.nonce is self._nonce
                     and record.index == index and isinstance(arguments, dict)
                     and record.action == _action_identity(function.get("name", ""), arguments))
        except (OSError, ValueError, TypeError, RuntimeError):
            valid = False
        if record is not None and not valid:
            # 不能把失效批准降为“未审批”，再借 auto 默认允许运行新动作。
            record.action = "invalidated"
        token = _EXECUTING.set(record)
        try:
            yield
        finally:
            _EXECUTING.reset(token)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._records.clear()


def record_permission_approval(owner: object, tool: str, arguments: dict, *, source: str, expected_identity: str | None = None) -> None:
    collecting = _COLLECTING.get()
    if collecting is None:
        return
    batch, nonce, index = collecting
    try:
        action = expected_identity or _action_identity(tool, arguments)
    except (OSError, ValueError, TypeError, RuntimeError):
        return
    with batch._lock:
        if not batch._closed and index not in batch._dispatched and nonce is batch._nonce:
            batch._records[index] = _PermissionApproval(nonce, index, owner, action, source)


def consume_permission_approval(owner: object, tool: str, arguments: dict) -> bool:
    record = _EXECUTING.get()
    if record is None:
        return False
    try:
        action = _action_identity(tool, arguments)
    except (OSError, ValueError, TypeError, RuntimeError):
        return False
    # ContextVar 可被复制到并发线程；目录解析后仍需原子核对并消费同一记录。
    with record.lock:
        if record.owner is not owner or record.consumed or record.action != action:
            return False
        record.consumed = True
        return True


def permission_approval_invalidated() -> bool:
    record = _EXECUTING.get()
    return record is not None and record.action == "invalidated"


def permission_approval_present() -> bool:
    return _EXECUTING.get() is not None


def permission_approval_was_human() -> bool:
    record = _EXECUTING.get()
    return record is not None and record.consumed and record.source == "human"


def authorization_failure(message: str, *, source: str = "policy", reason: str = "policy"):
    from nz_coder.tools import ToolOutput
    return ToolOutput(message, metadata={
        "command_not_executed": True, "permission_denied": True,
        "permission_source": source, "permission_reason": reason,
    })


class _Deadline:
    def __init__(self, deadline: float | None):
        self.deadline = deadline

    def is_set(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline


class CommandAuthorizer:
    """父宿主拥有权限副本和交互桥，子端只能提交待批准的动作。"""

    def __init__(self, permissions) -> None:
        self.permissions = permissions

    def fork(self, workspace: Path) -> CommandAuthorizer:
        return CommandAuthorizer(self.permissions.fork_for_commands(workspace))

    def authorize(
        self, tool: str, arguments: dict, *, workspace: Path, read_only: bool = False,
        cancel_event=None, deadline: float | None = None,
    ) -> str | None:
        from nz_coder.tool_platform.policies.command_policy import assess_shell_command
        from nz_coder.tools.bash import _resolve_bash_workdir, normalize_bash_command

        parent_cancel = current_tool_cancel_event()
        def interrupted():
            if ((cancel_event is not None and cancel_event.is_set())
                    or (parent_cancel is not None and parent_cancel.is_set())):
                return authorization_failure("Error: Command cancelled", source="interaction_failure", reason="cancelled")
            if deadline is not None and time.monotonic() >= deadline:
                return authorization_failure("Error: Command deadline exceeded", source="interaction_failure", reason="timed_out")
            return None

        cancellation = ToolCancellationScope(cancel_event, parent_cancel, _Deadline(deadline))
        with scoped_workdir(workspace), scoped_tool_cancellation(cancellation):
            error = interrupted()
            if error:
                return error
            cwd, error = _resolve_bash_workdir(arguments.get("workdir"), workspace=workspace)
            if error:
                return error
            assert cwd is not None
            expected = _action_identity(tool, arguments)
            starts = tool == "bash" or (tool == "process" and str(arguments.get("operation") or "").strip().lower() == "start")
            if starts:
                command = str(arguments.get("command") or "")
                if tool == "bash":
                    command = normalize_bash_command(command)
                assessment = assess_shell_command(command, workspace, command_cwd=cwd,
                    read_only=read_only, allow_package_installs=current_run_settings().allow_package_installs)
                if assessment.rejection:
                    return authorization_failure(f"Denied: {assessment.rejection}")
            decision = self.permissions.check(tool, arguments)
            if decision["behavior"] == "deny":
                return authorization_failure(f"Denied: {decision['reason']}")
            if decision["behavior"] == "ask":
                if self.permissions._asker is None:
                    return authorization_failure(str(PermissionInteractionError("unavailable")),
                        source="interaction_failure", reason="unavailable")
                try:
                    allowed = self.permissions.ask_user(tool, arguments)
                    error = interrupted()
                    if error:
                        return error
                    if not allowed:
                        return authorization_failure("Permission approval rejected; this action was not executed.", source="approval_reply", reason="rejected")
                except PermissionInteractionError as exc:
                    return interrupted() or authorization_failure(str(exc), source="interaction_failure", reason=exc.reason)
            # 等待期间可能取消、到期或切换目录；批准不延长资源寿命。
            error = interrupted()
            if error:
                return error
            try:
                changed = expected != _action_identity(tool, arguments)
            except (OSError, ValueError, TypeError, RuntimeError):
                changed = True
            if changed:
                return authorization_failure("Denied: Command execution identity changed during approval")
            return None
