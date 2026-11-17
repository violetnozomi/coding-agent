"""Task assessment cannot become a planning or reflection permission gate."""
from __future__ import annotations

import asyncio
import json

import pytest

from test_planning_recovery import PLAN, planning_agent as shared_planning_agent
from tests.test_loop_fake import FakeMessage, FakeResponse
from nz_coder.runtime.agent.planning_runtime import LegacyPlanningRuntime


@pytest.fixture
def planning_agent():
    yield from shared_planning_agent.__wrapped__()


def test_explicit_planning_enabled_also_plans_a_simple_unknown_task(planning_agent):
    agent, fake = planning_agent([FakeResponse(FakeMessage(json.dumps(PLAN)))])
    agent.runtime_state.initial_task_text = "Hello."

    asyncio.run(LegacyPlanningRuntime(agent).generate([]))

    assert len(fake.chat.completions.requests) == 1
    assert agent.runtime_state.plan_generated is True
    assert "Repair editions_match" in agent.runtime_state.plan_text
    assert "Task mode:" not in str(fake.chat.completions.requests[0]["messages"])


def test_contract_bootstrap_runs_independently_when_dedicated_planning_is_disabled(planning_agent):
    from dataclasses import replace
    from nz_coder.runtime.core.run_settings import current_run_settings, scoped_run_settings

    agent, fake = planning_agent([])
    task = "Update src/parser.py and tests/test_parser.py, then run pytest -q tests/test_parser.py."
    agent.runtime_state.initial_task_text = task
    agent.runtime_state.requested_paths = ["src/parser.py", "tests/test_parser.py"]
    with scoped_run_settings(replace(current_run_settings(), planning_enabled=False)):
        agent._bootstrap_task_contract([{"role": "user", "content": task}])

    assert agent.runtime_state.task_contract["requirements"]
    assert agent.runtime_state.plan_generated is False
    assert fake.chat.completions.requests == []


@pytest.mark.parametrize("boundary", ["restored", "plan_generated", "strict"])
def test_dedicated_planning_retains_existing_request_boundaries(planning_agent, boundary):
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    agent, fake = planning_agent([])
    agent._restored_state = boundary == "restored"
    agent.runtime_state.plan_generated = boundary == "plan_generated"
    with scoped_runtime_overrides(strict_local_tools=boundary == "strict"):
        asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert fake.chat.completions.requests == []
    assert agent.runtime_state.task_contract




def _publish_background_assessment(agent, *, reason="The user asks for explanation and review."):
    from nz_coder.protocol.message_schema import ensure_message_identities
    from nz_coder.runtime.agent.task_intent import initialize_task_assessment

    messages = [{"role": "user", "content": agent.runtime_state.initial_task_text}]
    ensure_message_identities(messages, agent.session_id)
    initialize_task_assessment(
        agent.runtime_state, agent.verification_runtime.run_context,
        messages, pure_continuation=False,
    )
    envelope = agent.runtime_state.task_assessment_state
    envelope["value"] = {
        "task_kinds": ["explain", "review"], "complexity": "simple",
        "reason": reason, "uncertainties": ["The failure cause still needs inspection."],
    }
    envelope["revision"] = 1
    return messages


def test_projected_request_counts_the_full_assessment_without_freezing_context(planning_agent):
    from dataclasses import replace
    from nz_coder.runtime.core.run_settings import current_run_settings, scoped_run_settings

    agent, _fake = planning_agent([])
    agent.hooks._prompt_messages.append("pending one-time hook")
    messages = _publish_background_assessment(agent)
    revision = agent._sp.revision
    with scoped_run_settings(replace(current_run_settings(), system_context_budget_tokens=0)):
        short = agent._projected_request_tokens(messages)
        agent.runtime_state.task_assessment_state["value"]["reason"] = "调查" * 6000
        long = agent._projected_request_tokens(messages)

    assert long - short >= 11900
    assert agent.hooks._prompt_messages == ["pending one-time hook"]
    assert agent._sp.revision == revision




def test_simple_explanation_assessment_does_not_disable_opted_in_reflection(planning_agent):
    from dataclasses import replace
    from nz_coder.runtime.core.run_settings import current_run_settings, scoped_run_settings

    agent, _fake = planning_agent([])
    _publish_background_assessment(agent)
    with scoped_run_settings(replace(current_run_settings(), reflection_enabled=True)):
        assert agent._should_run_reflection("completed") is True
        assert agent._should_run_reflection("max_turns") is False
