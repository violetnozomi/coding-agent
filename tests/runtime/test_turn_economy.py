"""Provider-turn attribution and evidence-safe convergence contracts."""
from __future__ import annotations

from types import SimpleNamespace

from nz_coder.runtime.execution.turn_economy import (
    begin_provider_turn,
    early_tool_completion_ready,
    settle_provider_turn,
)


def _state(**overrides):
    values = {
        "turn_count": 1,
        "transition": "",
        "work_phase": "normal",
        "budget_pressure_zone": "green",
        "has_diff": False,
        "mutation_generation": 0,
        "verification_generation": -1,
        "verification_failures": 0,
        "last_verification_failure": "",
        "completion_review_rejections": 0,
        "last_completion_review_rejection": "",
        "verification_contract": {},
        "requirement_ledger": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _call(name: str) -> dict:
    return {
        "id": f"call-{name}",
        "type": "function",
        "function": {"name": name, "arguments": "{}"},
    }


def test_provider_turn_reason_tracks_runtime_phase_and_evidence():
    assert begin_provider_turn(_state(), [], 1).reason == "initial_investigation"
    assert begin_provider_turn(_state(turn_count=2), [], 2).reason == "investigation"
    assert begin_provider_turn(
        _state(has_diff=True, mutation_generation=2, verification_generation=1),
        [],
        3,
    ).reason == "verification"
    assert begin_provider_turn(
        _state(
            has_diff=True,
            mutation_generation=2,
            verification_generation=2,
        ),
        [],
        4,
    ).reason == "implementation"


def test_provider_turn_reason_prioritizes_repair_and_convergence_prompts():
    failure = begin_provider_turn(
        _state(
            has_diff=True,
            mutation_generation=2,
            verification_failures=1,
            last_verification_failure="1 failed",
        ),
        [],
        5,
    )
    assert failure.reason == "failure_repair"

    review_failure = begin_provider_turn(
        _state(
            has_diff=True,
            mutation_generation=2,
            completion_review_rejections=1,
            last_completion_review_rejection="semantic revision required",
        ),
        [],
        5,
    )
    assert review_failure.reason == "failure_repair"

    requirement = begin_provider_turn(
        _state(has_diff=True, mutation_generation=2),
        [{"role": "user", "content": "continue", "_nz_completion_gate": True}],
        6,
    )
    assert requirement.reason == "requirement_repair"

    convergence = begin_provider_turn(
        _state(turn_count=12, work_phase="closure_repair", budget_pressure_zone="red"),
        [],
        12,
    )
    assert convergence.reason == "convergence"


def test_provider_turn_outcome_uses_structured_tool_classes_and_state_delta():
    state = _state(turn_count=2)
    snapshot = begin_provider_turn(state, [], 2)

    read = settle_provider_turn(
        snapshot,
        state,
        tool_calls=[_call("read_file"), _call("grep_search")],
        finish_reason="tool_calls",
    )
    assert read.outcome == "investigation_batch"
    assert read.tool_names == ("read_file", "grep_search")

    state.mutation_generation = 1
    mutation = settle_provider_turn(
        snapshot,
        state,
        tool_calls=[_call("apply_patch")],
        finish_reason="tool_calls",
    )
    assert mutation.outcome == "mutation_batch"
    assert mutation.mutation_delta == 1

    verification_snapshot = begin_provider_turn(state, [], 3)
    verification = settle_provider_turn(
        verification_snapshot,
        state,
        tool_calls=[_call("bash")],
        finish_reason="tool_calls",
    )
    assert verification.outcome == "verification_batch"

    mixed = settle_provider_turn(
        verification_snapshot,
        state,
        tool_calls=[_call("read_file"), _call("edit_file")],
        finish_reason="tool_calls",
    )
    assert mixed.outcome == "mixed_tool_batch"


def test_provider_turn_outcome_uses_registered_filesystem_effects():
    """Real NZ write tools must not depend on a copied reference-name set."""
    import nz_coder.runtime.agent.agent_manager  # noqa: F401

    state = _state(turn_count=2)
    snapshot = begin_provider_turn(state, [], 2)

    batch = settle_provider_turn(
        snapshot,
        state,
        tool_calls=[_call("write_files_batch"), _call("apply_agent_changes")],
        finish_reason="tool_calls",
    )

    assert batch.outcome == "mutation_batch"


def test_provider_turn_outcome_distinguishes_final_and_error():
    state = _state()
    snapshot = begin_provider_turn(state, [], 1)

    final = settle_provider_turn(
        snapshot,
        state,
        tool_calls=[],
        finish_reason="stop",
    )
    assert final.outcome == "final_answer"

    error = settle_provider_turn(
        snapshot,
        state,
        tool_calls=[],
        finish_reason="error",
    )
    assert error.outcome == "provider_error"


def test_early_tool_completion_requires_current_exact_and_complete_ledger():
    current = _state(
        has_diff=True,
        mutation_generation=3,
        verification_contract={
            "command": "python -m pytest -q tests",
            "targets": ["tests"],
            "attempted_generation": 3,
            "passed": True,
        },
        requirement_ledger={"items": []},
    )
    assert early_tool_completion_ready(current) is True

    current.verification_contract["attempted_generation"] = 2
    assert early_tool_completion_ready(current) is False
    current.verification_contract["attempted_generation"] = 3
    current.verification_contract["passed"] = False
    assert early_tool_completion_ready(current) is False


def test_early_tool_completion_keeps_acceptance_current_after_docs_only_edit():
    state = _state(
        has_diff=True,
        mutation_generation=4,
        acceptance_mutation_generation=3,
        verification_contract={
            "command": "python -m pytest -q tests",
            "targets": ["tests"],
            "attempted_generation": 3,
            "passed": True,
        },
        requirement_ledger={"items": []},
    )

    assert early_tool_completion_ready(state) is True


def test_early_tool_completion_accepts_runtime_owned_semantic_review_only():
    state = _state(
        has_diff=True,
        mutation_generation=4,
        verification_contract={
            "command": "pytest -q tests",
            "targets": ["tests"],
            "attempted_generation": 4,
            "passed": True,
        },
        requirement_ledger={
            "latest_generation": 4,
            "items": [{
                "requirement": {
                    "id": "R1",
                    "description": "preserve compatibility",
                    "kind": "compatibility",
                    "expected_artifacts": [],
                    "satisfaction_mode": "mixed",
                    "depends_on": [],
                    "required_evidence": ["semantic_review"],
                },
                "status": "candidate",
                "evidence": [{"type": "verification_passed", "generation": 4}],
                "mutation_generation": 4,
            }],
        },
    )

    assert early_tool_completion_ready(state) is True
    state.requirement_ledger["items"][0]["evidence"] = []
    assert early_tool_completion_ready(state) is False
