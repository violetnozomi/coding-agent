"""阶段路线不产生第二份合同，整理复用当前事实但不接管验收。"""
from __future__ import annotations

import asyncio
import copy
import json

import pytest

from nz_coder.runtime.agent.planning_runtime import LegacyPlanningRuntime
from nz_coder.runtime.agent.task_contract import parse_task_contract
from test_planning_recovery import planning_agent as shared_planning_agent
from tests.test_loop_fake import FakeMessage, FakeResponse


PLAN = {"plan": [{"title": "Repair expiry handling", "target": "matcher.py",
                  "verification": "Run focused expiry tests"}]}


@pytest.fixture
def planning_agent():
    yield from shared_planning_agent.__wrapped__()


@pytest.mark.parametrize("extra", [{}, {
    "requirements": [{"id": "R1", "kind": "unknown", "depends_on": ["R99"]}],
    "acceptance_commands": ["python forged.py"],
}])
def test_plan_only_is_accepted_once_without_altering_host_contract(planning_agent, extra):
    agent, fake = planning_agent([FakeResponse(FakeMessage(json.dumps({**PLAN, **extra})))])
    agent._bootstrap_task_contract([])
    before = copy.deepcopy(agent.runtime_state.task_contract)
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert len(fake.chat.completions.requests) == 1
    assert agent.runtime_state.plan_text == "## Plan\n1. Repair expiry handling - matcher.py - Run focused expiry tests"
    assert agent.runtime_state.task_contract == before
    assert "forged.py" not in agent.runtime_state.plan_text


def test_failed_planning_renders_user_fallback_only_once(planning_agent):
    agent, fake = planning_agent([FakeResponse(FakeMessage("{"))])
    agent.runtime_state.set_task_contract(parse_task_contract({
        "objective": "Update matcher.py", "requirements": [{"id": "R1", "kind": "artifact",
            "description": "Preserve this explicit deliverable", "expected_artifacts": ["matcher.py"]}],
    }))
    before = copy.deepcopy(agent.runtime_state.task_contract)
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    assert len(fake.chat.completions.requests) == 1
    assert agent.runtime_state.plan_text.count("Preserve this explicit deliverable") == 1
    assert agent.runtime_state.task_contract == before


def test_main_route_update_preserves_expensive_notes_and_accepted_handoff(planning_agent):
    agent, _ = planning_agent([FakeResponse(FakeMessage(json.dumps(PLAN)))])
    agent.runtime_state.plan_generated = True
    agent.runtime_state.plan_text = "## Plan\n1. Old route"
    agent.runtime_state.turn_count = 5
    agent._sp.update_section("findings", [{"summary": "Close the active client before joining the server.",
        "details": "Expensive two-client reproduction", "certainty": "confirmed", "basis": "observed trace"}])
    from test_execution_handoff import _case, _submit
    from nz_coder.runtime.agent.planning_runtime import render_execution_handoff
    _, messages, call, proposal = _case(agent.workdir)
    state = agent.runtime_state
    state.initial_task_text = "repair api"
    state.task_authority_epoch = 2
    state._verification_context.transcript[:] = messages
    assert not _submit(agent.workdir, state, messages, call, proposal).dispatch_failed
    accepted = copy.deepcopy(state.execution_handoff)
    before = agent._sp.read()
    from test_working_plan_state import publish
    result, _, _, _ = publish(state, state._verification_context, steps=PLAN["plan"])
    assert not result.dispatch_failed, result.output
    assert agent.runtime_state.plan_text == "## Plan\n1. Repair expiry handling - matcher.py - Run focused expiry tests"
    assert agent._sp.read() == before
    assert state.execution_handoff == accepted
    assert "handle empty input" in render_execution_handoff(state, workspace=agent.workdir)



