"""执行失败保留事实；只有真实检查或显式声明承担验收责任。"""
from __future__ import annotations

from dataclasses import asdict
import shlex
import sys
import threading

import pytest

from nz_coder.runtime.adapters.verification_plans import check_spec
from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import ExecutionFact, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain
from tests.verification_scenarios import VerificationScenario


def _project(root, *, require_targeted=False):
    (root / "demo.py").write_text("raise SystemExit(7)\n")
    (root / "tests").mkdir()
    (root / "tests/__init__.py").touch()
    (root / "tests/test_sample.py").write_text(
        "import unittest\nclass Sample(unittest.TestCase):\n"
        " def test_example(self): self.assertTrue(False)\n")
    return VerificationScenario(workspace=root, plan={"stages": []}, require_targeted=require_targeted)


def _declare(vm, command, source):
    from nz_coder.runtime.agent.task_policy import test_command_targets
    from nz_coder.runtime.verification.domain.requirements import TaskContract

    if source == "user_declared":
        # 真实宿主会按当前用户合同重新同步，手工贴来源标签不是验收权。
        vm.state.set_task_contract(TaskContract(acceptance_commands=(command,)))
        vm.runtime.synchronize_task()
        return next(spec for spec in vm.domain.snapshot().specs
                    if spec.source == source and spec.command.original == command)
    spec = check_spec(command, str(vm.root), capability="test" if "unittest" in command else "custom_assert",
                      source=source, required=True, targets=tuple(test_command_targets(command)))
    vm.domain.register_check(spec)
    return spec


def test_failed_application_demo_keeps_fact_without_acceptance_obligation(tmp_path):
    vm = _project(tmp_path)
    result = vm.settle_shell(f"{shlex.quote(sys.executable)} demo.py")
    snapshot = vm.domain.snapshot()
    assert result.command_failed
    assert len(snapshot.executions) == 1
    assert snapshot.executions[0].exit_code == 7
    assert snapshot.executions[0].capabilities == ("custom_assert",)
    assert snapshot.specs == ()
    assert snapshot.obligations == ()
    assert vm.runtime.status()["verification_needed"] is False


def test_failure_before_an_unexecuted_test_keeps_fact_without_guessing_responsibility(tmp_path):
    vm = _project(tmp_path)
    command = "find missing-directory && python -m unittest tests.test_sample -q"
    vm.settle_shell(command)
    snapshot = vm.domain.snapshot()
    assert snapshot.executions[0].outcome == "failed"
    assert snapshot.executions[0].command.original == command
    assert snapshot.executions[0].command.opaque
    assert snapshot.specs == ()
    assert snapshot.obligations == ()


@pytest.mark.parametrize("source", ["user_declared", "planner"])
def test_explicit_application_check_failure_keeps_declared_authority(tmp_path, source):
    vm = _project(tmp_path, require_targeted=source == "planner")
    command = f"{shlex.quote(sys.executable)} demo.py"
    spec = _declare(vm, command, source)
    vm.settle_shell(command)
    snapshot = vm.domain.snapshot()
    assert snapshot.specs[0].source == source
    assert snapshot.decision(spec.id).status == "failed"
    assert len(snapshot.obligations) == 1
    assert not snapshot.ready_for_completion
    vm.domain.register_check(check_spec(command, str(tmp_path), capability="custom_assert", source="failure_evidence"))
    assert vm.domain.snapshot().specs[0].source == source


@pytest.mark.parametrize("source", ["user_declared", "planner"])
def test_explicit_compound_failure_is_not_cleared_by_a_bare_test(tmp_path, source):
    vm = _project(tmp_path, require_targeted=source == "planner")
    command = "find missing-directory && python -m unittest tests.test_sample -q"
    spec = _declare(vm, command, source)
    vm.settle_shell(command)
    (tmp_path / "tests/test_sample.py").write_text(
        "import unittest\nclass Sample(unittest.TestCase):\n"
        " def test_example(self): self.assertTrue(True)\n")
    vm.settle_shell("python -m unittest tests.test_sample -q")
    snapshot = vm.domain.snapshot()
    obligation = next(item for item in snapshot.obligations if item.check_id == spec.id)
    assert not obligation.resolved
    assert snapshot.decision(spec.id).status == "failed"
    assert next(item for item in snapshot.specs if item.id == spec.id).source == source


