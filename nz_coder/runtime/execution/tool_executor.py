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
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from nz_coder.runtime.core.run_settings import current_run_settings
from nz_coder.foundation.json_safety import reject_nonstandard_json_constant
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.agent.agent_resilience import (
    extract_structured_tool_error_code,
    is_cancelled_tool_result_content,
    is_tool_result_error_content,
)
from nz_coder.protocol.session_events import publish_session_event
from nz_coder.protocol.public_error import format_public_error
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tool_platform.execution import (
    WRITE_TOOLS,
    ToolExecutionResult,
    command_failed_from_result,
    is_transactional_write_tool,
    is_write_tool,
)
from nz_coder.tool_platform.permissioning.interaction import format_tool_summary
from nz_coder.tools import (
    ToolOutput,
    collect_filesystem_mutation_paths,
    dispatch,
    get_tool_side_effect,
    scoped_dynamic_tool_snapshot,
    scoped_tool_call,
)

__all__ = [
    "WRITE_TOOLS",
    "ToolExecutionResult",
    "ToolExecutor",
    "command_failed_from_result",
    "is_transactional_write_tool",
    "is_write_tool",
    "tool_category",
]

# Product-event taxonomy is intentionally distinct from side effects: these
# tools coordinate or communicate with child Agents even when their dominant
# registry effect is an internal state mutation.
_AGENT_EVENT_TOOLS = frozenset({
    "agent_manager",
    "background_task_apply",
    "background_task_start",
    "emit_handoff",
    "message_parent",
    "send_message",
    "subagent",
    "task",
    "workflow_run",
})


@dataclass(frozen=True)
class _ReadCacheEntry:
    """Filesystem identity recorded for one observed text-file range."""

    mtime_ns: int
    ctime_ns: int
    size: int


