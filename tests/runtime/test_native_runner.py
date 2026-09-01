"""Behavioral contract for running the core without constructing AgentLoop."""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core import request as request_contracts
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.execution.runner import _request_max_turns
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.agent.task_contract import derive_task_contract
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.verification.verification_contract import VerificationContract
from nz_coder.runtime.execution.work_budget import WorkBudgetController


class _Model:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return LLMResult(
                content="",
                tool_calls=[{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_value", "arguments": "{}"},
                }],
                finish_reason="tool_calls",
                input_tokens=5,
                output_tokens=1,
            )
        return LLMResult(
            content="value is 42",
            finish_reason="stop",
            input_tokens=8,
            output_tokens=3,
        )


class _FailingModel:
    async def complete_turn(self, _context, _messages, **_kwargs):
        raise RuntimeError("provider exploded")


class _CancelledModel:
    async def complete_turn(self, _context, _messages, **_kwargs):
        raise asyncio.CancelledError


class _EmptyThenAnswerModel:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def complete_turn(self, _context, messages, **_kwargs):
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        if self.calls == 1:
            return LLMResult(
                content="",
                extra={"reasoning_content": "private reasoning only"},
                finish_reason="stop",
                input_tokens=2,
                output_tokens=1,
            )
        return LLMResult(
            content="visible answer",
            finish_reason="stop",
            input_tokens=3,
            output_tokens=1,
        )


class _AlwaysEmptyModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.calls += 1
        return LLMResult(content="", finish_reason="stop")


class _AlwaysLengthModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.calls += 1
        return LLMResult(
            content=f"partial-{self.calls}",
            finish_reason="length",
            input_tokens=1,
            output_tokens=1,
        )


class _MalformedUsageModel:
    async def complete_turn(self, _context, _messages, **_kwargs):
        return LLMResult(
            content="answer survives invalid metrics",
            finish_reason="stop",
            input_tokens=float("nan"),
            output_tokens=-4,
            total_tokens=float("inf"),
            reasoning_tokens=True,
            cache_read_tokens="9",
            cache_write_tokens=2.0,
            duration_ms=float("nan"),
            first_token_ms=float("inf"),
            attempts=True,
            provider_reported_cost=float("nan"),
            cost=float("inf"),
            cost_known=True,
        )


class _BudgetModel:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages = []

    async def complete_turn(self, _context, messages, **_kwargs):
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        if self.calls < 4:
            return LLMResult(
                content="",
                tool_calls=[{
                    "id": f"call-{self.calls}",
                    "type": "function",
                    "function": {"name": "read_value", "arguments": "{}"},
                }],
                finish_reason="tool_calls",
                input_tokens=5,
                output_tokens=1,
            )
        return LLMResult(
            content="finished",
            finish_reason="stop",
            input_tokens=5,
            output_tokens=1,
        )


class _EarlyStopModel:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages = []

    async def complete_turn(self, _context, messages, **_kwargs):
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        if self.calls == 1:
            return LLMResult(
                content="",
                tool_calls=[{
                    "id": "call-early-read",
                    "type": "function",
                    "function": {"name": "read_value", "arguments": "{}"},
                }],
                finish_reason="tool_calls",
                input_tokens=5,
                output_tokens=1,
            )
        return LLMResult(
            content="finished",
            finish_reason="stop",
            input_tokens=5,
            output_tokens=1,
        )


class _IdleYieldModel:
    def __init__(self) -> None:
        self.calls = 0
        self.seen_messages: list[list[dict]] = []

    async def complete_turn(self, _context, messages, **_kwargs):
        self.calls += 1
        self.seen_messages.append([dict(message) for message in messages])
        return LLMResult(
            content="waiting for child" if self.calls == 1 else "child incorporated",
            finish_reason="stop",
            input_tokens=2,
            output_tokens=1,
        )


class _AlwaysToolModel:
    def __init__(self) -> None:
        self.calls = 0

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.calls += 1
        return LLMResult(
            content="",
            tool_calls=[{
                "id": f"call-{self.calls}",
                "type": "function",
                "function": {"name": "read_value", "arguments": "{}"},
            }],
            finish_reason="tool_calls",
            input_tokens=1,
            output_tokens=1,
        )


