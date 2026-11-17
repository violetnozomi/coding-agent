"""域状态转换只由可信事实推进，修改与无关成功不能掩盖失败。"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import (
    ArtifactFact, CheckSpec, ExecutionFact, ReviewFact, RunScope, TaskDefinition, TaskScope,
    WorkspaceObservation, WorkspaceVersion,
)
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract


def _domain(root, *, task="task", run="run", lock=None):
    scope = RunScope(TaskScope("session", task, str(root)), run, "main")
    return VerificationDomain(lock=lock or threading.RLock(), scope=scope, version=WorkspaceVersion())


def _spec(root, id="original", command="python check.py"):
    return CheckSpec(id, command_identity(command, str(root)), "custom_assert", required=True, source="user_declared")


def _accept_review(domain):
    domain.observe_review(ReviewFact("review", domain.scope.task, (), domain.snapshot().version,
        "current-materials", True, basis_fingerprint=domain.review_basis()))


def _execute(domain, spec, id, *, passed=True, version=None):
    domain.begin_execution(id, (spec.id,))
    version = version or domain.snapshot().version
    fact = ExecutionFact(id, domain.scope, spec.command, "succeeded" if passed else "failed",
                         version, version, capabilities=(spec.capability,),
                         exit_code=0 if passed else 7)
    domain.observe_execution(fact)
    return fact


def test_fail_mutate_unrelated_pass_keeps_obligation(tmp_path):
    domain = _domain(tmp_path)
    original, other = _spec(tmp_path), _spec(tmp_path, "other", "python other.py")
    domain.register_check(original)
    _execute(domain, original, "failed", passed=False)
    version = WorkspaceVersion(1, 1, 1)
    domain.observe_workspace(WorkspaceObservation(version, paths=("other.py",)))
    domain.register_check(other)
    _execute(domain, other, "unrelated-pass")
    snapshot = domain.snapshot()
    assert not snapshot.ready_for_completion
    assert snapshot.decision("original").status != "passed"
    assert any(item.check_id == "original" and not item.resolved for item in snapshot.obligations)


def test_matching_current_recheck_settles_original_failure(tmp_path):
    domain, spec = _domain(tmp_path), _spec(tmp_path)
    domain.register_check(spec)
    _execute(domain, spec, "failed", passed=False)
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    _execute(domain, spec, "recheck")
    _accept_review(domain)
    snapshot = domain.snapshot()
    assert snapshot.ready_for_completion
    assert snapshot.decision(spec.id).execution_id == "recheck"
    assert snapshot.obligations[0].resolved
    assert snapshot.obligations[0].failure_execution_ids == ("failed",)


def test_empty_selection_does_not_resolve_an_existing_behavior_failure(tmp_path):
    domain = _domain(tmp_path)
    spec = CheckSpec("test", command_identity("python -m unittest discover", str(tmp_path)), "test", required=True, source="user_declared")
    domain.register_check(spec)
    _execute(domain, spec, "real-failure", passed=False)
    version = domain.snapshot().version
    domain.observe_execution(ExecutionFact("empty", domain.scope, spec.command, "failed", version, version,
        capabilities=("test",), exit_code=5, test_count=0, reason="Ran 0 tests\nNO TESTS RAN\n", test_selection_failure="empty_selection"))
    snapshot = domain.snapshot()
    assert snapshot.obligations[0].failure_execution_ids == ("real-failure",)
    assert not snapshot.obligations[0].resolved
    assert not snapshot.ready_for_completion


def test_restore_reclassifies_only_proven_empty_selection_and_retains_fact(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain = _domain(tmp_path)
    spec = CheckSpec("test", command_identity("python -m unittest discover -k missing", str(tmp_path)), "test", required=True, source="user_declared")
    domain.register_check(spec)
    version = domain.snapshot().version
    domain.observe_execution(ExecutionFact("empty", domain.scope, spec.command, "failed", version, version,
        capabilities=("test",), exit_code=5, test_count=0, reason="Ran 0 tests\nNO TESTS RAN\n", test_selection_failure="empty_selection"))
    saved = domain.checkpoint()
    # 原格式曾把明确的空选择也记录成必须精确复测的失败义务。
    saved["obligations"] = [{"check_id": "test", "failure_execution_ids": ["empty"], "resolved": False, "resolved_by": ""}]
    saved["failure_revisions"] = {"test": saved["revision"]}
    saved["decisions"][0]["status"] = "failed"
    restored = restore_domain(saved, lock=threading.RLock(), scope=domain.scope, version=version)
    snapshot = restored.snapshot()
    assert snapshot.obligations == ()
    assert snapshot.executions[0].execution_id == "empty"
    assert snapshot.executions[0].outcome == "failed"
    assert snapshot.decision("test").status == "pending"
    assert not snapshot.ready_for_completion


def test_optional_empty_selection_does_not_create_a_new_required_test(tmp_path):
    domain = _domain(tmp_path)
    empty = CheckSpec("empty", command_identity("python -m unittest discover -k missing", str(tmp_path)), "test")
    domain.register_check(empty)
    version = domain.snapshot().version
    domain.observe_execution(ExecutionFact("empty-run", domain.scope, empty.command, "failed", version, version,
        capabilities=("test",), exit_code=5, test_count=0, reason="Ran 0 tests\nNO TESTS RAN\n", test_selection_failure="empty_selection"))
    assert domain.snapshot().execution_ready
    assert domain.snapshot().obligations == ()
    actual = CheckSpec("actual", command_identity("python -m unittest discover", str(tmp_path)), "test")
    domain.register_check(actual)
    domain.observe_execution(ExecutionFact("actual-run", domain.scope, actual.command, "succeeded", version, version,
        capabilities=("test",), exit_code=0, test_count=2))
    assert domain.snapshot().ready_for_completion
    assert domain.snapshot().tests_passed is None


def test_fact_replay_is_idempotent_but_conflict_never_passes(tmp_path):
    domain, spec = _domain(tmp_path), _spec(tmp_path)
    domain.register_check(spec)
    failed = _execute(domain, spec, "execution", passed=False)
    revision = domain.snapshot().revision
    domain.observe_execution(failed)
    assert domain.snapshot().revision == revision
    domain.observe_execution(replace(failed, outcome="succeeded", exit_code=0))
    assert not domain.snapshot().ready_for_completion
    assert domain.snapshot().integrity_errors


def test_followup_authority_revision_preserves_lineage_and_failed_check(tmp_path):
    domain, spec = _domain(tmp_path), _spec(tmp_path)
    domain.declare_task(TaskDefinition(domain.scope.task, authority_revision=1))
    domain.register_check(spec)
    _execute(domain, spec, "failed", passed=False)
    domain.declare_task(TaskDefinition(domain.scope.task, authority_revision=2))
    snapshot = domain.snapshot()
    assert snapshot.scope.task.task_lineage_id == "task"
    assert snapshot.authority_revision == 2
    assert snapshot.decision(spec.id).status == "failed"
    assert not snapshot.ready_for_completion


def test_child_fact_cannot_settle_parent_check(tmp_path):
    domain = _domain(tmp_path)
    spec = _spec(tmp_path)
    child = _domain(tmp_path, task="child", run="child")
    domain.register_check(spec)
    child.register_check(spec)
    fact = _execute(child, spec, "child-pass")
    with pytest.raises(ValueError, match="scope"):
        domain.observe_execution(fact)
    assert not domain.snapshot().ready_for_completion


def test_snapshot_cannot_mutate_owner(tmp_path):
    domain, spec = _domain(tmp_path), _spec(tmp_path)
    domain.register_check(spec)
    _execute(domain, spec, "passed")
    public = domain.snapshot().to_dict()
    public["verification_pipeline"]["stages"][0]["commands"][0]["status"] = "failed"
    assert domain.snapshot().ready_for_completion
    assert domain.snapshot().decision(spec.id).status == "passed"


def test_overlapping_old_success_cannot_settle_new_failure(tmp_path):
    domain, spec = _domain(tmp_path), _spec(tmp_path)
    domain.register_check(spec)
    domain.begin_execution("old-success", (spec.id,))
    _execute(domain, spec, "new-failure", passed=False)
    version = domain.snapshot().version
    domain.observe_execution(ExecutionFact("old-success", domain.scope, spec.command, "succeeded",
                                         version, version, capabilities=(spec.capability,), exit_code=0))
    assert not domain.snapshot().ready_for_completion
    _execute(domain, spec, "fresh-success")
    _accept_review(domain)
    assert domain.snapshot().ready_for_completion


def test_read_only_task_is_complete_without_claiming_tests_passed(tmp_path):
    snapshot = _domain(tmp_path).snapshot()
    assert snapshot.ready_for_completion
    assert snapshot.tests_passed is None


def test_unsettled_write_blocks_completion_and_sealed_result_is_immutable(tmp_path):
    domain = _domain(tmp_path)
    domain.begin_execution("write", (), mutates=True)
    snapshot = domain.snapshot()
    assert not snapshot.ready_for_completion
    with pytest.raises(ValueError, match="unconfirmed"):
        domain.seal(expected_revision=snapshot.revision, terminal_reason="completed")
    sealed = domain.seal(expected_revision=snapshot.revision, terminal_reason="cancelled")
    assert domain.snapshot() is sealed
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    assert domain.snapshot() is sealed


def test_artifact_presence_without_requested_change_does_not_satisfy_requirement(tmp_path):
    domain = _domain(tmp_path)
    requirement = Requirement("R1", "change app.py", "artifact", expected_artifacts=("app.py",),
                              satisfaction_mode="deterministic", artifact_operations=(("app.py", "change"),))
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(requirement,))))
    domain.observe_artifact(ArtifactFact(domain.scope.task, "app.py", "read", 0, "original"))
    assert not domain.snapshot().ready_for_completion
    domain.observe_artifact(ArtifactFact(domain.scope.task, "app.py", "change", 0, "changed"))
    assert domain.snapshot().ready_for_completion
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    assert not domain.snapshot().ready_for_completion


def test_semantic_review_requires_successful_call_and_current_materials(tmp_path):
    domain = _domain(tmp_path)
    requirement = Requirement("R1", "preserve compatibility", "compatibility", satisfaction_mode="semantic",
                              required_evidence=("semantic_review",))
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(requirement,))))
    version = WorkspaceVersion()
    failed_call = ReviewFact("review", domain.scope.task, ("R1",), version, "materials", True, executed=False, basis_fingerprint=domain.review_basis())
    domain.observe_review(failed_call)
    assert not domain.snapshot().ready_for_completion
    domain.observe_review(replace(failed_call, request_id="review-2", executed=True))
    assert domain.snapshot().ready_for_completion
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    assert not domain.snapshot().ready_for_completion


def test_planner_parser_produces_domain_contract_without_owning_progress(tmp_path):
    from nz_coder.runtime.agent import task_contract

    parser = getattr(task_contract, "parse_task_contract", None)
    assert callable(parser), "task parsing must be separate from domain value types"
    value = parser({"objective": "change app.py", "requirements": [
        {"id": "R1", "description": "change app.py", "kind": "artifact", "expected_artifacts": ["app.py"]},
    ]}, workspace=tmp_path)
    assert isinstance(value, TaskContract)
    assert isinstance(value.requirements[0], Requirement)
    assert isinstance(value.to_dict()["requirements"], list)


def test_requirement_view_is_read_only_and_marks_semantic_pending(tmp_path):
    from dataclasses import FrozenInstanceError
    from nz_coder.runtime.verification.domain.requirements import RequirementLedgerView

    domain = _domain(tmp_path)
    requirement = Requirement("R1", "preserve compatibility", "compatibility", required_evidence=("semantic_review",))
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(requirement,))))
    spec = replace(_spec(tmp_path), source="user_declared", requirement_ids=("R1",))
    domain.register_check(spec)
    _execute(domain, spec, "test-pass")
    view = RequirementLedgerView.from_snapshot(domain.snapshot())
    assert view.semantic_review_pending_only(acceptance_required=True, generation=0)
    with pytest.raises(FrozenInstanceError):
        view.items["R1"].status = "satisfied"
    with pytest.raises(TypeError):
        view.items["R2"] = view.items["R1"]


def test_subcheck_keeps_original_group_start_revision(tmp_path):
    domain, spec = _domain(tmp_path), _spec(tmp_path)
    domain.register_check(spec)
    started = domain.begin_execution("group", (spec.id,))
    assert isinstance(started, int) and started > 0
    _execute(domain, spec, "later-failure", passed=False)
    version = domain.snapshot().version
    domain.observe_execution(ExecutionFact("subcheck", domain.scope, spec.command, "succeeded", version, version,
        capabilities=(spec.capability,), exit_code=0, started_revision=started))
    domain.end_execution("group")
    assert not domain.snapshot().ready_for_completion
    _execute(domain, spec, "fresh-check")
    _accept_review(domain)
    assert domain.snapshot().ready_for_completion


def test_unbound_artifact_requirement_is_not_satisfied_without_evidence(tmp_path):
    domain = _domain(tmp_path)
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(
        Requirement("R1", "create an entry point", "artifact", satisfaction_mode="deterministic"),))))
    assert not domain.snapshot().ready_for_completion


def test_review_for_old_materials_cannot_certify_documentation_change(tmp_path):
    domain = _domain(tmp_path)
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(
        Requirement("R1", "review behavior", "compatibility", satisfaction_mode="semantic", required_evidence=("semantic_review",)),))))
    basis = domain.review_basis()
    review = ReviewFact("request", domain.scope.task, ("R1",), WorkspaceVersion(), "materials", True,
                        basis_fingerprint=basis)
    domain.observe_review(review)
    assert domain.snapshot().ready_for_completion
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 0, 0), paths=("README.md",)))
    assert not domain.snapshot().ready_for_completion


def test_requirement_revision_does_not_erase_shared_execution_or_unchanged_requirement(tmp_path):
    domain = _domain(tmp_path)
    first = Requirement("R1", "change app", "behavior", ("app.py",))
    second = Requirement("R2", "retain API", "behavior", ("app.py",))
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(first, second))))
    domain.observe_artifact(ArtifactFact(domain.scope.task, "app.py", "change", 0, "contents"))
    spec = replace(_spec(tmp_path), requirement_ids=("R1", "R2"))
    domain.register_check(spec)
    _execute(domain, spec, "pass")
    domain.observe_review(ReviewFact("review", domain.scope.task, ("R1", "R2"), domain.snapshot().version,
        "materials", True, basis_fingerprint=domain.review_basis()))
    assert all(item.status == "satisfied" for item in domain.snapshot().requirements)
    domain.declare_task(TaskDefinition(domain.scope.task, authority_revision=1,
        contract=TaskContract(requirements=(replace(first, description="new behavior"), second))))
    snapshot = domain.snapshot()
    assert snapshot.decision(spec.id).status == "passed"
    assert [item.status for item in snapshot.requirements] == ["candidate", "candidate"]
    _execute(domain, spec, "fresh")
    domain.observe_review(ReviewFact("fresh-review", domain.scope.task, ("R1", "R2"), domain.snapshot().version,
        "fresh-materials", True, basis_fingerprint=domain.review_basis()))
    assert domain.snapshot().ready_for_completion


def test_new_requirement_cannot_retroactively_inherit_a_shared_old_pass(tmp_path):
    domain = _domain(tmp_path)
    first = Requirement("R1", "change app", "behavior", ("app.py",))
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(first,))))
    domain.observe_artifact(ArtifactFact(domain.scope.task, "app.py", "change", 0, "contents"))
    spec = replace(_spec(tmp_path), requirement_ids=("R1",))
    domain.register_check(spec)
    _execute(domain, spec, "pass")
    second = Requirement("R2", "new request", "behavior", ("app.py",))
    domain.declare_task(TaskDefinition(domain.scope.task, authority_revision=1,
        contract=TaskContract(requirements=(first, second))))
    domain.register_check(replace(spec, requirement_ids=("R1", "R2")))
    assert [item.status for item in domain.snapshot().requirements] == ["candidate", "candidate"]
    assert not any(e.type == "command_passed" for e in domain.snapshot().requirements[1].evidence)


def test_another_capability_phase_cannot_downgrade_valid_syntax(tmp_path):
    domain = _domain(tmp_path)
    syntax = replace(_spec(tmp_path), id="syntax", capability="syntax")
    symbol = replace(syntax, id="symbol", capability="symbol")
    domain.register_check(syntax)
    domain.register_check(symbol)
    _execute(domain, syntax, "syntax-pass")
    _execute(domain, symbol, "missing-symbol", passed=False)
    assert domain.snapshot().decision("syntax").status == "passed"
    assert domain.snapshot().decision("symbol").status == "failed"


def test_revised_deterministic_artifact_requires_new_origin_not_rescan(tmp_path):
    domain = _domain(tmp_path)
    original = Requirement('R1', 'Create README', 'docs', ('README.md',),
        satisfaction_mode='deterministic', artifact_operations=(('README.md', 'create'),))
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(original,))))
    old = ArtifactFact(domain.scope.task, 'README.md', 'create', 0, 'old contents')
    domain.observe_artifact(old)
    assert domain.snapshot().ready_for_completion
    revised = replace(original, description='Update README with new API and migration instructions',
                      artifact_operations=(('README.md', 'change'),))
    domain.declare_task(TaskDefinition(domain.scope.task, authority_revision=1,
        contract=TaskContract(requirements=(revised,))))
    assert not domain.snapshot().ready_for_completion
    domain.observe_artifact(old)
    assert not domain.snapshot().ready_for_completion
    domain.observe_artifact(replace(old, complete=False))
    domain.observe_artifact(old)
    assert not domain.snapshot().ready_for_completion
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 0, 0), paths=('README.md',)))
    domain.observe_artifact(replace(old, generation=1, fingerprint='new API and migration instructions'))
    assert domain.snapshot().ready_for_completion
