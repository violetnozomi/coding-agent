"""Run initialization lifecycle shared by every Agent profile."""
from __future__ import annotations

from contextlib import nullcontext

import re
import copy

from nz_coder.protocol.message_schema import (
    ASSISTANT_ERROR_KEY,
    INTERACTION_RUN_ID_KEY,
    cleanup_incomplete_tool_history,
    ensure_message_identities,
    is_synthetic_user_message,
    settle_interrupted_parts,
)
from nz_coder.runtime.agent.admission import AdmissionInvariantSession
from nz_coder.runtime.conversation.continuation_context import (
    RESUMABLE_STATUSES,
    continuation_task_text,
)
from nz_coder.runtime.core.lifecycle_context import LifecycleExecutionContext
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError, public_error_from_wire, to_public_error
from nz_coder.runtime.process.command_channel import current_command_client
from nz_coder.runtime.core.execution_context import (
    command_activation,
    derive_command_execution_policy,
    strict_local_tools,
    agent_timeout_seconds,
    max_agent_turns,
)


class ProductionRunLifecycle:
    """Reset, restore, and settle one run through focused capabilities."""

    def initialize(
        self, context: LifecycleExecutionContext, messages: list, stream: bool,
    ) -> tuple[int, int]:
        state = context.run_state
        state.reset()
        context.clear_reverter()
        run = getattr(context.runtime_state, "_verification_context", None)
        with run._state_lock if run is not None else nullcontext():
            context.recovery.start_tool_call_run()
        context.clear_read_cache()
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
        activation = command_activation(messages)
        task_text = activation.task_text
        resume_activation = activation.resume_activation
        pure_continuation = activation.pure_continuation
        followup = activation.current_user_message
        authority_kwargs = {"current_user_message": followup} if followup is not None else {}
        state.restored_state = context.prepare_runtime_state(
            task_text,
            max_turns,
            agent_timeout_seconds(),
            resume_activation,
            task_text if resume_activation and not pure_continuation else "",
            **authority_kwargs,
        )
        from nz_coder.runtime.agent.planning_runtime import activate_execution_handoff
        from nz_coder.runtime.process.workdir import current_workdir
        context.verification_runtime.initialize(restored=state.restored_state, messages=messages)
        from nz_coder.runtime.agent.task_intent import initialize_task_assessment
        # 无新增真实指令的内部恢复，才允许沿用同来源判断；“继续”新消息也须重新核对绑定。
        assessment_resume = state.restored_state and activation.current_user_message is None
        initialize_task_assessment(context.runtime_state, context.verification_runtime.run_context,
                                   messages, pure_continuation=assessment_resume)
        activate_execution_handoff(context.runtime_state, messages, workspace=current_workdir(),
                                   pure_continuation=pure_continuation)
        policy = derive_command_execution_policy(
            activation, restored=state.restored_state,
            initial_task_text=context.runtime_state.initial_task_text if state.restored_state else "",
            strict_local_tools=strict_local_tools(),
        )
        context.start_run_evidence()
        context.persist_runtime_state(active=True)
        context.commit()
        client = current_command_client()
        if client is not None and not client.policy_ready(policy, deadline=client.deadline):
            raise PublicRuntimeError(PublicError(
                "child_command_channel_error", "Child command policy initialization failed.",
            ))
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
        self._trace_terminal(context, messages)
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
        self._trace_terminal(context, messages)
        return context.run_state.last_status

    @staticmethod
    def _prepare_terminal(
        context: LifecycleExecutionContext, messages: list, status: str,
        on_text, on_token, stream: bool,
        content_text: str | None, max_turns: int | None,
    ) -> dict | None:
        state = context.run_state
        status = context.assert_terminal(status)
        inject_missing_tool_results(messages)
        runtime = context.verification_runtime
        resolved_content, content_source = _terminal_content(messages, content_text)
        active_agent = context.current_agent_name()
        outputs = context.structured_outputs()
        if status in {"completed", "completed_unverified"}:
            from nz_coder.runtime.verification.domain.models import candidate_fingerprint
            runtime.domain.set_completion_candidate(candidate_fingerprint(resolved_content, outputs.get(active_agent)))
        watermark = runtime.prepare_freeze()
        from nz_coder.runtime.core.result import normalize_run_status
        with runtime.run_context._state_lock:
            context.recovery.reset_tool_call_history(reason="terminal", context_changed=True)
            status = context.assert_terminal(status)
            snapshot = runtime.domain.snapshot()
            status = snapshot.assess_completion(status).status
            runtime.run_context.freeze_execution(normalize_run_status(status))
            snapshot = runtime.domain.snapshot()
            verification = snapshot.to_dict()
        runtime.finish_freeze(watermark)
        context.finish_lineage(status, messages)
        if status == "aborted" and not resolved_content:
            resolved_content = (
                f"Agent aborted after {context.recovery.consecutive_errors} "
                "consecutive errors"
            )
            content_source = "status"
        authored_content = str(runtime.run_context.metadata.get("authored_content") or "")
        terminal_error = None
        if status == "error":
            # 仅当前交互的宿主错误可以成为终态原因，不能捡起历史错误或原始异常文本。
            interaction = runtime.run_context.interaction_run_id
            for message in reversed(messages):
                if message.get("role") != "assistant" or message.get(INTERACTION_RUN_ID_KEY) != interaction:
                    continue
                value = message.get(ASSISTANT_ERROR_KEY) or {}
                terminal_error = public_error_from_wire(value.get("data", {}).get("public_error"))
                break
            terminal_error = terminal_error or to_public_error(None)
            if not resolved_content:
                resolved_content, content_source = terminal_error.message, "public_error"
        rejected = snapshot.latest_review is not None and not snapshot.latest_review.accepted
        if rejected:
            from nz_coder.runtime.verification.completion_gate import verification_appendix
            outputs.pop(active_agent, None)
            reason = resolved_content if resolved_content != authored_content else "Delivery review identified unresolved requirements."
            paths = sorted({fact.path for fact in snapshot.artifacts if fact.operation in {"create", "change", "delete"}})
            changes = "\n\n已记录的文件变更：" + ", ".join(paths) if paths else ""
            resolved_content = "本次交付尚未完成。\n\n" + reason + changes + "\n\n" + verification_appendix(verification)
        elif (status in {"completed", "completed_unverified"}
                and "verification_state" in verification
                and not context.output_schema_requested()):
            from nz_coder.runtime.verification.completion_gate import verification_appendix
            if status == "completed_unverified":
                resolved_content = ("本次执行已结束，验证尚未确认。\n\n" + verification_appendix(verification)
                    + "\n\n模型交付说明（未经独立确认）：\n" + resolved_content)
            else:
                resolved_content = (resolved_content.rstrip() + "\n\n" + verification_appendix(verification)).lstrip()
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
                "authored_content": authored_content,
                "errors": context.recovery.consecutive_errors,
                "last_error": context.recovery.last_error,
                **verification,
                "verification": verification,
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
                **context.verification_runtime.status(),
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
        elif resolved_content and on_text and (not stream or status == "blocked"):
            on_text(resolved_content)
        if status in {"completed", "completed_unverified"} and stream and on_token:
            if resolved_content:
                on_token(resolved_content)
            on_token(None)

        runtime = context.runtime_summary()
        state.last_status = {
            "status": status,
            "content": resolved_content,
            "authored_content": authored_content,
            "errors": (
                context.recovery.consecutive_errors if status == "max_turns" else 0
            ),
            **verification,
            "verification": verification,
            "runtime": runtime,
        }
        if terminal_error is not None:
            state.last_status["public_error"] = terminal_error.to_dict()
            state.last_status["last_error"] = terminal_error.message
        if active_agent in outputs and status in {"completed", "completed_unverified"} and not rejected:
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
    def _trace_terminal(
        context: LifecycleExecutionContext, messages: list,
    ) -> None:
        """记录冻结结果；对外终态由 Runner 在持久会话提交后发布。"""
        last_status = context.run_state.last_status
        status = str(last_status.get("status") or "completed")
        runtime = last_status.get("runtime", {})
        context.trace(
            "run_end", status=status, message_count=len(messages),
            runtime=runtime, **context.verification_runtime.status(),
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
