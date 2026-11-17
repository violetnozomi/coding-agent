"""临时尝试、验收定义和真实失败的行为边界。"""
from __future__ import annotations

from dataclasses import replace
import threading

import pytest

from nz_coder.runtime.adapters.verification_plans import check_spec, register_verification_plan
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import (
    CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceObservation, WorkspaceVersion,
)
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _domain(root, *, targeted=False):
    scope = RunScope(TaskScope("session", "task", str(root)), "run", "main")
    return VerificationDomain(lock=threading.RLock(), scope=scope,
                              version=WorkspaceVersion(), require_targeted=targeted)


def _check(root, id="test", command="pytest tests/test_a.py", **kwargs):
    return CheckSpec(id, command_identity(command, str(root)), "test", **kwargs)


def _execute(domain, spec, id="execution", *, passed=True, reliable=True, environment_error=False):
    domain.begin_execution(id, (spec.id,))
    version = domain.snapshot().version
    return domain.observe_execution(ExecutionFact(id, domain.scope, spec.command,
        "succeeded" if passed else "failed", version, version,
        capabilities=(spec.capability,), exit_code=0 if passed else 1,
        test_count=1, reliable=reliable, environment_error=environment_error))


def test_new_observation_does_not_create_required_work(tmp_path):
    domain = _domain(tmp_path)
    domain.register_check(_check(tmp_path))
    snapshot = domain.snapshot()
    assert not snapshot.specs[0].required
    assert snapshot.ready_for_completion
    assert snapshot.tests_passed is None
    stage = snapshot.to_dict()["verification_pipeline"]["stages"][0]
    assert stage["status"] == "optional"


@pytest.mark.parametrize("source", ["model_execution", "failure_evidence"])
def test_observation_cannot_claim_required_authority(tmp_path, source):
    domain = _domain(tmp_path)
    domain.register_check(_check(tmp_path, source=source, required=True))
    assert domain.snapshot().ready_for_completion
    assert not domain.snapshot().specs[0].required


def test_observation_keeps_existing_planner_authority_and_can_be_withdrawn(tmp_path):
    domain = _domain(tmp_path)
    planned = _check(tmp_path, source="planner", required=True)
    domain.replace_plan((planned,))
    domain.register_check(replace(planned, source="model_execution", required=False))
    assert domain.snapshot().specs[0].source == "planner"
    assert domain.snapshot().specs[0].required
    domain.replace_plan(())
    assert domain.snapshot().ready_for_completion


def test_optional_unreliable_failure_still_requires_matching_retest(tmp_path):
    domain = _domain(tmp_path)
    spec = _check(tmp_path, required=False)
    domain.register_check(spec)
    _execute(domain, spec, "failure", passed=False, reliable=False)
    snapshot = domain.snapshot()
    assert not snapshot.ready_for_completion
    assert snapshot.tests_passed is False
    assert snapshot.obligations[0].failure_execution_ids == ("failure",)
    domain.replace_plan(())
    other = _check(tmp_path, "other", "pytest tests/test_b.py", required=False)
    domain.register_check(other)
    _execute(domain, other, "unrelated")
    assert not domain.snapshot().ready_for_completion
    _execute(domain, spec, "retry")
    assert domain.snapshot().ready_for_completion
    assert domain.snapshot().obligations[0].resolved_by == "retry"


def test_stale_optional_focus_does_not_override_current_suite_result(tmp_path):
    domain = _domain(tmp_path, targeted=True)
    focused = _check(tmp_path, required=False)
    suite = _check(tmp_path, "suite", "pytest tests", source="user_declared", required=True)
    for spec in [focused, suite]:
        domain.register_check(spec)
    _execute(domain, focused)
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("tests/test_b.py",)))
    _execute(domain, suite, "suite")
    snapshot = domain.snapshot()
    assert snapshot.decision(focused.id).status == "pending"
    assert snapshot.decision(focused.id).covered_by == ""
    assert snapshot.tests_passed is True
    assert snapshot.ready_for_completion


def test_native_verification_without_check_id_blocks_until_settled(tmp_path):
    domain = _domain(tmp_path)
    domain.begin_execution("native", (), verifies=True)
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(), execution_id="native"))
    assert not domain.snapshot().ready_for_completion
    with pytest.raises(ValueError, match="unconfirmed"):
        domain.seal(expected_revision=domain.snapshot().revision, terminal_reason="completed")
    domain.end_execution("native")
    assert domain.snapshot().ready_for_completion


def test_active_optional_test_blocks_completion_and_test_summary(tmp_path):
    domain = _domain(tmp_path)
    spec = _check(tmp_path, required=False)
    domain.register_check(spec)
    _execute(domain, spec, "passed")
    domain.begin_execution("running", (spec.id,))
    assert not domain.snapshot().ready_for_completion
    assert domain.snapshot().tests_passed is False
    domain.end_execution("running")
    assert domain.snapshot().tests_passed is True


