"""Contracts for untrusted Agent admission and runtime capability clamps."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.test_loop_fake import FakeClient, FakeMessage, FakeResponse, FakeToolCall


def _graph(*agents, start="worker"):
    from nz_coder.runtime.agent.handoffs import AgentGraph

    return AgentGraph(agents, start=start)


def test_admission_requires_explicit_untrusted_tool_allowlist():
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.agent.handoffs import AgentSpec

    verdict = admit_agent_graph(
        _graph(AgentSpec("worker", "work")),
        SystemAgentCap(frozenset({"read"})),
    )

    assert verdict.ok is False
    assert verdict.handle is None
    assert verdict.retryable is True
    assert "explicit tool allowlist" in verdict.reason


def test_admission_rejects_handoffs_without_subagent_capability():
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.agent.handoffs import AgentSpec, HandoffSpec

    graph = _graph(
        AgentSpec(
            "worker",
            "work",
            allowed_tools=("read_file",),
            handoffs=(HandoffSpec("reviewer"),),
        ),
        AgentSpec("reviewer", "review", allowed_tools=("read_file",)),
    )

    verdict = admit_agent_graph(graph, SystemAgentCap(frozenset({"read"})))

    assert verdict.ok is False
    assert "handoffLegality" in verdict.reason


def test_admission_clamps_tools_without_mutating_source_graph():
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.agent.handoffs import AgentSpec

    graph = _graph(AgentSpec(
        "worker",
        "work",
        allowed_tools=("read_file", "write_file", "external_unknown"),
    ))

    verdict = admit_agent_graph(graph, SystemAgentCap(frozenset({"read"})))

    assert verdict.ok is True
    assert verdict.handle is not None
    assert verdict.handle.graph is not graph
    assert graph.agent("worker").allowed_tools == (
        "read_file", "write_file", "external_unknown",
    )
    assert verdict.handle.graph.agent("worker").allowed_tools == ("read_file",)
    assert "write_file=edit" in verdict.handle.clamp_notes[0]
    assert "external_unknown=subagent" in verdict.handle.clamp_notes[0]


def test_admission_derives_extension_capability_from_side_effect_metadata():
    """Scheduler mode must not decide an extension tool's authority tier."""
    from nz_coder.runtime.agent.admission import resolve_tool_capability
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )

    effects = {
        "_test_admission_read": ("readonly", "read"),
        "_test_admission_network": ("reads-network", "bash:network"),
        "_test_admission_shell": ("mutates-shell", "bash:mutating"),
        "_test_admission_fs": ("mutates-fs", "edit"),
        "_test_admission_state": ("mutates-state", "subagent"),
    }
    try:
        for name, (effect, _expected) in effects.items():
            register(
                name,
                "test",
                {"type": "object", "properties": {}},
                lambda: "ok",
                execution="serial",
                side_effect=effect,
            )

        assert {
            name: resolve_tool_capability(name)
            for name in effects
        } == {
            name: expected
            for name, (_effect, expected) in effects.items()
        }
    finally:
        for name in effects:
            TOOL_HANDLERS.pop(name, None)
            TOOL_EXECUTION_MODES.pop(name, None)
            TOOL_SIDE_EFFECTS.pop(name, None)
            TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] not in effects
        ]


def test_admission_metadata_promotes_registered_readonly_workflow_tool():
    """A newly added readonly builtin must not need another admission list edit."""
    import nz_coder.runtime.workflows.workflow_library  # noqa: F401
    from nz_coder.runtime.agent.admission import resolve_tool_capability

    assert resolve_tool_capability("workflow_library") == "read"


def test_admitted_runtime_requires_opaque_handle_and_preserves_trusted_path():
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.execution.composition import admitted_runtime, declared_runtime
    from nz_coder.runtime.agent.handoffs import AgentSpec

    graph = _graph(AgentSpec("worker", "work", allowed_tools=("read_file",)))
    verdict = admit_agent_graph(graph, SystemAgentCap(frozenset({"read"})))
    assert verdict.handle is not None

    admitted = admitted_runtime(verdict.handle)
    trusted = declared_runtime(graph)

    assert admitted.admission_handle is verdict.handle
    assert admitted.agent_graph is verdict.handle.graph
    assert trusted.admission_handle is None
    assert trusted.agent_graph is graph
    with pytest.raises(TypeError, match="AdmittedAgentHandle"):
        admitted_runtime(graph)  # type: ignore[arg-type]


