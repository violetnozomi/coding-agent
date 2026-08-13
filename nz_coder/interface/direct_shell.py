"""Direct terminal shell requests routed through normal tool permissions."""
from __future__ import annotations

import json
import uuid

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.tool_executor import ToolExecutionResult, ToolExecutor
import nz_coder.tools.bash  # noqa: F401  # register the canonical bash tool


def execute_direct_shell(
    command: str,
    *,
    permissions: PermissionManager,
) -> ToolExecutionResult:
    """Execute one `!command` without adding it to the Agent transcript."""
    selected = str(command).strip()
    if not selected:
        raise ValueError("Direct shell command must be non-empty")
    if not isinstance(permissions, PermissionManager):
        raise TypeError("permissions must be PermissionManager")
    call = {
        "id": f"direct-shell-{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": "bash",
            "arguments": json.dumps({"command": selected}),
        },
    }
    return ToolExecutor(permissions).execute_one(call, 0)


__all__ = ["execute_direct_shell"]
