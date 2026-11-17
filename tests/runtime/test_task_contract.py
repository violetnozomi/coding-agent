"""Behavioral coverage for host-owned task contracts and evidence ledgers."""
from __future__ import annotations

import json

import pytest


def _contract_payload() -> str:
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


def test_host_contract_keeps_workspace_safe_requirement_definitions(tmp_path):
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract(json.loads(_contract_payload()), tmp_path)
    assert contract.objective == "Add named cron fields and coverage"
    assert [item.id for item in contract.requirements] == ["R1", "R2", "R3"]
    assert contract.requirements[0].expected_artifacts == ("cron_engine/parser.py",)


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
def test_host_contract_rejects_duplicate_unsafe_or_unknown_requirements(
    tmp_path,
    mutation,
):
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    value = json.loads(_contract_payload())
    mutation(value)

    with pytest.raises(ValueError):
        parse_task_contract(value, tmp_path)


def test_ledger_keeps_behavior_candidate_until_current_generation_verification(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract(json.loads(_contract_payload()), tmp_path)
    ledger = VerificationScenario(contract, workspace=tmp_path)

    ledger.mutate(1, ["cron_engine/parser.py", "README.md"])

    assert ledger.status("R1") == "candidate"
    assert ledger.status("R2") == "pending"
    assert ledger.status("R3") == "satisfied"

    ledger.execute(
        1,
        command="pytest -q cron_engine/tests/test_parser.py",
        passed=True,
        acceptance=False,
    )

    assert ledger.status("R1") == "candidate"
    ledger.review(1, accepted=True, fingerprint="actual-semantic-review")
    assert ledger.status("R1") == "satisfied"
    assert ledger.status("R2") != "satisfied"


def test_acceptance_satisfies_only_requirements_with_artifact_evidence(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract(json.loads(_contract_payload()), tmp_path)
    ledger = VerificationScenario(contract, workspace=tmp_path)
    ledger.mutate(2, ["cron_engine/parser.py", "README.md"])

    ledger.execute(
        2,
        command="python -m pytest -q cron_engine/tests",
        passed=True,
        acceptance=True,
    )

    assert ledger.status("R1") == "candidate"
    ledger.review(2, accepted=True, fingerprint="actual-semantic-review")
    assert ledger.status("R1") == "satisfied"
    assert ledger.status("R2") != "satisfied"
    assert ledger.status("R3") == "satisfied"


def test_new_mutation_invalidates_semantic_satisfaction_but_not_docs(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract(json.loads(_contract_payload()), tmp_path)
    ledger = VerificationScenario(contract, workspace=tmp_path)
    ledger.mutate(
        1,
        ["cron_engine/parser.py", "cron_engine/tests/test_parser.py", "README.md"],
    )
    ledger.execute(
        1,
        command="python -m pytest -q cron_engine/tests",
        passed=True,
        acceptance=True,
    )

    ledger.mutate(2, ["cron_engine/parser.py"])

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
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
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
    ledger = VerificationScenario(contract, workspace=tmp_path)
    ledger.mutate(1, ["cron_engine/parser.py"])
    ledger.execute(
        1,
        command="python -m pytest -q cron_engine/tests/test_parser.py",
        passed=True,
        acceptance=True,
    )

    ledger.review(1, accepted=True, fingerprint="review-before-mutation")
    ledger.mutate(2, paths)

    assert ledger.status("R1") == "candidate"
    assert ledger.status("R2") == expected_status


def test_ledger_round_trip_preserves_requirement_evidence(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    ledger = VerificationScenario(
        parse_task_contract(json.loads(_contract_payload()), tmp_path)
    , workspace=tmp_path)
    ledger.mutate(3, ["README.md"])

    restored = RequirementLedgerView.from_dict(ledger.to_dict())

    assert restored.to_dict() == ledger.to_dict()


@pytest.mark.parametrize("reverse", [False, True], ids=["ordered", "forward-references"])
def test_ledger_round_trip_preserves_dependent_requirements_and_evidence(reverse):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    dependencies = [[], ["R1"], ["R1"], [], ["R1"], ["R1", "R5"]]
    requirements = [
        {
            "id": f"R{index}", "description": f"Oval requirement {index}",
            "kind": "test" if index == 6 else "compatibility" if index == 4 else "behavior",
            "expected_artifacts": ["oval/util_test.go" if index == 6 else "oval/util.go"],
            "depends_on": parents,
        }
        for index, parents in enumerate(dependencies, 1)
    ]
    if reverse:
        requirements.reverse()
    ledger = VerificationScenario(parse_task_contract({
        "requirements": requirements,
    }))
    ledger.mutate(3, ["oval/util.go", "oval/util_test.go"])
    ledger.execute(3, command="go test ./oval -run Test_util", passed=True,
                                acceptance=False)

    restored = RequirementLedgerView.from_dict(ledger.to_dict())

    assert restored.to_dict() == ledger.to_dict()
    assert len(restored.items) == 6


def test_runtime_keeps_requirement_dependencies_through_mutation_and_verification():
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    from tests.verification_scenarios import VerificationScenario
    scenario = VerificationScenario(parse_task_contract({
        "requirements": [
            {"id": "R1", "description": "Fix helper", "expected_artifacts": ["index.js"]},
            {"id": "R2", "description": "Preserve repeated input behavior",
             "expected_artifacts": ["index.js"], "depends_on": ["R1"]},
        ],
    }))
    state = scenario.state
    scenario.mutate(1, ["index.js"])
    scenario.execute(1, command="node --test index.test.cjs", passed=True,
                                           acceptance=False)

    scenario.review(1, accepted=True, fingerprint="dependency-review")
    ledger = state.requirement_ledger_snapshot()
    assert list(ledger.items) == ["R1", "R2"]
    assert ledger.items["R2"].requirement.depends_on == ("R1",)
    assert [item.status for item in ledger.items.values()] == ["satisfied", "satisfied"]
    assert all(
        any(evidence.type == "semantic_review_passed" and evidence.generation == 1
            for evidence in item.evidence)
        for item in ledger.items.values()
    )


def test_contract_preserves_dot_prefixed_workspace_artifact(tmp_path):
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
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

    task = (
        "完善 cron_engine：支持 JAN-DEC 月份名称；保持现有数字 API 兼容；"
        "补充 parser、scheduler、CLI 测试并更新 README。"
    )
    contract = derive_task_contract(
        task,
        acceptance_commands=("python -m pytest -q cron_engine/tests",),
        workspace=tmp_path,
    )

    # 泛化质量要求保留在原始需求中，不再凭关键词生成阻塞验收项。
    assert contract.objective == task
    assert [item.kind for item in contract.requirements] == ["verification"]
    assert contract.acceptance_commands == (
        "python -m pytest -q cron_engine/tests",
    )
    assert contract.contract_version == 2
    verification = contract.requirements[0]
    assert verification.satisfaction_mode == "deterministic"
    assert "python -m pytest -q cron_engine/tests" in verification.description
    assert verification.expected_artifacts == ()


def test_acceptance_does_not_satisfy_unmutated_docs_tests_or_compatibility(tmp_path):
    """Acceptance proves its command, not an uncovered compatibility promise."""
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
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
    ledger = VerificationScenario(contract, workspace=tmp_path)

    ledger.execute(
        1,
        command="python -m pytest -q",
        passed=True,
        acceptance=True,
    )

    assert ledger.status("R1") == "pending"
    assert ledger.status("R2") == "pending"
    assert ledger.status("R3") != "satisfied"


def test_current_generation_semantic_review_satisfies_compatibility(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
        "objective": "Preserve the existing public parser contract",
        "requirements": [{
            "id": "R1",
            "description": "Keep all existing parser inputs compatible",
            "kind": "compatibility",
            "expected_artifacts": [],
        }],
    }, workspace=tmp_path)
    ledger = VerificationScenario(contract, workspace=tmp_path)

    ledger.execute(
        1,
        command="python -m pytest -q",
        passed=True,
        acceptance=True,
    )
    assert ledger.status("R1") != "satisfied"

    ledger.review(
        1,
        accepted=True,
        fingerprint="verifier_ok:compatibility",
    )

    assert ledger.status("R1") == "satisfied"
    evidence = ledger.items["R1"].evidence
    assert evidence[-1].type == "semantic_review_passed"
    assert evidence[-1].generation == 1


def test_later_mutation_invalidates_semantic_compatibility_evidence(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve compatibility",
            "kind": "compatibility",
            "required_evidence": [],
        }],
    }, workspace=tmp_path)
    ledger = VerificationScenario(contract, workspace=tmp_path)
    ledger.execute(
        1,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )
    ledger.review(1, accepted=True, fingerprint="review-1")
    assert ledger.status("R1") == "satisfied"

    ledger.mutate(2, ["package/parser.py"])
    ledger.execute(
        2,
        command="pytest -q tests",
        passed=True,
        acceptance=True,
    )

    assert ledger.status("R1") != "satisfied"
    assert ledger.semantic_review_pending_only() is True

    ledger.review(2, accepted=True, fingerprint="review-2")
    assert ledger.status("R1") == "satisfied"