def test_initial_planner_receives_latest_contract_and_full_real_instruction(planning_agent):
    agent, fake = planning_agent([FakeResponse(FakeMessage(json.dumps(PLAN)))])
    agent.runtime_state.set_task_contract(parse_task_contract({
        "objective": "Old objective", "constraints": ["Do not modify matcher.py."],
        "requirements": [{"id": "R1", "kind": "artifact", "description": "Create the new entry point",
                          "expected_artifacts": ["new_entry.py"]}],
        "acceptance_commands": ["python check_one.py", "python check_two.py"],
    }))
    agent.runtime_state.acceptance_criteria = ["OLD_SUMMARY"]
    agent.runtime_state.current_round_instruction_text = "bounded hint"
    current = "The user adds this semantic requirement. " * 160 + "FULL_LATE_REQUIREMENT"
    messages = [{"role": "user", "content": current, "_nz_message_id": "msg-current"}]
    agent.runtime_state._verification_context.transcript[:] = messages
    agent._active_processor_messages = None
    asyncio.run(LegacyPlanningRuntime(agent).generate(messages))
    visible = str(fake.chat.completions.requests[0]["messages"])
    assert "Do not modify matcher.py." in visible
    assert "python check_two.py" in visible
    assert "new_entry.py" in visible
    assert "FULL_LATE_REQUIREMENT" in visible
    assert "OLD_SUMMARY" not in visible


@pytest.mark.parametrize("raw", ["{}", '{"plan":[]}', '{"plan":[{"title":""}]}', "## Plan\n1. Old protocol"])
def test_unusable_working_plan_does_not_invent_generic_stages(raw):
    from nz_coder.runtime.agent.planning_runtime import parse_working_plan
    with pytest.raises(ValueError):
        parse_working_plan(raw)


@pytest.mark.parametrize("stream", [False, True])
def test_native_plan_and_memory_share_real_facts_without_extra_freeze(monkeypatch, tmp_path, stream):
    from dataclasses import replace
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
    from test_native_product_runtime import _install_runtime, _request
    from test_working_memory_reminders import _reply, _call

    (tmp_path / "matcher.py").write_text("def match():\n    return True\n")
    requests, freezes = [], []
    freeze = ProductionPromptBuilder.freeze

    def capture(self, *args):
        freezes.append(True)
        return freeze(self, *args)

    monkeypatch.setattr(ProductionPromptBuilder, "freeze", capture)

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            system = kwargs["messages"][0]["content"]
            if system.startswith("Maintain working notes"):
                purpose, reply = "memory", _reply(kwargs, text='{"status":"unchanged"}')
            elif system == "You are a concise coding task planner.":
                purpose, reply = "planning", _reply(kwargs, text=json.dumps(PLAN))
            else:
                purpose = "main"
                first = not any(name == "main" for name, _ in requests)
                reply = (_reply(kwargs, calls=[_call(1, "read_file", {"path": "matcher.py"})])
                         if first else _reply(kwargs, text="Read-only inspection complete."))
            requests.append((purpose, copy.deepcopy(kwargs)))
            return reply

    runtime = _install_runtime(monkeypatch, Provider())
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=stream)
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_PLANNING_ENABLED": "1",
        "NZ_WORKING_MEMORY_REMINDER_STEPS": "1", "RUNTIME_STATE_PERSIST": "0"})
    text = "Inspect matcher.py and explain the existing behavior. " + "Keep this semantic detail. " * 180 + "FULL_USER_END"
    request = replace(_request(tmp_path), stream=stream, session_id=tmp_path.name, messages=({"role": "user", "content": text},))
    options = RunOptions(config_snapshot=snapshot, permission_asker=lambda *_: True)
    env = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        # 本场景只提供编码与记忆模型，没有独立交付评审。
        verification = env.verification_runtime.domain.snapshot()
        assert not verification.reviews and verification.assess_completion("completed").review_required
        assert result.status.value == "completed_unverified"
        assert [name for name, _ in requests] == ["planning", "main", "memory", "main"]
        assert len(freezes) == 2
        memory = json.loads(requests[2][1]["messages"][1]["content"])
        assert "<current-task-facts>" in memory["current_facts"]
        assert "Current verification unknown" in memory["current_facts"]
        assert "FULL_USER_END" in str(requests[0][1]["messages"])
        assert "Repair expiry handling" in str(requests[-1][1]["messages"])
        assert env._sp.completed_steps == 2
        assert env.runtime_state.task_contract["acceptance_commands"] == []
    finally:
        env.close()


