"""Behavioral tests for run-level work-budget convergence pressure."""
from __future__ import annotations


def test_work_budget_emits_each_pressure_zone_once():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    budget = WorkBudgetController(max_turns=20)

    assert budget.next_notice(10) is None
    assert budget.next_notice(11).zone == "yellow"
    assert budget.next_notice(12) is None
    assert budget.next_notice(13).zone == "orange"
    assert budget.next_notice(14) is None
    assert budget.next_notice(15).zone == "red"
    assert budget.next_notice(20) is None


def test_work_budget_resume_skips_previously_emitted_zone():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    budget = WorkBudgetController(max_turns=20, emitted=("yellow",))

    notice = budget.next_notice(13)

    assert notice is not None
    assert notice.zone == "orange"
    assert budget.emitted == ("yellow", "orange")


def test_work_budget_reports_current_zone_after_notice_was_consumed():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    budget = WorkBudgetController(max_turns=20)

    assert budget.next_notice(13).zone == "orange"
    assert budget.next_notice(13) is None
    assert budget.zone(13) == "orange"
    assert budget.zone(15) == "red"


def test_work_budget_does_not_pressure_single_turn_run_before_first_call():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    assert WorkBudgetController(max_turns=1).next_notice(0) is None


def test_work_budget_keeps_post_nominal_work_available_until_hard_cap():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    budget = WorkBudgetController(max_turns=20)

    assert budget.nominal_turns == 15
    assert budget.normal_turns == 13
    assert budget.phase(12) == "normal"
    assert budget.phase(13) == "closure_repair"
    assert budget.phase(14) == "closure_finalize"
    assert budget.phase(15) == "soft_extension"
    assert budget.phase(19) == "soft_extension"
    assert budget.phase(20) == "hard_cap"


def test_small_hard_cap_scales_closure_reserve_without_exceeding_limit():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    budget = WorkBudgetController(max_turns=4)

    assert budget.nominal_turns == 4
    assert budget.normal_turns == 2
    assert budget.phase(2) == "closure_repair"
    assert budget.phase(3) == "closure_finalize"
    assert budget.phase(4) == "hard_cap"


def test_three_turn_cap_preserves_two_normal_work_calls_before_closure():
    from nz_coder.runtime.execution.work_budget import WorkBudgetController

    budget = WorkBudgetController(max_turns=3)

    assert budget.nominal_turns == 3
    assert budget.normal_turns == 2
    assert budget.phase(1) == "normal"
    assert budget.phase(2) == "closure_repair"
    assert budget.phase(3) == "hard_cap"


def test_bounded_emergency_requires_all_four_objective_facts():
    from nz_coder.runtime.execution.work_budget import evaluate_emergency_extension

    eligible = evaluate_emergency_extension(
        has_diff=True,
        failure_evidence_exists=True,
        repair_target_known=True,
        needs_broad_exploration=False,
    )
    assert eligible.eligible is True

    assert evaluate_emergency_extension(
        has_diff=False,
        failure_evidence_exists=True,
        repair_target_known=True,
        needs_broad_exploration=False,
    ).eligible is False
    assert evaluate_emergency_extension(
        has_diff=True,
        failure_evidence_exists=True,
        repair_target_known=False,
        needs_broad_exploration=False,
    ).eligible is False
    assert evaluate_emergency_extension(
        has_diff=True,
        failure_evidence_exists=True,
        repair_target_known=True,
        needs_broad_exploration=True,
    ).eligible is False