def test_compatibility_required_evidence_round_trips_and_defaults(tmp_path):
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    defaulted = parse_task_contract({
        "objective": "Preserve compatibility",
        "requirements": [{
            "id": "R1",
            "description": "Preserve compatibility",
            "kind": "compatibility",
        }],
    }, workspace=tmp_path)
    assert defaulted.requirements[0].required_evidence == ("semantic_review",)

    restored = parse_task_contract(defaulted.to_dict(), workspace=tmp_path)
    assert restored == defaulted


def test_runtime_contract_never_invents_unrequested_acceptance(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    contract = derive_task_contract(
        "Refactor parser internals while preserving the public API.",
        acceptance_commands=("",),
        workspace=tmp_path,
    )

    assert contract.acceptance_commands == ()
    assert contract.requirements == ()


@pytest.mark.parametrize("restriction", ["不要修改验收文件", "不要修改测试文件", "不要修改 test_minitasks.py"])
def test_artifact_fallback_preserves_readonly_acceptance_file(tmp_path, restriction):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.apply_current_round_instruction(
        f"修复 minitasks.py，使 test_minitasks.py 的15项验收全部通过。{restriction}。运行 python -m unittest -v。",
        workspace=tmp_path,
    )
    artifacts = [path for item in state.task_contract["requirements"] for path in item["expected_artifacts"]]
    assert artifacts == ["minitasks.py"]
    assert state.task_contract["constraints"]


@pytest.mark.parametrize("restriction", ["不要修改测试文件。", "不要修改 test_minitasks.py。"])
def test_restriction_only_followup_removes_stale_write_requirement_but_keeps_acceptance(tmp_path, restriction):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.apply_current_round_instruction("修改 minitasks.py 和 test_minitasks.py。", workspace=tmp_path)
    # A real behavior and acceptance obligation must survive the write restriction.
    state.task_contract["requirements"].append({
        "id": "R3", "kind": "behavior", "description": "Handle stable task IDs",
        "satisfaction_mode": "semantic", "expected_artifacts": [],
    })
    state.task_contract["acceptance_commands"] = ["python -m unittest -v"]
    state.apply_current_round_instruction(restriction, workspace=tmp_path)
    requirements = state.task_contract["requirements"]
    assert [p for item in requirements for p in item["expected_artifacts"]] == ["minitasks.py"]
    assert any(item["description"] == "Handle stable task IDs" for item in requirements)
    assert state.task_contract["acceptance_commands"] == ["python -m unittest -v"]
    assert state.task_contract["constraints"]


def test_followup_can_explicitly_allow_modifying_a_previously_readonly_test(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.apply_current_round_instruction("修改 minitasks.py。不要修改 test_minitasks.py。", workspace=tmp_path)
    state.apply_current_round_instruction("允许修改 test_minitasks.py，添加回归测试。", workspace=tmp_path)
    assert "test_minitasks.py" in [
        p for item in state.task_contract["requirements"] for p in item["expected_artifacts"]
    ]


def test_restriction_update_reaches_the_bound_verification_domain(tmp_path):
    from tests.verification_scenarios import VerificationScenario

    vm = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    vm.state.apply_current_round_instruction("修改 minitasks.py 和 test_minitasks.py。", workspace=tmp_path)
    vm.state.apply_current_round_instruction("不要修改 test_minitasks.py。", workspace=tmp_path)
    assert [path for item in vm.domain.snapshot().requirements
            for path in item.requirement.expected_artifacts] == ["minitasks.py"]


def test_runtime_contract_treats_test_command_as_verification_not_test_work(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract

    (tmp_path / "tests").mkdir()
    (tmp_path / "parser.py").write_text("# source\n", encoding="utf-8")
    (tmp_path / "tests" / "test_parser.py").write_text(
        "# tests\n", encoding="utf-8"
    )

    task = (
        "Fix parser.py without changing its public API. Do not modify tests; "
        "then run python -m pytest -q tests/test_parser.py."
    )
    contract = derive_task_contract(
        task,
        acceptance_commands=("python -m pytest -q tests/test_parser.py",),
        workspace=tmp_path,
    )

    assert [item.kind for item in contract.requirements] == [
        "artifact",
        "verification",
    ]
    assert contract.objective == task
    artifact, verification = contract.requirements
    assert artifact.expected_artifacts == ("parser.py",)
    assert artifact.artifact_operations == (("parser.py", "change"),)
    assert artifact.satisfaction_mode == "deterministic"
    assert contract.acceptance_commands == ("python -m pytest -q tests/test_parser.py",)
    assert verification.satisfaction_mode == "deterministic"
    assert "Do not modify test files." in contract.constraints
    assert all(
        "tests/test_parser.py" not in item.expected_artifacts
        for item in contract.requirements
    )


def test_static_or_unrelated_targeted_check_does_not_satisfy_behavior(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    contract = parse_task_contract(json.loads(_contract_payload()), tmp_path)
    ledger = VerificationScenario(contract, workspace=tmp_path)
    ledger.mutate(1, ["cron_engine/parser.py"])

    ledger.execute(
        1,
        command="python -m py_compile cron_engine/parser.py",
        passed=True,
        acceptance=False,
    )
    assert ledger.status("R1") == "candidate"

    ledger.execute(
        1,
        command="pytest -q cron_engine/tests/test_scheduler.py",
        passed=True,
        acceptance=False,
    )
    assert ledger.status("R1") == "candidate"

    ledger.execute(
        1,
        command="pytest -q cron_engine/tests/test_parser.py",
        passed=True,
        acceptance=False,
    )
    assert ledger.status("R1") == "candidate"
    ledger.review(1, accepted=True, fingerprint="review-after-checks")
    assert ledger.status("R1") == "satisfied"


def test_sequential_entry_file_delivery_survives_unrelated_source_edits(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView
    from nz_coder.runtime.agent.task_contract import derive_round_artifact_contract

    paths = ("taskboard/__init__.py", "taskboard/__main__.py")
    ledger = VerificationScenario(derive_round_artifact_contract(
        "Create taskboard/__init__.py and taskboard/__main__.py.",
        artifact_paths=paths, workspace=tmp_path,
    ), workspace=tmp_path)
    for generation, path in enumerate(paths, 1):
        ledger.mutate(generation, [path], operations={path: "create"})
    ledger.mutate(3, ["taskboard/app.py"])

    assert [item.status for item in ledger.items.values()] == ["satisfied"] * 2
    assert all(item.evidence[0].type == "file_created" for item in ledger.items.values())
    assert RequirementLedgerView.from_dict(ledger.to_dict()).to_dict() == ledger.to_dict()


@pytest.mark.parametrize("kind,path", [("artifact", "entry.py"), ("docs", "README.md")])
@pytest.mark.parametrize("mode", ["deterministic", "mixed", "semantic"])
def test_declared_semantic_review_is_required_for_artifacts_after_each_source_edit(
    kind, path, mode,
):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Deliver the reviewed artifact",
            "kind": kind, "expected_artifacts": [path],
            "satisfaction_mode": mode, "required_evidence": ["semantic_review"],
        }],
    }))
    ledger.mutate(1, [path], operations={path: "create"})
    assert ledger.status("R1") == "candidate"
    ledger.execute(1, command="pytest -q tests", passed=True, acceptance=True)
    assert ledger.status("R1") == "candidate"
    ledger.review(1, accepted=True, fingerprint="review-1")
    assert ledger.status("R1") == "satisfied"

    ledger.mutate(2, ["other_source.py"])

    assert ledger.status("R1") == "candidate"
    ledger.execute(2, command="pytest -q tests", passed=True, acceptance=True)
    assert ledger.status("R1") == "candidate"
    ledger.review(2, accepted=True, fingerprint="review-2")
    assert ledger.status("R1") == "satisfied"


@pytest.mark.parametrize("operation", ["change", "create", "delete"])
def test_same_generation_reversal_invalidates_artifact_operation_evidence(operation):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Perform the requested file operation",
            "kind": "artifact", "expected_artifacts": ["entry.py"],
            "artifact_operations": [["entry.py", operation]],
        }],
    }))
    ledger.mutate(1, ["entry.py"], operations={"entry.py": operation})
    assert ledger.status("R1") == "satisfied"

    reverse = "create" if operation == "delete" else "delete"
    ledger.mutate(1, ["entry.py"], operations={"entry.py": reverse})

    assert ledger.status("R1") == "candidate"
    ledger.mutate(2, ["entry.py"], operations={"entry.py": operation})
    assert ledger.status("R1") == "satisfied"


def test_unknown_write_is_not_resolved_by_an_unrelated_edit_or_deserialization():
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView
    from nz_coder.runtime.agent.task_contract import derive_round_artifact_contract

    ledger = VerificationScenario(derive_round_artifact_contract(
        "Create entry.py.", artifact_paths=("entry.py",),
    ))
    ledger.mutate(1, ["entry.py"], operations={"entry.py": "create"})
    ledger.mutate(2, [])
    ledger.mutate(3, ["other.py"])

    assert ledger.status("R1") == "candidate"
    assert RequirementLedgerView.from_dict(ledger.to_dict()).status("R1") == "candidate"


def test_multi_artifact_rename_keeps_both_operation_requirements():
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Rename the module",
            "kind": "artifact", "expected_artifacts": ["old.py", "new.py"],
            "artifact_operations": [["old.py", "delete"], ["new.py", "create"]],
        }],
    }))
    ledger.mutate(1, ["old.py"], operations={"old.py": "delete"})
    assert ledger.status("R1") == "candidate"
    ledger.mutate(2, ["new.py"], operations={"new.py": "create"})
    assert ledger.status("R1") == "satisfied"
    ledger.mutate(3, ["other.py"])
    assert ledger.status("R1") == "satisfied"

    ledger.mutate(3, ["new.py"], operations={"new.py": "delete"})
    assert ledger.status("R1") == "candidate"


@pytest.mark.parametrize("operation,current,recorded,expected", [
    ("change", "file", True, "satisfied"),
    ("create", "file", True, "satisfied"),
    ("delete", "missing", True, "satisfied"),
    ("create", "inside-link", True, "satisfied"),
    ("change", "missing", True, "candidate"),
    ("create", "directory", True, "candidate"),
    ("create", "outside-link", True, "candidate"),
    ("create", "broken-link", True, "candidate"),
    ("delete", "broken-link", True, "candidate"),
    ("delete", "file", True, "candidate"),
    ("delete", "directory", True, "candidate"),
    ("delete", "unreadable", True, "candidate"),
    ("create", "file", False, "pending"),
    ("delete", "missing", False, "pending"),
])
def test_mutating_shell_rechecks_recorded_artifacts_without_inventing_writes(
    tmp_path, monkeypatch, operation, current, recorded, expected,
):
    from pathlib import Path

    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    from nz_coder.state.workdir import scoped_workdir

    target = tmp_path / "entry.py"
    if current in {"file", "unreadable"}:
        target.write_text("value = 2\n")
    elif current == "directory":
        target.mkdir()
    elif current.endswith("link"):
        destination = (
            tmp_path.parent / (tmp_path.name + "-outside.py")
            if current == "outside-link" else tmp_path / "destination.py"
        )
        if current != "broken-link":
            destination.write_text("value = 2\n")
        target.symlink_to(destination)
    if current == "unreadable":
        original_stat, original_lstat = Path.stat, Path.lstat

        def guarded_stat(path, *args, **kwargs):
            if path == target:
                raise PermissionError("controlled inaccessible artifact")
            return original_stat(path, *args, **kwargs)

        def guarded_lstat(path, *args, **kwargs):
            if path == target:
                raise PermissionError("controlled inaccessible artifact")
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", guarded_stat)
        monkeypatch.setattr(Path, "lstat", guarded_lstat)

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Deliver the requested artifact",
            "kind": "artifact", "expected_artifacts": ["entry.py"],
            "artifact_operations": [["entry.py", operation]],
        }],
    }), workspace=tmp_path)
    if recorded:
        ledger.mutate(1, ["entry.py"], operations={"entry.py": operation})
    old_evidence = tuple(ledger.items["R1"].evidence)
    state = ledger.state

    with scoped_workdir(tmp_path):
        ledger.settle_shell('rm -rf .smoke')

    restored = state.requirement_ledger_snapshot()
    assert restored.status("R1") == expected
    assert all(evidence in old_evidence for evidence in restored.items["R1"].evidence)
    if expected == "candidate":
        assert not restored.items["R1"].evidence


@pytest.mark.parametrize("complete_before_shell", [False, True])
def test_shell_invalidated_path_cannot_be_reused_by_another_artifact_write(
    tmp_path, complete_before_shell,
):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    from nz_coder.state.workdir import scoped_workdir

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Create both entry files",
            "kind": "artifact", "expected_artifacts": ["first.py", "second.py"],
            "artifact_operations": [["first.py", "create"], ["second.py", "create"]],
        }],
    }), workspace=tmp_path)
    ledger.mutate(1, ["first.py"], operations={"first.py": "create"})
    if complete_before_shell:
        (tmp_path / "second.py").write_text("value = 2\n")
        ledger.mutate(2, ["second.py"], operations={"second.py": "create"})
    # shell 结束后 first.py 已不存在；首文件的历史证明不能再参与后续满足判定。
    state = ledger.state
    with scoped_workdir(tmp_path):
        ledger.settle_shell('rm first.py')
    observed = state.requirement_ledger_snapshot()
    assert not any(e.path == "first.py" for e in observed.items["R1"].evidence)

    ledger.mutate(4, ["second.py"], operations={"second.py": "create"})
    observed = ledger.ledger

    assert observed.status("R1") == "candidate"


@pytest.mark.parametrize("kind,mode,required", [
    ("behavior", "mixed", []),
    ("artifact", "mixed", []),
    ("artifact", "semantic", []),
    ("artifact", "deterministic", ["semantic_review"]),
    ("docs", "semantic", ["semantic_review"]),
])
def test_shell_file_presence_cannot_restore_stale_behavior_or_review_evidence(
    tmp_path, kind, mode, required,
):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    from nz_coder.state.workdir import scoped_workdir

    path = "README.md" if kind == "docs" else "entry.py"
    (tmp_path / path).write_text("Existing delivered content\n")
    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Deliver verified behavior",
            "kind": kind, "satisfaction_mode": mode,
            "expected_artifacts": [path], "required_evidence": required,
        }],
    }), workspace=tmp_path)
    ledger.mutate(1, [path], operations={path: "create"})
    ledger.execute(1, command="pytest -q tests", passed=True, acceptance=True)
    ledger.review(1, accepted=True, fingerprint="review-1")
    assert ledger.status("R1") == "satisfied"
    state = ledger.state

    with scoped_workdir(tmp_path):
        ledger.settle_shell("printf 'value = 2\\n' > entry.py")

    assert state.requirement_ledger_snapshot().status("R1") == "candidate"


def test_diff_status_cannot_recreate_change_evidence_for_a_missing_artifact(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    from nz_coder.state.workdir import scoped_workdir

    ledger = VerificationScenario(parse_task_contract({
        "requirements": [{
            "id": "R1", "description": "Update entry.py", "kind": "artifact",
            "expected_artifacts": ["entry.py"],
            "artifact_operations": [["entry.py", "change"]],
        }],
    }), workspace=tmp_path)
    ledger.mutate(1, ["entry.py"], operations={"entry.py": "create"})
    state = ledger.state
    with scoped_workdir(tmp_path):
        ledger.settle_shell('rm entry.py')
        assert state.requirement_ledger_snapshot().items["R1"].evidence == ()
        ledger.settle_diff('has_non_empty_diff: true\nChanged files:\n  entry.py\n')

    observed = state.requirement_ledger_snapshot()
    assert observed.status("R1") == "candidate"
    assert observed.items["R1"].evidence == ()
