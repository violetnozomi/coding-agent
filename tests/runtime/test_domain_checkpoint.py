"""只恢复当前域格式，缺失检查点不能单独恢复旧 passed。"""
from __future__ import annotations

import json
import threading

import pytest

from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError, restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _scope(root, run="initial"):
    return RunScope(TaskScope("session", "lineage", str(root)), run, "main")


def test_missing_checkpoint_cannot_restore_passed(tmp_path):
    restored = restore_domain(None, lock=threading.RLock(), scope=_scope(tmp_path), version=WorkspaceVersion())
    snapshot = restored.snapshot()
    assert snapshot.rebuild_required and not snapshot.ready_for_completion
    assert snapshot.tests_passed is None


@pytest.mark.parametrize("checkpoint", [{}, {"schema": "old"},
    {"facts": {"_needed": False}, "verification_contract": {"passed": True}}])
def test_invalid_checkpoint_is_rejected(tmp_path, checkpoint):
    with pytest.raises(CheckpointRestoreError):
        restore_domain(checkpoint, lock=threading.RLock(), scope=_scope(tmp_path), version=WorkspaceVersion())


def test_valid_checkpoint_preserves_failed_obligation_and_original_execution(tmp_path):
    version, scope = WorkspaceVersion(), _scope(tmp_path)
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    spec = CheckSpec("script", command_identity("python check.py", str(tmp_path)), "custom_assert", required=True, source="user_declared")
    domain.register_check(spec)
    domain.begin_execution("failed", (spec.id,))
    domain.observe_execution(ExecutionFact("failed", scope, spec.command, "failed", version, version,
                                         capabilities=("custom_assert",), exit_code=7))
    saved = json.loads(json.dumps(domain.checkpoint()))
    restored = restore_domain(saved, lock=threading.RLock(), scope=_scope(tmp_path, "resumed"), version=version)
    snapshot = restored.snapshot()
    assert not snapshot.rebuild_required and not snapshot.ready_for_completion
    assert snapshot.obligations[0].failure_execution_ids == ("failed",)
    assert not snapshot.obligations[0].resolved
    restored.begin_execution("retry", (spec.id,))
    restored.observe_execution(ExecutionFact("retry", restored.scope, spec.command, "succeeded", version, version,
                                            capabilities=("custom_assert",), exit_code=0))
    assert restored.snapshot().execution_ready
    assert restored.snapshot().review_pending


def test_checkpoint_positive_requires_new_activation_environment_confirmation(tmp_path):
    scope, version = _scope(tmp_path), WorkspaceVersion()
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    spec = CheckSpec("test", command_identity("pytest tests/test_a.py", str(tmp_path)), "test", required=True, source="user_declared")
    domain.register_check(spec)
    domain.begin_execution("passed", (spec.id,))
    domain.observe_execution(ExecutionFact("passed", scope, spec.command, "succeeded", version, version,
                                         capabilities=("test",), exit_code=0, test_count=1))
    assert domain.snapshot().ready_for_completion
    restored = restore_domain(domain.checkpoint(), lock=threading.RLock(), scope=_scope(tmp_path, "resumed"), version=version)
    assert restored.snapshot().decision("test").status == "pending"
    assert not restored.snapshot().ready_for_completion


def test_checkpoint_wrong_lineage_or_workspace_enters_rebuild(tmp_path):
    domain = VerificationDomain(lock=threading.RLock(), scope=_scope(tmp_path), version=WorkspaceVersion())
    wrong_scope = RunScope(TaskScope("session", "different", str(tmp_path)), "resumed", "main")
    with pytest.raises(CheckpointRestoreError):
        restore_domain(domain.checkpoint(), lock=threading.RLock(), scope=wrong_scope, version=WorkspaceVersion())


def test_missing_checkpoint_requires_fresh_explicit_acceptance_to_rebuild(tmp_path):

    domain = restore_domain(None, lock=threading.RLock(), scope=_scope(tmp_path), version=WorkspaceVersion())
    spec = CheckSpec("acceptance", command_identity("pytest tests/test_a.py", str(tmp_path)), "test", source="user_declared")
    domain.register_check(spec)
    assert not domain.rebuild_from_current_acceptance()
    domain.begin_execution("fresh", (spec.id,))
    domain.observe_execution(ExecutionFact("fresh", domain.scope, spec.command, "succeeded", WorkspaceVersion(), WorkspaceVersion(),
        capabilities=("test",), exit_code=0, test_count=1))
    assert domain.snapshot().rebuild_required
    assert domain.rebuild_from_current_acceptance()
    assert domain.snapshot().ready_for_completion
    assert not domain.snapshot().rebuild_required


def test_unrelated_fresh_success_cannot_clear_unknown_old_obligations(tmp_path):
    domain = restore_domain(None, lock=threading.RLock(), scope=_scope(tmp_path), version=WorkspaceVersion())
    spec = CheckSpec("other", command_identity("pytest tests/test_a.py", str(tmp_path)), "test")
    domain.register_check(spec)
    domain.begin_execution("fresh", (spec.id,))
    domain.observe_execution(ExecutionFact("fresh", domain.scope, spec.command, "succeeded", WorkspaceVersion(), WorkspaceVersion(),
        capabilities=("test",), exit_code=0, test_count=1))
    assert not domain.rebuild_from_current_acceptance()
    assert domain.snapshot().rebuild_required


def test_checkpoint_keeps_artifact_provenance_but_requires_current_disk_check(tmp_path):
    import hashlib
    from nz_coder.runtime.verification.domain.models import ArtifactFact, TaskDefinition
    from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract
    from nz_coder.runtime.adapters.verification_workspace import collect_artifact_facts

    (tmp_path / "entry.py").write_text("value = 1\n")
    version = WorkspaceVersion()
    domain = VerificationDomain(lock=threading.RLock(), scope=_scope(tmp_path), version=version)
    domain.declare_task(TaskDefinition(domain.scope.task, contract=TaskContract(requirements=(
        Requirement("R1", "create entry", "artifact", ("entry.py",), "deterministic",
                    artifact_operations=(("entry.py", "create"),)),))))
    domain.observe_artifact(ArtifactFact(domain.scope.task, "entry.py", "create", 0,
        hashlib.sha256((tmp_path / "entry.py").read_bytes()).hexdigest()))
    assert domain.snapshot().ready_for_completion
    restored = restore_domain(domain.checkpoint(), lock=threading.RLock(), scope=_scope(tmp_path, "resumed"), version=version)
    assert len(restored.snapshot().artifacts) == 1
    assert restored.snapshot().artifacts[0].operation == "create"
    assert not restored.snapshot().ready_for_completion
    for fact in collect_artifact_facts(restored.snapshot(), tmp_path, {}, 0):
        restored.observe_artifact(fact)
    assert restored.snapshot().ready_for_completion


def test_restoration_uses_current_strict_policy_even_if_checkpoint_was_not_strict(tmp_path):
    domain = VerificationDomain(lock=threading.RLock(), scope=_scope(tmp_path), version=WorkspaceVersion())
    restored = restore_domain(domain.checkpoint(), lock=threading.RLock(), scope=_scope(tmp_path, "resume"),
        version=WorkspaceVersion(1, 1, 1), require_targeted=True)
    assert restored.requires_targeted
    assert restored.snapshot().targeted_pending
