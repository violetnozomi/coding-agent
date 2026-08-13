"""Projection of settled tool results into durable Agent history."""
from __future__ import annotations

from nz_coder import config
from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.tool_platform.permissioning.interaction import format_tool_summary
from nz_coder.tool_platform.results import ToolResultProjector


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
            batch_budget = min(
                batch_budget,
                max(1, int(context.available_result_tokens(messages))),
            )
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
            if result.is_write:
                state["write_total"] += 1
                if result.dispatch_failed and result.output.startswith("Denied"):
                    state["write_denied"] += 1
            if result.permission_denied and not config.CONTINUE_LOOP_ON_DENY:
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
                    continue_on_deny=config.CONTINUE_LOOP_ON_DENY,
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
            if result.attachments and not result.dispatch_failed and processor is None:
                tool_message["_nz_attachments"] = list(result.attachments)
            messages.append(tool_message)
            post_result_hooks.append((result, output))

        # Provider protocols require contiguous results for one Assistant batch.
        for result, output in post_result_hooks:
            context.after_result(messages, result, output)
        return state
