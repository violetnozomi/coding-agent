"""Shared tool-execution result and mutation classification contracts."""
from __future__ import annotations

from dataclasses import dataclass, field

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
