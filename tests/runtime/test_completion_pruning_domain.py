"""调试负证据触发实际评审，不再生成逐条精确复跑的硬义务。"""
from __future__ import annotations

from dataclasses import replace
import threading

from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import (
    CheckSpec, ExecutionFact, ReviewFact, RunScope, TaskDefinition, TaskScope,
    WorkspaceObservation, WorkspaceVersion,
)
from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract
from nz_coder.runtime.verification.domain.service import VerificationDomain


def domain(root):
    return VerificationDomain(lock=threading.RLock(), scope=RunScope(
        TaskScope("session", "task", str(root)), "run", "main"), version=WorkspaceVersion())


def check(owner, name="local", *, required=False, capability="custom_assert"):
    spec = CheckSpec(name, command_identity(f"python {name}.py", owner.scope.task.workspace),
                     capability, required=required, source="user_declared" if required else "model_execution")
    owner.register_check(spec)
    return spec


def execute(owner, spec, name, *, passed=True, count=None):
    version = owner.snapshot().version
    owner.begin_execution(name, (spec.id,))
    owner.observe_execution(ExecutionFact(name, owner.scope, spec.command,
        "succeeded" if passed else "failed", version, version,
        capabilities=(spec.capability,), exit_code=0 if passed else 1, test_count=count))


def accept(owner):
    owner.observe_review(ReviewFact("review", owner.scope.task, (), owner.snapshot().version,
                                    "materials", True, basis_fingerprint=owner.review_basis()))


def test_optional_failure_allows_review_after_current_acceptance_without_rerunning_old_command(tmp_path):
    owner = domain(tmp_path)
    local = check(owner)
    execute(owner, local, "old-failure", passed=False)
    owner.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    full = check(owner, "full", required=True)
    execute(owner, full, "full-pass")
    snapshot = owner.snapshot()
    assert snapshot.execution_ready
    assert not snapshot.ready_for_completion
    assert snapshot.review_pending
    assert snapshot.obligations == ()
    accept(owner)
    assert owner.snapshot().ready_for_completion
    assert owner.snapshot().executions[0].outcome == "failed"


def test_current_hard_failure_cannot_be_cleared_by_review_or_another_pass(tmp_path):
    owner = domain(tmp_path)
    hard, other = check(owner, required=True), check(owner, "other")
    execute(owner, hard, "failed", passed=False)
    execute(owner, other, "passed")
    accept(owner)
    assert not owner.snapshot().execution_ready
    assert not owner.snapshot().ready_for_completion
    assert owner.snapshot().obligations[0].failure_execution_ids == ("failed",)


def test_plain_source_change_without_check_does_not_create_implicit_test_requirement(tmp_path):
    owner = domain(tmp_path)
    owner.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    snapshot = owner.snapshot()
    assert snapshot.execution_ready
    assert snapshot.tests_passed is None
    assert not snapshot.targeted_pending


def test_optional_pass_is_not_exposed_as_all_required_tests_passed(tmp_path):
    owner = domain(tmp_path)
    spec = check(owner, capability="test")
    execute(owner, spec, "passed", count=3)
    assert owner.snapshot().tests_passed is None
    assert owner.snapshot().decision(spec.id).status == "passed"


def test_replacing_user_authority_can_remove_requirement_and_failed_hard_check(tmp_path):
    owner = domain(tmp_path)
    owner.declare_task(TaskDefinition(owner.scope.task, authority_revision=1,
        contract=TaskContract(requirements=(Requirement("R1", "create missing", "artifact", ("missing.py",)),))))
    spec = check(owner, required=True)
    execute(owner, spec, "failed", passed=False)
    owner.declare_task(TaskDefinition(owner.scope.task, authority_revision=2, contract=TaskContract()))
    owner.replace_user_checks(())
    assert owner.snapshot().requirements == ()
    assert owner.snapshot().obligations == ()
    assert owner.snapshot().execution_ready
    assert owner.snapshot().executions[0].execution_id == "failed"


def test_optional_failure_survives_save_restore_after_later_pass(tmp_path):
    owner = domain(tmp_path)
    spec = check(owner)
    execute(owner, spec, "old-failure", passed=False)
    execute(owner, spec, "later-pass")
    saved = owner.checkpoint()
    for _ in range(2):
        owner = restore_domain(saved, lock=threading.RLock(), scope=owner.scope, version=WorkspaceVersion())
        assert {f.execution_id for f in owner.snapshot().executions} == {"old-failure", "later-pass"}
        assert owner.snapshot().obligations == ()
        saved = owner.checkpoint()


def test_new_failure_same_version_invalidates_actual_review_acceptance(tmp_path):
    owner = domain(tmp_path)
    spec = check(owner)
    execute(owner, spec, "first-failure", passed=False)
    accept(owner)
    assert owner.snapshot().ready_for_completion
    execute(owner, spec, "new-failure", passed=False)
    assert owner.snapshot().execution_ready
    assert owner.snapshot().review_pending
    assert not owner.snapshot().ready_for_completion


def test_old_failure_and_compile_pass_cannot_enable_trivial_review_skip(tmp_path):
    owner = domain(tmp_path)
    spec = check(owner)
    execute(owner, spec, "failed", passed=False)
    owner.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    compile_check = check(owner, "compile", capability="syntax")
    execute(owner, compile_check, "compiled")
    assert owner.snapshot().review_pending


def test_model_planner_source_cannot_create_required_check_in_ordinary_mode(tmp_path):
    owner = domain(tmp_path)
    spec = replace(check(owner), id="planned", source="planner", required=True)
    owner.register_check(spec)
    assert not next(s for s in owner.snapshot().specs if s.id == "planned").required


def test_one_pass_cannot_satisfy_requirement_for_two_explicit_commands(tmp_path):
    owner = domain(tmp_path)
    owner.declare_task(TaskDefinition(owner.scope.task, contract=TaskContract(requirements=(
        Requirement("V", "Pass both commands", "verification", satisfaction_mode="deterministic"),))))
    first, second = check(owner, "first", required=True), check(owner, "second", required=True)
    owner.register_check(replace(first, requirement_ids=("V",)))
    owner.register_check(replace(second, requirement_ids=("V",)))
    execute(owner, first, "only-first")
    assert owner.snapshot().requirements[0].status != "satisfied"
    assert owner.snapshot().assess_completion("completed").status == "blocked"