class _ReviewRepairModel:
    """Reach the nominal boundary, then use one review-authorized repair turn."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.calls += 1
        if self.calls <= 15:
            return LLMResult(
                content="",
                tool_calls=[{
                    "id": f"call-{self.calls}",
                    "type": "function",
                    "function": {"name": "read_value", "arguments": "{}"},
                }],
                finish_reason="tool_calls",
                input_tokens=1,
                output_tokens=1,
            )
        return LLMResult(
            content="review feedback addressed",
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
        )


class _Tools:
    async def execute_batch_async(
        self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
    ):
        processor = kwargs["processor"]
        processor.start_tools(calls)
        processor.complete_tool("call-1", "42")
        messages.append({
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "42",
        })
        processor.finish_step("tool-calls")
        return "continue"


class _BudgetTools:
    async def execute_batch_async(
        self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
    ):
        processor = kwargs["processor"]
        processor.start_tools(calls)
        for call in calls:
            call_id = call["id"]
            processor.complete_tool(call_id, "42")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": "42",
            })
        processor.finish_step("tool-calls")
        return "continue"


class _VerificationTools:
    def __init__(self) -> None:
        self.names: list[str] = []

    async def execute_batch_async(
        self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
    ):
        processor = kwargs["processor"]
        processor.start_tools(calls)
        for call in calls:
            call_id = call["id"]
            name = call["function"]["name"]
            self.names.append(name)
            output = "1 passed" if name == "bash" else "42"
            processor.complete_tool(call_id, output)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": output,
            })
        processor.finish_step("tool-calls")
        return "continue"


class _Context:
    async def prepare_async(self, _context, _messages, **_kwargs) -> bool:
        return False


class _Sessions:
    def __init__(self) -> None:
        self.context: RunContext | None = None
        self.final_statuses: list[RunStatus] = []

    async def open(self, request: RunRequest) -> RunContext:
        session = Session.create(
            request.session_id,
            request.messages,
            workspace=request.workspace,
        )
        session.begin_run()
        self.context = RunContext(request, session, request.agent.name)
        return self.context

    async def checkpoint(self, _context, _status) -> None:
        return None

    async def finalize(self, context, status) -> None:
        self.final_statuses.append(status)
        context.finish(status)
        context.finalized = True


class _Events:
    def publish(self, _owner, _event_type, _payload) -> None:
        return None


class _RecordingEvents:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


class _OrderedSessions(_Sessions):
    def __init__(self, timeline: list[str]) -> None:
        super().__init__()
        self.timeline = timeline

    async def finalize(self, context, status) -> None:
        await super().finalize(context, status)
        self.timeline.append(f"session.finalized:{status.value}")


class _OrderedEvents:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline

    def publish(self, event) -> None:
        self.timeline.append(str(getattr(event.name, "value", event.name)))


class _FailBeforeRunMiddleware:
    async def before_run(self, _context) -> None:
        raise RuntimeError("middleware entry exploded")


class _FailAfterRunMiddleware:
    async def after_run(self, _context, _result) -> None:
        raise RuntimeError("middleware exit exploded")


class _InterruptBeforeRunMiddleware:
    async def before_run(self, _context) -> None:
        raise KeyboardInterrupt


class _UnusedHost:
    async def run(self, *_args, **_kwargs):
        raise AssertionError("native Runner must not enter RuntimeHost")


class _Memory:
    def prompt_block(self, _owner, _query):
        return ""

    async def finalize(self, _owner, _messages, _status) -> None:
        return None


class _Verifier:
    async def verify(self, _owner, _messages, status, _content):
        return status


class _Lifecycle:
    def initialize(self, _context, _messages, _stream):
        return 3, 0

    async def finalize(self, _context, _messages, status, **kwargs):
        return {"status": status, "content": kwargs.get("content_text", "")}

    def finalize_sync(self, _context, _messages, status, **_kwargs):
        return {"status": status}


class _BlockedLifecycle(_Lifecycle):
    async def finalize(self, _context, _messages, _status, **kwargs):
        return {"status": "blocked", "content": kwargs.get("content_text", "")}


class _FourTurnLifecycle(_Lifecycle):
    def initialize(self, _context, _messages, _stream):
        return 4, 0


class _ConfiguredTurnLifecycle(_Lifecycle):
    def __init__(self) -> None:
        self.observed_max_turns = None

    def initialize(self, _context, _messages, _stream):
        from nz_coder.runtime.core.execution_context import max_agent_turns

        self.observed_max_turns = max_agent_turns()
        return 1, 0


class _DefaultTurnLifecycle(_Lifecycle):
    def initialize(self, _context, _messages, _stream):
        from nz_coder.runtime.core.execution_context import max_agent_turns

        return max_agent_turns(), 0


class _OneTurnLifecycle(_Lifecycle):
    def initialize(self, _context, _messages, _stream):
        return 1, 0


class _ScopeMutatingLifecycle(_Lifecycle):
    def initialize(self, _context, _messages, _stream):
        from nz_coder.runtime.core.execution_context import (
            set_broad_tests_blocked,
            set_declared_test_scopes,
        )

        set_broad_tests_blocked(True)
        set_declared_test_scopes(("inner/tests",))
        return 1, 0


class _TerminalEditModel:
    def __init__(self, *, inline_tools: bool = False, content: str = "") -> None:
        self.calls = 0
        self.inline_tools = inline_tools
        self.content = content

    async def complete_turn(self, _context, _messages, **kwargs):
        self.calls += 1
        result = LLMResult(
            content=self.content,
            tool_calls=[{
                "id": "terminal-edit",
                "type": "function",
                "function": {
                    "name": "edit_file",
                    "arguments": json.dumps({"path": "app.py"}),
                },
            }],
            finish_reason="tool_calls",
            input_tokens=1,
            output_tokens=1,
        )
        if self.inline_tools:
            result.tool_outcome = await kwargs["stream_tool_handler"](result)
            result.tools_executed_in_stream = True
        return result


class _TerminalBoundaryTools:
    def __init__(self, state: RuntimeState, *, acceptance_passed: bool) -> None:
        self.state = state
        self.acceptance_passed = acceptance_passed
        self.names: list[str] = []

    async def execute_batch_async(
        self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
    ):
        processor = kwargs["processor"]
        processor.start_tools(calls)
        for call in calls:
            call_id = call["id"]
            name = call["function"]["name"]
            self.names.append(name)
            arguments = json.loads(call["function"].get("arguments") or "{}")
            if name == "edit_file":
                output = "Updated app.py"
                self.state.observe_tool(name, arguments, output, succeeded=True)
                self.state.has_diff = True
                self.state.changed_files = ["app.py"]
                self.state.diff_generation = self.state.mutation_generation
            else:
                output = (
                    "1 passed"
                    if self.acceptance_passed
                    else "Command exited with code 1\n1 failed"
                )
            processor.complete_tool(call_id, output)
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": output,
            })
        processor.finish_step("tool-calls")
        return "continue"


class _Guardrails:
    async def run_input(self, _owner, _messages):
        return None

    def has(self, _owner, _kind):
        return False

    async def run_output(self, _owner, content, _messages):
        return content

    async def before_tool(self, _owner, tool_call, _messages):
        return tool_call, None

    async def after_tool(self, _owner, _tool_call, result, _messages):
        return result


class _Inputs:
    async def prepare_user_images(self, _owner, _messages, _target):
        return "skipped"

    async def prepare_user_documents(self, _owner, _messages, _target):
        return "skipped"

    async def describe_read_results(self, _owner, _results, _messages):
        return False


class _Transitions:
    def signal_from_metadata(self, _owner, _metadata):
        return None

    def apply(self, _owner, _signal, _messages, _processor):
        return None

    def resolve_structured_output(self, _owner, _content, _messages):
        return False

    def return_from_as_tool(self, _owner, _messages, _summary=""):
        return {}

    async def terminal_content(self, _owner, fallback, _messages):
        return fallback


def _execution_context(run_context: RunContext, services: RuntimeServices):
    sequence = 0

    def new_message_part(_turn: int) -> dict:
        nonlocal sequence
        sequence += 1
        return {"message_id": f"msg-native-{sequence}"}

    def materialize(result, *, assistant_message, processor, **_kwargs) -> None:
        assistant_message["content"] = result.content or ""
        if result.tool_calls:
            assistant_message["tool_calls"] = result.tool_calls
            processor.register_tool_calls(result.tool_calls)
        if result.content:
            processor.stream_text(result.content, part_id=f"part-text-{sequence}")

    async def finalize(messages, status, *_args, **kwargs):
        return await services.lifecycle.finalize(
            run_context,
            messages,
            status,
            **kwargs,
        )

    return RunnerExecutionContext(
        session_id=run_context.session.session_id,
        runtime_state=run_context,
        execution=SimpleNamespace(
            context=lambda: object(),
            model=lambda: object(),
            tools=lambda: object(),
        ),
        lifecycle=SimpleNamespace(
            initialize=lambda messages, stream: services.lifecycle.initialize(
                run_context, messages, stream,
            ),
            finalize=finalize,
        ),
        policy=SimpleNamespace(
            run_input_guardrails=lambda _messages: _async_none(),
            has_output_guardrail=lambda: False,
            run_output_guardrail=lambda content, _messages: _async_value(content),
            prepare_user_images=lambda _messages, _owner: _async_value("skipped"),
            prepare_user_documents=lambda _messages, _owner: _async_value("skipped"),
            resolve_structured_output=lambda _content, _messages: False,
            return_from_as_tool=lambda _messages, _content: {},
            terminal_content=lambda content, _messages: _async_value(content),
            verify_completion=lambda _messages, status, _content: _async_value(status),
        ),
        planning=SimpleNamespace(
            generate=lambda _messages: _async_none(),
            replan=lambda: _async_none(),
        ),
        control=SimpleNamespace(
            has_queued_followup=lambda: False,
            drain_background_messages=lambda _messages: None,
            has_agent_call_stack=lambda: False,
            notify_agent_switched=lambda _transition: _async_none(),
            persist_runtime_state=lambda **_kwargs: None,
            stop_hook_reason=lambda: "",
        ),
        hooks=SimpleNamespace(
            on_turn_start=lambda _messages: None,
            on_pre_send=lambda _messages: None,
            on_turn_end=lambda _messages, _status: None,
            trace=lambda *_args, **_kwargs: None,
        ),
        messages=SimpleNamespace(
            persist_compaction_exhaustion=lambda *_args, **_kwargs: None,
            bind_assistant_context=lambda _message: None,
            bind_user_contexts=lambda _messages: None,
            new_message_part=new_message_part,
            publish_event=lambda *_args, **_kwargs: None,
            materialize_llm_result=materialize,
            reconcile_llm_result=materialize,
            retire_message_part=lambda *_args, **_kwargs: None,
            bind_active_processor=lambda *_args: None,
            build_api_messages=lambda messages: list(messages),
            apply_usage_cost=lambda _result: None,
            observe_llm_result=lambda *_args, **_kwargs: None,
            compact_messages=lambda messages, **_kwargs: messages,
            stamp_auto_compaction=lambda _messages: None,
            inject_api_diagnostic=lambda *_args: None,
        ),
        snapshots=SimpleNamespace(
            capture=lambda *_args, **_kwargs: None,
            await_start=lambda _task, _cancel: _async_value(None),
            retire=lambda *_args: None,
            capture_async=lambda *_args: _async_value(None),
            record_patch=lambda *_args: None,
        ),
    )


async def _async_none() -> None:
    return None


async def _async_value(value):
    return value


def test_native_runner_completes_model_tool_model_without_agent_loop(tmp_path: Path):
    RunOptions = getattr(request_contracts, "RunOptions", None)
    assert RunOptions is not None, "native RunOptions contract is missing"
    model = _Model()
    sessions = _Sessions()
    services = RuntimeServices(
        model=model,
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer with tools"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "read the value"},),
        workspace=tmp_path,
        session_id="native-session",
        tool_names=("read_value",),
        stream=False,
    )
    runner = AgentRunner(
        services,
        execution_context_factory=_execution_context,
    )

    result = asyncio.run(runner.run(request, options=RunOptions(stream=False)))

    assert result == {"status": "completed", "content": "value is 42"}
    assert model.calls == 2
    assert sessions.context is not None
    assert [message["role"] for message in sessions.context.transcript] == [
        "user", "assistant", "tool", "assistant",
    ]
    assert sessions.context.transcript[2]["content"] == "42"
    assert sessions.context.transcript[-1]["content"] == "value is 42"
    assert sessions.context.terminal_status is RunStatus.COMPLETED
    assert sessions.context.usage.input_tokens == 13
    assert sessions.context.usage.output_tokens == 4
    assert sessions.final_statuses == [RunStatus.COMPLETED]


def test_native_runner_sanitizes_invalid_model_metrics_without_losing_answer(
    tmp_path: Path,
):
    """A custom ModelPort cannot crash the run or poison typed usage with NaN."""
    RunOptions = getattr(request_contracts, "RunOptions", None)
    sessions = _Sessions()
    services = RuntimeServices(
        model=_MalformedUsageModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "answer"},),
        workspace=tmp_path,
        session_id="native-invalid-model-metrics",
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run(request, options=RunOptions(stream=False)))

    assert result == {
        "status": "completed",
        "content": "answer survives invalid metrics",
    }
    assert sessions.context is not None
    assert sessions.context.usage.input_tokens == 0
    assert sessions.context.usage.output_tokens == 0
    assert sessions.context.usage.cached_read_tokens == 0
    assert sessions.context.usage.cached_write_tokens == 2
    assert sessions.context.usage.reasoning_tokens == 0


def test_native_runner_recovers_one_empty_assistant_completion(tmp_path: Path):
    """Reasoning-only/empty 2xx responses get one bounded visible retry."""
    model = _EmptyThenAnswerModel()
    sessions = _Sessions()
    services = RuntimeServices(
        model=model,
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer visibly"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "explain the result"},),
        workspace=tmp_path,
        session_id="native-empty-recovery",
        tool_names=(),
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert result == {"status": "completed", "content": "visible answer"}
    assert model.calls == 2
    assert any(
        message.get("_nz_empty_completion_retry") is True
        for message in sessions.context.transcript
    )
    assert any(
        "no visible text" in str(message.get("content") or "")
        for message in model.seen_messages[1]
    )


def test_native_runner_bounds_repeated_empty_assistant_completion(tmp_path: Path):
    """A persistently empty Provider cannot consume the whole turn budget."""
    model = _AlwaysEmptyModel()
    sessions = _Sessions()
    services = RuntimeServices(
        model=model,
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer visibly"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "explain the result"},),
        workspace=tmp_path,
        session_id="native-empty-bounded",
        tool_names=(),
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert result["status"] == "error"
    assert "empty response" in result["content"].lower()
    assert model.calls == 2


def test_native_runner_bounds_repeated_output_limit_continuations(tmp_path: Path):
    """Repeated max_tokens finishes stop honestly after two continuations."""
    model = _AlwaysLengthModel()
    sessions = _Sessions()
    services = RuntimeServices(
        model=model,
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer visibly"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "write a long answer"},),
        workspace=tmp_path,
        session_id="native-length-bounded",
        tool_names=(),
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert result["status"] == "error"
    assert "recovery was exhausted" in result["content"]
    assert model.calls == 3
    assert sum(
        message.get("_nz_output_limit_continuation") is True
        for message in sessions.context.transcript
    ) == 2


def test_output_guardrail_applies_to_length_result(tmp_path: Path):
    """A truncated Provider segment is policy checked before publication."""
    class LengthThenStop:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_turn(self, _context, _messages, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return LLMResult(content="length-secret", finish_reason="length")
            return LLMResult(content="final", finish_reason="stop")

    observed: list[str] = []
    sessions = _Sessions()
    services = RuntimeServices(
        model=LengthThenStop(), tools=_Tools(), context=_Context(),
        session_runtime=sessions, events=_Events(), host=_UnusedHost(),
        memory=_Memory(), verifier=_Verifier(), lifecycle=_Lifecycle(),
        guardrails=_Guardrails(), inputs=_Inputs(), transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "answer"},),
        workspace=tmp_path,
        session_id="native-length-guarded",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        context = _execution_context(run_context, runtime_services)

        async def guard(content, _messages):
            observed.append(content)
            return content.replace("length-secret", "length-safe")

        return replace(context, policy=SimpleNamespace(
            **{
                **vars(context.policy),
                "has_output_guardrail": lambda: True,
                "run_output_guardrail": guard,
            }
        ))

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert result["status"] == "completed"
    assert observed == ["length-secret", "final"]
    assert "length-secret" not in repr(sessions.context.transcript)


def test_output_guardrail_applies_to_error_partial_content(tmp_path: Path):
    """Provider error partial text cannot bypass output policy."""
    class ErrorPartial:
        async def complete_turn(self, _context, _messages, **_kwargs):
            return LLMResult(content="error-secret", finish_reason="error")

    observed: list[str] = []
    sessions = _Sessions()
    services = RuntimeServices(
        model=ErrorPartial(), tools=_Tools(), context=_Context(),
        session_runtime=sessions, events=_Events(), host=_UnusedHost(),
        memory=_Memory(), verifier=_Verifier(), lifecycle=_Lifecycle(),
        guardrails=_Guardrails(), inputs=_Inputs(), transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "answer"},),
        workspace=tmp_path,
        session_id="native-error-guarded",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        context = _execution_context(run_context, runtime_services)

        async def guard(content, _messages):
            observed.append(content)
            return "error-safe"

        return replace(context, policy=SimpleNamespace(
            **{
                **vars(context.policy),
                "has_output_guardrail": lambda: True,
                "run_output_guardrail": guard,
            }
        ))

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert result["status"] == "error"
    assert observed == ["error-secret"]
    assert "error-secret" not in repr(sessions.context.transcript)


def test_length_continuation_does_not_publish_unapproved_segments(
    tmp_path: Path,
):
    """The integration proof above also covers the continuation transcript."""
    test_output_guardrail_applies_to_length_result(tmp_path)


def test_agent_as_tool_result_is_guarded_before_parent_commit():
    from nz_coder.runtime.execution.commit_boundary import (
        OutputVisibility,
        approve_model_result,
        commit_approved_model_result,
    )

    observed = []

    async def guard(content, _messages):
        observed.append(content)
        return "parent-safe"

    context = SimpleNamespace(
        policy=SimpleNamespace(run_output_guardrail=guard),
        messages=SimpleNamespace(
            materialize_llm_result=lambda *_args, **_kwargs: pytest.fail(
                "internal result became a public model commit"
            ),
            reconcile_llm_result=lambda *_args, **_kwargs: pytest.fail(
                "internal result became a public model commit"
            ),
        ),
    )
    approved = asyncio.run(approve_model_result(
        context=context,
        result=LLMResult(content="child-secret", finish_reason="stop"),
        messages=[],
        visibility=OutputVisibility.INTERNAL_AGENT_RESULT,
    ))
    assistant = {"role": "assistant", "content": ""}
    commit_approved_model_result(
        approved,
        context=context,
        assistant_message=assistant,
        processor=object(),
        message_part={},
        messages=[],
    )

    assert observed == ["child-secret"]
    assert assistant["content"] == "parent-safe"
    assert assistant["_nz_internal"] is True
    assert assistant["_nz_visible"] is False


def _commit_internal_extra(extra):
    from nz_coder.runtime.execution.commit_boundary import (
        ApprovedModelResult,
        OutputVisibility,
        commit_approved_model_result,
    )

    context = SimpleNamespace(
        messages=SimpleNamespace(
            materialize_llm_result=lambda *_args, **_kwargs: None,
            reconcile_llm_result=lambda *_args, **_kwargs: None,
        )
    )
    assistant = {
        "role": "assistant",
        "content": "",
        "_nz_message_id": "message-safe",
        "_nz_parts": [],
    }
    commit_approved_model_result(
        ApprovedModelResult(
            LLMResult(content="child result", extra=extra),
            OutputVisibility.INTERNAL_AGENT_RESULT,
        ),
        context=context,
        assistant_message=assistant,
        processor=object(),
        message_part={},
        messages=[],
    )
    return assistant


def test_internal_result_extra_cannot_override_visibility():
    assistant = _commit_internal_extra({"_nz_visible": True})
    assert assistant["_nz_visible"] is False


def test_internal_result_extra_cannot_override_internal_flag():
    assistant = _commit_internal_extra({"_nz_internal": False})
    assert assistant["_nz_internal"] is True


def test_internal_result_extra_cannot_override_authoritative_state():
    assistant = _commit_internal_extra({"_nz_authoritative": False})
    assert assistant["_nz_authoritative"] is True


def test_provider_extra_cannot_replace_parts():
    assistant = _commit_internal_extra({
        "_nz_parts": [{"type": "text", "text": "LEAK"}],
    })
    assert assistant["_nz_parts"] == []


def test_provider_extra_cannot_replace_content():
    assistant = _commit_internal_extra({"content": "OVERRIDE"})
    assert assistant["content"] == "child result"


def test_user_visible_result_extra_cannot_override_message_identity():
    from nz_coder.runtime.execution.commit_boundary import (
        ApprovedModelResult,
        OutputVisibility,
        commit_approved_model_result,
    )

    observed = {}

    def materialize(result, *, assistant_message, **_kwargs):
        observed.update(result.extra)
        assistant_message.update(result.extra)

    assistant = {"role": "assistant", "content": "", "_nz_message_id": "safe"}
    commit_approved_model_result(
        ApprovedModelResult(
            LLMResult(content="answer", extra={"_nz_message_id": "attacker"}),
            OutputVisibility.USER_VISIBLE,
        ),
        context=SimpleNamespace(
            messages=SimpleNamespace(
                materialize_llm_result=materialize,
                reconcile_llm_result=materialize,
            )
        ),
        assistant_message=assistant,
        processor=object(),
        message_part={},
        messages=[],
    )

    assert observed == {}
    assert assistant["_nz_message_id"] == "safe"


def test_reserved_message_keys_are_rejected_from_provider_extra():
    from nz_coder.protocol.message_schema import sanitize_provider_extra

    assert sanitize_provider_extra({
        "role": "user",
        "content": "override",
        "tool_calls": [],
        "_nz_visible": True,
    }) == {}


def test_safe_provider_extra_is_preserved():
    from nz_coder.protocol.message_schema import sanitize_provider_extra

    assert sanitize_provider_extra({
        "reasoning_content": "safe reasoning",
        "provider_extra": {"response_id": "response-1"},
    }) == {
        "reasoning_content": "safe reasoning",
        "provider_extra": {"response_id": "response-1"},
    }


def test_malicious_child_extra_never_enters_snapshot():
    assistant = _commit_internal_extra({
        "_nz_parts": [{"type": "text", "text": "LEAK"}],
        "_nz_visible": True,
        "role": "user",
    })
    assert "LEAK" not in repr(assistant)
    assert assistant["role"] == "assistant"


def test_guardrail_failure_settles_current_step(tmp_path: Path):
    from nz_coder.runtime.agent.guardrails import GuardrailBlockedError
    from nz_coder.protocol.public_error import PublicRuntimeError

    class OneAnswer:
        async def complete_turn(self, _context, _messages, **_kwargs):
            return LLMResult(content="never-public", finish_reason="stop")

    sessions = _Sessions()
    services = RuntimeServices(
        model=OneAnswer(), tools=_Tools(), context=_Context(),
        session_runtime=sessions, events=_Events(), host=_UnusedHost(),
        memory=_Memory(), verifier=_Verifier(), lifecycle=_Lifecycle(),
        guardrails=_Guardrails(), inputs=_Inputs(), transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "answer"},),
        workspace=tmp_path,
        session_id="native-guardrail-settle",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        context = _execution_context(run_context, runtime_services)

        async def block(_content, _messages):
            raise GuardrailBlockedError("redactor", "output", "private reason")

        return replace(context, policy=SimpleNamespace(**{
            **vars(context.policy),
            "has_output_guardrail": lambda: True,
            "run_output_guardrail": block,
        }))

    with pytest.raises(PublicRuntimeError):
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=execution_context,
        ).run(request, options=request_contracts.RunOptions(stream=False)))

    assistant = sessions.context.transcript[-1]
    parts = assistant.get("_nz_parts", [])
    assert assistant["_nz_error"] == 'Output blocked by guardrail "redactor".'
    assert any(
        part.get("type") == "step-finish"
        and part.get("reason") == "blocked"
        for part in parts
    )
    assert not any(
        part.get("type") == "tool"
        and part.get("state", {}).get("status") in {"pending", "running"}
        for part in parts
    )
    assert "never-public" not in repr(assistant)


def test_native_runner_preserves_blocked_terminal_status(tmp_path: Path):
    """A policy blocker remains distinct from provider or runtime errors."""
    sessions = _Sessions()
    services = RuntimeServices(
        model=_Model(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_BlockedLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer with tools"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "read the value"},),
        workspace=tmp_path,
        session_id="native-blocked-session",
        tool_names=("read_value",),
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.BLOCKED
    assert result.metadata["raw_status"] == "blocked"
    assert sessions.context is not None
    assert sessions.context.terminal_status is RunStatus.BLOCKED
    assert sessions.final_statuses == [RunStatus.BLOCKED]


def test_native_runner_attributes_each_main_provider_turn(tmp_path: Path):
    model = _Model()
    sessions = _Sessions()
    runtime_state = RuntimeState()
    traces: list[tuple[str, dict]] = []
    services = RuntimeServices(
        model=model,
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer with tools"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "read the value"},),
        workspace=tmp_path,
        session_id="native-turn-attribution-session",
        tool_names=("read_value",),
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        base = _execution_context(run_context, runtime_services)
        base.hooks.trace = lambda name, **payload: traces.append((name, payload))
        return replace(
            base,
            runtime_state=runtime_state,
            hooks=base.hooks,
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    started = [payload for name, payload in traces if name == "provider_turn_started"]
    settled = [payload for name, payload in traces if name == "provider_turn_settled"]
    assert [item["turn"] for item in started] == [1, 2]
    assert [item["reason"] for item in started] == [
        "initial_investigation",
        "investigation",
    ]
    assert [item["outcome"] for item in settled] == [
        "other_tool_batch",
        "final_answer",
    ]
    assert runtime_state.provider_turns_by_reason == {
        "initial_investigation": 1,
        "investigation": 1,
    }
    assert runtime_state.provider_turns_by_outcome == {
        "other_tool_batch": 1,
        "final_answer": 1,
    }


def test_native_runner_finalizes_session_once_when_execution_raises(tmp_path: Path):
    sessions = _Sessions()
    services = RuntimeServices(
        model=_FailingModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="fail safely"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "trigger failure"},),
        workspace=tmp_path,
        session_id="native-error-session",
        stream=False,
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=_execution_context,
        ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert sessions.final_statuses == [RunStatus.ERROR]
    assert sessions.context is not None
    assert sessions.context.finalized is True


def test_native_runner_persists_success_before_publishing_terminal_event(
    tmp_path: Path,
):
    """SDK/HTTP completion cannot outrun the durable Session commit."""
    timeline: list[str] = []
    sessions = _OrderedSessions(timeline)
    services = RuntimeServices(
        model=_MalformedUsageModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_OrderedEvents(timeline),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "answer"},),
        workspace=tmp_path,
        session_id="native-terminal-order-success",
        stream=False,
    )

    asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert timeline.index("session.finalized:completed") < timeline.index(
        "session.run.completed"
    )


def test_native_runner_persists_error_before_publishing_terminal_event(
    tmp_path: Path,
):
    """Failure observers must only run after resumable catch history is saved."""
    timeline: list[str] = []
    sessions = _OrderedSessions(timeline)
    services = RuntimeServices(
        model=_FailingModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_OrderedEvents(timeline),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="fail safely"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "fail"},),
        workspace=tmp_path,
        session_id="native-terminal-order-error",
        stream=False,
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=_execution_context,
        ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert timeline.index("session.finalized:error") < timeline.index(
        "session.run.failed"
    )


def test_native_runner_persists_keyboard_interrupt_before_cancel_event(
    tmp_path: Path,
):
    """A raw Ctrl+C follows the same resumable terminal contract as legacy."""
    timeline: list[str] = []
    sessions = _OrderedSessions(timeline)
    services = RuntimeServices(
        model=_MalformedUsageModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_OrderedEvents(timeline),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="interrupt safely"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "interrupt"},),
        workspace=tmp_path,
        session_id="native-keyboard-interrupt",
        stream=False,
    )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=_execution_context,
            middleware=(_InterruptBeforeRunMiddleware(),),
        ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert sessions.final_statuses == [RunStatus.INTERRUPTED]
    assert timeline.index("session.finalized:interrupted") < timeline.index(
        "session.run.cancelled"
    )


@pytest.mark.parametrize(
    ("middleware", "message"),
    [
        (_FailBeforeRunMiddleware(), "middleware entry exploded"),
        (_FailAfterRunMiddleware(), "middleware exit exploded"),
    ],
)
def test_native_runner_middleware_failure_is_persisted_before_terminal_event(
    tmp_path: Path,
    middleware,
    message: str,
):
    """Run hooks are inside the same durable terminal boundary as execution."""
    timeline: list[str] = []
    sessions = _OrderedSessions(timeline)
    services = RuntimeServices(
        model=_MalformedUsageModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_OrderedEvents(timeline),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="answer"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "answer"},),
        workspace=tmp_path,
        session_id=f"native-terminal-order-{type(middleware).__name__}",
        stream=False,
    )

    with pytest.raises(RuntimeError, match=message):
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=_execution_context,
            middleware=(middleware,),
        ).run(request, options=request_contracts.RunOptions(stream=False)))

    assert sessions.final_statuses == [RunStatus.ERROR]
    assert timeline.index("session.finalized:error") < timeline.index(
        "session.run.failed"
    )


def test_native_runner_preserves_original_error_when_failure_persistence_breaks(
    tmp_path: Path,
):
    """Catch cleanup/storage failures must not replace the Provider failure."""
    sessions = _Sessions()

    async def broken_finalize(_context, _status):
        raise OSError("session storage unavailable")

    sessions.finalize = broken_finalize
    services = RuntimeServices(
        model=_FailingModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="fail safely"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "trigger failure"},),
        workspace=tmp_path,
        session_id="native-original-error-session",
        stream=False,
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        asyncio.run(AgentRunner(
            services,
            execution_context_factory=_execution_context,
        ).run(request, options=request_contracts.RunOptions(stream=False)))


def test_native_runner_restores_run_scoped_verification_context_after_error(
    tmp_path: Path,
):
    """A failed run cannot leak its verification policy into its caller."""
    from nz_coder.runtime.core.execution_context import (
        broad_tests_blocked,
        declared_test_scopes,
        scoped_broad_test_guard,
        scoped_declared_test_scopes,
    )

    sessions = _Sessions()
    services = RuntimeServices(
        model=_FailingModel(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_ScopeMutatingLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="fail safely"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "trigger failure"},),
        workspace=tmp_path,
        session_id="native-scope-release-session",
        stream=False,
    )

    async def exercise_same_task() -> None:
        with (
            scoped_broad_test_guard(False),
            scoped_declared_test_scopes(("outer/tests",)),
        ):
            with pytest.raises(RuntimeError, match="provider exploded"):
                await AgentRunner(
                    services,
                    execution_context_factory=_execution_context,
                ).run(request, options=request_contracts.RunOptions(stream=False))
            assert broad_tests_blocked() is False
            assert declared_test_scopes() == ("outer/tests",)

    asyncio.run(exercise_same_task())


def test_native_runner_cancellation_closes_as_resumable_terminal(tmp_path: Path):
    """A Provider cancellation must close the run, not escape before run_end."""
    sessions = _Sessions()
    events = _RecordingEvents()
    services = RuntimeServices(
        model=_CancelledModel(),
        tools=_Tools(),
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
        agent=AgentDefinition(name="native", instructions="cancel safely"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "start then cancel"},),
        workspace=tmp_path,
        session_id="native-cancel-session",
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.CANCELLED
    assert sessions.final_statuses == [RunStatus.CANCELLED]
    assert sessions.context is not None
    assert sessions.context.finalized is True
    assistant = sessions.context.transcript[-1]
    assert assistant["role"] == "assistant"
    assert assistant["_nz_assistant_error"]["name"] == "MessageAbortedError"
    assert [getattr(event.name, "value", event.name) for event in events.events] == [
        "session.run.started",
        "session.model.started",
        "session.run.cancelled",
    ]


def test_native_runner_injects_budget_convergence_before_late_provider_call(
    tmp_path: Path,
):
    """Crossing 70% must change the next model turn from explore to converge."""
    model = _BudgetModel()
    services = RuntimeServices(
        model=model,
        tools=_BudgetTools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_FourTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish efficiently"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "inspect then answer"},),
        workspace=tmp_path,
        session_id="native-budget-session",
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    late_contents = [
        message.get("content", "") for message in model.seen_messages[-1]
        if message.get("role") == "user"
    ]
    assert any("Work budget: begin converging" in content for content in late_contents)


def test_native_runner_executes_explicit_verification_contract_at_completion_boundary(
    tmp_path: Path,
):
    """A changed generation receives exact acceptance once at natural completion."""
    model = _BudgetModel()
    tools = _VerificationTools()
    runtime_state = RuntimeState()
    runtime_state.has_diff = True
    runtime_state.mutation_generation = 1
    runtime_state.verification_contract = VerificationContract(
        command="python -m pytest -q tests/runtime/test_native_runner.py",
        targets=("tests/runtime/test_native_runner.py",),
    ).to_dict()
    services = RuntimeServices(
        model=model,
        tools=tools,
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_FourTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish efficiently"),
        profile=MAIN_PROFILE,
        messages=({
            "role": "user",
            "content": (
                "Make the change, then run "
                "python -m pytest -q tests/runtime/test_native_runner.py"
            ),
        },),
        workspace=tmp_path,
        session_id="native-verification-contract-session",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        return replace(
            _execution_context(run_context, runtime_services),
            runtime_state=runtime_state,
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert tools.names.count("bash") == 1
    bash_assistants = [
        message for message in result.messages
        if message.get("role") == "assistant"
        and message.get("_nz_verification_contract")
    ]
    assert len(bash_assistants) == 1
    arguments = json.loads(bash_assistants[0]["tool_calls"][0]["function"]["arguments"])
    assert arguments["command"] == (
        "python -m pytest -q tests/runtime/test_native_runner.py"
    )
    assert arguments["_nz_runtime_contract"] is True
    assert runtime_state.verification_generation == 1
    contract = VerificationContract.from_dict(runtime_state.verification_contract)
    assert contract.attempts == 1
    assert contract.passed is True


def test_native_runner_does_not_repeat_acceptance_after_docs_only_edit(
    tmp_path: Path,
):
    """A later docs mutation keeps already-passed code acceptance current."""
    model = _BudgetModel()
    tools = _VerificationTools()
    runtime_state = RuntimeState()
    runtime_state.has_diff = True
    runtime_state.mutation_generation = 2
    runtime_state.acceptance_mutation_generation = 1
    runtime_state.verification_contract = VerificationContract(
        command="python -m pytest -q tests/runtime/test_native_runner.py",
        targets=("tests/runtime/test_native_runner.py",),
        attempted_generation=1,
        attempts=1,
        passed=True,
        output="1 passed",
    ).to_dict()
    services = RuntimeServices(
        model=model,
        tools=tools,
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_FourTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish efficiently"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "Update docs only"},),
        workspace=tmp_path,
        session_id="native-docs-current-acceptance-session",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        return replace(
            _execution_context(run_context, runtime_services),
            runtime_state=runtime_state,
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert tools.names.count("bash") == 0
    contract = VerificationContract.from_dict(runtime_state.verification_contract)
    assert contract.attempts == 1
    assert contract.attempted_generation == 1


def test_native_runner_closes_early_tool_boundary_after_current_acceptance(
    tmp_path: Path,
):
    """Current exact evidence plus semantic acceptance needs no summary-only call."""
    from nz_coder.runtime.agent.task_contract import Requirement, TaskContract

    class OneVerificationModel:
        def __init__(self) -> None:
            self.calls = 0

        async def complete_turn(self, _context, _messages, **_kwargs):
            self.calls += 1
            return LLMResult(
                content="",
                tool_calls=[{
                    "id": f"call-verify-{self.calls}",
                    "type": "function",
                    "function": {
                        "name": "bash",
                        "arguments": json.dumps({
                            "command": "python -m pytest -q tests/test_target.py",
                        }),
                    },
                }],
                finish_reason="tool_calls",
                input_tokens=5,
                output_tokens=1,
            )

    class ContractPassingTools:
        async def execute_batch_async(
            self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
        ):
            processor = kwargs["processor"]
            processor.start_tools(calls)
            contract = VerificationContract.from_dict(
                runtime_state.verification_contract
            )
            contract.record_attempt(
                runtime_state.mutation_generation,
                passed=True,
                output="1 passed",
                source="agent",
                zone="tool",
            )
            runtime_state.verification_contract = contract.to_dict()
            runtime_state.verification_generation = runtime_state.mutation_generation
            runtime_state.observe_requirement_verification(
                contract.command,
                passed=True,
                acceptance=True,
            )
            for call in calls:
                processor.complete_tool(call["id"], "1 passed")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": "1 passed",
                })
            processor.finish_step("tool-calls")
            return "continue"

    model = OneVerificationModel()
    runtime_state = RuntimeState()
    runtime_state.has_diff = True
    runtime_state.changed_files = ["target.py"]
    runtime_state.mutation_generation = 1
    runtime_state.verification_contract = VerificationContract(
        command="python -m pytest -q tests/test_target.py",
        targets=("tests/test_target.py",),
    ).to_dict()
    runtime_state.set_task_contract(TaskContract(requirements=(Requirement(
        id="R1",
        description="preserve target behavior",
        kind="compatibility",
        expected_artifacts=("target.py",),
        satisfaction_mode="mixed",
        required_evidence=("semantic_review",),
    ),)))
    ledger = runtime_state.requirement_ledger_snapshot()
    ledger.observe_mutation(1, ["target.py"])
    runtime_state.requirement_ledger = ledger.to_dict()
    services = RuntimeServices(
        model=model,
        tools=ContractPassingTools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_FourTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="verify and finish"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "fix target.py and run the test"},),
        workspace=tmp_path,
        session_id="native-early-accepted-tool-boundary",
        tool_names=("bash",),
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        base = _execution_context(run_context, runtime_services)

        async def accept_semantics(_messages, status, _content):
            runtime_state.observe_requirement_semantic_review(
                accepted=True,
                fingerprint="independent-review",
            )
            return status

        base.policy.verify_completion = accept_semantics
        return replace(base, runtime_state=runtime_state, policy=base.policy)

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert model.calls == 1
    assert runtime_state.requirement_ledger_snapshot().unresolved() == ()


def test_native_runner_does_not_schedule_exact_contract_in_yellow_zone(
    tmp_path: Path,
):
    model = _BudgetModel()
    tools = _VerificationTools()
    trace_events = []
    runtime_state = RuntimeState()
    runtime_state.has_diff = True
    runtime_state.mutation_generation = 1
    runtime_state.open_todo_items = 1
    runtime_state.verification_contract = VerificationContract(
        command="python -m pytest -q tests/runtime/test_native_runner.py",
        targets=("tests/runtime/test_native_runner.py",),
    ).to_dict()
    services = RuntimeServices(
        model=model,
        tools=tools,
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_FourTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish efficiently"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "finish every todo then test"},),
        workspace=tmp_path,
        session_id="native-open-todo-contract-session",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        native_context = _execution_context(run_context, runtime_services)
        hooks = SimpleNamespace(
            on_turn_start=lambda _messages: None,
            on_pre_send=lambda _messages: None,
            on_turn_end=lambda _messages, _status: None,
            trace=lambda event, **payload: trace_events.append((event, payload)),
        )
        return replace(
            native_context,
            runtime_state=runtime_state,
            hooks=hooks,
        )

    asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert tools.names.count("bash") == 1
    assert not any(
        event == "verification_contract_executed"
        and payload["zone"] == "yellow"
        for event, payload in trace_events
    )
    assert any(
        event == "verification_contract_executed"
        and payload["zone"] == "completion"
        for event, payload in trace_events
    )


def test_native_runner_executes_contract_before_an_early_natural_completion(
    tmp_path: Path,
):
    """A short task must not bypass its explicit acceptance command."""
    model = _EarlyStopModel()
    tools = _VerificationTools()
    accepted_contracts = []
    runtime_state = RuntimeState()
    runtime_state.has_diff = True
    runtime_state.mutation_generation = 1
    runtime_state.verification_contract = VerificationContract(
        command="pytest -q tests/test_small.py",
        targets=("tests/test_small.py",),
    ).to_dict()
    services = RuntimeServices(
        model=model,
        tools=tools,
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish efficiently"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "make a small tested change"},),
        workspace=tmp_path,
        session_id="native-early-verification-session",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        native_context = _execution_context(run_context, runtime_services)
        native_context.policy.observe_verification_contract = (
            lambda command, output, passed: accepted_contracts.append(
                (command, output, passed)
            )
        )
        return replace(
            native_context,
            runtime_state=runtime_state,
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert tools.names.count("bash") == 1
    assert model.calls == 2
    assert any(
        message.get("role") == "tool" and message.get("content") == "1 passed"
        for message in result.messages
    )
    assert accepted_contracts == [("pytest -q tests/test_small.py", "1 passed", True)]


def test_native_runner_exact_acceptance_unlocks_bootstrap_requirement_gate(
    tmp_path: Path,
):
    """The completion contract must update evidence before the hard ledger gate."""
    from nz_coder.runtime.verification.completion_gate import CompletionGate

    command = "pytest -q tests/test_small.py"
    model = _EarlyStopModel()
    tools = _VerificationTools()
    runtime_state = RuntimeState()
    runtime_state.has_diff = True
    runtime_state.mutation_generation = 1
    runtime_state.verification_contract = VerificationContract(
        command=command,
        targets=("tests/test_small.py",),
    ).to_dict()
    runtime_state.set_task_contract(derive_task_contract(
        "Implement the requested behavior and tests, then run pytest -q tests/test_small.py",
        acceptance_command=command,
        workspace=tmp_path,
    ))
    runtime_state._observe_requirement_mutation(["tests/test_small.py"])
    services = RuntimeServices(
        model=model,
        tools=tools,
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish efficiently"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "make a small tested change"},),
        workspace=tmp_path,
        session_id="native-bootstrap-gate-session",
        stream=False,
    )

    def execution_context(run_context, runtime_services):
        native_context = _execution_context(run_context, runtime_services)

        async def verify_completion(_messages, status, _content):
            decision = CompletionGate().evaluate(
                runtime_state.requirement_ledger_snapshot(),
                mutation_generation=runtime_state.mutation_generation,
            )
            return status if decision.ready else "continue"

        policy_members = dict(vars(native_context.policy))
        policy_members.update(
            observe_verification_contract=(
                lambda accepted_command, _output, passed: (
                    runtime_state.observe_requirement_verification(
                        accepted_command,
                        passed=passed,
                        acceptance=True,
                    )
                )
            ),
            verify_completion=verify_completion,
        )
        policy = SimpleNamespace(**policy_members)
        return replace(
            native_context,
            runtime_state=runtime_state,
            policy=policy,
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert tools.names.count("bash") == 1
    assert model.calls == 2
    assert not runtime_state.requirement_ledger_snapshot().unresolved()


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_request_max_turns_rejects_nonfinite_metadata(value: float):
    """Untrusted HTTP/session metadata must fail with the public error contract."""
    with pytest.raises(ValueError, match="positive integer"):
        _request_max_turns(SimpleNamespace(metadata={"max_turns": value}))


def test_native_runner_binds_request_max_turns_for_product_lifecycle(tmp_path: Path):
    lifecycle = _ConfiguredTurnLifecycle()
    services = RuntimeServices(
        model=_Model(),
        tools=_Tools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=lifecycle,
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="respect run cap"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "one turn"},),
        workspace=tmp_path,
        session_id="native-max-turns-session",
        stream=False,
        metadata={"max_turns": 7},
    )

    asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert lifecycle.observed_max_turns == 7


def test_natural_completion_idle_yields_and_resumes_after_background_wake(
    tmp_path: Path,
):
    model = _IdleYieldModel()
    services = RuntimeServices(
        model=model,
        tools=_BudgetTools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_DefaultTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="wait for children"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "delegate then synthesize"},),
        workspace=tmp_path,
        session_id="native-idle-yield",
        stream=False,
        metadata={"max_turns": 3},
    )
    idle_calls = []

    def execution_context(run_context, runtime_services):
        native = _execution_context(run_context, runtime_services)

        async def idle_yield(messages):
            idle_calls.append(len(messages))
            if len(idle_calls) > 1:
                return False
            messages.append({
                "role": "user",
                "content": "<task-completed id=\"child-1\">done</task-completed>",
                "_nz_synthetic": True,
                "_nz_task_completed": True,
            })
            return True

        control_members = dict(vars(native.control))
        control_members["idle_yield"] = idle_yield
        return replace(
            native,
            control=SimpleNamespace(**control_members),
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert model.calls == 2
    assert len(idle_calls) == 2
    assert any(
        message.get("_nz_task_completed") is True
        for message in model.seen_messages[1]
    )


def test_soft_nominal_budget_never_stops_runaway_model_before_hard_cap(
    tmp_path: Path,
):
    model = _AlwaysToolModel()
    services = RuntimeServices(
        model=model,
        tools=_BudgetTools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_DefaultTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="do not loop forever"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "pathological loop"},),
        workspace=tmp_path,
        session_id="native-default-cap-session",
        stream=False,
    )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=_execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.MAX_TURNS
    assert model.calls == 500


def test_native_runner_consumes_context_nominal_turn_override(tmp_path: Path):
    """SWE can grant a bounded DeepSeek budget without changing the hard cap."""
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    model = _AlwaysToolModel()
    services = RuntimeServices(
        model=model,
        tools=_BudgetTools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_DefaultTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="use the SWE budget"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "pathological loop"},),
        workspace=tmp_path,
        session_id="native-overridden-nominal-cap",
        stream=False,
    )

    with scoped_runtime_overrides(
        max_agent_turns=80,
        nominal_agent_turns=20,
    ):
        result = asyncio.run(AgentRunner(
            services,
            execution_context_factory=_execution_context,
        ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.MAX_TURNS
    assert model.calls == 80


def test_completion_review_rejection_authorizes_bounded_repair_after_nominal_sla(
    tmp_path: Path,
):
    """A turn-15 Sidecar revise must reach a bounded turn 16, not die at the gate."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    command = "pytest -q tests/test_app.py"
    state = RuntimeState()
    state.has_diff = True
    state.changed_files = ["app.py", "tests/test_app.py"]
    state.mutation_generation = 1
    state.source_mutation_generation = 1
    state.verification_contract = VerificationContract(
        command=command,
        targets=("tests/test_app.py",),
        attempted_generation=1,
        attempts=1,
        passed=True,
        output="1 passed",
        source="runtime",
        zone="completion",
    ).to_dict()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Change app while preserving compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
        "acceptance_commands": [command],
        "contract_version": 2,
    }, workspace=tmp_path))
    state._observe_requirement_mutation(["app.py", "tests/test_app.py"])
    state.observe_requirement_verification(command, passed=True, acceptance=True)
    assert state.semantic_review_pending_only() is True

    model = _ReviewRepairModel()
    sessions = _Sessions()
    services = RuntimeServices(
        model=model,
        tools=_BudgetTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_DefaultTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="preserve compatibility"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "preserve compatibility"},),
        workspace=tmp_path,
        session_id="semantic-review-repair-reserve",
        stream=False,
        metadata={"max_turns": 20},
    )
    verifier_calls = []

    def execution_context(run_context, runtime_services):
        native = _execution_context(run_context, runtime_services)
        policy_members = dict(vars(native.policy))

        async def verify_completion(_messages, status, _content):
            verifier_calls.append(status)
            if len(verifier_calls) == 1:
                return "continue"
            state.observe_requirement_semantic_review(
                accepted=True,
                fingerprint="verifier_ok:compatibility",
            )
            return status

        policy_members["verify_completion"] = verify_completion
        return replace(
            native,
            runtime_state=state,
            policy=SimpleNamespace(**policy_members),
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.COMPLETED
    assert model.calls == 16
    assert verifier_calls == ["completed", "completed"]
    assert state.completion_review_rejections == 1
    assert state.emergency_eligibility().eligible is True


@pytest.mark.parametrize(
    ("inline_tools", "acceptance_passed", "expected_status"),
    [
        (False, True, RunStatus.COMPLETED),
        (False, False, RunStatus.MAX_TURNS),
        (True, True, RunStatus.COMPLETED),
        (True, False, RunStatus.MAX_TURNS),
    ],
)
def test_final_tool_call_settles_exact_contract_and_ledger_at_hard_cap(
    tmp_path: Path,
    inline_tools: bool,
    acceptance_passed: bool,
    expected_status: RunStatus,
):
    """G1/G2: final tool batches cannot bypass exact terminal settlement."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    command = "python -m pytest -q tests/test_app.py"
    state = RuntimeState()
    state.verification_contract = VerificationContract(
        command=command,
        targets=("tests/test_app.py",),
    ).to_dict()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Update app and preserve behavior",
        "requirements": [
            {
                "id": "R1",
                "description": "Update app.py",
                "kind": "behavior",
                "expected_artifacts": ["app.py"],
            },
            {
                "id": "R2",
                "description": "Pass exact acceptance",
                "kind": "verification",
                "expected_artifacts": [],
            },
        ],
        "acceptance_commands": [command],
    }, workspace=tmp_path))
    tools = _TerminalBoundaryTools(state, acceptance_passed=acceptance_passed)
    model = _TerminalEditModel(
        inline_tools=inline_tools,
        content="Tests pass. Next I will update the documentation.",
    )
    sessions = _Sessions()
    trace_events = []
    persist_calls = []
    services = RuntimeServices(
        model=model,
        tools=tools,
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_OneTurnLifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish with one edit"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update app.py and test it"},),
        workspace=tmp_path,
        session_id=f"terminal-boundary-{inline_tools}-{acceptance_passed}",
        stream=inline_tools,
    )

    def execution_context(run_context, runtime_services):
        native = _execution_context(run_context, runtime_services)
        hooks = SimpleNamespace(
            on_turn_start=lambda _messages: None,
            on_pre_send=lambda _messages: None,
            on_turn_end=lambda _messages, _status: None,
            trace=lambda event, **payload: trace_events.append((event, payload)),
        )
        control_members = dict(vars(native.control))
        control_members["persist_runtime_state"] = (
            lambda **payload: persist_calls.append(payload)
        )
        return replace(
            native,
            runtime_state=state,
            hooks=hooks,
            control=SimpleNamespace(**control_members),
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=inline_tools)))

    assert result.status is expected_status
    assert model.calls == 1
    assert tools.names == ["edit_file", "bash"]
    contract = VerificationContract.from_dict(state.verification_contract)
    assert contract.attempts == 1
    assert contract.passed is acceptance_passed
    assert persist_calls
    boundary = "streamed_tool_batch" if inline_tools else "tool_batch"
    assert any(
        event == "terminal_boundary_settled" and payload["boundary"] == boundary
        for event, payload in trace_events
    )
    if acceptance_passed:
        assert not state.requirement_ledger_snapshot().unresolved()
        assert result.final_text
        assert "app.py" in result.final_text
        assert "Next I will" not in result.final_text
    else:
        assert state.requirement_ledger_snapshot().unresolved()
        assert result.final_text
        assert "did not pass" in result.final_text
        assert command in result.final_text


@pytest.mark.parametrize(
    ("verifier_accepts", "expected_action", "expected_status", "expected_reason"),
    [
        (True, "finalize", "completed", "semantic_review_and_ledger_satisfied"),
        (False, "continue", "", "semantic_review_requires_revision"),
    ],
)
def test_tool_boundary_routes_semantic_only_contract_through_verifier(
    tmp_path: Path,
    verifier_accepts: bool,
    expected_action: str,
    expected_status: str,
    expected_reason: str,
):
    """Passing tests cannot bypass compatibility review at nominal settlement."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    command = "pytest -q tests/test_app.py"
    state = RuntimeState()
    state.has_diff = True
    state.changed_files = ["app.py"]
    state.mutation_generation = 1
    state.source_mutation_generation = 1
    state.verification_contract = VerificationContract(
        command=command,
        targets=("tests/test_app.py",),
        attempted_generation=1,
        attempts=1,
        passed=True,
        output="1 passed",
        source="runtime",
        zone="completion",
    ).to_dict()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Change app while preserving compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
        "acceptance_commands": [command],
        "contract_version": 2,
    }, workspace=tmp_path))
    state._observe_requirement_mutation(["app.py"])
    state.observe_requirement_verification(
        command,
        passed=True,
        acceptance=True,
    )
    assert state.semantic_review_pending_only() is True

    sessions = _Sessions()
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=_VerificationTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="preserve compatibility"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "preserve compatibility"},),
        workspace=tmp_path,
        session_id="semantic-tool-boundary",
        stream=False,
    )
    verifier_calls = []
    trace_events = []

    async def exercise():
        run_context = await sessions.open(request)
        native = _execution_context(run_context, services)
        policy_members = dict(vars(native.policy))

        async def verify_completion(_messages, status, content):
            verifier_calls.append((status, content))
            if verifier_accepts:
                state.observe_requirement_semantic_review(
                    accepted=True,
                    fingerprint="verifier_ok:compatibility",
                )
                return status
            return "continue"

        policy_members["verify_completion"] = verify_completion
        context = replace(
            native,
            runtime_state=state,
            policy=SimpleNamespace(**policy_members),
            hooks=SimpleNamespace(
                on_turn_start=lambda _messages: None,
                on_pre_send=lambda _messages: None,
                on_turn_end=lambda _messages, _status: None,
                trace=lambda event, **payload: trace_events.append((event, payload)),
            ),
        )
        runner = AgentRunner(
            services,
            execution_context_factory=_execution_context,
        )
        budget = WorkBudgetController(max_turns=20)
        decisions = [await runner._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="tool_batch",
            content="",
            completed_turns=15,
            work_budget=budget,
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=False,
        )]
        if not verifier_accepts:
            decisions.append(await runner._settle_terminal_boundary(
                context,
                services,
                run_context,
                run_context.transcript,
                boundary="tool_batch",
                content="",
                completed_turns=16,
                work_budget=budget,
                resolve_tool_runtime_context=lambda: object(),
                natural_completion=False,
            ))
        return decisions

    decisions = asyncio.run(exercise())
    decision = decisions[0]

    assert verifier_calls == [(
        "completed",
        "Completed the requested changes in app.py. "
        "Exact acceptance passed: `pytest -q tests/test_app.py`.",
    )]
    assert decision.action == expected_action
    assert decision.status == expected_status
    assert state.semantic_review_pending_only() is (not verifier_accepts)
    if not verifier_accepts:
        assert [item.action for item in decisions] == ["continue", "continue"]
        assert state.completion_review_generation == 1
    assert any(
        event == "terminal_boundary_settled"
        and payload["reason"] == expected_reason
        for event, payload in trace_events
    )


