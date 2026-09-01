"""Run initialization lifecycle shared by every Agent profile."""
from __future__ import annotations

import re
import copy

from nz_coder.protocol.message_schema import (
    cleanup_incomplete_tool_history,
    ensure_message_identities,
    is_synthetic_user_message,
    settle_interrupted_parts,
)
from nz_coder.runtime.agent.admission import AdmissionInvariantSession
from nz_coder.runtime.conversation.continuation_context import (
    RESUMABLE_STATUSES,
    continuation_task_text,
    is_continuation_activation,
    is_pure_continuation_activation,
)
from nz_coder.runtime.core.lifecycle_context import LifecycleExecutionContext
from nz_coder.runtime.core.execution_context import (
    agent_timeout_seconds,
    max_agent_turns,
    set_broad_tests_blocked,
    set_declared_test_scopes,
)
from nz_coder.runtime.agent.task_policy import declared_test_scopes


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
        context.clear_read_cache()
        if context.stall_orchestrator is not None:
            cancel_and_settle = getattr(
                context.stall_orchestrator,
                "cancel_and_settle",
                None,
            )
            if callable(cancel_and_settle):
                cancel_and_settle(timeout=0.5)
            else:
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
        resume_activation = is_continuation_activation(messages)
        pure_continuation = is_pure_continuation_activation(messages)
        state.restored_state = context.prepare_runtime_state(
            task_text,
            max_turns,
            agent_timeout_seconds(),
            resume_activation,
            task_text if resume_activation and not pure_continuation else "",
        )
        policy_task = (
            context.runtime_state.initial_task_text
            if state.restored_state
            and pure_continuation
            and context.runtime_state.initial_task_text
            else task_text
        )
        set_declared_test_scopes(declared_test_scopes(policy_task))
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
        if _terminal_allows_learning(status):
            try:
                context.save_learnings(messages)
            except Exception as exc:
                context.trace(
                    "terminal_learning_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:1000],
                )
        self._refresh_terminal_runtime(context)
        context.persist_runtime_state(
            active=_resumable_terminal(context.run_state.last_status)
        )
        context.commit()
        self._publish_terminal(context, messages)
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
        if _terminal_allows_learning(status):
            try:
                await context.save_learnings_async(messages)
            except Exception as exc:
                context.trace(
                    "terminal_learning_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:1000],
                )
        self._refresh_terminal_runtime(context)
        context.persist_runtime_state(
            active=_resumable_terminal(context.run_state.last_status)
        )
        context.commit()
        self._publish_terminal(context, messages)
        return context.run_state.last_status

    @staticmethod
    def _prepare_terminal(
        context: LifecycleExecutionContext, messages: list, status: str,
        on_text, on_token, stream: bool,
        content_text: str | None, max_turns: int | None,
    ) -> dict | None:
        state = context.run_state
        status = context.assert_terminal(status)
        ProductionRunLifecycle._settle_terminal_sidecars(context)
        inject_missing_tool_results(messages)
        context.finish_lineage(status, messages)
        resolved_content, content_source = _terminal_content(messages, content_text)
        if status == "aborted" and not resolved_content:
            resolved_content = (
                f"Agent aborted after {context.recovery.consecutive_errors} "
                "consecutive errors"
            )
            content_source = "status"
        persisted_into_assistant = bool(
            context.persist_assistant_end(messages, status, resolved_content)
        )
        if status == "aborted":
            if on_text:
                on_text(resolved_content)
            runtime = context.runtime_summary()
            state.last_status = {
                "status": "aborted",
                "content": resolved_content,
                "errors": context.recovery.consecutive_errors,
                "last_error": context.recovery.last_error,
                **context.vm.status(),
                "runtime": runtime,
            }
            context.commit()
            context.trace(
                "terminal_content_persisted",
                source=content_source,
                nonempty=bool(resolved_content),
                persisted_into_assistant=persisted_into_assistant,
            )
            ProductionRunLifecycle._trace_evidence(context)
            context.trace(
                "run_end", status="aborted",
                errors=context.recovery.consecutive_errors,
                runtime=runtime,
                **context.vm.status(),
            )
            context.publish_event(
                "session.run.completed",
                {"status": "aborted", "message_count": len(messages)},
            )
            context.persist_runtime_state(active=False)
            return state.last_status

        if status == "max_turns" and on_text:
            on_text(
                resolved_content
                or (
                    f"Agent stopped after reaching max_turns={max_turns}"
                    if max_turns is not None
                    else "Agent stopped at the configured work limit."
                )
            )
        elif resolved_content and on_text and not stream:
            on_text(resolved_content)
        if status in {"completed", "completed_unverified"} and stream and on_token:
            if resolved_content:
                on_token(resolved_content)
            on_token(None)

        runtime = context.runtime_summary()
        state.last_status = {
            "status": status,
            "content": resolved_content,
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
        context.trace(
            "terminal_content_persisted",
            source=content_source,
            nonempty=bool(resolved_content),
            persisted_into_assistant=persisted_into_assistant,
        )
        ProductionRunLifecycle._trace_evidence(context)
        return None

    @staticmethod
    def _refresh_terminal_runtime(context: LifecycleExecutionContext) -> None:
        """Close accounting after awaited/synchronous terminal side effects."""
        last_status = context.run_state.last_status
        if isinstance(last_status, dict):
            last_status["runtime"] = context.runtime_summary()

    @staticmethod
    def _settle_terminal_sidecars(context: LifecycleExecutionContext) -> None:
        """Cancel unconsumable L2 work before freezing the final ledger."""
        orchestrator = context.stall_orchestrator
        cancel_and_settle = getattr(orchestrator, "cancel_and_settle", None)
        if not callable(cancel_and_settle):
            return
        if not cancel_and_settle(timeout=1.0):
            context.trace(
                "stall_sidecar_terminal_unsettled",
                timeout_seconds=1.0,
            )

    @staticmethod
    def _publish_terminal(
        context: LifecycleExecutionContext, messages: list,
    ) -> None:
        """Publish the terminal boundary only after the final ledger settles."""
        last_status = context.run_state.last_status
        status = str(last_status.get("status") or "completed")
        runtime = last_status.get("runtime", {})
        context.trace(
            "run_end", status=status, message_count=len(messages),
            runtime=runtime, **context.vm.status(),
        )
        context.publish_event(
            (
                "session.run.cancelled"
                if status in {"cancelled", "interrupted"}
                else "session.run.completed"
            ),
            {"status": status, "message_count": len(messages)},
        )

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


def _terminal_allows_learning(status: str) -> bool:
    """Never start fresh teardown work after an explicit user interruption."""
    return status not in {"cancelled", "interrupted", "aborted"}


def inject_missing_tool_results(messages: list) -> None:
    """Repair interrupted tool history before resume or terminal persistence.

    The historical name remains for compatibility.  Source-aligned cleanup
    removes incomplete protocol envelopes instead of inventing a tool result;
    durable tool/question parts still record the interruption for the UI.
    """
    if not messages:
        return
    settle_interrupted_parts(messages)
    messages[:] = cleanup_incomplete_tool_history(messages)


def last_user_text(messages: list) -> str:
    """Return canonical task text for runtime policy.

    Retrieval and memory queries use their own bounded projection.  Lifecycle
    policy retains late acceptance commands and artifact requirements, while a
    pure continuation activation keeps the unfinished original task authoritative.
    """
    return continuation_task_text(messages)


def _resumable_terminal(result: dict) -> bool:
    """Return whether terminal task state must remain available to resume."""
    return str(result.get("status") or "") in RESUMABLE_STATUSES


def _terminal_content(
    messages: list,
    supplied: str | None,
) -> tuple[str, str]:
    """Resolve durable terminal text for only the current real User turn."""
    if isinstance(supplied, str) and supplied.strip():
        return supplied, "boundary"
    last_real_user = max(
        (
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict)
            and message.get("role") == "user"
            and not is_synthetic_user_message(message)
        ),
        default=-1,
    )
    for index, message in reversed(list(enumerate(messages))):
        if index <= last_real_user:
            break
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if message.get("_nz_internal") is True or message.get("_nz_visible") is False:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content, "assistant"
    return "", "empty"


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