class _ReadFileStateCache:
    """Per-Agent unchanged-read suppression with filesystem invalidation."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, int], _ReadCacheEntry] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _target(tool_input: dict) -> tuple[Path, int, int] | None:
        if tool_input.get("pages") not in (None, ""):
            return None
        raw_path = tool_input.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        root = current_workdir().resolve()
        target = (root / raw_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        raw_offset = tool_input.get("offset")
        raw_limit = tool_input.get("limit")
        if isinstance(raw_offset, bool) or isinstance(raw_limit, bool):
            return None
        offset = 1 if raw_offset in (None, 0) else raw_offset
        limit = 2000 if raw_limit is None else raw_limit
        if not isinstance(offset, int) or not isinstance(limit, int):
            return None
        if offset < 1 or limit < 0:
            return None
        return target, offset, limit

    @staticmethod
    def _identity(target: Path) -> _ReadCacheEntry | None:
        try:
            stat = target.stat()
        except OSError:
            return None
        if not target.is_file():
            return None
        return _ReadCacheEntry(
            mtime_ns=stat.st_mtime_ns,
            ctime_ns=stat.st_ctime_ns,
            size=stat.st_size,
        )

    def lookup(self, tool_input: dict) -> str | None:
        target_range = self._target(tool_input)
        if target_range is None:
            return None
        target, offset, limit = target_range
        key = (str(target), offset, limit)
        identity = self._identity(target)
        with self._lock:
            previous = self._entries.get(key)
            if previous is None or identity != previous:
                if previous is not None:
                    self._entries.pop(key, None)
                return None
        return (
            f"[Read Cache] {target} is unchanged since you read it earlier "
            f"in this task (offset={offset}, limit={limit}). The content from "
            "the earlier read tool_result in this conversation is still current "
            "— refer to that instead of re-reading. If you need different lines, "
            "call read_file with a different offset/limit. This cache invalidates "
            "automatically when the file changes."
        )

    def record(self, tool_input: dict) -> None:
        target_range = self._target(tool_input)
        if target_range is None:
            return
        target, offset, limit = target_range
        identity = self._identity(target)
        if identity is None:
            return
        with self._lock:
            self._entries[(str(target), offset, limit)] = identity

    def forget_paths(self, paths: set[str]) -> None:
        if not paths:
            return
        root = current_workdir().resolve()
        targets: set[str] = set()
        for raw_path in paths:
            try:
                target = (root / raw_path).resolve()
                target.relative_to(root)
            except (OSError, ValueError):
                continue
            targets.add(str(target))
        with self._lock:
            for key in list(self._entries):
                if key[0] in targets:
                    self._entries.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


def _write_paths(tool_input: dict) -> set[str]:
    """Extract explicit mutation targets without inferring model intent."""
    return set(collect_filesystem_mutation_paths(tool_input))


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


def tool_category(name: str) -> str:
    """Project declarative side effects into the public event categories."""
    if name in _AGENT_EVENT_TOOLS:
        return "agent"
    effect = get_tool_side_effect(name)
    if effect == "mutates-fs":
        return "edit"
    if effect == "mutates-shell":
        return "command"
    if effect in {"readonly", "reads-network"}:
        return "read"
    return "state"


class ToolExecutor:
    """解析、权限检查并执行单次工具调用，返回分类结果。

    不负责 messages 追加、事务管理或 tracer 记录。
    """

    def __init__(self, permissions: PermissionManager):
        self._permissions = permissions
        self._read_cache = _ReadFileStateCache()

    def clear_read_cache(self) -> None:
        """Drop read references after context compaction removes their results."""
        self._read_cache.clear()

    def execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult:
        """Resolve one dynamic generation before authorization and dispatch."""
        with scoped_dynamic_tool_snapshot():
            return self._execute_one_with_snapshot(tool_call, index)

    def _execute_one_with_snapshot(
        self,
        tool_call: dict,
        index: int,
    ) -> ToolExecutionResult:
        """执行单次工具调用，返回 ToolExecutionResult。

        Args:
            tool_call: 形如 {"function": {"name": ..., "arguments": ...}} 的字典。
            index: 本轮响应中的零起始序号，用于限制单轮调用次数。
        """
        envelope = tool_call if isinstance(tool_call, dict) else {}
        function = envelope.get("function")
        raw_name = function.get("name") if isinstance(function, dict) else None
        provider_extra = envelope.get("provider_extra")
        repaired_malformed_envelope = bool(
            isinstance(provider_extra, dict)
            and provider_extra.get("nz_malformed_tool_call") is True
        )
        valid_envelope = bool(
            isinstance(raw_name, str) and raw_name.strip()
        ) and not repaired_malformed_envelope
        fn_name = raw_name.strip() if valid_envelope else "unknown"
        tool_input = _best_effort_arguments(
            function.get("arguments", {}) if isinstance(function, dict) else {},
        )
        is_write = is_transactional_write_tool(fn_name)
        category = tool_category(fn_name)
        publish_session_event(
            "session.tool.started",
            {
                "tool_call_id": envelope.get("id"),
                "index": index,
                "name": fn_name,
                "category": category,
                "summary": format_tool_summary(fn_name, tool_input),
                "is_write": is_write,
            },
        )
        started = time.perf_counter()
        if valid_envelope:
            result = self._execute_one(envelope, index)
        else:
            result = ToolExecutionResult(
                name="unknown",
                tool_input={},
                output=(
                    "Error: Malformed tool call: function.name must be a "
                    "non-empty string. Please issue a new valid tool call."
                ),
                executed=False,
                dispatch_failed=True,
                command_failed=False,
                is_write=False,
            )
        result.duration_ms = round((time.perf_counter() - started) * 1000, 3)
        result.category = category
        return result

    def _execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult:
        """Implementation split out so every return path receives timing metadata."""
        fn_name = tool_call["function"]["name"]
        fn_args_raw = tool_call["function"].get("arguments", "{}")
        is_write = is_transactional_write_tool(fn_name)

        # ── 单轮调用数限制 ──────────────────────────────────────────────────
        settings = current_run_settings()
        if index >= settings.max_tool_calls:
            output = (
                f"Error: Too many tool calls in one response "
                f"(limit {settings.max_tool_calls})"
            )
            return ToolExecutionResult(fn_name, {}, output, False, True, False, is_write)

        # ── JSON 解析 ──────────────────────────────────────────────────────
        try:
            tool_input = (
                json.loads(
                    fn_args_raw,
                    parse_constant=reject_nonstandard_json_constant,
                )
                if isinstance(fn_args_raw, str)
                else ({} if fn_args_raw is None else fn_args_raw)
            )
            if isinstance(tool_input, dict):
                json.dumps(tool_input, ensure_ascii=False, allow_nan=False)
        except (json.JSONDecodeError, TypeError, ValueError, OverflowError):
            output = (
                f"Error: Invalid JSON arguments for {fn_name}. "
                "Your tool call arguments are not valid JSON. "
                "Common causes: unescaped quotes inside strings, "
                "literal backslashes not doubled, trailing commas. "
                "Please rewrite the arguments with properly escaped JSON strings."
            )
            return ToolExecutionResult(fn_name, {}, output, False, True, False, is_write)

        # ── 权限检查 ───────────────────────────────────────────────────────
        if not isinstance(tool_input, dict):
            output = (
                f"Error: Invalid arguments for {fn_name}: tool arguments must "
                "be a JSON object. Please rewrite the call using named fields."
            )
            return ToolExecutionResult(
                fn_name, {}, output, False, True, False, is_write,
            )

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

        if fn_name == "read_file" and settings.read_dedup_enabled:
            cached_output = self._read_cache.lookup(tool_input)
            if cached_output is not None:
                return ToolExecutionResult(
                    name=fn_name,
                    tool_input=tool_input,
                    output=cached_output,
                    executed=True,
                    dispatch_failed=False,
                    command_failed=False,
                    is_write=False,
                    metadata={"read_cache_hit": True},
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
                output=format_public_error(
                    exc, context=f"{fn_name} failed: ",
                ),
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
                output=format_public_error(
                    exc, context=f"{fn_name} returned an invalid result: ",
                ),
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
        if (
            fn_name == "read_file"
            and settings.read_dedup_enabled
            and not dispatch_failed
            and metadata.get("encoding")
            and not attachments
        ):
            self._read_cache.record(tool_input)
        elif is_write and not dispatch_failed:
            self._read_cache.forget_paths(_write_paths(tool_input))
        # bash 命令非零退出是"验证反馈"，不是调度失败。
        # 不应将其计入 all_succeeded=False，从而触发事务回滚。
        command_failed = command_failed_from_result(
            fn_name,
            output,
            metadata,
            structured=isinstance(raw_output, ToolOutput),
        )

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
