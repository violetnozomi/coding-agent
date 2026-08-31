"""Contract tests for the single production AgentRunner state machine."""
from __future__ import annotations

import asyncio
from pathlib import Path

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, PARTS_KEY, SESSION_ID_KEY
from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.core.contracts import RuntimeServices
from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.execution.runner import _result_status


class UnusedModel:
    async def complete_turn(self, host, messages, **kwargs):
        raise AssertionError("zero-turn test must not call the model")


class UnusedTools:
    async def execute_batch_async(self, host, calls, messages, **kwargs):
        raise AssertionError("zero-turn test must not call tools")


class UnusedContext:
    async def prepare_async(self, context, messages, **kwargs):
        raise AssertionError("zero-turn test must not prepare context")


class RecordingSessions:
    def __init__(self) -> None:
        self.statuses = []

    def checkpoint(self, host, messages, run_status):
        self.statuses.append(run_status)


class RecordingSessionRuntime:
    def __init__(self) -> None:
        self.opened = []
        self.checkpoints = []
        self.final_statuses = []

    async def open(self, request: RunRequest) -> RunContext:
        self.opened.append(request.session_id)
        session = Session.create(
            request.session_id,
            request.messages,
            workspace=request.workspace,
        )
        session.begin_run()
        return RunContext(request, session, request.agent.name)

    async def checkpoint(self, context, status):
        self.checkpoints.append((context.session.session_id, str(status)))

    async def finalize(self, context, status):
        self.final_statuses.append(status)
        context.finish(status)
        context.finalized = True


class UnusedEvents:
    def publish(self, host, event_type, payload):
        return None


class UnusedMemory:
    def prompt_block(self, host, query):
        return ""

    async def finalize(self, host, messages, status):
        return None


class UnusedVerifier:
    async def verify(self, host, messages, status, content):
        return status


class TestLifecycle:
    def initialize(self, host, messages, stream):
        return host._init_run(messages, stream)

    async def finalize(self, host, messages, status, *args, **kwargs):
        return await host._finalize_async(messages, status, *args, **kwargs)

    def finalize_sync(self, host, messages, status, *args, **kwargs):
        return {"status": status}


class PassThroughGuardrails:
    def has(self, host, kind):
        return False

    async def run_input(self, host, messages):
        return None

    async def run_output(self, host, content, messages):
        return content

    async def before_tool(self, host, tool_call, messages):
        return tool_call, None

    async def after_tool(self, host, tool_call, result, messages):
        return result


class NoInputPreflight:
    async def prepare_user_images(self, host, messages, owner):
        return "skipped"

    async def prepare_user_documents(self, host, messages, owner):
        return "skipped"

    async def describe_read_results(self, host, dispatched, messages):
        return False


class NoAgentTransitions:
    def signal_from_metadata(self, host, metadata):
        return None

    def apply(self, host, signal, messages, processor):
        return None

    def resolve_structured_output(self, host, content, messages):
        return False

    def return_from_as_tool(self, host, messages, summary=""):
        return {}

    async def terminal_content(self, host, fallback, messages):
        return fallback


class SettledHost:
    def __init__(self, *, interrupt: bool = False) -> None:
        self.session_id = "runner-session"
        self.interrupt = interrupt
        self.finalized = []
        self.runtime_host = DirectRuntimeHost()
        self.workdir = Path.cwd()
        self.system_prompt = "Fix the repository."
        self.runtime_profile = "coding"
        self.current_agent_name = "coder"
        self.tool_allowlist = None
        self.active_run_context = None

    def _init_run(self, messages, stream):
        return 0, 0

    async def _run_input_guardrails(self, messages):
        return None

    def _checkpoint_messages(self, messages, status):
        return None

    async def _maybe_generate_plan(self, messages):
        if self.interrupt:
            raise KeyboardInterrupt

    async def _finalize_async(self, messages, status, *args, **kwargs):
        self.finalized.append(status)
        return {"status": status}