@pytest.mark.parametrize("command", [
    "python -m unittest tests.test_sample -q",
    "python -c 'assert False'",
])
def test_observed_check_failure_keeps_negative_fact_for_delivery_review(tmp_path, command):
    vm = _project(tmp_path)
    vm.settle_shell(command)
    snapshot = vm.domain.snapshot()
    fact, = snapshot.executions
    assert fact.outcome == "failed" and fact.exit_code != 0
    assert snapshot.obligations == ()
    vm.domain.set_completion_candidate("candidate after failed check")
    snapshot = vm.domain.snapshot()
    assessment = snapshot.assess_completion("completed")
    assert assessment.status == "completed_unverified" and assessment.review_required
    assert not snapshot.ready_for_completion


@pytest.mark.parametrize("command", [
    "python -m unittest tests.test_sample -q",
    "python -c 'assert False'",
])
def test_user_acceptance_failure_retains_a_hard_requirement(tmp_path, command):
    from nz_coder.runtime.verification.domain.requirements import TaskContract

    vm = _project(tmp_path)
    vm.state.set_task_contract(TaskContract(acceptance_commands=(command,)))
    vm.settle_shell(command)
    snapshot = vm.domain.snapshot()
    assert any(spec.source == "user_declared" and spec.required for spec in snapshot.specs)
    assert snapshot.obligations
    assert snapshot.assess_completion("completed").status == "blocked"


@pytest.mark.parametrize("source", ["user_declared", "planner"])
def test_later_declaration_promotes_an_observed_check_without_losing_failure(tmp_path, source):
    vm = _project(tmp_path, require_targeted=source == "planner")
    command = "python -m unittest tests.test_sample -q"
    vm.settle_shell(command)
    spec = _declare(vm, command, source)
    vm.settle_shell(command)
    snapshot = vm.domain.snapshot()
    assert next(item for item in snapshot.specs if item.id == spec.id).source == source
    obligation = next(item for item in snapshot.obligations if item.check_id == spec.id)
    # 声明前的失败仍保留，但验收权不倒推到先前的普通观察。
    assert len(snapshot.executions) == 2
    assert all(fact.outcome == "failed" for fact in snapshot.executions)
    assert obligation.failure_execution_ids == (snapshot.executions[-1].execution_id,)
    assert not obligation.resolved


def _saved_failure(root, command="python demo.py", *, source="failure_evidence", capability="custom_assert", in_process=False):
    scope = RunScope(TaskScope("session", "task", str(root)), "old-run", "main")
    version = WorkspaceVersion()
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version,
                                require_targeted=source == "planner")
    spec = check_spec(command, str(root), capability=capability, source=source,
                      required=source in {"user_declared", "planner"},
                      stage="static" if in_process or "assert" in command else "targeted")
    domain.register_check(spec)
    domain.begin_execution("failed-execution", (spec.id,))
    fact = ExecutionFact("failed-execution", scope, spec.command, "failed", version, version,
                         capabilities=(capability,), exit_code=1, in_process=in_process)
    domain.observe_execution(fact)
    return domain.checkpoint(), scope, version, spec.id


def _restore(saved, scope, version):
    return restore_domain(saved, lock=threading.RLock(), scope=scope, version=version)


@pytest.mark.parametrize("command,capability", [
    ("python demo.py", "custom_assert"),
    ("find missing-directory && python -m unittest tests.test_sample -q", "test"),
])
def test_restore_retires_only_misregistered_automatic_checks_and_retains_failed_fact(tmp_path, command, capability):
    saved, scope, version, check_id = _saved_failure(tmp_path, command, capability=capability)
    restored = _restore(saved, scope, version)
    snapshot = restored.snapshot()
    assert snapshot.specs == ()
    assert snapshot.checks == ()
    assert snapshot.obligations == ()
    assert len(snapshot.executions) == 1 and snapshot.executions[0].outcome == "failed"
    assert snapshot.executions[0].execution_id == "failed-execution"
    assert not snapshot.rebuild_required and not snapshot.integrity_errors
    roundtrip = restored.checkpoint()
    assert check_id not in roundtrip["attempt_counts"]
    assert check_id not in roundtrip["failure_revisions"]


@pytest.mark.parametrize("source", ["user_declared", "planner"])
def test_restore_keeps_explicit_script_check_and_unresolved_failure(tmp_path, source):
    saved, scope, version, check_id = _saved_failure(tmp_path, source=source)
    snapshot = _restore(saved, scope, version).snapshot()
    assert snapshot.specs[0].source == source
    assert snapshot.obligations[0].check_id == check_id
    assert not snapshot.obligations[0].resolved


def test_restore_raw_required_record_is_protected_without_granting_observation_authority(tmp_path):
    saved, scope, version, check_id = _saved_failure(tmp_path)
    saved["specs"][0]["required"] = True
    snapshot = _restore(saved, scope, version).snapshot()
    assert len(snapshot.specs) == 1 and not snapshot.specs[0].required
    assert snapshot.specs[0].id == check_id
    assert snapshot.obligations == ()
    assert snapshot.executions[0].outcome == "failed" and snapshot.review_pending
    assert not snapshot.ready_for_completion


