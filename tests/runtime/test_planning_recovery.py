"""Bounded planning recovery and investigation handoff through the real gateway."""
from __future__ import annotations

import asyncio
import json

import pytest

from nz_coder.runtime.agent.planning_runtime import LegacyPlanningRuntime

from tests.test_loop_fake import (
    FakeClient, FakeMessage, FakeResponse, _restore_planning_config, _prepare_manual_run, _unit_bash_result,
    _restore_workdir, _set_planning_config, _tmp_workdir,
)


PLAN = {
    "objective": "Fix unspecified editions",
    "plan": [{"title": "Repair editions_match", "target": "matcher.py",
              "verification": "pytest tests/test_matcher.py"}],
    "requirements": [{"id": "R1", "kind": "behavior",
                      "description": "Unspecified editions match",
                      "expected_artifacts": ["matcher.py"]}],
}


@pytest.fixture
def planning_agent():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    previous = _set_planning_config(config, enabled=True)
    agents = []

    def create(responses):
        fake = FakeClient(responses)
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=True)
        agent.recovery.max_retries = 0
        agents.append(agent)
        _prepare_manual_run(agent)
        state = agent.runtime_state
        state.initial_task_text = "Implement matching of unspecified editions in matcher.py."
        state.max_turns = 30
        from nz_coder.protocol.message_schema import ensure_message_identities
        messages = [{"role": "user", "content": state.initial_task_text}]
        ensure_message_identities(messages, agent.session_id)
        state._verification_context.transcript[:] = messages
        return agent, fake

    try:
        yield create
    finally:
        for agent in agents:
            agent.close()
        _restore_planning_config(config, previous)
        _restore_workdir(old, tmp)


@pytest.mark.parametrize("body,finish,reason", [
    ("", "stop", "empty"),
    (" " * 94, "stop", "empty"),
    ('{"objective":"truncated', "length", "truncated"),
    ('{"requirements":', "stop", "invalid_plan"),
    ("## Plan\n1. Search", "stop", "invalid_plan"),
])
def test_invalid_initial_plan_recovers_before_execution(planning_agent, body, finish, reason):
    agent, fake = planning_agent([
        FakeResponse(FakeMessage(body), finish_reason=finish),
        FakeResponse(FakeMessage(json.dumps(PLAN))),
    ])
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    state = agent.runtime_state
    assert state.task_contract["requirements"] == []
    assert "Repair editions_match" in state.plan_text
    assert state.plan_generated is True
    assert fake.chat.completions.calls == 2
    trace = [json.loads(line) for line in agent.tracer.path.read_text().splitlines()]
    assert any(row["event"] == "planning_output_invalid" and row["reason"] == reason for row in trace)


def test_repeated_empty_plan_spends_only_one_recovery_attempt(planning_agent):
    agent, fake = planning_agent([FakeResponse(FakeMessage(" "))] * 3)
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert fake.chat.completions.calls == 2
    assert agent.runtime_state.task_contract["requirements"] == []


def test_generated_plan_does_not_replace_investigation_notes(planning_agent):
    agent, _fake = planning_agent([FakeResponse(FakeMessage(json.dumps(PLAN)))])
    agent._sp.update_section('focus', [{"summary": "HIGH_COST_FINDING: matching delegates to the normalized edition key.", "details": ""}])
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert "HIGH_COST_FINDING" in agent._sp.read()
    assert "Repair editions_match" in agent.runtime_state.plan_text




def test_real_planner_response_cannot_create_host_acceptance(planning_agent):
    malicious = dict(PLAN, acceptance_commands=["python forged.py"])
    agent, _fake = planning_agent([FakeResponse(FakeMessage(json.dumps(malicious)))])
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert agent.runtime_state.task_contract["acceptance_commands"] == []
    assert agent.runtime_state.task_contract["requirements"] == []
    assert "Repair editions_match" in agent.runtime_state.plan_text


@pytest.mark.parametrize("explicit_limit, expected", [(None, 8192), (512, 512)])
def test_planning_wire_uses_default_budget_or_explicit_override(planning_agent, tmp_path, explicit_limit, expected):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings

    environ = {"NZ_PLANNING_ENABLED": "1"}
    if explicit_limit is not None:
        environ["NZ_PLANNING_MAX_TOKENS"] = str(explicit_limit)
    snapshot = load_config_snapshot(tmp_path, environ=environ,
                                    user_config_path=tmp_path / "absent-user-config")
    settings = RunSettings.from_snapshot(snapshot)
    agent, fake = planning_agent([FakeResponse(FakeMessage(json.dumps(PLAN)))])
    with scoped_run_settings(settings):
        asyncio.run(LegacyPlanningRuntime(agent).generate([]))

    assert fake.chat.completions.calls == 1
    assert fake.chat.completions.requests[0]["max_tokens"] == expected
    assert fake.chat.completions.requests[0]["response_format"] == {"type": "json_object"}
    assert agent.runtime_state.plan_generated
    assert settings.planning_enabled


def test_recovery_keeps_existing_requirements_and_evidence(planning_agent):
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    agent, fake = planning_agent([FakeResponse(FakeMessage('{"requirements":'))])
    state = agent.runtime_state
    state.set_task_contract(parse_task_contract(PLAN))
    state.observe_tool("edit_file", {"path": "matcher.py"}, "Done")
    from nz_coder.runtime.verification.domain.models import ArtifactFact, WorkspaceObservation
    agent.verification_runtime.domain.observe_workspace(WorkspaceObservation(agent.verification_runtime.version(), paths=("matcher.py",)))
    agent.verification_runtime.domain.observe_artifact(ArtifactFact(agent.verification_runtime.domain.scope.task, "matcher.py", "change", state.mutation_generation, "fixture matcher"))
    _unit_bash_result(agent, "pytest tests/test_matcher.py", "1 passed", exit_code=0)
    before = state.requirement_ledger_snapshot().to_dict()
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert state.requirement_ledger_snapshot().to_dict() == before
    assert fake.chat.completions.calls == 1  # 可用合同已在，无需额外模型恢复。