def test_planned_failing_target_keeps_required_without_fabricated_failure(tmp_path):
    domain = _domain(tmp_path)
    register_verification_plan(domain, {"stages": [{"name": "targeted", "commands": [{
        "command": "pytest tests/test_a.py", "required": True,
        "automation_provenance": "failure_evidence",
    }]}]})
    snapshot = domain.snapshot()
    assert snapshot.specs[0].required
    assert snapshot.specs[0].source == "planner"
    assert not snapshot.obligations
    assert not snapshot.ready_for_completion


def test_check_factory_defaults_to_nonblocking_observation(tmp_path):
    domain = _domain(tmp_path)
    domain.register_check(check_spec("pytest tests/test_a.py", str(tmp_path), capability="test"))
    assert domain.snapshot().ready_for_completion


@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.parametrize("preallocated", [False, True])
def test_unexecuted_tool_does_not_create_check_or_execution(tmp_path, denied, preallocated):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.tool_platform.execution import ToolExecutionResult

    scenario = VerificationScenario(workspace=tmp_path)
    args = {"command": "pytest tests/test_a.py", "timeout": 180}
    start = scenario.runtime.begin_tool({"function": {"name": "bash", "arguments": args}}) if preallocated else None
    result = ToolExecutionResult("bash", args, "Error: rejected before execution", False, True, False, False,
        permission_denied=denied, execution_id=start.execution_id if start else "")
    scenario.runtime.record_result(result)
    snapshot = scenario.domain.snapshot()
    assert snapshot.executions == ()
    assert snapshot.obligations == ()
    assert all(not spec.required for spec in snapshot.specs)
    assert snapshot.ready_for_completion
    if not preallocated:
        assert snapshot.specs == ()


def test_runtime_native_check_is_active_before_real_failure_arrives(tmp_path):
    import importlib
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.workdir import scoped_workdir

    importlib.import_module("nz_coder.tools.python_ast")
    (tmp_path / "broken.py").write_text("def broken(:\n")
    scenario = VerificationScenario(workspace=tmp_path)
    call = {"id": "native", "function": {"name": "python_symbol_check", "arguments": {"path": "broken.py"}}}
    with scoped_workdir(tmp_path):
        start = scenario.runtime.begin_tool(call)
        assert not scenario.domain.snapshot().ready_for_completion
        result = ToolExecutor(PermissionManager("auto")).execute_one(call, 0)
        result.execution_id = start.execution_id
        scenario.runtime.record_result(result)
    snapshot = scenario.domain.snapshot()
    assert snapshot.obligations
    assert all(not spec.required for spec in snapshot.specs)
    assert not snapshot.ready_for_completion


def test_withdrawn_active_plan_blocks_only_until_execution_settles(tmp_path):
    domain = _domain(tmp_path)
    spec = _check(tmp_path, required=True, source="planner")
    domain.replace_plan((spec,))
    domain.begin_execution("active", (spec.id,))
    domain.replace_plan(())
    assert not domain.snapshot().ready_for_completion
    domain.end_execution("active")
    assert domain.snapshot().ready_for_completion


def _command_projection(domain, id="test"):
    return next(command for stage in domain.snapshot().to_dict()["verification_pipeline"]["stages"]
                for command in stage["commands"] if command["id"] == id)


def test_retry_eligibility_comes_from_execution_not_source_label(tmp_path):
    domain = _domain(tmp_path)
    spec = _check(tmp_path, source="planner", required=True)
    domain.register_check(spec)
    assert not _command_projection(domain)["retry_eligible"]
    _execute(domain, spec)
    assert _command_projection(domain)["retry_eligible"]
    assert domain.snapshot().specs[0].source == "planner"


def test_retry_material_survives_invalidated_decision_and_checkpoint(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain = _domain(tmp_path)
    spec = _check(tmp_path, source="planner", required=True)
    domain.register_check(spec)
    _execute(domain, spec)
    version = WorkspaceVersion(1, 1, 1)
    domain.observe_workspace(WorkspaceObservation(version, paths=("app.py",)))
    saved = domain.checkpoint()
    assert [fact["execution_id"] for fact in saved["facts"]] == ["execution"]
    restored = restore_domain(saved, lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id="resumed"), version=version)
    assert _command_projection(restored)["retry_eligible"]
    assert not restored.snapshot().ready_for_completion
    assert restored.snapshot().decision(spec.id).status == "pending"


def test_restore_normalizes_old_required_observation_without_erasing_failure(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain = _domain(tmp_path)
    observed = _check(tmp_path, required=False)
    domain.register_check(observed)
    _execute(domain, observed, "failure", passed=False)
    saved = domain.checkpoint()
    saved["specs"][0]["required"] = True
    restored = restore_domain(saved, lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id="resumed"), version=WorkspaceVersion())
    assert not restored.snapshot().specs[0].required
    assert restored.snapshot().obligations[0].failure_execution_ids == ("failure",)
    assert not restored.snapshot().ready_for_completion


