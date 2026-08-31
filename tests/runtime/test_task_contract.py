"""Behavioral coverage for planner-owned task contracts and evidence ledgers."""
from __future__ import annotations

import json

import pytest


def _planner_payload() -> str:
    return json.dumps({
        "objective": "Add named cron fields and coverage",
        "plan": [
            {
                "title": "Implement named fields",
                "target": "cron_engine/parser.py",
                "verification": "parser tests",
            },
            {
                "title": "Document syntax",
                "target": "README.md",
                "verification": "review diff",
            },
        ],
        "requirements": [
            {
                "id": "R1",
                "description": "Support JAN-DEC names",
                "kind": "behavior",
                "expected_artifacts": ["cron_engine/parser.py"],
                "satisfaction_mode": "mixed",
            },
            {
                "id": "R2",
                "description": "Add parser coverage",
                "kind": "test",
                "expected_artifacts": ["cron_engine/tests/test_parser.py"],
                "satisfaction_mode": "mixed",
            },
            {
                "id": "R3",
                "description": "Update README",
                "kind": "docs",
                "expected_artifacts": ["README.md"],
                "satisfaction_mode": "deterministic",
            },
        ],
        "constraints": ["Preserve numeric API"],
    })


def test_planner_envelope_builds_plan_and_workspace_safe_contract(tmp_path):
    from nz_coder.runtime.agent.task_contract import parse_planner_output

    envelope = parse_planner_output(_planner_payload(), tmp_path)

    assert envelope.plan_text.startswith("## Plan\n1. Implement named fields")
    assert envelope.contract.objective == "Add named cron fields and coverage"
    assert [item.id for item in envelope.contract.requirements] == ["R1", "R2", "R3"]
    assert envelope.contract.requirements[0].expected_artifacts == (
        "cron_engine/parser.py",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["requirements"].append(dict(value["requirements"][0])),
        lambda value: value["requirements"][0].update(
            {"expected_artifacts": ["../outside.py"]}
        ),
        lambda value: value["requirements"][0].update({"kind": "mystery"}),
    ],
)
def test_planner_contract_rejects_duplicate_unsafe_or_unknown_requirements(
    tmp_path,
    mutation,
):
    from nz_coder.runtime.agent.task_contract import parse_planner_output

    value = json.loads(_planner_payload())
    mutation(value)

    with pytest.raises(ValueError):
        parse_planner_output(json.dumps(value), tmp_path)


def test_legacy_markdown_planner_output_remains_usable_without_contract(tmp_path):
    from nz_coder.runtime.agent.task_contract import parse_planner_output

    envelope = parse_planner_output("## Plan\n1. Existing behavior", tmp_path)

    assert envelope.plan_text == "## Plan\n1. Existing behavior"
    assert envelope.contract.requirements == ()


