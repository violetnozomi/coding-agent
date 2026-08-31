"""Projection of settled tool results into durable Agent history."""
from __future__ import annotations

from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.tool_platform.permissioning.interaction import format_tool_summary
from nz_coder.tool_platform.results import ToolResultProjector


_FILE_READ_TOOLS = frozenset({"read_file", "read_symbol"})
_TOOL_RESULT_BATCH_ENVELOPE_TOKENS = 4
_TOOL_RESULT_ITEM_ENVELOPE_TOKENS = 4


class ProductionToolResultProjector:
    """Own contiguous tool-result materialization and post-result hooks."""

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
        post_result_hooks: list[tuple[object, str]] = []
        batch_items = [
            (str(tool_call["id"]), result.name, result.output)
            for _index, tool_call, result in dispatched
        ]
        batch_budget = self._projector.batch_max_tokens
        if context.available_result_tokens is not None:
            physical_capacity = max(
                1, int(context.available_result_tokens(messages)),
            )
            envelope_tokens = (
                _TOOL_RESULT_BATCH_ENVELOPE_TOKENS
                + len(batch_items) * _TOOL_RESULT_ITEM_ENVELOPE_TOKENS
            )
            batch_budget = max(1, physical_capacity - envelope_tokens)
        projected_batch = self._projector.project_batch(
            batch_items, max_tokens=batch_budget,
        )
        for (index, tool_call, result), projected in zip(dispatched, projected_batch):
            if state["handoff_signal"] is None:
                state["handoff_signal"] = (
                    context.signal_from_metadata(result.metadata)
                )
            if context.record_result(result):
                state["all_succeeded"] = False
            if result.executed and not result.dispatch_failed and result.name == "compact":
                state["manual_compact"] = True
            if result.executed and not result.dispatch_failed and result.name == "todo":
                state["used_todo"] = True
            if (
                result.executed
                and not result.dispatch_failed
                and result.name == "plan_exit"
                and bool((result.metadata or {}).get("plan_exit_terminal"))
            ):
                state["terminal"] = True
            if result.is_write:
                state["write_total"] += 1
                if result.dispatch_failed and result.output.startswith("Denied"):
                    state["write_denied"] += 1
            # Denial is recoverable model-visible feedback. Terminal blockers
            # are settled explicitly by lifecycle policy, not inferred from a
            # single rejected tool invocation.
            terminal_guard = _terminal_denial(result)
            if terminal_guard:
                state["blocked"] = True

            output = projected.text
            if projected.metadata.get("truncated"):
                result.metadata = {
                    **(result.metadata if isinstance(result.metadata, dict) else {}),
                    "projection": projected.metadata,
                }
            if processor is not None:
                if (
                    result.name == "task"
                    and not result.dispatch_failed
                    and isinstance(result.metadata, dict)
                ):
                    processor.add_child_cost(result.metadata.get("child_cost_delta", 0.0))
                processor.settle_tool(
                    str(tool_call["id"]),
                    output if not result.dispatch_failed else result.output,
                    failed=result.dispatch_failed,
                    denied=result.permission_denied,
                    title=(
                        result.title
                        or format_tool_summary(result.name, result.tool_input)
                    ),
                    metadata=result.metadata,
                    attachments=result.attachments,
                    continue_on_deny=not terminal_guard,
                )
            context.trace_result(
                result,
                output,
                tool_call_id=tool_call["id"],
                index=index,
            )
            stall = context.stall_orchestrator
            if stall is not None and not bool((result.metadata or {}).get("stall_nudge")):
                stall.record_tool_result(str(tool_call["id"]), output)
            if on_tool:
                on_tool(result.name, output)
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": output,
            }
            tool_message.update(_semantic_evidence_metadata(context, result))
            if result.attachments and not result.dispatch_failed and processor is None:
                tool_message["_nz_attachments"] = list(result.attachments)
            messages.append(tool_message)
            post_result_hooks.append((result, output))

        # Provider protocols require contiguous results for one Assistant batch.
        for result, output in post_result_hooks:
            context.after_result(messages, result, output)
        return state


def _terminal_denial(result) -> bool:
    """Return whether one denied tool result is a settled Runtime blocker."""
    metadata = result.metadata if isinstance(result.metadata, dict) else {}
    return bool(
        result.permission_denied
        and (
            metadata.get("stall_kind") == "consecutive"
        )
    )


def _semantic_evidence_metadata(context: ToolProjectionContext, result) -> dict:
    """Describe model-context freshness without changing visible tool output."""
    runtime_state = context.runtime_state
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
    from nz_coder.intelligence.verification_planner import verification_output_failed

    passed = bool(
        result.executed
        and not result.dispatch_failed
        and not result.command_failed
        and not verification_output_failed(str(result.output or ""))
    )
    return {
        "_nz_evidence_kind": "verification",
        "_nz_resource": stage,
        "_nz_mutation_generation": generation,
        "_nz_verification_passed": passed,
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
