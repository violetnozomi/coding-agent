"""Ownership tests for the focused Tool Runtime context."""
from __future__ import annotations

import asyncio
from pathlib import Path

from nz_coder.runtime.adapters.tool import (
    projection_context_from_legacy_host,
    tool_context_from_legacy_host,
)
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.tool_context import ToolExecutionContext
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.execution.tool_executor import ToolExecutionResult


class _SessionRuntime:
    def __init__(self) -> None:
        self.statuses: list[str] = []

    async def checkpoint(self, _context, status: str) -> None:
        self.statuses.append(status)


class _Services:
    def __init__(self) -> None:
        self.session_runtime = _SessionRuntime()
        self.transitions = _Transitions()


class _Transitions:
    def apply(self, host, signal, _messages, _processor):
        host.current_agent_name = signal["to"]
        return {"from": signal["from"], "to": signal["to"]}


class _Tracer:
    def log(self, *_args, **_kwargs) -> None:
        return None


class _Hooks:
    def after_tool_result(self, *_args, **_kwargs) -> None:
        return None

    def on_post_tool_use(self, *_args, **_kwargs) -> None:
        return None


class _Host:
    def __init__(self) -> None:
        self.tracer = _Tracer()
        self.current_agent_name = "coder"
        self.agent_graph = None
        self.tool_allowlist = None
        self.admission_handle = None
        self.runtime_state = object()
        self.recovery = object()
        self.permissions = object()
        self.stall_orchestrator = None
        self._tool_observability = {}
        self._tool_batch_sequence = 0
        self.hooks = _Hooks()

    def _best_effort_tool_input(self, value):
        return value if isinstance(value, dict) else {}

    def _record_tool_result(self, _result) -> bool:
        return False

    def _trace_tool_result(self, *_args, **_kwargs) -> None:
        return None

    def _infer_hook_file_path(self, _tool_input):
        return None


def _run_context(tmp_path: Path) -> RunContext:
    request = RunRequest(
        agent=AgentDefinition(name="coder", instructions="fix"),
        profile=MAIN_PROFILE,
        session_id="session-tool",
        workspace=tmp_path,
        messages=(),
    )
    session = Session.create("session-tool", [], workspace=tmp_path)
    session.begin_run()
    return RunContext(request, session, "coder")


def test_tool_context_owns_run_scoped_policy_state_and_session_checkpoint(tmp_path) -> None:
    """Dropping RunContext or checkpoint binding would cross Session ownership."""
    host = _Host()
    services = _Services()
    run_context = _run_context(tmp_path)

    context = tool_context_from_legacy_host(host, run_context, services)

    assert isinstance(context, ToolExecutionContext)
    assert context.run is run_context
    assert context.policy.agent_name == "coder"
    assert context.policy.parse_input({"path": "a.py"}) == {"path": "a.py"}
    assert context.policy.next_batch_id() == "batch-1"
    assert context.policy.next_batch_id() == "batch-2"
    assert "host" not in vars(context)

    asyncio.run(context.lifecycle.checkpoint([], "running"))
    assert services.session_runtime.statuses == ["running"]


def test_tool_context_policy_state_isolated_between_runs(tmp_path) -> None:
    """Batch sequence stored on AgentLoop would leak across independent runs."""
    host = _Host()
    first = tool_context_from_legacy_host(host, _run_context(tmp_path), _Services())
    second = tool_context_from_legacy_host(host, _run_context(tmp_path), _Services())

    assert first.policy.next_batch_id() == "batch-1"
    assert first.policy.next_batch_id() == "batch-2"
    assert second.policy.next_batch_id() == "batch-1"


def test_tool_context_refreshes_policy_identity_after_handoff(tmp_path) -> None:
    """The next tool batch must use the newly activated Agent policy."""
    host = _Host()
    context = tool_context_from_legacy_host(
        host,
        _run_context(tmp_path),
        _Services(),
    )

    transition = asyncio.run(context.lifecycle.apply_transition(
        {"from": "coder", "to": "reviewer"},
        [],
        None,
    ))

    assert transition == {"from": "coder", "to": "reviewer"}
    assert context.policy.agent_name == "reviewer"


def test_projected_output_reaches_every_post_result_hook() -> None:
    """No legacy hook may bypass the unified result admission decision."""
    received: list[tuple[str, str]] = []

    class Hooks:
        def after_tool_result(self, _host, _messages, _result, output) -> None:
            received.append(("after", output))

        def on_post_tool_use(self, *_args, output="", **_kwargs) -> None:
            received.append(("post", output))

    host = _Host()
    host.hooks = Hooks()
    result = ToolExecutionResult(
        name="bash",
        tool_input={"command": "pytest"},
        output="raw-result-that-did-not-fit",
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    )
    projections = (
        tool_context_from_legacy_host(host).projection,
        projection_context_from_legacy_host(host),
    )

    for index, projection in enumerate(projections):
        projection.after_result([], result, f"admitted-result-{index}")

    assert received == [
        ("after", "admitted-result-0"),
        ("post", "admitted-result-0"),
        ("after", "admitted-result-1"),
        ("post", "admitted-result-1"),
    ]
