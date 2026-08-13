"""Behavioral contract for running the core without constructing AgentLoop."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core import request as request_contracts
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.model_result import LLMResult
from nz_coder.runtime.runner import AgentRunner
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.core.run_context import RunContext


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