def test_pressure_boundary_schedules_evidence_backed_targeted_once_per_mutation(
    tmp_path: Path,
):
    state = RuntimeState()
    state.mutation_generation = 2
    state.source_mutation_generation = 2
    state.turn_count = 13
    tools = _VerificationTools()
    sessions = _Sessions()
    trace_events = []
    persist_calls = []
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=tools,
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="verify the mutation"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update and test app.py"},),
        workspace=tmp_path,
        session_id="pressure-verification",
        stream=False,
    )

    async def exercise():
        run_context = await sessions.open(request)
        native = _execution_context(run_context, services)
        policy_members = dict(vars(native.policy))
        policy_members["verification_status"] = lambda: {
            "verification_pipeline": {
                "stages": [
                    {
                        "name": "static",
                        "commands": [{
                            "command": "python -m py_compile app.py",
                            "required": True,
                            "status": "passed",
                        }],
                    },
                    {
                        "name": "targeted",
                        "commands": [{
                            "command": "pytest tests/test_app.py",
                            "required": True,
                            "status": "pending",
                            "automation_provenance": "model_execution",
                        }],
                    },
                ],
            },
        }
        control_members = dict(vars(native.control))
        control_members["persist_runtime_state"] = (
            lambda **payload: persist_calls.append(payload)
        )
        context = replace(
            native,
            runtime_state=state,
            policy=SimpleNamespace(**policy_members),
            control=SimpleNamespace(**control_members),
            hooks=SimpleNamespace(
                on_turn_start=lambda _messages: None,
                on_pre_send=lambda _messages: None,
                on_turn_end=lambda _messages, _status: None,
                trace=lambda event, **payload: trace_events.append((event, payload)),
            ),
        )
        runner = AgentRunner(services, execution_context_factory=_execution_context)
        budget = WorkBudgetController(max_turns=20)
        decisions = []
        for _ in range(2):
            decisions.append(await runner._settle_terminal_boundary(
                context,
                services,
                run_context,
                run_context.transcript,
                boundary="tool_batch",
                content="",
                completed_turns=13,
                work_budget=budget,
                resolve_tool_runtime_context=lambda: object(),
                natural_completion=False,
            ))
        return decisions

    decisions = asyncio.run(exercise())

    assert [item.action for item in decisions] == ["continue", "continue"]
    assert tools.names.count("bash") == 1
    assert state.scheduled_verification_generations == {"targeted": 2}
    assert persist_calls
    assert any(
        event == "verification_scheduler_decision"
        and payload["stage"] == "targeted"
        and payload["mutation_generation"] == 2
        for event, payload in trace_events
    )