def test_ledger_keeps_behavior_candidate_until_current_generation_verification(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, parse_planner_output

    contract = parse_planner_output(_planner_payload(), tmp_path).contract
    ledger = RequirementLedger.from_contract(contract)

    ledger.observe_mutation(1, ["cron_engine/parser.py", "README.md"])

    assert ledger.status("R1") == "candidate"
    assert ledger.status("R2") == "pending"
    assert ledger.status("R3") == "satisfied"

    ledger.observe_verification(
        1,
        command="pytest -q cron_engine/tests/test_parser.py",
        passed=True,
        acceptance=False,
    )

    assert ledger.status("R1") == "satisfied"
    assert ledger.status("R2") == "pending"


def test_acceptance_satisfies_only_requirements_with_artifact_evidence(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, parse_planner_output

    contract = parse_planner_output(_planner_payload(), tmp_path).contract
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(2, ["cron_engine/parser.py", "README.md"])

    ledger.observe_verification(
        2,
        command="python -m pytest -q cron_engine/tests",
        passed=True,
        acceptance=True,
    )

    assert ledger.status("R1") == "satisfied"
    assert ledger.status("R2") == "pending"
    assert ledger.status("R3") == "satisfied"


def test_new_mutation_invalidates_semantic_satisfaction_but_not_docs(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, parse_planner_output

    contract = parse_planner_output(_planner_payload(), tmp_path).contract
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(
        1,
        ["cron_engine/parser.py", "cron_engine/tests/test_parser.py", "README.md"],
    )
    ledger.observe_verification(
        1,
        command="python -m pytest -q cron_engine/tests",
        passed=True,
        acceptance=True,
    )

    ledger.observe_mutation(2, ["cron_engine/parser.py"])

    assert ledger.status("R1") == "candidate"
    assert ledger.status("R2") == "candidate"
    assert ledger.status("R3") == "satisfied"


@pytest.mark.parametrize(
    ("paths", "expected_status"),
    [
        (["README.md"], "satisfied"),
        (["docs/guide.rst"], "satisfied"),
        (["README.md", "cron_engine/parser.py"], "candidate"),
        (["cron_engine/tests/test_parser.py"], "candidate"),
        ([], "candidate"),
    ],
)
def test_mutation_invalidation_preserves_evidence_only_for_attributed_docs(
    tmp_path,
    paths,
    expected_status,
):
    """Docs-only writes cannot stale code evidence; every other scope can."""
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Fix parser behavior",
        "requirements": [
            {
                "id": "R1",
                "description": "Fix parser behavior",
                "kind": "behavior",
                "expected_artifacts": ["cron_engine/parser.py"],
            },
            {
                "id": "R2",
                "description": "Pass parser acceptance",
                "kind": "verification",
            },
        ],
        "acceptance_commands": [
            "python -m pytest -q cron_engine/tests/test_parser.py"
        ],
    }, workspace=tmp_path)
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(1, ["cron_engine/parser.py"])
    ledger.observe_verification(
        1,
        command="python -m pytest -q cron_engine/tests/test_parser.py",
        passed=True,
        acceptance=True,
    )

    ledger.observe_mutation(2, paths)

    assert ledger.status("R1") == expected_status
    assert ledger.status("R2") == expected_status


def test_ledger_round_trip_preserves_requirement_evidence(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, parse_planner_output

    ledger = RequirementLedger.from_contract(
        parse_planner_output(_planner_payload(), tmp_path).contract
    )
    ledger.observe_mutation(3, ["README.md"])

    restored = RequirementLedger.from_dict(ledger.to_dict())

    assert restored.to_dict() == ledger.to_dict()


def test_contract_preserves_dot_prefixed_workspace_artifact(tmp_path):
    from nz_coder.runtime.agent.task_contract import TaskContract

    contract = TaskContract.from_dict({
        "objective": "Update CI",
        "requirements": [{
            "id": "R1",
            "description": "Update workflow",
            "kind": "artifact",
            "expected_artifacts": [".github/workflows/ci.yml"],
        }],
    }, workspace=tmp_path)

    assert contract.requirements[0].expected_artifacts == (
        ".github/workflows/ci.yml",
    )


def test_runtime_derives_conservative_contract_without_planner_call(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    package = tmp_path / "cron_engine"
    (package / "tests").mkdir(parents=True)
    for relative in (
        "parser.py",
        "tests/test_parser.py",
        "tests/test_scheduler.py",
        "tests/test_cli.py",
        "README.md",
    ):
        target = package / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# fixture\n", encoding="utf-8")

    contract = derive_task_contract(
        (
            "完善 cron_engine：支持 JAN-DEC 月份名称；保持现有数字 API 兼容；"
            "补充 parser、scheduler、CLI 测试并更新 README。"
        ),
        acceptance_command="python -m pytest -q cron_engine/tests",
        workspace=tmp_path,
    )

    assert contract.objective.startswith("完善 cron_engine")
    assert [item.kind for item in contract.requirements] == [
        "behavior",
        "test",
        "test",
        "test",
        "docs",
        "compatibility",
        "verification",
    ]
    assert contract.acceptance_commands == (
        "python -m pytest -q cron_engine/tests",
    )
    assert contract.contract_version == 2
    assert next(
        item for item in contract.requirements if item.kind == "compatibility"
    ).required_evidence == ("semantic_review",)
    assert all(item.description for item in contract.requirements)


def test_acceptance_does_not_satisfy_unmutated_docs_tests_or_compatibility(tmp_path):
    """Acceptance proves its command, not an uncovered compatibility promise."""
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Update docs and tests",
        "requirements": [
            {
                "id": "R1",
                "description": "Update README",
                "kind": "docs",
                "expected_artifacts": [],
            },
            {
                "id": "R2",
                "description": "Update parser tests",
                "kind": "test",
                "expected_artifacts": [],
            },
            {
                "id": "R3",
                "description": "Preserve compatibility",
                "kind": "compatibility",
                "expected_artifacts": [],
            },
        ],
    }, workspace=tmp_path)
    ledger = RequirementLedger.from_contract(contract)

    ledger.observe_verification(
        1,
        command="python -m pytest -q",
        passed=True,
        acceptance=True,
    )

    assert ledger.status("R1") == "pending"
    assert ledger.status("R2") == "pending"
    assert ledger.status("R3") == "candidate"


def test_current_generation_semantic_review_satisfies_compatibility(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Preserve the existing public parser contract",
        "requirements": [{
            "id": "R1",
            "description": "Keep all existing parser inputs compatible",
            "kind": "compatibility",
            "expected_artifacts": [],
        }],
    }, workspace=tmp_path)
    ledger = RequirementLedger.from_contract(contract)

    ledger.observe_verification(
        1,
        command="python -m pytest -q",
        passed=True,
        acceptance=True,
    )
    assert ledger.status("R1") == "candidate"

    ledger.observe_semantic_review(
        1,
        accepted=True,
        fingerprint="verifier_ok:compatibility",
    )

    assert ledger.status("R1") == "satisfied"
    evidence = ledger.items["R1"].evidence
    assert evidence[-1].type == "semantic_review_passed"
    assert evidence[-1].generation == 1


