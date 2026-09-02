"""Regression coverage for atomic pre-commit policy failure settlement."""
from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

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


def _run_faulted_settlement(phase: str):
    calls = {
        "snapshot": 0,
        "part": 0,
        "policy": 0,
        "error": 0,
        "finish": 0,
        "checkpoint": 0,
    }
    failed = False

    def step(name):
        nonlocal failed
        calls[name] += 1
        if name == phase and not failed:
            failed = True
            raise RuntimeError(f"{name} failed once")

    async def checkpoint(*_args):
        step("checkpoint")

    processor = SimpleNamespace(
        settle_policy_failure=lambda _error: step("policy"),
        finish_step=lambda _reason: step("finish"),
    )
    context = SimpleNamespace(
        snapshots=SimpleNamespace(retire=lambda *_args: step("snapshot")),
        messages=SimpleNamespace(
            retire_message_part=lambda *_args: step("part"),
            publish_event=lambda *_args: None,
        ),
    )
    assistant = {"role": "assistant", "content": ""}
    settlement = commit_boundary.FailedAttemptSettlement()
    original_set_error = commit_boundary.set_assistant_error

    def attach(*args, **kwargs):
        step("error")
        return original_set_error(*args, **kwargs)

    kwargs = {
        "context": context,
        "services": SimpleNamespace(
            session_runtime=SimpleNamespace(checkpoint=checkpoint)
        ),
        "run_context": object(),
        "assistant_message": assistant,
        "processor": processor,
        "message_part": {},
        "public_error": PublicError(
            "guardrail_review_required",
            "Output requires policy review.",
            metadata={"hook_point": "tool"},
        ),
        "failure_kind": "tool_guardrail",
        "settlement": settlement,
    }

    async def exercise():
        with pytest.raises(RuntimeError, match="failed once"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.completed is False
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    with patch.object(commit_boundary, "set_assistant_error", attach):
        asyncio.run(exercise())
    assert settlement.completed is True
    assert calls[phase] == 2
    for name, count in calls.items():
        if name != phase:
            assert count == 1
    return settlement, calls


def test_settlement_recovers_when_snapshot_retire_fails_once():
    _run_faulted_settlement("snapshot")


def test_settlement_recovers_when_part_retire_fails_once():
    _run_faulted_settlement("part")


def test_settlement_recovers_when_policy_settle_fails_once():
    _run_faulted_settlement("policy")


def test_settlement_recovers_when_error_attachment_fails_once():
    _run_faulted_settlement("error")


def test_settlement_recovers_when_finish_step_fails_once():
    _run_faulted_settlement("finish")


def test_settlement_recovers_when_checkpoint_fails_once():
    _run_faulted_settlement("checkpoint")


def test_settlement_completed_only_after_all_phases():
    settlement, _calls = _run_faulted_settlement("checkpoint")
    assert all((
        settlement.snapshot_retired,
        settlement.part_retired,
        settlement.policy_parts_settled,
        settlement.error_attached,
        settlement.step_finished,
        settlement.checkpointed,
        settlement.completed,
    ))


def _after_side_effect_settlement(*, assistant=None, processor=None, retire=None):
    selected_assistant = assistant or {"role": "assistant", "content": ""}
    if "_nz_message_id" not in selected_assistant:
        attach_message_identity(
            selected_assistant,
            "msg-after-side-effect",
            session_id="session-policy",
        )
    selected_processor = processor or SessionProcessor(selected_assistant)
    if not any(
        part.get("type") == "step-start"
        for part in selected_assistant.get("_nz_parts", [])
    ):
        selected_processor.start_step()
    message_part = {
        "part_id": "part-after-side-effect",
        "retired": False,
    }
    retire_part = retire or (
        lambda part, _reason: part.__setitem__("retired", True)
    )
    settlement = commit_boundary.FailedAttemptSettlement()
    kwargs = {
        "context": SimpleNamespace(
            snapshots=SimpleNamespace(retire=lambda *_args: None),
            messages=SimpleNamespace(
                retire_message_part=retire_part,
                publish_event=lambda *_args: None,
            ),
        ),
        "services": SimpleNamespace(
            session_runtime=SimpleNamespace(
                checkpoint=lambda *_args: _record_async([], "error")
            )
        ),
        "run_context": object(),
        "assistant_message": selected_assistant,
        "processor": selected_processor,
        "message_part": message_part,
        "public_error": PublicError(
            "guardrail_review_required",
            "Output requires policy review.",
            metadata={"hook_point": "output"},
        ),
        "failure_kind": "output_guardrail",
        "settlement": settlement,
    }
    return kwargs, settlement, selected_assistant, message_part


def test_settlement_does_not_duplicate_after_publish_then_raise():
    published = []
    kwargs, settlement, assistant, _part = _after_side_effect_settlement()
    failed = False

    def publish(*args):
        nonlocal failed
        published.append(args)
        if not failed:
            failed = True
            raise RuntimeError("publish failed after accepting event")

    kwargs["context"].messages.publish_event = publish

    async def exercise():
        with pytest.raises(RuntimeError, match="after accepting"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.error_attached is True
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    assert len(published) == 1
    assert assistant["_nz_assistant_error"]["name"] == "OutputGuardrailError"


def test_finish_step_does_not_duplicate_after_mutate_then_raise():
    assistant = {"role": "assistant", "content": ""}
    attach_message_identity(
        assistant,
        "msg-finish-after-effect",
        session_id="session-policy",
    )
    real_processor = SessionProcessor(assistant)
    real_processor.start_step()
    failed = False

    class Processor:
        def settle_policy_failure(self, error):
            return real_processor.settle_policy_failure(error)

        def finish_step(self, reason):
            nonlocal failed
            result = real_processor.finish_step(reason)
            if not failed:
                failed = True
                raise RuntimeError("finish failed after mutation")
            return result

    kwargs, settlement, _assistant, _part = _after_side_effect_settlement(
        assistant=assistant,
        processor=Processor(),
    )

    async def exercise():
        with pytest.raises(RuntimeError, match="after mutation"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.step_finished is True
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    finishes = [
        part for part in assistant["_nz_parts"]
        if part.get("type") == "step-finish"
    ]
    assert len(finishes) == 1


def test_part_retirement_is_idempotent_after_partial_failure():
    calls = 0

    def retire(part, _reason):
        nonlocal calls
        calls += 1
        part["retired"] = True
        if calls == 1:
            raise RuntimeError("retire failed after mutation")

    kwargs, settlement, _assistant, part = _after_side_effect_settlement(
        retire=retire,
    )

    async def exercise():
        with pytest.raises(RuntimeError, match="after mutation"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.part_retired is True
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    assert part["retired"] is True
    assert calls == 1


def test_policy_settle_after_publish_then_raise_does_not_duplicate():
    kwargs, settlement, assistant, _part = _after_side_effect_settlement()
    real_processor = kwargs["processor"]
    calls = 0

    class Processor:
        def settle_policy_failure(self, error):
            nonlocal calls
            calls += 1
            real_processor.settle_policy_failure(error)
            raise RuntimeError("policy publish failed after mutation")

        def finish_step(self, reason):
            return real_processor.finish_step(reason)

    kwargs["processor"] = Processor()

    async def exercise():
        with pytest.raises(RuntimeError, match="after mutation"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.policy_parts_settled is True
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    assert calls == 1
    assert assistant["_nz_policy_settlement"]["error_code"] == (
        "guardrail_review_required"
    )


def test_checkpoint_commit_then_raise_is_idempotent():
    kwargs, settlement, _assistant, _part = _after_side_effect_settlement()
    run_context = SimpleNamespace(metadata={})
    calls = 0

    class Runtime:
        async def checkpoint(self, context, _status):
            nonlocal calls
            calls += 1
            marker = context.metadata["_nz_active_checkpoint_marker"]
            context.metadata.setdefault("_nz_checkpoint_commits", []).append(marker)
            raise RuntimeError("checkpoint failed after durable commit")

        async def checkpoint_committed(self, context, marker):
            return marker in context.metadata.get("_nz_checkpoint_commits", [])

    kwargs["run_context"] = run_context
    kwargs["services"].session_runtime = Runtime()

    async def exercise():
        with pytest.raises(RuntimeError, match="after durable commit"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.checkpointed is True
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    assert calls == 1


def test_snapshot_retire_after_signal_then_raise_is_idempotent():
    kwargs, settlement, _assistant, _part = _after_side_effect_settlement()
    cancel = threading.Event()
    calls = 0

    def retire(_task, signal):
        nonlocal calls
        calls += 1
        signal.set()
        raise RuntimeError("snapshot retire failed after signal")

    kwargs["context"].snapshots.retire = retire
    kwargs["snapshot_cancel"] = cancel

    async def exercise():
        with pytest.raises(RuntimeError, match="after signal"):
            await commit_boundary.settle_failed_attempt(**kwargs)
        assert settlement.snapshot_retired is True
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    assert calls == 1


def test_all_settlement_phases_have_recoverable_postconditions():
    assistant = {"role": "assistant", "content": "unapproved"}
    attach_message_identity(
        assistant,
        "msg-all-postconditions",
        session_id="session-policy",
    )
    real_processor = SessionProcessor(assistant)
    real_processor.start_step()
    counters = {
        "snapshot": 0,
        "part": 0,
        "policy": 0,
        "error": 0,
        "finish": 0,
        "checkpoint": 0,
    }
    cancel = threading.Event()
    message_part = {"retired": False}
    run_context = SimpleNamespace(metadata={})

    class Processor:
        def settle_policy_failure(self, error):
            counters["policy"] += 1
            real_processor.settle_policy_failure(error)
            raise RuntimeError("policy after effect")

        def finish_step(self, reason):
            counters["finish"] += 1
            real_processor.finish_step(reason)
            raise RuntimeError("finish after effect")

    class Runtime:
        async def checkpoint(self, context, _status):
            counters["checkpoint"] += 1
            marker = context.metadata["_nz_active_checkpoint_marker"]
            context.metadata.setdefault("_nz_checkpoint_commits", []).append(marker)
            raise RuntimeError("checkpoint after effect")

        async def checkpoint_committed(self, context, marker):
            return marker in context.metadata.get("_nz_checkpoint_commits", [])

    def retire_snapshot(_task, signal):
        counters["snapshot"] += 1
        signal.set()
        raise RuntimeError("snapshot after effect")

    def retire_part(part, _reason):
        counters["part"] += 1
        part["retired"] = True
        raise RuntimeError("part after effect")

    def publish(*_args):
        counters["error"] += 1
        raise RuntimeError("error after effect")

    settlement = commit_boundary.FailedAttemptSettlement()
    kwargs = {
        "context": SimpleNamespace(
            snapshots=SimpleNamespace(retire=retire_snapshot),
            messages=SimpleNamespace(
                retire_message_part=retire_part,
                publish_event=publish,
            ),
        ),
        "services": SimpleNamespace(session_runtime=Runtime()),
        "run_context": run_context,
        "assistant_message": assistant,
        "processor": Processor(),
        "message_part": message_part,
        "public_error": PublicError(
            "guardrail_review_required",
            "Output requires policy review.",
            metadata={"hook_point": "output"},
        ),
        "failure_kind": "output_guardrail",
        "settlement": settlement,
        "snapshot_cancel": cancel,
    }

    async def exercise():
        for _index in range(6):
            try:
                await commit_boundary.settle_failed_attempt(**kwargs)
            except RuntimeError:
                continue
        assert await commit_boundary.settle_failed_attempt(**kwargs) is True

    asyncio.run(exercise())
    assert settlement.completed is True
    assert all(count == 1 for count in counters.values())


def test_concurrent_settlement_calls_do_not_duplicate_parts():
    calls = []

    async def checkpoint(*_args):
        calls.append("checkpoint")
        await asyncio.sleep(0)

    settlement = commit_boundary.FailedAttemptSettlement()
    kwargs = {
        "context": SimpleNamespace(
            snapshots=SimpleNamespace(
                retire=lambda *_args: calls.append("snapshot")
            ),
            messages=SimpleNamespace(
                retire_message_part=lambda *_args: calls.append("part"),
                publish_event=lambda *_args: None,
            ),
        ),
        "services": SimpleNamespace(
            session_runtime=SimpleNamespace(checkpoint=checkpoint)
        ),
        "run_context": object(),
        "assistant_message": {"role": "assistant", "content": ""},
        "processor": SimpleNamespace(
            settle_policy_failure=lambda _error: calls.append("policy"),
            finish_step=lambda _reason: calls.append("finish"),
        ),
        "message_part": {},
        "public_error": PublicError("internal_error", "Request failed."),
        "failure_kind": "policy",
        "settlement": settlement,
    }

    async def exercise():
        return await asyncio.gather(
            commit_boundary.settle_failed_attempt(**kwargs),
            commit_boundary.settle_failed_attempt(**kwargs),
        )

    assert asyncio.run(exercise()) == [True, False]
    assert calls.count("part") == 1
    assert calls.count("finish") == 1
    assert calls.count("checkpoint") == 1


def test_original_policy_error_survives_secondary_settlement_failure(tmp_path):
    active: dict[str, object] = {}
    retired: list[str] = []
    tools = _EscalatingTools()
    services = RuntimeServices(
        model=_EscalatingModel(active),
        tools=tools,
        context=_Context(),
        session_runtime=_CheckpointSessions(),
        events=_RecordingEvents(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    base_factory = _policy_execution_context(active, retired)
    attempts = []

    def execution_context(run_context, runtime_services):
        context = base_factory(run_context, runtime_services)

        def fail_retire(*_args):
            attempts.append("retire")
            raise RuntimeError("SECONDARY-SETTLEMENT-SECRET")

        return replace(
            context,
            snapshots=SimpleNamespace(
                **{
                    **vars(context.snapshots),
                    "retire": fail_retire,
                }
            ),
        )

    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="use one tool"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "write"},),
        workspace=tmp_path,
        session_id="policy-settlement-secondary",
        stream=True,
    )

    with pytest.raises(PublicRuntimeError) as raised:
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=execution_context,
        ).run(request, options=RunOptions(stream=True)))

    assert raised.value.public_error.code == "guardrail_review_required"
    assert attempts == ["retire", "retire"]
