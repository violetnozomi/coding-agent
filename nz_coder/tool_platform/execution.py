"""Shared tool-execution result and mutation classification contracts."""
from __future__ import annotations

from dataclasses import dataclass, field
from nz_coder.protocol.public_error import PublicError
from nz_coder.runtime.verification.domain.models import ExecutionReceipt

from nz_coder.tools import (
    FILESYSTEM_MUTATION_TOOLS,
    is_filesystem_mutation_tool,
    is_transactional_dynamic_tool,
)


WRITE_TOOLS: frozenset[str] = FILESYSTEM_MUTATION_TOOLS


def is_write_tool(name: str) -> bool:
    """Return whether a tool mutates task-workspace files."""
    return is_filesystem_mutation_tool(name)


def is_transactional_write_tool(name: str) -> bool:
    """Return whether the tool writes local state covered by TransactionManager."""
    return is_write_tool(name) and is_transactional_dynamic_tool(name)


def command_failed_from_result(
    name: str,
    output: str,
    metadata: dict,
    *,
    structured: bool,
) -> bool:
    """Classify Bash failure from canonical metadata, with legacy fallback."""
    if str(name or "") != "bash":
        return False
    if structured:
        try:
            return int(metadata.get("exit", 0)) != 0
        except (TypeError, ValueError, OverflowError):
            return False
    return str(output or "").startswith("Command exited with code")


@dataclass
class ToolExecutionResult:
    """Canonical result of one authorized tool invocation."""

    name: str
    tool_input: dict
    output: str
    executed: bool
    dispatch_failed: bool
    command_failed: bool
    is_write: bool
    duration_ms: float = 0.0
    queue_wait_ms: float = 0.0
    permission_denied: bool = False
    title: str = ""
    metadata: dict = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)
    category: str = ""
    # 仅由宿主执行边界生成；不从模型参数或工具输出接收范围证明。
    workspace_scope: dict | None = None
    execution_facts: tuple[ExecutionReceipt, ...] = ()
    execution_id: str = ""
    # 宿主异常或内置命令回执产生的公开原因，工具自报的 metadata 不能赋予它可信身份。
    public_error: PublicError | None = None
    # 宿主在事实提交时生成历史标签，延迟展示不能改用批末版本或公开 metadata。
    evidence_metadata: dict = field(default_factory=dict)

    @property
    def approval_stopped(self) -> bool:
        """审批回复或交互故障终止本轮；静态策略拒绝仍可调整操作。"""
        return bool(self.permission_denied and self.metadata.get("permission_source") in {
            "approval_reply", "interaction_failure", "batch_cancelled",
        })


def skipped_after_approval(tool_call: dict) -> ToolExecutionResult:
    """补齐同批请求的结果身份，但不再审批或执行后续操作。"""
    return ToolExecutionResult(
        name=tool_call["function"]["name"], tool_input={},
        output="Skipped because an earlier permission approval stopped this batch.",
        executed=False, dispatch_failed=True, command_failed=False, is_write=False,
        permission_denied=True, metadata={"permission_source": "batch_cancelled"},
    )
