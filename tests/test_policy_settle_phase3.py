"""Regression coverage for atomic pre-commit policy failure settlement."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.protocol.message_schema import attach_message_identity
from nz_coder.protocol.public_error import (
    PublicError,
    PublicRuntimeError,
    to_public_error,
)
from nz_coder.runtime.agent.guardrails import GuardrailEscalateError
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.core.events import RuntimeEventName
from nz_coder.runtime.execution import commit_boundary
from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.session.session_processor import SessionProcessor
from tests.runtime.test_native_runner import (
    _Context,
    _Events,
    _Guardrails,
    _Inputs,
    _Lifecycle,
    _Memory,
    _Sessions,
    _Transitions,
    _UnusedHost,
    _Verifier,
    _execution_context,
)


_PRIVATE_REASON = (
    "Authorization=Bearer SECRET-123 body=PRIVATE-PROMPT requires review"
)


class _EscalatingModel:
    """Simulate a flushed text delta followed by an unapproved tool envelope."""

    def __init__(self, active: dict[str, object]) -> None:
        self.active = active

    async def complete_turn(self, _context, _messages, **kwargs):
        processor = self.active["processor"]
        part = kwargs["message_part"]
        processor.stream_text(
            "unapproved streaming text",
            part_id=part["part_id"],
            run_id=part["run_id"],
            attempt_id=part["attempt_id"],
            generation_id=part["generation_id"],
            generation=part["generation"],
            version=1,
        )
        return LLMResult(
            content="unapproved streaming text",
            tool_calls=[{
                "id": "call-escalate",
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": '{"path":"private.py","content":"secret"}',
                },
            }],
            finish_reason="tool_calls",
        )


class _EscalatingTools:
    def __init__(self) -> None:
        self.executions = 0

    async def approve_tool_calls_async(self, _context, _calls, _messages):
        raise GuardrailEscalateError("reviewer", "tool", _PRIVATE_REASON)

    async def execute_batch_async(self, *_args, **_kwargs):
        self.executions += 1
        raise AssertionError("an escalated tool batch must not execute")


class _CheckpointSessions(_Sessions):
    def __init__(self) -> None:
        super().__init__()
        self.checkpoints: list[str] = []

    async def checkpoint(self, _context, status) -> None:
        self.checkpoints.append(str(getattr(status, "value", status)))


class _RecordingEvents(_Events):
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


def _policy_execution_context(active: dict[str, object], retired: list[str]):
    def factory(run_context, services):
        context = _execution_context(run_context, services)
        sequence = 0

        def new_message_part(_turn: int) -> dict:
            nonlocal sequence
            sequence += 1
            return {
                "message_id": f"msg-policy-{sequence}",
                "part_id": f"part-policy-{sequence}",
                "run_id": "run-policy",
                "interaction_run_id": run_context.interaction_run_id,
                "attempt_id": f"attempt-policy-{sequence}",
                "generation_id": f"generation-policy-{sequence}",
                "generation": 1,
                "retired": False,
            }

        def bind_active_processor(processor, _messages) -> None:
            if processor is None:
                active.pop("processor", None)
            else:
                active["processor"] = processor

        def retire_message_part(part: dict, reason: str) -> None:
            if part.get("retired"):
                return
            part["retired"] = True
            part["generation"] += 1
            retired.append(reason)
            processor = active.get("processor")
            if processor is not None:
                processor.remove_part(part["part_id"], reason)

        return replace(
            context,
            messages=SimpleNamespace(
                **{
                    **vars(context.messages),
                    "new_message_part": new_message_part,
                    "bind_active_processor": bind_active_processor,
                    "retire_message_part": retire_message_part,
                }
            ),
        )

    return factory


def _exercise_tool_escalation(tmp_path):
    active: dict[str, object] = {}
    retired: list[str] = []
    sessions = _CheckpointSessions()
    events = _RecordingEvents()
    tools = _EscalatingTools()
    services = RuntimeServices(
        model=_EscalatingModel(active),
        tools=tools,
        context=_Context(),
        session_runtime=sessions,
        events=events,
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="use one tool"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "write"},),
        workspace=tmp_path,
        session_id="policy-escalation",
        stream=True,
    )

    with pytest.raises(PublicRuntimeError) as raised:
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=_policy_execution_context(active, retired),
        ).run(request, options=RunOptions(stream=True)))
    assert raised.value.__cause__ is None

    assistant = next(
        message
        for message in reversed(sessions.context.transcript)
        if message.get("role") == "assistant"
    )
    return assistant, sessions, events, tools, retired


def test_tool_guardrail_escalation_settles_assistant_step(tmp_path):
    assistant, *_rest = _exercise_tool_escalation(tmp_path)

    assert assistant["_nz_finish"] == "blocked"
    assert assistant["_nz_assistant_error"]["name"] == "ToolGuardrailError"


def test_tool_guardrail_escalation_adds_step_finish(tmp_path):
    assistant, *_rest = _exercise_tool_escalation(tmp_path)

    finishes = [
        part for part in assistant["_nz_parts"]
        if part.get("type") == "step-finish"
    ]
    assert len(finishes) == 1
    assert finishes[0]["reason"] == "blocked"


def test_tool_guardrail_escalation_leaves_no_streaming_text_part(tmp_path):
    assistant, *_rest = _exercise_tool_escalation(tmp_path)

    assert "unapproved streaming text" not in repr(assistant)


def test_tool_guardrail_escalation_leaves_no_pending_tool_part(tmp_path):
    assistant, *_rest = _exercise_tool_escalation(tmp_path)

    assert not any(
        part.get("type") == "tool"
        and part.get("state", {}).get("status") in {"pending", "running"}
        for part in assistant.get("_nz_parts", [])
    )


def test_tool_guardrail_escalation_checkpoints_once(tmp_path):
    _assistant, sessions, _events, _tools, _retired = _exercise_tool_escalation(
        tmp_path
    )

    assert sessions.checkpoints.count("error") == 1


def test_tool_guardrail_escalation_publishes_single_terminal_event(tmp_path):
    _assistant, _sessions, events, _tools, _retired = _exercise_tool_escalation(
        tmp_path
    )
    failures = [
        event for event in events.events
        if event.name is RuntimeEventName.RUN_FAILED
    ]

    assert len(failures) == 1


def test_tool_guardrail_escalation_public_error_has_no_reason(tmp_path):
    assistant, _sessions, events, tools, retired = _exercise_tool_escalation(
        tmp_path
    )
    public = assistant["_nz_assistant_error"]["data"]["public_error"]

    assert public == to_public_error(
        GuardrailEscalateError("reviewer", "tool", "different private reason")
    ).to_dict()
    assert all(
        marker not in repr((assistant, [event.payload for event in events.events]))
        for marker in ("SECRET-123", "PRIVATE-PROMPT", "Authorization")
    )
    assert tools.executions == 0
    assert retired == ["tool_guardrail_failed"]


def test_settle_failed_attempt_is_idempotent():
    assistant = {"role": "assistant", "content": "unapproved"}
    attach_message_identity(assistant, "msg-idempotent", session_id="session-policy")
    processor = SessionProcessor(assistant)
    processor.start_step()
    processor.stream_text("unapproved", part_id="part-unapproved")
    retired = []
    checkpoints = []
    context = SimpleNamespace(
        snapshots=SimpleNamespace(retire=lambda *_args: retired.append("snapshot")),
        messages=SimpleNamespace(
            retire_message_part=lambda *_args: retired.append("part"),
            publish_event=lambda *_args: None,
        ),
    )
    services = SimpleNamespace(
        session_runtime=SimpleNamespace(
            checkpoint=lambda *_args: _record_async(checkpoints, "error")
        )
    )
    settlement = commit_boundary.FailedAttemptSettlement()

    async def settle_twice():
        kwargs = {
            "context": context,
            "services": services,
            "run_context": object(),
            "assistant_message": assistant,
            "processor": processor,
            "message_part": {"part_id": "part-unapproved"},
            "public_error": PublicError(
                "guardrail_review_required",
                "Output requires policy review.",
                metadata={"hook_point": "tool"},
            ),
            "failure_kind": "tool_guardrail",
            "settlement": settlement,
        }
        first = await commit_boundary.settle_failed_attempt(**kwargs)
        second = await commit_boundary.settle_failed_attempt(**kwargs)
        return first, second

    assert asyncio.run(settle_twice()) == (True, False)
    assert retired == ["snapshot", "part"]
    assert checkpoints == ["error"]
    assert len([
        part for part in assistant["_nz_parts"]
        if part.get("type") == "step-finish"
    ]) == 1


async def _record_async(values: list[str], value: str) -> None:
    values.append(value)
