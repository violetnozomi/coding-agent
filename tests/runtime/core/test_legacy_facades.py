"""Characterization tests for public facades preserved during Runner migration."""
from __future__ import annotations

import asyncio
import inspect
import json

from nz_coder.runtime.core import (
    BACKGROUND_PROFILE,
    MAIN_PROFILE,
    READ_CHILD_PROFILE,
    WRITE_CHILD_PROFILE,
)
from tests.test_loop_fake import FakeClient, FakeMessage, FakeResponse


def test_main_facade_preserves_result_envelope(tmp_path) -> None:  # noqa: ANN001
    """The future Runner facade must preserve current host-visible result keys."""
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
                sidecar_verifier=False,
            )
            messages = [{"role": "user", "content": "inspect"}]
            result = asyncio.run(agent.run(messages, stream=False))
    finally:
        config.WORKDIR = old_workdir

    assert isinstance(result, dict)
    assert result["status"] == "completed"
    assert "messages" not in result
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "done"
    assert result["runtime"]["profile"] == "coding"
    assert result["runtime"]["control_plane"] == "native-coding-loop"


def test_child_facades_preserve_public_parameters() -> None:
    """Child migration must remain callable by every existing orchestrator."""
    from nz_coder.runtime.agent.subagent import run_subagent, run_subagent_async

    expected = {
        "prompt",
        "agent_type",
        "session_id",
        "allowed_tools",
        "target_paths",
        "cancel_event",
        "output_schema",
        "model_hint",
        "evidence_refs",
        "verification",
    }

    assert expected <= set(inspect.signature(run_subagent).parameters)
    assert expected <= set(inspect.signature(run_subagent_async).parameters)


def test_main_trace_proves_shared_runner_chain(tmp_path) -> None:
    from nz_coder.runtime.execution.composition import build_coding_agent
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.trace import TraceRecorder

    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    with scoped_workdir(tmp_path):
        agent = build_coding_agent(
            "CODING_ROLE",
            client=FakeClient([FakeResponse(FakeMessage("done"))]),
            tracer=tracer,
            sidecar_verifier=False,
        )
        try:
            asyncio.run(agent.run(
                [{"role": "user", "content": "inspect"}],
                stream=False,
            ))
        finally:
            agent.close()

    events = [json.loads(line)["event"] for line in tracer.path.read_text().splitlines()]
    assert events.index("agent_runner_enter") < events.index("run_start")
    assert events.index("run_start") < events.index("llm_response") < events.index("run_end")


def test_profile_matrix_matches_existing_runtime_surfaces() -> None:
    """Main interaction and child/background isolation are explicit policy."""
    assert (
        MAIN_PROFILE.allow_mutation,
        MAIN_PROFILE.allow_child_agents,
        MAIN_PROFILE.interactive_questions,
    ) == (True, True, True)
    assert (
        READ_CHILD_PROFILE.allow_mutation,
        READ_CHILD_PROFILE.allow_child_agents,
        READ_CHILD_PROFILE.interactive_questions,
    ) == (False, False, False)
    assert (
        WRITE_CHILD_PROFILE.allow_mutation,
        WRITE_CHILD_PROFILE.allow_child_agents,
        WRITE_CHILD_PROFILE.interactive_questions,
    ) == (True, False, False)
    assert BACKGROUND_PROFILE.interactive_questions is False