def test_natural_completion_injects_actionable_unresolved_requirement_guidance(
    tmp_path: Path,
):
    """A premature claim must tell the next model call which artifact is missing."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.mutation_generation = 3
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Update the package documentation",
        "requirements": [{
            "id": "R5",
            "description": "Update package README examples",
            "kind": "docs",
            "expected_artifacts": ["cron_engine/README.md"],
        }],
    }, workspace=tmp_path))
    sessions = _Sessions()
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=_VerificationTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish documentation"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update package docs"},),
        workspace=tmp_path,
        session_id="unresolved-requirement-guidance",
        stream=False,
    )
    trace_events = []

    async def exercise():
        run_context = await sessions.open(request)
        native = _execution_context(run_context, services)
        context = replace(
            native,
            runtime_state=state,
            hooks=SimpleNamespace(
                on_turn_start=lambda _messages: None,
                on_pre_send=lambda _messages: None,
                on_turn_end=lambda _messages, _status: None,
                trace=lambda event, **payload: trace_events.append((event, payload)),
            ),
        )
        runner = AgentRunner(services, execution_context_factory=_execution_context)
        budget = WorkBudgetController(max_turns=20)
        first = await runner._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="Everything is complete.",
            completed_turns=10,
            work_budget=budget,
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )
        second = await runner._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="Everything is complete.",
            completed_turns=11,
            work_budget=budget,
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )
        message_count = len(run_context.transcript)
        third = await runner._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="Everything is complete.",
            completed_turns=12,
            work_budget=budget,
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )
        return first, second, third, message_count, run_context.transcript

    first, second, third, message_count, messages = asyncio.run(exercise())

    assert first.action == second.action == "continue"
    assert first.reason == second.reason == "hard_requirements_unresolved"
    assert len(messages) == message_count
    assert third.action == "continue"
    assert third.status == ""
    assert third.reason == "hard_requirements_unresolved"
    guidance = [
        message for message in messages
        if message.get("_nz_completion_gate") is True
    ]
    assert len(guidance) == 2
    assert all(message["role"] == "user" for message in guidance)
    assert all(message["_nz_synthetic"] is True for message in guidance)
    assert all("R5: Update package README examples" in message["content"] for message in guidance)
    assert all("cron_engine/README.md" in message["content"] for message in guidance)
    assert state.completion_gate_prompts == 2
    assert sum(
        event == "requirement_completion_blocked"
        and payload["missing_ids"] == ["R5"]
        for event, payload in trace_events
    ) == 2
    assert any(
        event == "requirement_completion_budget_exhausted"
        and payload["missing_ids"] == ["R5"]
        for event, payload in trace_events
    )


def test_natural_completion_at_work_limit_never_preserves_false_success_text(
    tmp_path: Path,
):
    """MAX_TURNS output must be Runtime-owned truth, not model self-report."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.has_diff = True
    state.mutation_generation = 2
    state.changed_files = ["README.md"]
    command = "python -m pytest -q cron_engine/tests"
    state.verification_contract = VerificationContract(
        command=command,
        targets=("cron_engine/tests",),
        attempted_generation=2,
        attempts=1,
        passed=True,
        output="99 passed",
        source="runtime",
        zone="completion",
    ).to_dict()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Update the package documentation",
        "requirements": [{
            "id": "R5",
            "description": "Update package README examples",
            "kind": "docs",
            "expected_artifacts": ["cron_engine/README.md"],
        }],
    }, workspace=tmp_path))
    sessions = _Sessions()
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=_VerificationTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish documentation"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update package docs"},),
        workspace=tmp_path,
        session_id="truthful-work-limit-summary",
        stream=False,
    )

    async def exercise():
        run_context = await sessions.open(request)
        context = replace(
            _execution_context(run_context, services),
            runtime_state=state,
        )
        return await AgentRunner(
            services,
            execution_context_factory=_execution_context,
        )._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="All requested work is complete and verified.",
            completed_turns=20,
            work_budget=WorkBudgetController(max_turns=20),
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )

    decision = asyncio.run(exercise())

    assert decision.action == "finalize"
    assert decision.status == "max_turns"
    assert decision.reason == "hard_requirements_unresolved"
    assert "without claiming completion" in decision.content
    assert f"Exact acceptance passed: `{command}`" in decision.content
    assert "Exact acceptance did not pass" not in decision.content
    assert "Unresolved requirements: R5" in decision.content
    assert "All requested work is complete" not in decision.content


