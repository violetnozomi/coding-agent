"""语义完成不以测试名称匹配为前提，明确执行与恢复资源边界仍独立成立。"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from test_completion_pruning_domain import domain
from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError, restore_domain
from nz_coder.runtime.verification.domain.models import ArtifactFact, ReviewFact, TaskDefinition
from nz_coder.runtime.verification.domain.requirements import Requirement, RequirementLedgerView, TaskContract


def define(owner, item):
    owner.declare_task(TaskDefinition(owner.scope.task, contract=TaskContract(requirements=(item,))))


def review(owner, item):
    owner.observe_review(ReviewFact("review", owner.scope.task, (item.id,), owner.snapshot().version,
        "actual-materials", True, basis_fingerprint=owner.review_basis(),
        candidate_fingerprint=owner.snapshot().candidate_fingerprint))


def test_semantic_requirement_without_command_is_ready_for_review(tmp_path):
    owner = domain(tmp_path)
    item = Requirement("R1", "Explain the design from source", "behavior", satisfaction_mode="semantic")
    define(owner, item)
    owner.set_completion_candidate("explanation")
    assert owner.snapshot().assess_completion("completed").review_required
    assert RequirementLedgerView.from_snapshot(owner.snapshot()).semantic_review_pending_only()
    review(owner, item)
    assert owner.snapshot().requirements[0].status == "satisfied"
    assert owner.snapshot().assess_completion("completed").status == "completed"


def test_mixed_document_only_needs_artifact_then_actual_review(tmp_path):
    owner = domain(tmp_path)
    item = Requirement("R1", "Write useful instructions", "docs", ("README.md",), "mixed")
    define(owner, item)
    owner.set_completion_candidate("documentation")
    assert not owner.snapshot().assess_completion("completed").review_required
    owner.observe_artifact(ArtifactFact(owner.scope.task, "README.md", "create", 0, "digest"))
    assert owner.snapshot().assess_completion("completed").review_required
    review(owner, item)
    assert owner.snapshot().requirements[0].status == "satisfied"


def test_explicit_semantic_evidence_cannot_replace_deterministic_artifact(tmp_path):
    owner = domain(tmp_path)
    item = Requirement("R1", "Create and review entry", "artifact", ("entry.py",), "deterministic",
                       required_evidence=("semantic_review",))
    define(owner, item)
    owner.set_completion_candidate("candidate")
    review(owner, item)
    assert owner.snapshot().assess_completion("completed").status == "blocked"
    owner.observe_artifact(ArtifactFact(owner.scope.task, "entry.py", "create", 0, "digest"))
    assert owner.snapshot().assess_completion("completed").review_required
    review(owner, item)
    assert owner.snapshot().assess_completion("completed").status == "completed"


def test_checkpoint_with_unknown_old_running_resources_is_rejected(tmp_path):
    owner = domain(tmp_path)
    owner.begin_execution("old-process", (), mutates=True)
    saved = owner.checkpoint()
    assert saved["rebuild_required"]
    with pytest.raises(CheckpointRestoreError):
        restore_domain(saved, lock=threading.RLock(), scope=replace(owner.scope, interaction_run_id="resumed"),
                       version=owner.snapshot().version)


def test_quiescent_document_checkpoint_needs_new_review_not_test_command(tmp_path):
    owner = domain(tmp_path)
    item = Requirement("R1", "Write the usage", "docs", ("README.md",), "mixed")
    define(owner, item)
    fact = ArtifactFact(owner.scope.task, "README.md", "create", 0, "digest")
    owner.observe_artifact(fact)
    saved = owner.checkpoint()
    restored = restore_domain(saved, lock=threading.RLock(),
        scope=replace(owner.scope, interaction_run_id="resumed"), version=owner.snapshot().version)
    restored.observe_artifact(fact)
    restored.set_completion_candidate("new-summary")
    assert restored.snapshot().assess_completion("completed").review_required
    review(restored, item)
    assert restored.snapshot().assess_completion("completed").status == "completed"


def test_acceptance_command_does_not_claim_all_behavior_requirements(tmp_path):
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(workspace=tmp_path)
    scenario.state.set_task_contract(TaskContract(requirements=(
        Requirement("R1", "The feature is correct", "behavior", satisfaction_mode="semantic"),
        Requirement("R2", "Run the command", "verification", satisfaction_mode="deterministic"),
    ), acceptance_commands=("python check.py",)))
    scenario.runtime.synchronize_task()
    specs = scenario.domain.snapshot().specs
    assert specs
    assert all(spec.requirement_ids == ("R2",) for spec in specs if spec.source == "user_declared")


def test_overall_accept_without_requirement_scope_does_not_certify_semantic_item(tmp_path):
    owner = domain(tmp_path)
    define(owner, Requirement("R1", "Explain actual behavior", "behavior", satisfaction_mode="semantic"))
    owner.set_completion_candidate("candidate")
    owner.observe_review(ReviewFact("unrelated", owner.scope.task, (), owner.snapshot().version,
        "materials", True, basis_fingerprint=owner.review_basis(), candidate_fingerprint="candidate"))
    assert owner.snapshot().requirements[0].status != "satisfied"
    assert owner.snapshot().assess_completion("completed").review_required


def test_semantic_dependency_chain_can_enter_one_actual_review(tmp_path):
    owner = domain(tmp_path)
    first = Requirement("R1", "Explain the storage model", "behavior", satisfaction_mode="semantic")
    second = Requirement("R2", "Explain recovery based on that model", "behavior",
                         satisfaction_mode="semantic", depends_on=("R1",))
    owner.declare_task(TaskDefinition(owner.scope.task, contract=TaskContract(requirements=(first, second))))
    owner.set_completion_candidate("explanation")
    assessment = owner.snapshot().assess_completion("completed")
    assert assessment.review_required and not assessment.blocking_ids
    owner.observe_review(ReviewFact("chain-review", owner.scope.task, ("R1", "R2"), owner.snapshot().version,
        "materials", True, basis_fingerprint=owner.review_basis(), candidate_fingerprint="explanation"))
    assert all(item.status == "satisfied" for item in owner.snapshot().requirements)


def test_verification_requirement_defaults_to_deterministic_command_evidence():
    from nz_coder.runtime.agent.task_contract import parse_task_contract
    contract = parse_task_contract({"requirements": [{"id": "V", "kind": "verification", "description": "Run checks"}]})
    assert contract.requirements[0].satisfaction_mode == "deterministic"



def test_new_explicit_write_cannot_reuse_an_older_modification(tmp_path):
    from nz_coder.runtime.verification.domain.models import ArtifactFact, ReviewFact, TaskDefinition
    from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract
    owner = domain(tmp_path)
    owner.observe_artifact(ArtifactFact(owner.scope.task, "README.md", "change", 0, "old"))
    owner.declare_task(TaskDefinition(owner.scope.task, contract=TaskContract(requirements=(
        Requirement("new", "Revise README", "docs", ("README.md",), "mixed",
                    artifact_operations=(("README.md", "change"),)),))))
    snapshot = owner.snapshot()
    owner.observe_review(ReviewFact("request", owner.scope.task, ("new",), snapshot.version,
        "materials", True, basis_fingerprint=owner.review_basis()))
    assert not owner.snapshot().requirements[0].deterministic_ready
    assert owner.snapshot().assess_completion("completed").status == "blocked"

def test_explicit_review_remains_blocking_while_workspace_is_unsettled(tmp_path):
    from nz_coder.runtime.verification.domain.models import WorkspaceObservation, WorkspaceVersion
    owner = domain(tmp_path)
    define(owner, Requirement('R1', 'Create and review README', 'docs', ('README.md',),
                             'deterministic', required_evidence=('semantic_review',)))
    owner.observe_artifact(ArtifactFact(owner.scope.task, 'README.md', 'create', 0, 'digest'))
    owner.observe_workspace(WorkspaceObservation(WorkspaceVersion(scope_complete=False)))
    assessment = owner.snapshot().assess_completion('completed')
    assert assessment.status == 'blocked'
    assert not assessment.review_required  # 资源未结清时不启动评审。