@pytest.mark.parametrize("content,expected", [
    ([{"type": "text", "text": "CURRENT_TEXT"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}], "CURRENT_TEXT"),
    ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}], ""),
])
def test_planning_selects_current_multimodal_source_before_extracting_text(planning_agent, content, expected):
    from nz_coder.protocol.message_schema import ensure_message_identities
    agent, fake = planning_agent([FakeResponse(FakeMessage(json.dumps(PLAN)))])
    agent.runtime_state._verification_context.transcript[:] = [
        {"role": "user", "content": "OLDER_USER_INSTRUCTION"},
        {"role": "user", "content": content},
    ]
    ensure_message_identities(agent.runtime_state._verification_context.transcript, agent.session_id)
    agent._active_processor_messages = None
    asyncio.run(LegacyPlanningRuntime(agent).generate([]))
    prompt = fake.chat.completions.requests[0]["messages"][1]["content"]
    body = prompt.split("Current user task and contract definition:\n", 1)[1]
    task, _ = json.JSONDecoder().raw_decode(body)
    assert task["current_instruction"] == expected
    assert "OLDER_USER_INSTRUCTION" not in body


def test_native_followup_keeps_full_instruction_and_observation_without_replanner(monkeypatch, tmp_path):
    from dataclasses import replace
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _install_runtime, _request
    from test_working_memory_reminders import _reply, _call

    (tmp_path / "matcher.py").write_text("ACTUAL_MATCHER_SOURCE = True\n")
    requests = []

    class Provider:
        name = "offline"

        def __init__(self):
            self.main_count = 0

        def create_completion(self, _client, **kwargs):
            system = kwargs["messages"][0]["content"]
            requests.append((phase, system, copy.deepcopy(kwargs)))
            if system == "You are a concise coding task planner.":
                return _reply(kwargs, text=json.dumps(PLAN))
            self.main_count += 1
            return (_reply(kwargs, calls=[_call(1, "read_file", {"path": "matcher.py"})])
                    if self.main_count == 1 else _reply(kwargs, text="Inspection complete."))

    snapshot = load_config_snapshot(tmp_path, environ={"NZ_PLANNING_ENABLED": "1",
        "NZ_WORKING_MEMORY_REMINDER_STEPS": "0", "RUNTIME_STATE_PERSIST": "1"})
    options = RunOptions(config_snapshot=snapshot, permission_asker=lambda *_: True)
    original = "Inspect matcher.py and explain existing behavior."
    followup = "Continue investigating. " + "Keep this condition in mind. " * 220 + "FULL_LATE_REQUIREMENT"
    for phase in (1, 2):
        _install_runtime(monkeypatch, Provider())
        messages = [{"role": "user", "content": original}]
        if phase == 2:
            messages += [{"role": "assistant", "content": "Inspection complete."}, {"role": "user", "content": followup}]
        request = replace(_request(tmp_path), session_id=tmp_path.name, messages=tuple(messages))
        env = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
        try:
            result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
            # 本场景只提供编码与记忆模型，没有独立交付评审。
            verification = env.verification_runtime.domain.snapshot()
            assert not verification.reviews and verification.assess_completion("completed").review_required
            assert result.status.value == "completed_unverified"
            if phase == 2:
                assert env.runtime_state.initial_task_text == original
                assert len(env.runtime_state.current_round_instruction_text) == 4000
        finally:
            env.close()
    followup_requests = [kwargs for phase, system, kwargs in requests
                         if phase == 2 and system != "You are a concise coding task planner."]
    assert len(followup_requests) == 2
    visible = str(followup_requests[-1]["messages"])
    assert "FULL_LATE_REQUIREMENT" in visible
    assert "ACTUAL_MATCHER_SOURCE" in visible
