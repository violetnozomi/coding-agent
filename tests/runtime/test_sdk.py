"""Public SDK acceptance tests against the production Agent entry chain."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, AgentHandoff, RunOptions, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.result import RunResult, TokenUsage
from nz_coder.runtime.guardrails import InputGuardrail, OutputGuardrail
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.sdk import AgentClient, _agent_graph_from_definition, run_agent


class FakeProductionAgent:
    def __init__(self) -> None:
        self.closed = False
        self.current_agent_name = "sdk"
        self.trace_id = "trace-sdk"

    async def run(self, messages, **kwargs):
        messages.append({
            "role": "assistant",
            "content": "sdk complete",
            "_nz_usage": {"input": 4, "output": 2, "reasoning": 1},
        })
        return {
            "status": "completed",
            "runtime": {"active_agent": "sdk", "turn_count": 1},
        }

    def close(self):
        self.closed = True


def _request(tmp_path):
    return RunRequest(
        agent=AgentDefinition(name="sdk", instructions="Complete the task."),
        profile=MAIN_PROFILE,
        messages=[{"role": "user", "content": "work"}],
        workspace=tmp_path,
        session_id="sdk-session",
        stream=False,
    )


def test_agent_client_returns_stable_run_result_and_closes_host(tmp_path):
    agent = FakeProductionAgent()
    client = AgentClient(agent_factory=lambda request: agent)

    result = asyncio.run(client.run(_request(tmp_path)))

    assert result.status is RunStatus.COMPLETED
    assert result.final_text == "sdk complete"
    assert result.messages[-1]["role"] == "assistant"
    assert result.usage.input_tokens == 4
    assert result.usage.output_tokens == 2
    assert result.usage.reasoning_tokens == 1
    assert result.metadata["trace_id"] == "trace-sdk"
    assert agent.closed is True


def test_one_shot_sdk_uses_same_production_agent_contract(tmp_path):
    agent = FakeProductionAgent()

    result = asyncio.run(run_agent(
        _request(tmp_path),
        agent_factory=lambda request: agent,
    ))

    assert result.status is RunStatus.COMPLETED
    assert result.session_id == "sdk-session"


def test_sdk_projects_agent_policy_into_runtime_graph():
    input_guard = InputGuardrail("scope", lambda **kwargs: {"action": "allow"})
    output_guard = OutputGuardrail("shape", lambda **kwargs: {"action": "allow"})
    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    definition = AgentDefinition(
        name="owner",
        instructions="Return structured output.",
        description="Final owner",
        allowed_tools=("read_file",),
        provider="openai-compatible",
        model="gpt-test",
        reasoning_effort="high",
        guardrails=(input_guard, output_guard),
        output_schema=schema,
    )

    graph = _agent_graph_from_definition(definition)
    spec = graph.agent("owner")

    assert spec.instructions == definition.instructions
    assert spec.description == "Final owner"
    assert spec.allowed_tools == ("read_file",)
    assert spec.provider == "openai-compatible"
    assert spec.model == "gpt-test"
    assert spec.effort == "high"
    assert spec.guardrails == (input_guard, output_guard)
    assert spec.output_schema == schema


def test_sdk_projects_nested_handoffs_into_executable_graph():
    reviewer = AgentDefinition(
        name="reviewer",
        instructions="Review the patch and finish.",
        allowed_tools=("read_file", "grep_search"),
    )
    owner = AgentDefinition(
        name="owner",
        instructions="Implement the patch.",
        handoffs=(AgentHandoff(
            target=reviewer,
            kind="continuation",
            description="Independent review",
        ),),
    )

    graph = _agent_graph_from_definition(owner)

    assert graph.names() == ("owner", "reviewer")
    assert graph.handoff("owner", "reviewer").description == "Independent review"
    assert graph.agent("reviewer").allowed_tools == ("read_file", "grep_search")


def test_sdk_rejects_unresolved_handoff_declarations():
    definition = AgentDefinition(
        name="owner",
        instructions="Implement.",
        handoffs=(object(),),
    )

    try:
        _agent_graph_from_definition(definition)
    except TypeError as exc:
        assert "AgentHandoff" in str(exc)
    else:
        raise AssertionError("unresolved SDK handoff must fail before runtime")


class FakeNativeRunner:
    def __init__(self) -> None:
        self.requests = []

    async def run_result(self, request, options=None):
        self.requests.append(request)
        return RunResult(
            status=RunStatus.COMPLETED,
            final_text="native complete",
            messages=request.messages,
            usage=TokenUsage(input_tokens=2, output_tokens=1),
            session_id=request.session_id,
            active_agent=request.agent.name,
        )


def test_agent_client_default_selects_native_sdk_runner(monkeypatch, tmp_path):
    runner = FakeNativeRunner()
    constructed = []

    def build_runner():
        constructed.append(True)
        return runner

    monkeypatch.setattr("nz_coder.sdk.build_native_sdk_runner", build_runner)

    result = asyncio.run(AgentClient().run(_request(tmp_path)))

    assert result.final_text == "native complete"
    assert constructed == [True]
    assert runner.requests[0].session_id == "sdk-session"


def test_agent_client_direct_runner_path_never_constructs_legacy_host(tmp_path):
    runner = FakeNativeRunner()
    client = AgentClient(runner=runner)

    result = asyncio.run(client.run(_request(tmp_path)))

    assert result.final_text == "native complete"
    assert runner.requests[0].session_id == "sdk-session"


def test_run_options_rejects_non_callable_event_sink():
    try:
        RunOptions(on_event="not-callable")
    except TypeError as exc:
        assert "on_event" in str(exc)
    else:
        raise AssertionError("non-callable event sink must be rejected")


def test_sdk_child_and_resume_reuse_parent_linked_session(tmp_path):
    runner = FakeNativeRunner()
    client = AgentClient(runner=runner)
    child = AgentDefinition(name="reviewer", instructions="Review the patch.")

    first = asyncio.run(client.run_child(
        parent=_request(tmp_path),
        agent=child,
        prompt="review once",
        session_id="child-1",
    ))
    resumed = asyncio.run(client.run_child(
        parent=_request(tmp_path),
        agent=child,
        prompt="continue review",
        session_id="child-1",
    ))

    assert first.session_id == resumed.session_id == "child-1"
    assert runner.requests[0].metadata["parent_session_id"] == "sdk-session"
    assert runner.requests[1].messages == ({"role": "user", "content": "continue review"},)


def test_sdk_exports_stable_runtime_contracts() -> None:
    from nz_coder.sdk import Agent, AgentRunner, Session, Tool

    assert Agent is AgentDefinition
    assert AgentRunner.__name__ == "AgentRunner"
    assert Session.__name__ == "Session"
    assert Tool is not None


def test_default_sdk_completes_offline_model_tool_model(monkeypatch, tmp_path):
    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                message = SimpleNamespace(
                    content="",
                    tool_calls=[SimpleNamespace(
                        id="call-list",
                        type="function",
                        function=SimpleNamespace(
                            name="list_directory",
                            arguments='{"path": ".", "depth": 1}',
                        ),
                    )],
                )
                finish = "tool_calls"
            else:
                message = SimpleNamespace(content="workspace inspected", tool_calls=[])
                finish = "stop"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason=finish)],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )

    provider = Provider()
    runtime = ResolvedModelRuntime(
        provider_id="offline",
        model_id="offline-model",
        request_model_id="offline-model",
        variant=None,
        provider=provider,
        client=object(),
        capabilities=ModelCapabilities(
            provider="offline",
            model_id="offline-model",
            supports_streaming=False,
        ),
        owns_client=False,
    )
    monkeypatch.setattr("nz_coder.runtime.native_sdk.resolve_model_runtime", lambda _request: runtime)
    request = RunRequest(
        agent=AgentDefinition(
            name="sdk",
            instructions="Inspect the workspace.",
            allowed_tools=("list_directory",),
        ),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "inspect"},),
        workspace=tmp_path,
        session_id="sdk-offline-native",
        stream=False,
        metadata={"permission_mode": "auto"},
    )

    events = []
    result = asyncio.run(AgentClient().run(request, on_event=events.append))

    assert result.status is RunStatus.COMPLETED
    assert result.final_text == "workspace inspected"
    assert provider.calls == 2
    assert [event.name.value for event in events] == [
        "session.created",
        "session.run.started",
        "session.model.started",
        "session.model.finished",
        "session.tool.started",
        "session.tool.finished",
        "session.model.started",
        "session.model.finished",
        "session.run.completed",
    ]
    assert [message["role"] for message in result.messages] == [
        "user", "assistant", "tool", "assistant",
    ]
