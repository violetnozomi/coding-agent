"""把同一批工具的结果写入会话，生成模型下一轮能读取的消息。"""
from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.tool_platform.permissioning.interaction import format_tool_summary
from nz_coder.tool_platform.results import ProjectedToolResult, ToolResultProjector

if TYPE_CHECKING:
    from nz_coder.tool_platform.execution import ToolExecutionResult


_FILE_READ_TOOLS = frozenset({"read_file", "read_symbol"})
_TOOL_RESULT_BATCH_ENVELOPE_TOKENS = 4
_TOOL_RESULT_ITEM_ENVELOPE_TOKENS = 4


class ProductionToolResultProjector:
    """按同一份输出预算处理整批结果，并保持工具调用与结果一一对应。"""

    def __init__(self, *, projector: ToolResultProjector | None = None) -> None:
        self._projector = projector or ToolResultProjector()

    def consume(
        self,
        context: ToolProjectionContext,
        dispatched: list,
        messages: list,
        *,
        on_tool=None,
        processor=None,
    ) -> dict:
        state = {
            "manual_compact": False,
            "used_todo": False,
            "all_succeeded": True,
            "write_total": 0,
            "write_denied": 0,
            "blocked": False,
            "handoff_signal": None,
            "agent_transition": None,
            "terminal": False,
        }
        post_result_hooks: list[tuple[ToolExecutionResult, str]] = []
        projected_batch = self._project_outputs(context, dispatched, messages)
        for (index, tool_call, result), projected in zip(dispatched, projected_batch):
            _record_batch_result(context, state, result)
            stop_on_denial = _terminal_denial(result)
            if stop_on_denial:
                state["blocked"] = True

            output = projected.text
            if projected.metadata.get("truncated"):
                result.metadata = {
                    **(result.metadata if isinstance(result.metadata, dict) else {}),
                    "projection": projected.metadata,
                }
            if processor is not None:
                _settle_session_tool(processor, tool_call["id"], result, output, stop_on_denial=stop_on_denial)
            context.trace_result(result, output, tool_call_id=tool_call["id"], index=index)
            if on_tool:
                on_tool(result.name, output)
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": output,
            }
            tool_message.update(result.evidence_metadata)
            if result.attachments and not result.dispatch_failed and processor is None:
                tool_message["_nz_attachments"] = list(result.attachments)
            messages.append(tool_message)
            post_result_hooks.append((result, output))

        # 回调可以追加新消息。整批工具结果必须先相邻写入，模型才能把每个结果配回它的调用。
        for result, output in post_result_hooks:
            context.after_result(messages, result, output)
        return state

    def _project_outputs(
        self, context: ToolProjectionContext, dispatched: list, messages: list,
    ) -> list[ProjectedToolResult]:
        items = [
            (
                str(tool_call["id"]),
                result.name,
                _model_tool_output(result),
                (result.metadata or {}).get("raw_artifact_id"),
                (result.metadata or {}).get("raw_artifact_complete", True),
            )
            for _index, tool_call, result in dispatched
        ]
        budget = self._projector.batch_max_tokens
        if context.available_result_tokens is not None:
            # 下一次请求还要容纳工具消息本身的开销，剩余空间才分给结果正文。
            capacity = max(1, int(context.available_result_tokens(messages)))
            overhead = _TOOL_RESULT_BATCH_ENVELOPE_TOKENS + len(items) * _TOOL_RESULT_ITEM_ENVELOPE_TOKENS
            budget = max(1, capacity - overhead)
        return self._projector.project_batch(items, max_tokens=budget)


def _record_batch_result(context: ToolProjectionContext, state: dict, result: ToolExecutionResult) -> None:
    if state["handoff_signal"] is None:
        state["handoff_signal"] = context.signal_from_metadata(result.metadata)
    # 批次按工具派发结果决定是否提交写入。命令的非零退出会在会话中单独记录。
    if context.record_result(result):
        state["all_succeeded"] = False
    if result.executed and not result.dispatch_failed:
        if result.name == "compact":
            state["manual_compact"] = True
        elif result.name == "todo":
            state["used_todo"] = True
        elif result.name == "plan_exit" and (result.metadata or {}).get("plan_exit_terminal"):
            state["terminal"] = True
    if result.is_write:
        state["write_total"] += 1
        # 只有用户明确回复拒绝，才计入拒绝写入的次数。
        if result.permission_denied and result.metadata.get("permission_source") == "approval_reply":
            state["write_denied"] += 1