class DirectRuntimeHost:
    async def run(self, agent, messages, *, execute, **kwargs):
        return await execute(
            agent,
            messages,
            kwargs.get("on_tool"),
            kwargs.get("on_text"),
            kwargs.get("on_token"),
            kwargs.get("stream", True),
        )


def test_provider_error_abort_maps_to_error_not_user_interruption():
    """Recovery exhaustion and Ctrl+C are distinct product outcomes."""
    assert _result_status({"status": "aborted"}) is RunStatus.ERROR
    assert _result_status({"status": "interrupted"}) is RunStatus.INTERRUPTED


def _services(
    host: SettledHost,
    *,
    session_runtime: RecordingSessionRuntime | None = None,
) -> RuntimeServices:
    values = dict(
        model=UnusedModel(),
        tools=UnusedTools(),
        context=UnusedContext(),
        session_runtime=session_runtime or RecordingSessionRuntime(),
        events=UnusedEvents(),
        host=host.runtime_host,
        memory=UnusedMemory(),
        verifier=UnusedVerifier(),
        lifecycle=TestLifecycle(),
        guardrails=PassThroughGuardrails(),
        inputs=NoInputPreflight(),
        transitions=NoAgentTransitions(),
    )
    return RuntimeServices(**values)


def test_runner_owns_terminal_turn_limit_path():
    host = SettledHost()
    messages = [{"role": "user", "content": "work"}]

    result = asyncio.run(AgentRunner(_services(host)).run(
        host, messages, stream=False,
    ))

    assert result == {"status": "max_turns"}
    assert host.finalized == ["max_turns"]
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "work"
    assert messages[0][MESSAGE_ID_KEY].startswith("msg-")
    assert messages[0][SESSION_ID_KEY] == "runner-session"
    assert messages[0][PARTS_KEY][0]["text"] == "work"


def test_runner_checkpoints_session_runtime_before_terminal_turn_limit():
    host = SettledHost()
    runtime = RecordingSessionRuntime()

    result = asyncio.run(AgentRunner(_services(host, session_runtime=runtime)).run(
        host, [{"role": "user", "content": "work"}], stream=False,
    ))

    assert result == {"status": "max_turns"}
    assert runtime.checkpoints == [("runner-session", "running")]


def test_runner_opens_run_context_and_uses_session_runtime_checkpoint():
    host = SettledHost()
    runtime = RecordingSessionRuntime()
    messages = [{"role": "user", "content": "work"}]

    result = asyncio.run(AgentRunner(_services(
        host,
        session_runtime=runtime,
    )).run(host, messages, stream=False))

    assert result == {"status": "max_turns"}
    assert runtime.opened == ["runner-session"]
    assert runtime.checkpoints == [("runner-session", "running")]
    assert host.active_run_context is None
    assert messages[0][MESSAGE_ID_KEY].startswith("msg-")


def test_runner_normalizes_keyboard_interrupt():
    host = SettledHost(interrupt=True)

    result = asyncio.run(AgentRunner(_services(host)).run(
        host, [{"role": "user", "content": "work"}], stream=False,
    ))

    assert result == {"status": "interrupted"}
    assert host.finalized == ["interrupted"]


def test_legacy_runner_finalizes_native_session_when_planning_raises():
    """Compatibility entry must obey the native catch-finalization contract."""
    host = SettledHost()
    runtime = RecordingSessionRuntime()

    async def fail_planning(_messages):
        raise RuntimeError("planning exploded")

    host._maybe_generate_plan = fail_planning
    messages = [{"role": "user", "content": "work"}]

    try:
        asyncio.run(AgentRunner(_services(
            host,
            session_runtime=runtime,
        )).run(host, messages, stream=False))
    except RuntimeError as exc:
        assert str(exc) == "planning exploded"
    else:
        raise AssertionError("planning failure must propagate")

    assert runtime.final_statuses == [RunStatus.ERROR]
    assert host.active_run_context is None
