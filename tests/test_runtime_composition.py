"""Contract tests for the canonical Agent runtime composition owner."""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.test_loop_fake import FakeClient, FakeMessage, FakeResponse


def test_coding_runtime_is_single_native_control_plane(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_coding_agent
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_coding_agent(
                "CODING_ROLE",
                client=FakeClient([FakeResponse(FakeMessage("done"))]),
                trace_enabled=False,
            )
            result = asyncio.run(agent.run(
                [{"role": "user", "content": "inspect"}],
                stream=False,
            ))
    finally:
        config.WORKDIR = old_workdir

    assert agent.agent_graph is None
    assert result["runtime"]["profile"] == "coding"
    assert result["runtime"]["control_plane"] == "native-coding-loop"
    assert result["runtime"]["active_agent"] == "worker"
    started = next(
        entry for entry in agent.lineage.entries()
        if entry["type"] == "run_started"
    )
    assert started["payload"]["runtime_profile"] == "coding"
    assert started["payload"]["control_plane"] == "native-coding-loop"


def test_declared_runtime_makes_graph_the_authoritative_control_plane():
    from nz_coder.runtime.execution.composition import declared_runtime
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    graph = AgentGraph([AgentSpec("worker", "DECLARED_ROLE")], start="worker")
    captured = {}

    class StubAgent:
        def __init__(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs

    agent = declared_runtime(graph).build(agent_cls=StubAgent, trace_enabled=False)

    assert captured["prompt"] == "DECLARED_ROLE"
    assert captured["kwargs"]["agent_graph"] is graph
    assert agent.runtime_profile == "declared"
    assert agent.runtime_control_plane == "declared-agent-graph"


def test_runtime_assembly_rejects_mixed_control_planes():
    from nz_coder.runtime.execution.composition import AgentRuntimeAssembly, coding_runtime
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec

    graph = AgentGraph([AgentSpec("worker", "ROLE")], start="worker")
    with pytest.raises(ValueError, match="cannot also install"):
        AgentRuntimeAssembly("coding", "prompt", graph)
    with pytest.raises(ValueError, match="requires an Agent graph"):
        AgentRuntimeAssembly("declared", "prompt")
    with pytest.raises(TypeError, match="owned by AgentRuntimeAssembly"):
        coding_runtime("prompt").build(
            agent_cls=lambda *_args, **_kwargs: None,
            agent_graph=graph,
        )


def test_coding_runtime_assembles_strict_generation_stop_consumer(tmp_path):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.composition import build_coding_agent
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.process.workdir import scoped_workdir

    old_workdir = config.WORKDIR
    config.WORKDIR = tmp_path
    try:
        with scoped_workdir(tmp_path):
            agent = build_coding_agent(
                "CODING_ROLE",
                client=FakeClient([]),
                trace_enabled=False,
            )
        agent.runtime_state.mutation_generation = 1
        agent.runtime_state.has_diff = True
        messages = [{"role": "assistant", "content": "done"}]

        with scoped_runtime_overrides(strict_local_tools=True):
            result = agent.hooks.handle_no_tool_response(agent, messages, message="done")
    finally:
        config.WORKDIR = old_workdir

    assert result == "continue"
    assert messages[-1]["_nz_stop_hook"] is True
    assert "diff_status" in messages[-1]["content"]


def test_strict_product_runtime_requires_evidence_without_promoting_inferred_target(
    tmp_path,
):
    """Strict SWE requires behavior evidence while filename guesses stay advisory."""
    from nz_coder.runtime.execution.composition import build_product_environment
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.process.workdir import scoped_workdir

    source = tmp_path / "src" / "widget.py"
    test = tmp_path / "tests" / "test_widget.py"
    source.parent.mkdir()
    test.parent.mkdir()
    source.write_text("def widget(): return 1\n", encoding="utf-8")
    test.write_text("def test_widget(): assert True\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

    with (
        scoped_workdir(tmp_path),
        scoped_runtime_overrides(strict_local_tools=True),
    ):
        agent = build_product_environment(
            "CODING_ROLE",
            client=FakeClient([]),
            trace_enabled=False,
            sidecar_verifier=False,
        )
        try:
            agent.vm.mark_write("edit_file", {"path": "src/widget.py"})
            agent.vm.observe_verify_changed_files("OK: py_compile changed files")
            verification_needed = agent.vm.should_gate()
            pipeline = agent.vm.status()["verification_pipeline"]
        finally:
            agent.close()

    targeted = next(stage for stage in pipeline["stages"] if stage["name"] == "targeted")
    assert verification_needed is True
    assert targeted["required"] is True
    assert targeted["evidence_required"] is True
    assert targeted["status"] == "pending"
    assert targeted["commands"][0]["command"] == "pytest tests/test_widget.py"
    assert targeted["commands"][0]["required"] is False


def test_coding_and_declared_profiles_share_services_runner_and_trace(tmp_path):
    from nz_coder.runtime.execution.composition import build_coding_agent, build_declared_agent
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.trace import TraceRecorder

    graph = AgentGraph([AgentSpec("worker", "DECLARED_ROLE")], start="worker")
    observed = []
    for name, factory in (
        ("coding", lambda tracer: build_coding_agent(
            "CODING_ROLE",
            client=FakeClient([FakeResponse(FakeMessage("done"))]),
            tracer=tracer,
            sidecar_verifier=False,
        )),
        ("declared", lambda tracer: build_declared_agent(
            graph,
            client=FakeClient([FakeResponse(FakeMessage("done"))]),
            tracer=tracer,
            sidecar_verifier=False,
        )),
    ):
        workspace = tmp_path / name
        workspace.mkdir()
        tracer = TraceRecorder(trace_dir=workspace / "traces", enabled=True)
        with scoped_workdir(workspace):
            agent = factory(tracer)
            try:
                result = asyncio.run(agent.run(
                    [{"role": "user", "content": "inspect"}],
                    stream=False,
                ))
            finally:
                agent.close()
        events = [
            json.loads(line)["event"]
            for line in tracer.path.read_text(encoding="utf-8").splitlines()
        ]
        observed.append((agent, result, events))

    coding, declared = observed
    assert type(coding[0].runner) is type(declared[0].runner)
    assert type(coding[0].runtime_services) is type(declared[0].runtime_services)
    for index, expected_profile in enumerate(("coding", "declared")):
        agent, result, events = observed[index]
        assert result["status"] == "completed"
        assert result["runtime"]["profile"] == expected_profile
        assert events.index("agent_runner_enter") < events.index("run_start")
        assert events.index("run_start") < events.index("llm_response")
        assert events.index("llm_response") < events.index("run_end")