def test_model_hard_cap_acknowledgement_uses_runtime_owned_summary(tmp_path: Path):
    """Even an evidence-complete MAX_TURNS result must not echo model prose."""
    state = RuntimeState()
    state.has_diff = True
    state.mutation_generation = 1
    state.changed_files = ["app.py"]
    command = "pytest -q tests/test_app.py"
    state.verification_contract = VerificationContract(
        command=command,
        targets=("tests/test_app.py",),
        attempted_generation=1,
        attempts=1,
        passed=True,
        output="1 passed",
        source="runtime",
        zone="completion",
    ).to_dict()
    sessions = _Sessions()
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=_VerificationTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update app"},),
        workspace=tmp_path,
        session_id="model-hard-cap-summary",
        stream=False,
    )

    async def exercise():
        run_context = await sessions.open(request)
        return await AgentRunner(
            services,
            execution_context_factory=_execution_context,
        )._settle_terminal_boundary(
            replace(
                _execution_context(run_context, services),
                runtime_state=state,
            ),
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="Maximum number of steps reached; everything is complete.",
            completed_turns=20,
            work_budget=WorkBudgetController(max_turns=20),
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )

    decision = asyncio.run(exercise())

    assert decision.status == "max_turns"
    assert decision.reason == "model_reported_hard_cap_exhaustion"
    assert "without claiming completion" in decision.content
    assert f"Exact acceptance passed: `{command}`" in decision.content
    assert "everything is complete" not in decision.content