def test_later_mutation_invalidates_semantic_compatibility_evidence(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, TaskContract

    contract = TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve compatibility",
            "kind": "compatibility",
            "required_evidence": [],
        }],
    }, workspace=tmp_path)
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_verification(
        1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    ledger.observe_semantic_review(1, accepted=True, fingerprint="review-1")
    assert ledger.status("R1") == "satisfied"

    ledger.observe_mutation(2, ["package/parser.py"])
    ledger.observe_verification(
        2,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )

    assert ledger.status("R1") == "candidate"
    assert ledger.semantic_review_pending_only() is True

    ledger.observe_semantic_review(2, accepted=True, fingerprint="review-2")
    assert ledger.status("R1") == "satisfied"


def test_compatibility_required_evidence_round_trips_and_defaults(tmp_path):
    from nz_coder.runtime.agent.task_contract import TaskContract

    defaulted = TaskContract.from_dict({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve compatibility",
            "kind": "compatibility",
        }],
    }, workspace=tmp_path)
    assert defaulted.requirements[0].required_evidence == ("semantic_review",)

    restored = TaskContract.from_dict(defaulted.to_dict(), workspace=tmp_path)
    assert restored == defaulted


def test_runtime_contract_never_invents_unrequested_acceptance(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    contract = derive_task_contract(
        "Refactor parser internals while preserving the public API.",
        acceptance_command="",
        workspace=tmp_path,
    )

    assert contract.acceptance_commands == ()
    assert contract.requirements == ()


def test_runtime_contract_treats_test_command_as_verification_not_test_work(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    (tmp_path / "tests").mkdir()
    (tmp_path / "parser.py").write_text("# source\n", encoding="utf-8")
    (tmp_path / "tests" / "test_parser.py").write_text(
        "# tests\n", encoding="utf-8"
    )

    contract = derive_task_contract(
        (
            "Fix parser.py without changing its public API. Do not modify tests; "
            "then run python -m pytest -q tests/test_parser.py."
        ),
        acceptance_command="python -m pytest -q tests/test_parser.py",
        workspace=tmp_path,
    )

    assert [item.kind for item in contract.requirements] == [
        "behavior",
        "compatibility",
        "verification",
    ]
    assert "Do not modify test files." in contract.constraints
    assert all(
        "tests/test_parser.py" not in item.expected_artifacts
        for item in contract.requirements
    )


def test_static_or_unrelated_targeted_check_does_not_satisfy_behavior(tmp_path):
    from nz_coder.runtime.agent.task_contract import RequirementLedger, parse_planner_output

    contract = parse_planner_output(_planner_payload(), tmp_path).contract
    ledger = RequirementLedger.from_contract(contract)
    ledger.observe_mutation(1, ["cron_engine/parser.py"])

    ledger.observe_verification(
        1,
        command="python -m py_compile cron_engine/parser.py",
        passed=True,
        acceptance=False,
    )
    assert ledger.status("R1") == "candidate"

    ledger.observe_verification(
        1,
        command="pytest -q cron_engine/tests/test_scheduler.py",
        passed=True,
        acceptance=False,
    )
    assert ledger.status("R1") == "candidate"

    ledger.observe_verification(
        1,
        command="pytest -q cron_engine/tests/test_parser.py",
        passed=True,
        acceptance=False,
    )
    assert ledger.status("R1") == "satisfied"
