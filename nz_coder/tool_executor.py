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
from dataclasses import dataclass

from nz_coder import config
from nz_coder.permissions import PermissionManager
from nz_coder.tools import dispatch

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
        fn_name = tool_call["function"]["name"]
        fn_args_raw = tool_call["function"].get("arguments", "{}")
        is_write = fn_name in WRITE_TOOLS

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
            return ToolExecutionResult(fn_name, tool_input, output, False, True, False, is_write)
        if decision["behavior"] == "ask":
            if not self._permissions.ask_user(fn_name, tool_input):
                return ToolExecutionResult(
                    fn_name, tool_input, "Denied by user", False, True, False, is_write
                )

        # ── 执行 ───────────────────────────────────────────────────────────
        output = dispatch(fn_name, tool_input)
        dispatch_failed = output.startswith("Error:") or output.startswith("Denied")
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
        )