def test_completion_review_budget_exhaustion_uses_runtime_owned_summary(
    tmp_path: Path,
):
    """A verifier stop cannot return the model's false completion claim."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.has_diff = True
    state.mutation_generation = 1
    state.changed_files = ["app.py"]
    command = "pytest -q tests/test_app.py"
    state.verification_contract = VerificationContract(
        command=command,
        targets=("tests/test_app.py",),
        attempted_generation=1,
        attempts=1,
        passed=True,
        output="1 passed",
        source="runtime",
        zone="completion",
    ).to_dict()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
        "acceptance_commands": [command],
    }, workspace=tmp_path))
    state._observe_requirement_mutation(["app.py"])
    state.observe_requirement_verification(command, passed=True, acceptance=True)
    sessions = _Sessions()
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=_VerificationTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="preserve compatibility"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "preserve compatibility"},),
        workspace=tmp_path,
        session_id="completion-review-budget-summary",
        stream=False,
    )

    async def exercise():
        run_context = await sessions.open(request)
        native = _execution_context(run_context, services)
        policy_members = dict(vars(native.policy))
        policy_members["verify_completion"] = (
            lambda _messages, _status, _content: _async_value("max_turns")
        )
        return await AgentRunner(
            services,
            execution_context_factory=_execution_context,
        )._settle_terminal_boundary(
            replace(
                native,
                runtime_state=state,
                policy=SimpleNamespace(**policy_members),
            ),
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="All requested work is complete and verified.",
            completed_turns=10,
            work_budget=WorkBudgetController(max_turns=20),
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )

    decision = asyncio.run(exercise())

    assert decision.action == "finalize"
    assert decision.status == "max_turns"
    assert "without claiming completion" in decision.content
    assert f"Exact acceptance passed: `{command}`" in decision.content
    assert "Unresolved requirements: R1" in decision.content
    assert "All requested work is complete" not in decision.content


def test_completion_guidance_repair_then_semantic_review_closes_mixed_ledger(
    tmp_path: Path,
):
    """The mixed docs/compatibility path must converge through one Runner."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    command = "pytest -q cron_engine/tests"
    state = RuntimeState()
    state.has_diff = True
    state.mutation_generation = 1
    state.changed_files = ["README.md", "cron_engine/parser.py"]
    state.verification_contract = VerificationContract(
        command=command,
        targets=("cron_engine/tests",),
        attempted_generation=1,
        attempts=1,
        passed=True,
        output="99 passed",
        source="model",
    ).to_dict()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Add names while preserving compatibility and update docs",
        "requirements": [
            {
                "id": "R5",
                "description": "Update package README examples",
                "kind": "docs",
                "expected_artifacts": ["cron_engine/README.md"],
            },
            {
                "id": "R6",
                "description": "Preserve numeric range compatibility",
                "kind": "compatibility",
            },
        ],
        "acceptance_commands": [command],
    }, workspace=tmp_path))
    state._observe_requirement_mutation(state.changed_files)
    state.observe_requirement_verification(command, passed=True, acceptance=True)
    sessions = _Sessions()
    services = RuntimeServices(
        model=_EarlyStopModel(),
        tools=_VerificationTools(),
        context=_Context(),
        session_runtime=sessions,
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=_Lifecycle(),
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="finish the contract"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "finish docs and compatibility"},),
        workspace=tmp_path,
        session_id="mixed-ledger-convergence",
        stream=False,
    )
    verifier_calls = []

    async def exercise():
        run_context = await sessions.open(request)
        native = _execution_context(run_context, services)
        policy_members = dict(vars(native.policy))

        async def verify_completion(_messages, status, content):
            verifier_calls.append(content)
            state.observe_requirement_semantic_review(
                accepted=True,
                fingerprint="verifier_ok:compatibility",
            )
            return status

        policy_members["verify_completion"] = verify_completion
        context = replace(
            native,
            runtime_state=state,
            policy=SimpleNamespace(**policy_members),
        )
        runner = AgentRunner(services, execution_context_factory=_execution_context)
        budget = WorkBudgetController(max_turns=20)
        blocked = await runner._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="Everything is complete.",
            completed_turns=10,
            work_budget=budget,
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )

        state.observe_tool(
            "edit_file",
            {"path": "cron_engine/README.md"},
            "Updated cron_engine/README.md",
            succeeded=True,
        )
        state.changed_files = ["cron_engine/README.md", "cron_engine/parser.py"]
        reviewed = await runner._settle_terminal_boundary(
            context,
            services,
            run_context,
            run_context.transcript,
            boundary="natural_completion",
            content="Implemented and verified the requested changes.",
            completed_turns=11,
            work_budget=budget,
            resolve_tool_runtime_context=lambda: object(),
            natural_completion=True,
        )
        return blocked, reviewed, run_context.transcript

    blocked, reviewed, messages = asyncio.run(exercise())

    assert blocked.action == "continue"
    assert blocked.reason == "hard_requirements_unresolved"
    guidance = next(
        message for message in messages
        if message.get("_nz_completion_gate") is True
    )["content"]
    repair_section, review_section = guidance.split(
        "Runtime-owned evidence pending", 1,
    )
    assert "R5: Update package README examples" in repair_section
    assert "R6: Preserve numeric range compatibility" not in repair_section
    assert "R6: Preserve numeric range compatibility" in review_section
    assert state.semantic_review_pending_only() is False
    assert not state.requirement_ledger_snapshot().unresolved()
    assert len(verifier_calls) == 1
    assert reviewed.action == "finalize"
    assert reviewed.status == "completed"
    assert reviewed.reason == "semantic_review_and_ledger_satisfied"


