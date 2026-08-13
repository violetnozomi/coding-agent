"""Tool executor: parse arguments, check permissions, dispatch, classify results.

职责:
  - 检查单轮工具调用数量限制
  - 解析 JSON 参数（给出可读诊断）
  - 权限检查
  - 调用 dispatch()
  - 将结果分类（dispatch_failed / command_failed / is_write）

不负责:
  - 追加 messages（由 AgentLoop 负责）
  - 事务管理（由 AgentLoop 负责）
  - tracer 记录（由 AgentLoop 负责）
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from nz_coder import config
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.agent_resilience import (
    extract_structured_tool_error_code,
    is_cancelled_tool_result_content,
    is_tool_result_error_content,
)
from nz_coder.session_events import publish_session_event
from nz_coder.tool_platform.permissioning.interaction import format_tool_summary
from nz_coder.tools import (
    ToolOutput,
    dispatch,
    get_execution_mode,
    is_transactional_dynamic_tool,
    scoped_tool_call,
)

# 会修改文件、需要纳入事务跟踪的工具集合
WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file",
    "edit_file",
    "apply_patch",
    "replace_lines",
    "python_structural_edit",
    "write_files_batch",
    "scaffold_project",
})


def is_write_tool(name: str) -> bool:
    """Return whether a built-in or dynamically registered tool writes state."""
    return name in WRITE_TOOLS or get_execution_mode(name) == "write"


def is_transactional_write_tool(name: str) -> bool:
    """Return whether the tool writes local state covered by TransactionManager."""
    return is_write_tool(name) and is_transactional_dynamic_tool(name)


@dataclass
class ToolExecutionResult:
    """单次工具调用的执行结果。"""

    name: str
    """工具名称。"""

    tool_input: dict
    """解析后的参数字典；解析失败时为空 dict。"""

    output: str
    """工具返回的字符串输出。"""

    executed: bool
    """True 表示 dispatch() 被实际调用了（未被权限/限制提前拦截）。"""

    dispatch_failed: bool
    """True 表示输出以 'Error:' 或 'Denied' 开头。"""

    command_failed: bool
    """True 表示 bash 命令返回了非零退出码。

    注意：command_failed 与 dispatch_failed 互斥。
    bash 测试失败（command_failed=True）不应触发事务回滚——
    测试失败是修复的反馈信号，不是工具执行层面的错误。
    (Non-zero verification commands should not trigger transaction rollback.
     Failed tests are feedback for repair, not tool execution failure.)
    """

    is_write: bool
    """True 表示该工具属于文件写入类（会修改工作目录）。"""

    duration_ms: float = 0.0
    """Monotonic elapsed time spent parsing, authorizing, and executing this call."""

    queue_wait_ms: float = 0.0
    """Time spent waiting for a parallel scheduler worker, when applicable."""

    permission_denied: bool = False
    """True when policy, user choice, or a guard rejected this invocation."""

    title: str = ""
    """Tool-supplied display title, when distinct from the call summary."""

    metadata: dict = field(default_factory=dict)
    """Structured result metadata persisted beside successful output."""

    attachments: list[dict] = field(default_factory=list)
    """Validated inline files persisted and replayed to capable models."""


def _best_effort_arguments(raw_arguments) -> dict:  # noqa: ANN001
    if isinstance(raw_arguments, dict):
        return dict(raw_arguments)
    if not isinstance(raw_arguments, str):
        return {}
    try:
        payload = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _tool_category(name: str, is_write: bool) -> str:
    if is_write:
        return "edit"
    if name in {"bash", "process"}:
        return "command"
    if name in {"task", "background_task_start", "background_task_apply"}:
        return "agent"
    if get_execution_mode(name) == "read":
        return "read"
    return "state"


class ToolExecutor:
    """解析、权限检查并执行单次工具调用，返回分类结果。

    不负责 messages 追加、事务管理或 tracer 记录。
    """

    def __init__(self, permissions: PermissionManager):
        self._permissions = permissions

    def execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult:
        """执行单次工具调用，返回 ToolExecutionResult。

        Args:
            tool_call: 形如 {"function": {"name": ..., "arguments": ...}} 的字典。
            index: 本轮响应中的零起始序号，用于限制单轮调用次数。
        """
        fn_name = str(tool_call.get("function", {}).get("name") or "unknown")
        tool_input = _best_effort_arguments(
            tool_call.get("function", {}).get("arguments", {}),
        )
        is_write = is_transactional_write_tool(fn_name)
        publish_session_event(
            "session.tool.started",
            {
                "tool_call_id": tool_call.get("id"),
                "index": index,
                "name": fn_name,
                "category": _tool_category(fn_name, is_write),
                "summary": format_tool_summary(fn_name, tool_input),
                "is_write": is_write,
            },
        )
        started = time.perf_counter()
        result = self._execute_one(tool_call, index)
        result.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return result

    def _execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult:
        """Implementation split out so every return path receives timing metadata."""
        fn_name = tool_call["function"]["name"]
        fn_args_raw = tool_call["function"].get("arguments", "{}")
        is_write = is_transactional_write_tool(fn_name)

        # ── 单轮调用数限制 ──────────────────────────────────────────────────
        if index >= config.MAX_TOOL_CALLS_PER_RESPONSE:
            output = (
                f"Error: Too many tool calls in one response "
                f"(limit {config.MAX_TOOL_CALLS_PER_RESPONSE})"
            )
            return ToolExecutionResult(fn_name, {}, output, False, True, False, is_write)

        # ── JSON 解析 ──────────────────────────────────────────────────────
        try:
            tool_input = (
                json.loads(fn_args_raw)
                if isinstance(fn_args_raw, str)
                else (fn_args_raw or {})
            )
        except json.JSONDecodeError as e:
            output = (
                f"Error: Invalid JSON arguments for {fn_name}: {e}. "
                "Your tool call arguments are not valid JSON. "
                "Common causes: unescaped quotes inside strings, "
                "literal backslashes not doubled, trailing commas. "
                "Please rewrite the arguments with properly escaped JSON strings."
            )
            return ToolExecutionResult(fn_name, {}, output, False, True, False, is_write)

        # ── 权限检查 ───────────────────────────────────────────────────────
        decision = self._permissions.check(fn_name, tool_input)
        if decision["behavior"] == "deny":
            output = f"Denied: {decision['reason']}"
            return ToolExecutionResult(
                fn_name, tool_input, output, False, True, False, is_write,
                permission_denied=True,
            )
        if decision["behavior"] == "ask":
            if not self._permissions.ask_user(fn_name, tool_input):
                return ToolExecutionResult(
                    fn_name, tool_input, "Denied by user", False, True, False, is_write,
                    permission_denied=True,
                )

        # ── 执行 ───────────────────────────────────────────────────────────
        try:
            with scoped_tool_call(str(tool_call.get("id") or "")):
                raw_output = dispatch(fn_name, tool_input)
        except Exception as exc:
            # Tool implementation failures are repair evidence, not Provider
            # failures. Returning a normal failed result lets the transaction
            # pipeline roll back writes and gives the Agent a fallback chance.
            return ToolExecutionResult(
                name=fn_name,
                tool_input=tool_input,
                output=f"Error: {fn_name} raised {type(exc).__name__}: {exc}",
                executed=True,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_write,
                metadata={"error_type": type(exc).__name__},
            )
        try:
            output = str(raw_output)
            title = raw_output.title if isinstance(raw_output, ToolOutput) else ""
            metadata = dict(raw_output.metadata) if isinstance(raw_output, ToolOutput) else {}
            attachments = raw_output.attachments if isinstance(raw_output, ToolOutput) else []
        except Exception as exc:
            return ToolExecutionResult(
                name=fn_name,
                tool_input=tool_input,
                output=f"Error: {fn_name} returned an invalid result: {type(exc).__name__}: {exc}",
                executed=True,
                dispatch_failed=True,
                command_failed=False,
                is_write=is_write,
                metadata={"error_type": type(exc).__name__, "malformed_result": True},
            )
        dispatch_failed = is_tool_result_error_content(output)
        error_code = extract_structured_tool_error_code(output)
        if error_code is not None:
            metadata["error_code"] = error_code
        if is_cancelled_tool_result_content(output):
            metadata["cancelled"] = True
        # bash 命令非零退出是"验证反馈"，不是调度失败。
        # 不应将其计入 all_succeeded=False，从而触发事务回滚。
        command_failed = fn_name == "bash" and output.startswith("Command exited with code")

        return ToolExecutionResult(
            name=fn_name,
            tool_input=tool_input,
            output=output,
            executed=True,
            dispatch_failed=dispatch_failed,
            command_failed=command_failed,
            is_write=is_write,
            permission_denied=output.startswith("Denied"),
            title=title,
            metadata=metadata,
            attachments=attachments,
        )