def _settle_session_tool(
    processor, call_id, result: ToolExecutionResult, model_output: str, *, stop_on_denial: bool,
) -> None:
    if result.name == "task" and not result.dispatch_failed and isinstance(result.metadata, dict):
        processor.add_child_cost(result.metadata.get("child_cost_delta", 0.0))
    failed = result.dispatch_failed or result.command_failed or any(
        (result.metadata or {}).get(key) for key in ("timed_out", "cancelled", "output_limit_exceeded")
    )
    denied_before_launch = result.permission_denied and not result.executed
    public_error = result.public_error
    if (public_error is not None and public_error.code == "permission_stopped"
            and not (denied_before_launch and (result.metadata or {}).get("permission_source"))):
        # 工具内部也可能遇到权限错误。保留它的公开说明，只有宿主能证明整次调用未执行。
        public_error = replace(public_error, code="permission_denied")
    processor.settle_tool(
        str(call_id),
        result.output if result.dispatch_failed else model_output,
        failed=failed,
        denied=denied_before_launch,
        title=result.title or format_tool_summary(result.name, result.tool_input),
        metadata=result.metadata,
        attachments=result.attachments,
        continue_on_deny=not stop_on_denial,
        public_error=public_error,
    )
    if denied_before_launch and result.name in {"bash", "process"}:
        from nz_coder.runtime.core.run_context import current_run_context
        from nz_coder.runtime.verification.report_evidence import project_denied_commands
        run = current_run_context()
        if run is not None and not run.defer_model_report:
            receipt = project_denied_commands([processor.message], session_id=run.session.session_id,
                                              interaction_id=run.interaction_run_id, include_archives=False)
            run.defer_model_report = bool(receipt.total)


def _model_tool_output(result: ToolExecutionResult) -> str:
    if result.command_failed and result.public_error is not None:
        # 第一行保留 Shell 退出码，后续同时给模型原因和原始断言，并一起计入输出预算。
        exit_line, newline, details = result.output.partition("\n")
        return f"{exit_line}\n{result.public_error.message}{newline}{details}"
    if (result.name != "bash" or not result.executed or result.dispatch_failed or result.permission_denied
            or result.command_failed or len(result.execution_facts) != 1
            or result.evidence_metadata.get("_nz_verification_passed") is True):
        return result.output
    receipt, = result.execution_facts
    if (receipt.producer != "bash" or receipt.outcome != "succeeded" or receipt.exit_code != 0
            or "test" not in receipt.capabilities):
        return result.output
    from nz_coder.runtime.verification.domain.identity import command_identity, verified_check_subject

    identity = command_identity(receipt.command, receipt.cwd)
    if not identity.opaque or (receipt.reliable and verified_check_subject(identity) is not None):
        return result.output
    # 只引导下一次调用；合并命令的回执和正文仍保留原样。
    return ("【运行时验证提示】这次退出状态属于整条 Shell 命令，运行时未取得可单独采信的测试结果。"
            "请用 bash.workdir 指定原测试目录，保留原参数和测试选择器，单独调用 bash 运行测试。"
            "直接捕获输出即可，清理和查看文件可分开执行。\n\n" + result.output)


def _terminal_denial(result: ToolExecutionResult) -> bool:
    """返回本次拒绝是否要求结束当前运行，以便其他拒绝仍可交给模型调整做法。"""
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return bool(
        result.permission_denied
        and (
            metadata.get("stall_kind") == "consecutive" or result.approval_stopped
        )
    )


def _semantic_evidence_metadata(runtime_state, result) -> dict:
    """Describe model-context freshness without changing visible tool output."""
    if runtime_state is None:
        return {}
    generation = max(
        0, int(getattr(runtime_state, "mutation_generation", 0) or 0)
    )
    name = str(result.name or "")
    tool_input = result.tool_input if isinstance(result.tool_input, dict) else {}
    if name in _FILE_READ_TOOLS:
        path = _normalized_path(tool_input.get("path"))
        return (
            {
                "_nz_evidence_kind": "file_read",
                "_nz_resource": path,
                "_nz_mutation_generation": generation,
            }
            if path else {}
        )
    if result.is_write and result.executed and not result.dispatch_failed:
        paths = _write_resources(tool_input)
        return {
            "_nz_evidence_kind": "file_write",
            "_nz_mutated_resources": paths,
            "_nz_mutation_generation": generation,
        }
    if not result.executed or result.dispatch_failed or result.permission_denied:
        return {}
    stage = ""
    if name == "bash":
        stage = str(tool_input.get("_nz_runtime_verification_stage") or "")
        if tool_input.get("_nz_runtime_contract"):
            stage = "acceptance"
        if not stage:
            from nz_coder.intelligence.verification_planner import classify_verification_command

            stage = str(
                classify_verification_command(str(tool_input.get("command") or ""))
                or ""
            )
    elif name in {"verify_changed_files", "python_symbol_check"}:
        stage = "static"
    elif name == "verify_project_build":
        stage = "targeted"
    if not stage:
        return {}
    domain = runtime_state.verification_domain
    if domain is None:
        return {}
    snapshot = domain.snapshot()
    execution_id = result.execution_id
    decisions = [item for item in snapshot.checks
                 if item.execution_id == execution_id or item.execution_id.startswith(execution_id + ":")]
    if not execution_id or not decisions:
        return {}
    return {
        "_nz_evidence_kind": "verification",
        "_nz_resource": stage,
        "_nz_mutation_generation": generation,
        "_nz_verification_passed": all(item.status == "passed" for item in decisions),
        "_nz_verification_revision": snapshot.revision,
    }


def _write_resources(tool_input: dict) -> list[str]:
    values = [tool_input.get("path"), tool_input.get("file_path")]
    for key in ("files", "changes"):
        for item in tool_input.get(key) or []:
            values.append(item.get("path") if isinstance(item, dict) else item)
    result: list[str] = []
    for value in values:
        path = _normalized_path(value)
        if path and path not in result:
            result.append(path)
    return result


def _normalized_path(value) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized
