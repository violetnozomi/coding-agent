"""Run initialization lifecycle shared by every Agent profile."""
from __future__ import annotations

import re
import copy

from nz_coder.message_schema import (
    ensure_message_identities,
    is_synthetic_user_message,
    settle_interrupted_parts,
)
from nz_coder.runtime.admission import AdmissionInvariantSession
from nz_coder.runtime.core.lifecycle_context import LifecycleExecutionContext
from nz_coder.runtime.execution_context import (
    agent_timeout_seconds,
    max_agent_turns,
    set_broad_tests_blocked,
)


class ProductionRunLifecycle:
    """Reset, restore, and settle one run through focused capabilities."""

    def initialize(
        self, context: LifecycleExecutionContext, messages: list, stream: bool,
    ) -> tuple[int, int]:
        state = context.run_state
        state.reset()
        context.clear_reverter()
        context.vm.reset()
        context.recovery.start_tool_call_run()
        if context.stall_orchestrator is not None:
            context.stall_orchestrator.reset()
        state.admission_session = (
            AdmissionInvariantSession(context.admission_handle)
            if context.admission_handle is not None else None
        )
        context.reset_hooks()
        context.clear_reasoning_escalation()
        context.commit()
        context.restore_agent_role()

        inject_missing_tool_results(messages)
        context.bind_user_messages(messages)
        ensure_message_identities(messages, context.session_id)
        max_turns = parse_turn_budget(messages) or max_agent_turns()
        if context.admission_handle is not None:
            max_turns = min(
                max_turns,
                context.admission_handle.system_cap.max_iterations,
            )
        task_text = last_user_text(messages)
        state.restored_state = context.prepare_runtime_state(
            task_text, max_turns, agent_timeout_seconds(),
        )
        if state.restored_state:
            state.replan_count = context.runtime_state.replan_count
        context.start_run_evidence()
        context.persist_runtime_state(active=True)
        set_broad_tests_blocked(False)
        context.commit()
        context.publish_started(messages, stream, max_turns)
        start_turn = context.runtime_state.turn_count if state.restored_state else 0
        return max_turns, start_turn

    def finalize_sync(
        self, context: LifecycleExecutionContext, messages: list, status: str,
        on_text=None, on_token=None,
        stream: bool = True, content_text: str | None = None,
        max_turns: int | None = None,
    ) -> dict:
        terminal = self._prepare_terminal(
            context, messages, status, on_text, on_token, stream,
            content_text, max_turns,
        )
        if terminal is not None:
            return terminal
        context.save_learnings(messages)
        context.persist_runtime_state(active=False)
        context.commit()
        return context.run_state.last_status

    async def finalize(
        self, context: LifecycleExecutionContext, messages: list, status: str,
        on_text=None, on_token=None,
        stream: bool = True, content_text: str | None = None,
        max_turns: int | None = None,
    ) -> dict:
        terminal = self._prepare_terminal(
            context, messages, status, on_text, on_token, stream,
            content_text, max_turns,
        )
        if terminal is not None:
            return terminal
        await context.save_learnings_async(messages)
        context.persist_runtime_state(active=False)
        context.commit()
        return context.run_state.last_status

    @staticmethod
    def _prepare_terminal(
        context: LifecycleExecutionContext, messages: list, status: str,
        on_text, on_token, stream: bool,
        content_text: str | None, max_turns: int | None,
    ) -> dict | None:
        state = context.run_state
        status = context.assert_terminal(status)
        context.finish_lineage(status, messages)
        context.persist_assistant_end(messages, status)
        if status == "aborted":
            if on_text:
                on_text(
                    f"Agent aborted after {context.recovery.consecutive_errors} "
                    "consecutive errors"
                )
            state.last_status = {
                "status": "aborted",
                "errors": context.recovery.consecutive_errors,
                "last_error": context.recovery.last_error,
            }
            context.commit()
            ProductionRunLifecycle._trace_evidence(context)
            context.trace(
                "run_end", status="aborted",
                errors=context.recovery.consecutive_errors,
            )
            context.publish_event(
                "session.run.completed",
                {"status": "aborted", "message_count": len(messages)},
            )
            context.persist_runtime_state(active=False)
            return state.last_status

        if status == "max_turns" and on_text:
            on_text(f"Agent stopped after reaching max_turns={max_turns}")
        elif content_text and on_text and not stream:
            on_text(content_text)
        if status in {"completed", "completed_unverified"} and stream and on_token:
            if content_text:
                on_token(content_text)
            on_token(None)

        runtime = context.runtime_summary()
        state.last_status = {
            "status": status,
            "errors": (
                context.recovery.consecutive_errors if status == "max_turns" else 0
            ),
            **context.vm.status(),
            "runtime": runtime,
        }
        active_agent = context.current_agent_name()
        outputs = context.structured_outputs()
        if active_agent in outputs:
            state.last_status["structured"] = copy.deepcopy(outputs[active_agent])
        context.commit()
        ProductionRunLifecycle._trace_evidence(context)
        context.trace(
            "run_end", status=status, message_count=len(messages),
            runtime=runtime, **context.vm.status(),
        )
        context.publish_event(
            "session.run.completed",
            {"status": status, "message_count": len(messages)},
        )
        return None

    @staticmethod
    def _trace_evidence(context: LifecycleExecutionContext) -> None:
        evidence = context.run_evidence()
        if evidence.is_empty():
            return
        context.trace(
            "run_evidence",
            summary=evidence.summary_text(max_items=6),
            **context.trace_evidence_summary(),
        )