def test_unknown_source_restore_keeps_known_failure_and_cannot_be_rebuilt_away(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain = _domain(tmp_path)
    failing = _check(tmp_path, required=False)
    domain.register_check(failing)
    _execute(domain, failing, "failure", passed=False)
    unknown = _check(tmp_path, "unknown", "pytest tests/test_unknown.py", source="planner", required=True)
    domain.register_check(unknown)
    saved = domain.checkpoint()
    saved["specs"][1]["source"] = "unrecognized_source"
    restored = restore_domain(saved, lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id="resumed"), version=WorkspaceVersion())
    assert restored.snapshot().rebuild_required
    assert restored.snapshot().integrity_errors
    assert restored.snapshot().obligations[0].failure_execution_ids == ("failure",)
    acceptance = _check(tmp_path, "acceptance", "pytest tests/test_other.py", source="user_declared", required=True)
    restored.register_check(acceptance)
    _execute(restored, acceptance, "unrelated-acceptance")
    assert not restored.rebuild_from_current_acceptance()
    assert not restored.snapshot().ready_for_completion


def test_unknown_public_metadata_cannot_authorize_automatic_retry(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.tool_platform.execution import ToolExecutionResult

    scenario = VerificationScenario(workspace=tmp_path)
    args = {"command": "pytest tests/test_a.py"}
    start = scenario.runtime.begin_tool({"function": {"name": "bash", "arguments": args}})
    scenario.runtime.record_result(ToolExecutionResult("bash", args, "all passed", True, False, False, False,
        execution_id=start.execution_id, metadata={"retry_eligible": True, "automation_provenance": "user_declared"}))
    commands = scenario.runtime.status()["verification_pipeline"]["stages"][0]["commands"]
    assert not commands[0]["retry_eligible"]


def _schedule(domain):
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    return VerificationScheduler().action("orange", verification_status=domain.snapshot().to_dict(),
        unresolved_requirements=("R1",), has_exact_contract=False, mutation_generation=1)


def test_scheduler_retries_executed_planner_without_changing_authority(tmp_path):
    domain = _domain(tmp_path)
    spec = _check(tmp_path, required=True, source="planner")
    domain.register_check(spec)
    _execute(domain, spec)
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    action = _schedule(domain)
    assert action.kind == "stage"
    assert action.command == "pytest tests/test_a.py"
    assert action.cwd == str(tmp_path)


def test_scheduler_can_retry_optional_failure_in_original_subdirectory(tmp_path):
    sub = tmp_path / "package"
    sub.mkdir()
    domain = _domain(tmp_path)
    spec = _check(sub, required=False)
    domain.register_check(spec)
    _execute(domain, spec, passed=False)
    action = _schedule(domain)
    assert action.kind == "stage"
    assert action.cwd == str(sub)


def test_scheduler_and_guidance_do_not_resurrect_unreliable_history(tmp_path):
    from nz_coder.runtime.verification.completion_gate import ordinary_verification_guidance

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text("def test_ok(): pass\n")
    domain = _domain(tmp_path)
    ignored = _check(tmp_path, command="pytest tests/test_a.py | tail -3", required=False)
    suite = _check(tmp_path, "suite", "pytest tests", source="user_declared", required=True)
    for spec in [ignored, suite]:
        domain.register_check(spec)
    _execute(domain, ignored, "ignored", reliable=False)
    _execute(domain, suite, "accepted")
    assert ordinary_verification_guidance(domain.snapshot().to_dict(), tmp_path) == ""
    assert _schedule(domain).kind == "none"


def test_scheduler_never_first_runs_unexecuted_planner_target(tmp_path):
    domain = _domain(tmp_path)
    domain.register_check(_check(tmp_path, required=True, source="planner"))
    assert _schedule(domain).kind == "none"


def test_retry_support_survives_later_unreliable_result_and_two_checkpoints(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain = _domain(tmp_path)
    spec = _check(tmp_path, command="pytest tests/test_a.py 2>&1", required=True, source="planner")
    domain.register_check(spec)
    _execute(domain, spec, "reliable")
    _execute(domain, spec, "unreliable", reliable=False)
    version = WorkspaceVersion(1, 1, 1)
    domain.observe_workspace(WorkspaceObservation(version, paths=("app.py",)))
    assert _schedule(domain).kind == "stage"
    for index in range(2):
        domain = restore_domain(domain.checkpoint(), lock=threading.RLock(),
            scope=replace(domain.scope, interaction_run_id=f"resumed-{index}"), version=version)
        assert _command_projection(domain)["retry_eligible"]
        assert _schedule(domain).kind == "stage"
        assert not domain.snapshot().ready_for_completion


@pytest.mark.parametrize("environment", [False, True])
def test_reopened_failure_stage_and_environment_follow_unresolved_obligation(tmp_path, environment):
    domain = _domain(tmp_path)
    spec = _check(tmp_path)
    domain.register_check(spec)
    _execute(domain, spec, "failure", passed=False, environment_error=environment)
    _execute(domain, spec, "resolved")
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=("app.py",)))
    snapshot = domain.snapshot()
    projected = snapshot.to_dict()
    assert not snapshot.ready_for_completion
    assert projected["verification_pipeline"]["stages"][0]["status"] == ("blocked_environment" if environment else "failed")
    assert bool(projected["environment_blocker"]) is environment
