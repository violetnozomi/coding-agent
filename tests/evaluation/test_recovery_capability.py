from __future__ import annotations


def test_recovery_fault_injection_suite_exercises_all_declared_failures(tmp_path) -> None:
    from nz_coder.evaluation.recovery_capability import run_recovery_fault_injection_suite

    result = run_recovery_fault_injection_suite(tmp_path)

    assert [run["case_id"] for run in result["runs"]] == [
        "R1", "R2", "R3", "R4", "R5", "R6", "R7",
    ]
    assert result["passed"] == 7
    assert result["success_rate"] == 1.0
