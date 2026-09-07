"""Single-tool hook policy around a declared executor, independent of product hosts."""

from __future__ import annotations

import json
import time
from typing import Callable

from nz_coder.runtime.core.tool_contracts import ToolExecutorPort
from nz_coder.runtime.verification.hooks import HookDecision
from nz_coder.tool_platform.execution import (
    ToolExecutionResult,
    is_transactional_write_tool,
)


def parse_tool_input(raw: object) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def infer_tool_path(tool_input: dict) -> str:
    for key in ("path", "file_path", "project_dir", "target_dir"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


class HookedToolExecutor:
    """Hook rejection precedes executor authorization; no transcript publication here."""

    def __init__(
        self,
        executor: ToolExecutorPort,
        before: Callable[[list[dict], str, dict, str, bool], HookDecision | None],
    ) -> None:
        self.executor = executor
        self.before = before

    def execute_one(
        self, call: dict, index: int, messages: list[dict]
    ) -> ToolExecutionResult:
        started = time.perf_counter()
        name = call["function"]["name"]
        arguments = parse_tool_input(call["function"].get("arguments", {}))
        is_write = is_transactional_write_tool(name)
        decision = self.before(
            messages, name, arguments, infer_tool_path(arguments), is_write
        )
        if decision is not None and decision.rejected:
            reason = decision.message or f"Blocked by hook {decision.hook_id}"
            result = ToolExecutionResult(
                name,
                arguments,
                f"Denied: {reason}",
                False,
                True,
                False,
                is_write,
                permission_denied=True,
            )
        else:
            result = self.executor.execute_one(call, index)
        result.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return result
