"""Deterministic long-horizon scenario through the canonical native AgentRunner."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.runner_context import RunnerExecutionContext
from nz_coder.runtime.conversation.message_projection import project_provider_messages
from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot, message_version
from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.agent.task_intent import initialize_task_assessment
from nz_coder.runtime.verification.domain.models import TaskScope, RunScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import EphemeralSessionStore
from nz_coder.runtime.verification.recovery import RecoveryState


class _LongModel:
    def __init__(self, tool_turns: int, events: list[dict]) -> None:
        self.tool_turns = tool_turns
        self.calls = 0
        self.events = events

    async def complete_turn(self, _context, _messages, **_kwargs):
        self.calls += 1
        self.events.append({
            "event": "model_call", "input_tokens": 10,
            "output_tokens": 1 if self.calls <= self.tool_turns else 3,
            "context_window": 20_000,
        })
        if self.calls <= self.tool_turns:
            call_id = f"call-{self.calls}"
            return LLMResult(
                content="", tool_calls=[{
                    "id": call_id, "type": "function",
                    "function": {"name": "read_value", "arguments": "{}"},
                }], finish_reason="tool_calls", input_tokens=10, output_tokens=1,
            )
        return LLMResult(
            content="long horizon complete", finish_reason="stop",
            input_tokens=10, output_tokens=3,
        )


class _Tools:
    def __init__(self, events: list[dict]) -> None:
        self.results = 0
        self.events = events

    async def execute_batch_async(
        self, _context, calls, messages, _on_tool=None, _on_text=None, **kwargs,
    ):
        processor = kwargs["processor"]
        processor.start_tools(calls)
        for call in calls:
            call_id = str(call["id"])
            processor.complete_tool(call_id, "value")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": "value"})
            self.results += 1
            self.events.append({
                "event": "tool_result", "tool_name": "read_file",
                "path": f"file-{self.results}.py", "tokens": 1,
            })
        processor.finish_step("tool-calls")
        return "continue"


class _Sessions(SessionRuntime):
    def __init__(self) -> None:
        super().__init__(EphemeralSessionStore())
        self.context: RunContext | None = None

    async def open(self, request: RunRequest, *, session_lease=None) -> RunContext:
        self.context = await super().open(request, session_lease=session_lease)
        return self.context


class _Context:
    async def prepare_async(self, _context, _messages, **_kwargs) -> bool:
        return False


class _Lifecycle:
    def __init__(self, max_turns: int) -> None:
        self.max_turns = max_turns

    def initialize(self, _context, _messages, _stream):
        return self.max_turns, 0

    async def finalize(self, _context, _messages, status, **kwargs):
        return {"status": status, "content": kwargs.get("content_text", "")}

    def finalize_sync(self, _context, _messages, status, **_kwargs):
        return {"status": status}


class _Events:
    def publish(self, _owner, _event_type, _payload) -> None:
        return None


class _Host:
    async def run(self, *_args, **_kwargs):
        raise AssertionError("native scenario must not enter the legacy host")


class _Memory:
    def prompt_block(self, _owner, _query):
        return ""

    async def finalize(self, _owner, _messages, _status) -> None:
        return None


class _Verifier:
    async def verify(self, _owner, _messages, status, _content):
        return status


class _Guardrails:
    async def run_input(self, _owner, _messages):
        return None

    def has(self, _owner, _kind):
        return False

    async def run_output(self, _owner, content, _messages):
        return content

    async def before_tool(self, _owner, call, _messages):
        return call, None

    async def after_tool(self, _owner, _call, result, _messages):
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


async def _none() -> None:
    return None


async def _value(value):
    return value


@dataclass(frozen=True)
class _ScenarioToolContext:
    assessment_scope: object | None = None
    working_plan_scope: object | None = None
    repeat_source: object | None = None


def _execution_context(run_context: RunContext, services: RuntimeServices):
    sequence = 0
    state = RuntimeState()
    state._verification_context = run_context
    run_context.attach_verification(VerificationDomain(
        lock=run_context._state_lock,
        scope=RunScope(TaskScope(run_context.session.session_id, "scenario", str(run_context.request.workspace)),
                       run_context.interaction_run_id, run_context.active_agent),
        version=WorkspaceVersion(0, 0, 0),
    ))
    initialize_task_assessment(state, run_context, run_context.transcript, pure_continuation=False)

    def new_message_part(_turn: int) -> dict:
        nonlocal sequence
        sequence += 1
        return {"message_id": f"msg-benchmark-{sequence}"}

    def materialize(result, *, assistant_message, processor, **_kwargs) -> None:
        assistant_message["content"] = result.content or ""
        if result.tool_calls:
            assistant_message["tool_calls"] = result.tool_calls
            processor.register_tool_calls(result.tool_calls)
        if result.content:
            processor.stream_text(result.content, part_id=f"part-{sequence}")

    async def finalize(messages, status, *_args, **kwargs):
        return await services.lifecycle.finalize(run_context, messages, status, **kwargs)

    def freeze_request(messages: list) -> FrozenRequestSnapshot:
        # 受控基准也走真实请求快照和历史投影；这里没有动态指令或工具 schema。
        return FrozenRequestSnapshot(
            system=run_context.request.agent.instructions,
            dynamic="",
            instruction_reminder="",
            source_version=message_version(messages),
            project_history=project_provider_messages,
            _tools=(),
        )

    return RunnerExecutionContext(
        session_id=run_context.session.session_id,
        runtime_state=state,
        execution=SimpleNamespace(
            context=lambda: SimpleNamespace(freeze_request=freeze_request),
            model=lambda: SimpleNamespace(working_memory_allowed=lambda: False),
            tools=lambda: _ScenarioToolContext(), recovery=RecoveryState(),
        ),
        lifecycle=SimpleNamespace(
            initialize=lambda messages, stream: services.lifecycle.initialize(
                run_context, messages, stream,
            ), finalize=finalize,
            settle_session_checkpoints=_none,
            publish_terminal=lambda _error=None: None,
        ),
        policy=SimpleNamespace(
            run_input_guardrails=lambda _messages: _none(),
            has_output_guardrail=lambda: False,
            run_output_guardrail=lambda content, _messages: _value(content),
            prepare_user_images=lambda _messages, _owner: _value("skipped"),
            prepare_user_documents=lambda _messages, _owner: _value("skipped"),
            resolve_structured_output=lambda _content, _messages: False,
            return_from_as_tool=lambda _messages, _content: {},
            terminal_content=lambda content, _messages: _value(content),
            verify_completion=lambda _messages, status, _content: _value(status),
            prepare_completion=lambda: None,
            verification_status=lambda: run_context.verification.snapshot().to_dict(),
            # 受控情景没有真实交付评审；保留域裁决的未验证状态。
            settle_completion=lambda _messages, _content, **_kwargs: _value(
                run_context.verification.snapshot().assess_completion("completed").status),
        ),
        planning=SimpleNamespace(generate=lambda _messages: _none()),
        control=SimpleNamespace(
            has_queued_followup=lambda: False,
            current_agent_name=lambda: run_context.active_agent,
            drain_background_messages=lambda _messages: None,
            has_agent_call_stack=lambda: False,
            notify_agent_switched=lambda _transition: _none(),
            persist_runtime_state=lambda **_kwargs: None,
            stop_hook_reason=lambda: "",
            structured_output=lambda: None,
        ),
        hooks=SimpleNamespace(
            on_turn_start=lambda _messages: None, on_pre_send=lambda _messages: None,
            on_turn_end=lambda _messages, _status: None,
            trace=lambda *_args, **_kwargs: None,
        ),
        messages=SimpleNamespace(
            persist_compaction_exhaustion=lambda *_args, **_kwargs: None,
            bind_assistant_context=lambda _message: None,
            bind_user_contexts=lambda _messages: None,
            new_message_part=new_message_part,
            publish_event=lambda *_args, **_kwargs: None,
            materialize_llm_result=materialize, reconcile_llm_result=materialize,
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
            await_start=lambda _task, _cancel: _value(None),
            retire=lambda *_args: None,
            capture_async=lambda *_args: _value(None),
            record_patch=lambda *_args: None,
        ),
    )


def run_native_long_horizon(workspace: Path, tool_turns: int = 40) -> dict:
    """Run Model→Tool cycles through AgentRunner and return observable counts."""
    events: list[dict] = []
    model = _LongModel(tool_turns, events)
    tools = _Tools(events)
    sessions = _Sessions()
    lifecycle = _Lifecycle(tool_turns + 2)
    services = RuntimeServices(
        model=model, tools=tools, context=_Context(), session_runtime=sessions,
        events=_Events(), host=_Host(), memory=_Memory(), verifier=_Verifier(),
        lifecycle=lifecycle, guardrails=_Guardrails(), inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="benchmark", instructions="exercise long horizon"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "run deterministic scenario"},),
        workspace=Path(workspace), session_id="core-capability-long-horizon",
        tool_names=("read_value",), stream=False,
    )
    result = asyncio.run(AgentRunner(
        services, execution_context_factory=_execution_context,
    ).run(request, options=RunOptions(stream=False)))
    transcript = sessions.context.transcript if sessions.context is not None else []
    return {
        "result": result, "model_calls": model.calls, "tool_results": tools.results,
        "transcript_messages": len(transcript), "events": events,
    }


def run_native_agent_scenario(
    workspace: Path, *, prompt: str, model, tools, max_turns: int,
    tool_names: tuple[str, ...], session_id: str,
) -> dict:
    """Run supplied real/controllable model services through canonical AgentRunner."""
    sessions = _Sessions()
    lifecycle = _Lifecycle(max(1, int(max_turns)))
    services = RuntimeServices(
        model=model, tools=tools, context=_Context(), session_runtime=sessions,
        events=_Events(), host=_Host(), memory=_Memory(), verifier=_Verifier(),
        lifecycle=lifecycle, guardrails=_Guardrails(), inputs=_Inputs(),
        transitions=_Transitions(),
    )
    request = RunRequest(
        agent=AgentDefinition(name="behavior-benchmark", instructions="solve the repository task"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": prompt},),
        workspace=Path(workspace), session_id=session_id,
        tool_names=tool_names, stream=False,
    )
    result = asyncio.run(AgentRunner(
        services, execution_context_factory=_execution_context,
    ).run(request, options=RunOptions(stream=False)))
    transcript = sessions.context.transcript if sessions.context is not None else []
    return {"result": result, "transcript": transcript}


__all__ = ["run_native_agent_scenario", "run_native_long_horizon"]
