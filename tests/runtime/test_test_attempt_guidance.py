"""测试命令次数是观察数据，不是独立于任务预算的执行禁令。"""
from __future__ import annotations

import pytest

from nz_coder.runtime.execution.runtime_state import RuntimeState


@pytest.mark.parametrize("phase", ["current", "after_change", "restored"])
def test_test_attempt_count_never_forbids_required_retest(phase):
    state = RuntimeState()
    state.reset(max_turns=500, timeout_seconds=300)
    state.broad_test_attempts = 3
    if phase == "after_change":
        state.has_diff = True
        state.diff_chars = 20
        state.changed_files = ["app.py"]
    if phase == "restored":
        saved = state.to_dict(active=True)
        state = RuntimeState()
        assert state.restore(saved)
        state.begin_resumed_activation(max_turns=500, timeout_seconds=300)
    assert state.broad_test_attempts == 3
    prompt = state.build_prompt_block()
    assert "Do NOT run broad test runners again" not in prompt
    assert "STOP: 3 broad test" not in prompt


def test_actual_turn_budget_still_produces_low_budget_guidance():
    state = RuntimeState()
    state.reset(max_turns=500)
    state.broad_test_attempts = 10
    state.turn_count = 499
    state.has_diff = True
    assert "Only 1 turns remaining" in state.build_prompt_block()


def test_static_check_success_does_not_instruct_immediate_completion(tmp_path):
    from tests.verification_scenarios import VerificationScenario

    (tmp_path / "module.py").write_text("VALUE = 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.mutate(1, ["module.py"])
    scenario.settle_shell("python -m py_compile module.py")
    state = scenario.state
    state.max_turns = 500
    assert state.py_compile_ok
    state.verification_attempts = 1
    state.turn_count = state.last_edit_turn = 4
    prompt = state.build_prompt_block(include_current_facts=True)
    assert "Python compilation passed for the changed files" in prompt
    assert "FINALIZE now" not in prompt
    assert "behavior" in prompt