def test_natural_completion_budget_exhaustion_preserves_configured_turn_cap(
    tmp_path: Path,
):
    """Lifecycle/UI must never receive ``max_turns=None`` for an early gate stop."""
    from nz_coder.runtime.agent.task_contract import TaskContract

    class RecordingLifecycle(_DefaultTurnLifecycle):
        def __init__(self):
            self.finalize_kwargs = []

        async def finalize(self, context, messages, status, **kwargs):
            self.finalize_kwargs.append((status, dict(kwargs)))
            return await super().finalize(context, messages, status, **kwargs)

    model = _EarlyStopModel()
    lifecycle = RecordingLifecycle()
    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Update package docs",
        "requirements": [{
            "id": "R1",
            "description": "Update package README",
            "kind": "docs",
            "expected_artifacts": ["pkg/README.md"],
        }],
    }, workspace=tmp_path))
    services = RuntimeServices(
        model=model,
        tools=_BudgetTools(),
        context=_Context(),
        session_runtime=_Sessions(),
        events=_Events(),
        host=_UnusedHost(),
        memory=_Memory(),
        verifier=_Verifier(),
        lifecycle=lifecycle,
        guardrails=_Guardrails(),
        inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="native", instructions="update docs"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update package docs"},),
        workspace=tmp_path,
        session_id="natural-gate-turn-cap",
        stream=False,
        metadata={"max_turns": 20},
    )

    def execution_context(run_context, runtime_services):
        return replace(
            _execution_context(run_context, runtime_services),
            runtime_state=state,
        )

    result = asyncio.run(AgentRunner(
        services,
        execution_context_factory=execution_context,
    ).run_result(request, options=request_contracts.RunOptions(stream=False)))

    assert result.status is RunStatus.MAX_TURNS
    assert model.calls == 20
    assert lifecycle.finalize_kwargs[-1][0] == "max_turns"
    assert lifecycle.finalize_kwargs[-1][1]["max_turns"] == 20