def inject_missing_tool_results(messages: list) -> None:
    if not messages:
        return
    settle_interrupted_parts(messages)
    answered = {
        message.get("tool_call_id") for message in messages
        if isinstance(message, dict)
        and message.get("role") == "tool"
        and message.get("tool_call_id")
    }
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        calls = message.get("tool_calls") or []
        if not calls:
            break
        for call in (call for call in calls if call.get("id") not in answered):
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": (
                    "<interrupted>\nThis tool call was interrupted before it could "
                    "complete. The previous run was stopped mid-execution. Please "
                    "re-assess the current state of the workspace and continue.\n"
                    "</interrupted>"
                ),
            })
        break


def last_user_text(messages: list) -> str:
    for message in reversed(messages):
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and not is_synthetic_user_message(message)
            and isinstance(message.get("content"), str)
        ):
            return message["content"][:300]
    return ""


_BUDGET_SHORTHAND = re.compile(r"(?:^|\s)\+(\d+(?:\.\d+)?)\s*(k|m)\b", re.I)
_BUDGET_VERBOSE = re.compile(r"\buse\s+(\d+(?:\.\d+)?)\s*(k|m)?\s*turns?\b", re.I)
_BUDGET_BARE = re.compile(r"(?:^|\s)\+(\d+)\s*turns?\b", re.I)


def parse_turn_budget(messages: list) -> int | None:
    text = last_user_text(messages)
    if not text:
        return None
    cap = max_agent_turns() * 10
    if _BUDGET_SHORTHAND.search(text):
        return min(200, cap)
    match = _BUDGET_VERBOSE.search(text)
    if match:
        multiplier = {"k": 1000, "m": 1_000_000}.get(
            (match.group(2) or "").lower(), 1
        )
        return min(max(1, int(float(match.group(1)) * multiplier)), cap)
    match = _BUDGET_BARE.search(text)
    return min(max(1, int(match.group(1))), cap) if match else None


def empty_tool_observability() -> dict:
    return {
        "batches": 0,
        "calls": 0,
        "wall_ms": 0.0,
        "tool_duration_ms": 0.0,
        "peak_concurrency": 0,
        "parallel_segments": 0,
        "serial_segments": 0,
        "barrier_wait_ms": 0.0,
        "streak_resets": 0,
    }