def test_runtime_clamps_dynamic_bash_network_capability(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.execution.composition import build_admitted_agent
    from nz_coder.runtime.agent.handoffs import AgentSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = _graph(AgentSpec("worker", "work", allowed_tools=("bash",)))
    verdict = admit_agent_graph(
        graph,
        SystemAgentCap(frozenset({"bash:mutating"})),
    )
    assert verdict.handle is not None
    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
            "bash",
            {"command": "curl https://example.invalid/payload"},
        )])),
        FakeResponse(FakeMessage("adapted without network")),
    ])
    messages = [{"role": "user", "content": "inspect locally"}]
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_admitted_agent(
                verdict.handle,
                client=fake,
                permission_mode="auto",
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    tool_message = next(item for item in messages if item.get("role") == "tool")
    assert "[Invariant toolPermission]" in tool_message["content"]
    assert "bash:network" in tool_message["content"]
    assert result["status"] == "completed"
    assert result["runtime"]["admitted"] is True
    assert result["runtime"]["admitted_capabilities"] == [
        "bash:mutating", "bash:read-only", "bash:test",
    ]


def test_admission_max_iterations_clamps_user_turn_budget(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.execution.composition import build_admitted_agent
    from nz_coder.runtime.agent.handoffs import AgentSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = _graph(AgentSpec("worker", "work", allowed_tools=()))
    verdict = admit_agent_graph(
        graph,
        SystemAgentCap(frozenset(), max_iterations=2),
    )
    assert verdict.handle is not None
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_admitted_agent(
                verdict.handle,
                client=FakeClient([FakeResponse(FakeMessage("done"))]),
                trace_enabled=False,
            )
            max_turns, _start_turn = agent._init_run([
                {"role": "user", "content": "task\n<max_turns>40</max_turns>"},
            ], False)
    finally:
        config.WORKDIR = old_workdir

    assert max_turns == 2
    assert agent.runtime_state.max_turns == 2


def test_admitted_run_cannot_finish_from_non_terminal_owner(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.admission import SystemAgentCap, admit_agent_graph
    from nz_coder.runtime.execution.composition import build_admitted_agent
    from nz_coder.runtime.agent.handoffs import AgentSpec, HandoffSpec
    from nz_coder.runtime.process.workdir import scoped_workdir

    graph = _graph(
        AgentSpec(
            "worker",
            "work",
            allowed_tools=(),
            handoffs=(HandoffSpec("owner"),),
        ),
        AgentSpec("owner", "own", allowed_tools=()),
    )
    verdict = admit_agent_graph(
        graph,
        SystemAgentCap(frozenset({"subagent"})),
    )
    assert verdict.handle is not None
    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_admitted_agent(
                verdict.handle,
                client=FakeClient([FakeResponse(FakeMessage("premature final"))]),
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(
                [{"role": "user", "content": "inspect"}],
                stream=False,
            ))
    finally:
        config.WORKDIR = old_workdir

    assert result["status"] == "blocked"
    assert "finalOwner" in result["runtime"]["admission_violations"][0]
    assert any(
        entry["type"] == "invariant_violation"
        for entry in agent.lineage.entries()
    )


def test_terminal_evidence_invariant_tracks_only_committed_writes():
    from nz_coder.runtime.agent.admission import (
        AdmissionInvariantSession,
        SystemAgentCap,
        admit_agent_graph,
    )
    from nz_coder.runtime.agent.handoffs import AgentSpec

    graph = _graph(AgentSpec("worker", "work", allowed_tools=("write_file", "bash")))
    verdict = admit_agent_graph(
        graph,
        SystemAgentCap(frozenset({"edit", "bash:mutating"})),
    )
    assert verdict.handle is not None
    session = AdmissionInvariantSession(verdict.handle)
    write = SimpleNamespace(
        name="write_file",
        tool_input={"path": "app.py"},
        output="Created app.py",
        is_write=True,
        executed=True,
        dispatch_failed=False,
        command_failed=False,
    )

    session.record_tool_result(write)
    assert session.mutation_count == 0
    session.record_committed_mutation(write)
    violations = session.assert_terminal("worker", "completed")

    assert session.mutation_files == {"app.py"}
    assert violations == (
        "evidenceTrail: mutating run produced no successful verification artifact",
    )


def test_terminal_evidence_accepts_successful_verification_artifact():
    from nz_coder.runtime.agent.admission import (
        AdmissionInvariantSession,
        SystemAgentCap,
        admit_agent_graph,
    )
    from nz_coder.runtime.agent.handoffs import AgentSpec

    graph = _graph(AgentSpec("worker", "work", allowed_tools=("write_file", "bash")))
    verdict = admit_agent_graph(
        graph,
        SystemAgentCap(frozenset({"edit", "bash:mutating"})),
    )
    assert verdict.handle is not None
    session = AdmissionInvariantSession(verdict.handle)
    session.record_committed_mutation(SimpleNamespace(
        is_write=True,
        executed=True,
        dispatch_failed=False,
        tool_input={"path": "app.py"},
    ))
    session.record_tool_result(SimpleNamespace(
        name="bash",
        tool_input={"command": "python -m pytest -q"},
        output="1 passed",
        is_write=False,
        executed=True,
        dispatch_failed=False,
        command_failed=False,
    ))

    assert session.assert_terminal("worker", "completed") == ()
    assert session.evidence_artifacts == ["python -m pytest -q"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", "bash:read-only"),
        ("python -m pytest -q", "bash:test"),
        ("touch marker", "bash:mutating"),
        ("git fetch origin", "bash:network"),
    ],
)
def test_concrete_bash_capability_classification(command, expected):
    from nz_coder.runtime.agent.admission import resolve_tool_capability

    assert resolve_tool_capability("bash", {"command": command}) == expected
