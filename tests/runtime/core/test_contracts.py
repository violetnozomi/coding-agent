"""Structural service ports for the future shared AgentRunner."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nz_coder.runtime.core.contracts import (
    ContextManager,
    ModelGateway,
    RuntimeServices,
    SessionRuntimePort,
    ToolRuntime,
)
from nz_coder.runtime.core.events import RuntimeEvent, RuntimeEventSink


class FakeModelGateway:
    async def complete_turn(self, host, messages, **kwargs):  # noqa: ANN001
        return {"content": "done"}


class FakeToolRuntime:
    async def execute_batch_async(self, host, calls, messages, **kwargs):  # noqa: ANN001
        return tuple({"id": call.get("id")} for call in calls)


class FakeContextManager:
    async def prepare_async(self, context, messages, **kwargs) -> None:  # noqa: ANN001
        return None


class FakeSessionRuntime:
    async def open(self, request):  # noqa: ANN001
        return None

    async def checkpoint(self, context, status):  # noqa: ANN001
        return None

    async def finalize(self, context, status):  # noqa: ANN001
        return None


class RecordingEventSink:
    def __init__(self) -> None:
        self.events = []

    def publish(self, host, event_type, payload) -> None:  # noqa: ANN001
        self.events.append((event_type, payload))


class FakeRuntimeHost:
    async def run(self, agent, messages, *, execute, **kwargs):  # noqa: ANN001
        return await execute(agent, messages, **kwargs)


class FakeMemoryService:
    def prompt_block(self, host, query):  # noqa: ANN001
        return ""

    async def finalize(self, host, messages, status):  # noqa: ANN001
        return None


class FakeCompletionVerifier:
    async def verify(self, host, messages, status, content):  # noqa: ANN001
        return status


class FakeRunLifecycle:
    def initialize(self, host, messages, stream):  # noqa: ANN001
        return 0, 0

    async def finalize(self, host, messages, status, *args, **kwargs):  # noqa: ANN001
        return {"status": status}

    def finalize_sync(self, host, messages, status, *args, **kwargs):  # noqa: ANN001
        return {"status": status}


class FakeGuardrailRuntime:
    def has(self, host, kind):  # noqa: ANN001
        return False

    async def run_input(self, host, messages):  # noqa: ANN001
        return None

    async def run_output(self, host, content, messages):  # noqa: ANN001
        return content

    async def before_tool(self, host, tool_call, messages):  # noqa: ANN001
        return tool_call, None

    async def after_tool(self, host, tool_call, result, messages):  # noqa: ANN001
        return result


class FakeInputPreflight:
    async def prepare_user_images(self, host, messages, owner):  # noqa: ANN001
        return "skipped"

    async def prepare_user_documents(self, host, messages, owner):  # noqa: ANN001
        return "skipped"

    async def describe_read_results(self, host, dispatched, messages):  # noqa: ANN001
        return False


class FakeAgentTransitions:
    def signal_from_metadata(self, host, metadata):  # noqa: ANN001
        return None

    def apply(self, host, signal, messages, processor):  # noqa: ANN001
        return None

    def resolve_structured_output(self, host, content, messages):  # noqa: ANN001
        return False

    def return_from_as_tool(self, host, messages, summary=""):  # noqa: ANN001
        return {}

    async def terminal_content(self, host, fallback, messages):  # noqa: ANN001
        return fallback


def _services(**overrides) -> RuntimeServices:  # noqa: ANN003
    values = {
        "model": FakeModelGateway(),
        "tools": FakeToolRuntime(),
        "context": FakeContextManager(),
        "session_runtime": FakeSessionRuntime(),
        "events": RecordingEventSink(),
        "host": FakeRuntimeHost(),
        "memory": FakeMemoryService(),
        "verifier": FakeCompletionVerifier(),
        "lifecycle": FakeRunLifecycle(),
        "guardrails": FakeGuardrailRuntime(),
        "inputs": FakeInputPreflight(),
        "transitions": FakeAgentTransitions(),
    }
    values.update(overrides)
    return RuntimeServices(**values)


def test_runtime_services_accept_structural_implementations() -> None:
    """Adapters should satisfy ports without inheriting framework base classes."""
    services = _services()

    assert isinstance(services.model, ModelGateway)
    assert isinstance(services.tools, ToolRuntime)
    assert isinstance(services.context, ContextManager)
    assert isinstance(services.session_runtime, SessionRuntimePort)
    assert isinstance(services.events, RuntimeEventSink)
    assert services.host is not None


def test_runtime_services_exposes_only_native_session_runtime() -> None:
    """The production graph cannot retain a second transcript owner."""
    services = _services()

    assert not hasattr(services, "sessions")
    assert isinstance(services.session_runtime, SessionRuntimePort)


@pytest.mark.parametrize("field", [
    "model", "tools", "context", "session_runtime", "events", "host", "memory", "verifier",
    "lifecycle", "guardrails", "inputs", "transitions",
])
def test_runtime_services_reject_missing_required_port(field: str) -> None:
    """A Runner frame must fail composition before performing partial work."""
    with pytest.raises(TypeError, match=field):
        _services(**{field: None})


def test_runtime_services_reject_wrong_structural_port() -> None:
    """An object without the required operation cannot enter composition."""
    with pytest.raises(TypeError, match="model"):
        _services(model=object())


def test_runtime_event_snapshots_payload_and_is_immutable() -> None:
    """Trace consumers must observe the facts emitted at event creation time."""
    payload = {"turn": 1, "nested": {"status": "running"}}
    event = RuntimeEvent(
        name="run.started",
        run_id="run-1",
        session_id="session-1",
        payload=payload,
    )
    payload["nested"]["status"] = "changed"

    assert event.payload["nested"]["status"] == "running"
    with pytest.raises(FrozenInstanceError):
        event.name = "changed"


def test_runtime_event_rejects_missing_identity() -> None:
    """Events without correlation identity cannot form a public Agent trace."""
    with pytest.raises(ValueError, match="name"):
        RuntimeEvent(name="", run_id="run-1", session_id="session-1")
    with pytest.raises(ValueError, match="run_id"):
        RuntimeEvent(name="run.started", run_id="", session_id="session-1")
