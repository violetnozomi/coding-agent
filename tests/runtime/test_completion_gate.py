"""Completion gate coverage independent of model-maintained Todo state."""
from __future__ import annotations

import json


def _ledger(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, parse_planner_output

    envelope = parse_planner_output(json.dumps({
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
    }), tmp_path)
    return RequirementLedger.from_contract(envelope.contract)


def test_completion_gate_blocks_with_bounded_missing_requirements(tmp_path):
    from nz_coder.runtime.verification.completion_gate import CompletionGate

    ledger = _ledger(tmp_path)
    ledger.observe_mutation(1, ["src/app.py"])

    decision = CompletionGate().evaluate(ledger, mutation_generation=1)

    assert decision.ready is False
    assert decision.missing_ids == ("R1", "R2")
    assert "R1: Change behavior" in decision.message
    assert "R2: Update docs" in decision.message
    assert "expected artifacts: src/app.py" in decision.message
    assert "expected artifacts: README.md" in decision.message


def test_completion_gate_allows_current_generation_satisfied_ledger(tmp_path):
    from nz_coder.runtime.verification.completion_gate import CompletionGate

    ledger = _ledger(tmp_path)
    ledger.observe_mutation(2, ["src/app.py", "README.md"])
    ledger.observe_verification(
        2,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )

    decision = CompletionGate().evaluate(ledger, mutation_generation=2)

    assert decision.ready is True
    assert decision.missing_ids == ()


def test_completion_gate_separates_agent_repairs_from_runtime_semantic_evidence(
    tmp_path,
):
    from nz_coder.runtime.verification.completion_gate import CompletionGate
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
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
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(1, ["cron_engine/parser.py"])
    ledger.observe_verification(
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
    from nz_coder.runtime.agent.task_contract import RequirementLedger

    decision = CompletionGate().evaluate(
        RequirementLedger(),
        mutation_generation=0,
    )

    assert decision.ready is True


def test_policy_runs_semantic_verifier_before_rechecking_requirement_ledger():
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.adapters.runner import _PolicyService
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests",
        passed=True,
        acceptance=True,
    )
    order = []

    class Verifier:
        async def verify(self, _context, _messages, status, _content):
            order.append("semantic-review")
            state.observe_requirement_semantic_review(
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


def test_policy_keeps_semantic_requirement_open_when_verifier_has_no_evidence():
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.adapters.runner import _PolicyService
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests",
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

    assert result == "continue"
    assert state.semantic_review_pending_only() is True
    assert messages[-1]["_nz_completion_gate"] is True


def test_policy_continues_when_completion_guidance_budget_is_exhausted():
    import asyncio
    from types import SimpleNamespace

    from nz_coder.runtime.adapters.runner import _PolicyService
    from nz_coder.runtime.verification.completion_gate import COMPLETION_GATE_REANIMATE_BUDGET
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.agent.task_contract import TaskContract

    state = RuntimeState()
    state.set_task_contract(TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve the public API",
            "kind": "compatibility",
        }],
    }))
    state.observe_tool("edit_file", {"path": "app.py"}, "Done")
    state.observe_requirement_verification(
        "pytest -q tests",
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

    assert result == "continue"
    assert messages == []
    assert persisted == [{"active": True}]
    assert any(
        event == "requirement_completion_budget_exhausted"
        and payload["missing_ids"] == ["R1"]
        for event, payload in trace_events
    )
