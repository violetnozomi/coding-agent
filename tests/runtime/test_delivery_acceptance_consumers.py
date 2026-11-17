"""同一组明确验收在提前收尾、动态提示和评审材料中必须完整出现。"""
from __future__ import annotations

from tests.verification_scenarios import VerificationScenario
from nz_coder.runtime.verification.domain.models import ExecutionFact, WorkspaceObservation, WorkspaceVersion
from nz_coder.runtime.verification.domain.requirements import TaskContract
from nz_coder.runtime.execution.turn_economy import early_tool_completion_ready
from nz_coder.runtime.verification.sidecar_verifier import _render_contract_criteria
from nz_coder.runtime.conversation.continuation_context import build_current_facts


def partial(root):
    scenario = VerificationScenario(workspace=root)
    scenario.state.set_task_contract(TaskContract(objective="Run both checks",
        acceptance_commands=("python first.py", "python second.py")))
    scenario.runtime.synchronize_task()
    scenario.state.has_diff = True
    scenario.state.mutation_generation = 1
    scenario.state.source_mutation_generation = 1
    scenario.state.acceptance_mutation_generation = 1
    version = WorkspaceVersion(1, 1, 1)
    scenario.domain.observe_workspace(WorkspaceObservation(version))
    first = next(item for item in scenario.domain.snapshot().specs if "first.py" in item.command.original)
    scenario.domain.begin_execution("first", (first.id,))
    scenario.domain.observe_execution(ExecutionFact("first", scenario.domain.scope, first.command,
        "succeeded", version, version, capabilities=(first.capability,), exit_code=0))
    return scenario


def test_first_pass_cannot_enable_early_tool_completion(tmp_path):
    scenario = partial(tmp_path)
    assert scenario.state.verification_contract["passed"] is True
    assert not early_tool_completion_ready(scenario.state)


def test_partial_acceptance_does_not_prompt_immediate_final_answer(tmp_path):
    scenario = partial(tmp_path)
    prompt = scenario.state.build_prompt_block()
    assert "DECLARED ACCEPTANCE PASSED" not in prompt
    assert "Do not call more tools" not in prompt


def test_semantic_review_criteria_include_every_declared_command(tmp_path):
    scenario = partial(tmp_path)
    rendered = "\n".join(_render_contract_criteria(scenario.state.to_dict()))
    assert "python first.py" in rendered
    assert "python second.py" in rendered


def test_current_facts_include_unexecuted_second_command(tmp_path):
    scenario = partial(tmp_path)
    rendered = build_current_facts(scenario.state.to_dict(), workspace=tmp_path)
    assert "python first.py" in rendered
    assert "python second.py" in rendered
    assert "pending" in rendered or "unknown" in rendered