@pytest.mark.parametrize("command,capability,in_process", [
    ("python -c 'assert False'", "custom_assert", False),
    ("python -m unittest tests.test_sample -q", "test", False),
    ("python_symbol_check sample.py", "symbol", True),
])
def test_restore_keeps_recognized_checks_and_host_in_process_facts(tmp_path, command, capability, in_process):
    saved, scope, version, check_id = _saved_failure(tmp_path, command, capability=capability, in_process=in_process)
    snapshot = _restore(saved, scope, version).snapshot()
    assert snapshot.specs[0].id == check_id
    assert snapshot.decision(check_id).status == "failed"
    assert snapshot.obligations == ()
    assert snapshot.executions[0].in_process is in_process
    assert snapshot.executions[0].outcome == "failed" and snapshot.review_pending
    assert not snapshot.ready_for_completion


def test_restore_unknown_source_is_not_hidden_by_automatic_check_retirement(tmp_path):
    saved, scope, version, check_id = _saved_failure(tmp_path)
    saved["specs"][0]["source"] = "unrecognized_source"
    saved["integrity_errors"] = ["existing integrity failure"]
    snapshot = _restore(saved, scope, version).snapshot()
    assert snapshot.specs[0].id == check_id
    assert snapshot.rebuild_required
    assert "existing integrity failure" in snapshot.integrity_errors
    assert any("unknown check source" in item for item in snapshot.integrity_errors)


def test_restore_retirement_keeps_existing_integrity_errors_and_rebuild_requirement(tmp_path):
    saved, scope, version, _check_id = _saved_failure(tmp_path)
    saved["integrity_errors"] = ["existing integrity failure"]
    saved["rebuild_required"] = True
    snapshot = _restore(saved, scope, version).snapshot()
    assert snapshot.specs == ()
    assert snapshot.rebuild_required
    assert snapshot.integrity_errors == ("existing integrity failure",)
    assert not snapshot.ready_for_completion


def test_restore_does_not_retire_a_check_using_an_inconsistent_raw_identity(tmp_path):
    saved, scope, version, _check_id = _saved_failure(tmp_path)
    saved["specs"][0]["command"]["argv"] = ["not-the-recorded-command"]
    saved["facts"][0]["command"]["argv"] = ["not-the-recorded-command"]
    snapshot = _restore(saved, scope, version).snapshot()
    assert snapshot.rebuild_required
    assert snapshot.specs
    assert any("command identity" in item for item in snapshot.integrity_errors)
    assert not snapshot.ready_for_completion


@pytest.mark.parametrize("damage", [
    "missing_failure", "mismatched_failure", "missing_decision", "duplicate_fact",
    "positive_failure", "invalid_in_process",
])
def test_restore_rejects_damaged_references_before_retiring_automatic_checks(tmp_path, damage):
    saved, scope, version, _check_id = _saved_failure(tmp_path, source="user_declared")
    # 显式构造历史误登记的义务；当前普通观察已不再生产这种状态。
    saved["specs"][0].update(source="failure_evidence", required=False)
    if damage == "missing_failure":
        saved["obligations"][0]["failure_execution_ids"] = ["not-recorded"]
    elif damage == "mismatched_failure":
        saved["facts"][0]["command"] = asdict(command_identity("python another.py", str(tmp_path)))
    elif damage == "missing_decision":
        saved["decisions"][0]["execution_id"] = "not-recorded"
    elif damage == "duplicate_fact":
        saved["facts"].append(dict(saved["facts"][0]))
    elif damage == "positive_failure":
        saved["facts"][0]["outcome"] = "succeeded"
        saved["facts"][0]["exit_code"] = 0
    else:
        saved["facts"][0]["in_process"] = "true"
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
    with pytest.raises(CheckpointRestoreError):
        _restore(saved, scope, version)


def test_native_application_demo_failure_does_not_invent_user_acceptance(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    (tmp_path / "demo.py").write_text("raise SystemExit(7)\n")
    command = "python demo.py"
    actions = [[_tool("bash", command=command)], "The demo exited with code 7; no files were changed."]
    result, _state, _trace, _requests, *_ = _run(monkeypatch, tmp_path, actions,
        task="Run demo.py once and report its exit status. Do not modify files.",
        allowed_commands=(command,), limit=len(actions), semantic_response="accept")
    assert result.status.value == "completed", (result.status, result.error)
    assert result.metadata["verification"]["test_obligations"] == []
    assert result.metadata["verification"]["verification_needed"] is False
