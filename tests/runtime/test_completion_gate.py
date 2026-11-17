"""Completion gate coverage independent of model-maintained Todo state."""
from __future__ import annotations

import pytest


def _ledger(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
        "objective": "Change behavior and docs",
        "plan": [],
        "requirements": [
            {
                "id": "R1",
                "description": "Change behavior",
                "kind": "behavior",
                "expected_artifacts": ["src/app.py"],
                "satisfaction_mode": "mixed",
            },
            {
                "id": "R2",
                "description": "Update docs",
                "kind": "docs",
                "expected_artifacts": ["README.md"],
                "satisfaction_mode": "deterministic",
            },
        ],
        "constraints": [],
    }, tmp_path)
    return VerificationScenario(contract)


def test_completion_gate_blocks_with_bounded_missing_requirements(tmp_path):
    from nz_coder.runtime.verification.completion_gate import CompletionGate

    ledger = _ledger(tmp_path)
    ledger.mutate(1, ["src/app.py"])

    decision = CompletionGate().evaluate(ledger, mutation_generation=1)

    assert decision.ready is False
    assert decision.missing_ids == ("R1", "R2")
    assert "R1: Change behavior" in decision.message
    assert "R2: Update docs" in decision.message
    assert "Runtime-owned evidence pending" in decision.message
    assert "expected artifacts: README.md" in decision.message


def test_completion_gate_allows_current_generation_satisfied_ledger(tmp_path):
    from nz_coder.runtime.verification.completion_gate import CompletionGate

    ledger = _ledger(tmp_path)
    ledger.mutate(2, ["src/app.py", "README.md"])
    ledger.execute(
        2,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )

    ledger.review(2, accepted=True)

    decision = CompletionGate().evaluate(ledger, mutation_generation=2)

    assert decision.ready is True
    assert decision.missing_ids == ()


def test_completion_gate_separates_agent_repairs_from_runtime_semantic_evidence(
    tmp_path,
):
    from nz_coder.runtime.verification.completion_gate import CompletionGate
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
        "objective": "Update docs while preserving compatibility",
        "requirements": [
            {
                "id": "R5",
                "description": "Update package README examples",
                "kind": "docs",
                "expected_artifacts": ["cron_engine/README.md"],
            },
            {
                "id": "R6",
                "description": "Preserve numeric range compatibility",
                "kind": "compatibility",
            },
        ],
    }, workspace=tmp_path)
    ledger = VerificationScenario(contract)
    ledger.mutate(1, ["cron_engine/parser.py"])
    ledger.execute(
        1,
        command="pytest -q cron_engine/tests",
        passed=True,
        acceptance=True,
    )

    decision = CompletionGate().evaluate(ledger, mutation_generation=1)

    assert decision.missing_ids == ("R5", "R6")
    repair_section, review_section = decision.message.split(
        "Runtime-owned evidence pending", 1,
    )
    assert "R5: Update package README examples" in repair_section
    assert "expected artifacts: cron_engine/README.md" in repair_section
    assert "R6: Preserve numeric range compatibility" not in repair_section
    assert "R6: Preserve numeric range compatibility" in review_section
    assert "semantic_review" in review_section
    assert "Do not edit code solely to satisfy" in review_section


def test_completion_gate_is_inert_without_requirements():
    from nz_coder.runtime.verification.completion_gate import CompletionGate
    from tests.verification_scenarios import VerificationScenario

    decision = CompletionGate().evaluate(
        VerificationScenario(),
        mutation_generation=0,
    )

    assert decision.ready is True


@pytest.mark.parametrize("prompt_count", [0, 2])
def test_policy_runs_semantic_verifier_before_rechecking_requirement_ledger(prompt_count):
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.adapters.runner import _PolicyService
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    from tests.verification_scenarios import VerificationScenario
    scenario = VerificationScenario(parse_task_contract({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state = scenario.state
    scenario.mutate(1, ["app.py"])
    scenario.execute(1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    state.completion_gate_prompts = prompt_count
    order = []

    class Verifier:
        async def verify(self, _context, _messages, status, _content):
            order.append("semantic-review")
            scenario.review(state.mutation_generation,
                accepted=True,
                fingerprint="verifier_ok:compatibility",
            )
            return status

    host = SimpleNamespace(
        runtime_state=state,
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
        _persist_runtime_state=lambda **_kwargs: None,
    )
    policy = _PolicyService(host, SimpleNamespace(verifier=Verifier()))

    result = asyncio.run(policy.verify_completion([], "completed", "Done"))

    assert order == ["semantic-review"]
    assert result == "completed"
    assert state.requirement_ledger_snapshot().unresolved() == ()


def test_policy_leaves_unresolved_requirement_for_runner_settlement():
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.adapters.runner import _PolicyService
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    from tests.verification_scenarios import VerificationScenario
    scenario = VerificationScenario(parse_task_contract({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state = scenario.state
    scenario.mutate(1, ["app.py"])
    scenario.execute(1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )

    class Verifier:
        async def verify(self, _context, _messages, status, _content):
            return status

    messages = []
    host = SimpleNamespace(
        runtime_state=state,
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
        _persist_runtime_state=lambda **_kwargs: None,
    )
    policy = _PolicyService(host, SimpleNamespace(verifier=Verifier()))

    result = asyncio.run(policy.verify_completion(messages, "completed", "Done"))

    assert result == "completed"
    assert state.semantic_review_pending_only() is True
    assert messages == []
    assert state.completion_gate_prompts == 0


def test_policy_does_not_own_exhausted_runner_guidance_budget():
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.adapters.runner import _PolicyService
    from nz_coder.runtime.verification.completion_gate import COMPLETION_GATE_REANIMATE_BUDGET
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    from tests.verification_scenarios import VerificationScenario
    scenario = VerificationScenario(parse_task_contract({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state = scenario.state
    scenario.mutate(1, ["app.py"])
    scenario.execute(1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    state.completion_gate_prompts = COMPLETION_GATE_REANIMATE_BUDGET
    trace_events = []
    persisted = []

    class Verifier:
        async def verify(self, _context, _messages, status, _content):
            return status

    host = SimpleNamespace(
        runtime_state=state,
        tracer=SimpleNamespace(
            log=lambda event, **payload: trace_events.append((event, payload)),
        ),
        _persist_runtime_state=lambda **payload: persisted.append(payload),
    )
    policy = _PolicyService(host, SimpleNamespace(verifier=Verifier()))
    messages = []

    result = asyncio.run(policy.verify_completion(messages, "completed", "Done"))

    assert result == "completed"
    assert messages == []
    assert persisted == []
    assert state.completion_gate_prompts == COMPLETION_GATE_REANIMATE_BUDGET
    assert [item.requirement.id for item in state.requirement_ledger_snapshot().unresolved()] == ["R1"]
    assert trace_events == []
