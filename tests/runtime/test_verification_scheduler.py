"""Budget-zone verification scheduling contracts."""
from __future__ import annotations


def _status(
    *,
    static="pending",
    targeted="pending",
    regression="optional",
    targeted_provenance="failure_evidence",
):
    return {
        "verification_pipeline": {
            "stages": [
                {
                    "name": "static",
                    "status": static,
                    "required": True,
                    "commands": [{
                        "command": "python -m py_compile src/app.py",
                        "required": True,
                        "status": static,
                    }],
                    "observed": [],
                },
                {
                    "name": "targeted",
                    "status": targeted,
                    "required": True,
                    "commands": [{
                        "command": "pytest tests/test_app.py",
                        "required": True,
                        "status": targeted,
                        "automation_provenance": targeted_provenance,
                    }],
                    "observed": [],
                },
                {
                    "name": "regression",
                    "status": regression,
                    "required": False,
                    "commands": [{
                        "command": "pytest",
                        "required": False,
                        "status": "not_run",
                    }],
                    "observed": [],
                },
            ],
        },
    }


def test_yellow_schedules_static_not_exact_acceptance():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "yellow",
        verification_status=_status(),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
    )

    assert action.kind == "stage"
    assert action.stage == "static"
    assert action.command == "python -m py_compile src/app.py"


def test_orange_schedules_targeted_not_regression():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "orange",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
    )

    assert action.kind == "stage"
    assert action.stage == "targeted"
    assert action.command == "pytest tests/test_app.py"


def test_scheduler_does_not_execute_inferred_targeted_command():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "orange",
        verification_status=_status(
            static="passed",
            targeted_provenance="",
        ),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
    )

    assert action.kind == "none"
    assert action.command == ""


def test_red_allows_exact_only_after_requirement_ledger_is_clear():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    blocked = VerificationScheduler().action(
        "red",
        verification_status=_status(static="passed", targeted="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
    )
    ready = VerificationScheduler().action(
        "red",
        verification_status=_status(static="passed", targeted="passed"),
        unresolved_requirements=(),
        has_exact_contract=True,
    )

    assert blocked.kind == "none"
    assert ready.kind == "acceptance"


def test_completion_always_requests_available_exact_contract():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "completion",
        verification_status=_status(),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
    )

    assert action.kind == "acceptance"


def test_scheduler_never_selects_optional_regression_at_budget_zone():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "red",
        verification_status=_status(static="passed", targeted="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=False,
    )

    assert action.kind == "none"
    assert action.command == ""


def test_targeted_stage_is_eligible_once_per_mutation_generation():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    scheduler = VerificationScheduler()
    first = scheduler.action(
        "orange",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
        mutation_generation=2,
        scheduled_generations={"targeted": 1},
    )
    repeated = scheduler.action(
        "orange",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
        mutation_generation=2,
        scheduled_generations={"targeted": 2},
    )

    assert first.stage == "targeted"
    assert first.mutation_generation == 2
    assert repeated.kind == "none"


def test_test_or_documentation_mutations_do_not_repeat_passed_source_stage():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "red",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
        mutation_generation=6,
        source_mutation_generation=2,
        scheduled_generations={"targeted": 3},
    )

    assert action.kind == "none"


def test_exact_failure_followed_by_test_repair_skips_weaker_targeted_stage():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action(
        "red",
        verification_status=_status(static="passed"),
        unresolved_requirements=("R1",),
        has_exact_contract=True,
        exact_attempts=1,
        mutation_generation=6,
        source_mutation_generation=2,
        scheduled_generations={},
    )

    assert action.kind == "none"
