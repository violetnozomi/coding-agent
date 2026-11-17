"""自动验收、模型复测与计划覆盖必须消费同一检查裁决。"""
from __future__ import annotations

from dataclasses import replace
import threading

from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import CheckSpec, ExecutionFact, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _domain(root):
    return VerificationDomain(lock=threading.RLock(),
        scope=RunScope(TaskScope("session", "lineage", str(root)), "run", "main"), version=WorkspaceVersion())


def _run(domain, spec, id, *, outcome="succeeded", exit_code=0, **kwargs):
    domain.begin_execution(id, (spec.id,))
    version = domain.snapshot().version
    return domain.observe_execution(ExecutionFact(id, domain.scope, spec.command, outcome, version, version,
        capabilities=(spec.capability,), targets=spec.targets, exit_code=exit_code, **kwargs))


def test_user_contract_covers_only_unexecuted_plan_and_retains_origin(tmp_path):
    domain = _domain(tmp_path)
    planned = CheckSpec("syntax", command_identity("python -m py_compile app.py", str(tmp_path)),
                        "syntax", targets=("app.py",), stage="static", required=True, source="planner")
    domain.replace_plan((planned,))
    contract = CheckSpec("acceptance", command_identity("pytest tests/test_app.py", str(tmp_path)),
                        "test", targets=("tests/test_app.py",), required=True, source="user_declared", covers=(planned.id,))
    domain.register_check(contract)
    _run(domain, contract, "contract-pass", test_count=1)
    snapshot = domain.snapshot()
    assert snapshot.ready_for_completion
    assert snapshot.decision(planned.id).status == "covered"
    assert snapshot.decision(planned.id).covered_by == contract.id
    assert snapshot.decision(contract.id).status == "passed"


def test_user_contract_cannot_erase_an_observed_static_failure(tmp_path):
    domain = _domain(tmp_path)
    planned = CheckSpec("assertion", command_identity("python -c 'assert 1 == 2'", str(tmp_path)),
                        "custom_assert", stage="static", required=True, source="planner")
    domain.replace_plan((planned,))
    _run(domain, planned, "failure", outcome="failed", exit_code=1)
    contract = CheckSpec("acceptance", command_identity("pytest tests/test_app.py", str(tmp_path)),
                        "test", required=True, source="user_declared", covers=(planned.id,))
    domain.register_check(contract)
    _run(domain, contract, "contract-pass", test_count=1)
    assert not domain.snapshot().ready_for_completion
    assert domain.snapshot().decision(planned.id).status == "failed"


def test_replan_retains_real_failure_but_retires_unexecuted_plan(tmp_path):
    domain = _domain(tmp_path)
    first = CheckSpec("first", command_identity("python first.py", str(tmp_path)), "custom_assert", required=True, source="planner")
    second = replace(first, id="second", command=command_identity("python second.py", str(tmp_path)))
    domain.replace_plan((first, second))
    _run(domain, first, "failed", outcome="failed", exit_code=7)
    domain.replace_plan(())
    snapshot = domain.snapshot()
    assert not next(spec for spec in snapshot.specs if spec.id == first.id).required
    assert snapshot.behavior_failures[0].execution_id == "failed"
    assert not next(spec for spec in snapshot.specs if spec.id == second.id).required
    assert not snapshot.ready_for_completion


def test_plan_adapter_registers_check_capability_and_original_parameter_binding(tmp_path):
    from nz_coder.runtime.adapters.verification_plans import register_verification_plan

    domain = _domain(tmp_path)
    command = "python -m pytest tests/test_a.py --ignore tests/test_b.py"
    register_verification_plan(domain, {"stages": [{"name": "targeted", "commands": [
        {"command": command, "required": True},
    ]}]})
    spec, = domain.snapshot().specs
    assert spec.command.original == command
    assert spec.capability == "test" and spec.source == "planner" and not spec.required


def test_failed_zero_test_or_drifting_acceptance_never_creates_pass(tmp_path):
    domain = _domain(tmp_path)
    spec = CheckSpec("acceptance", command_identity("pytest tests/test_a.py", str(tmp_path)), "test", required=True, source="user_declared")
    domain.register_check(spec)
    _run(domain, spec, "failed", outcome="failed", exit_code=1, reason="Tool execution failed")
    assert domain.snapshot().decision(spec.id).status == "failed"
    _run(domain, spec, "zero-tests", test_count=0)
    assert not domain.snapshot().ready_for_completion
    domain.begin_execution("drift", (spec.id,))
    domain.observe_execution(ExecutionFact("drift", domain.scope, spec.command, "succeeded", WorkspaceVersion(),
        WorkspaceVersion(1, 1, 1), capabilities=("test",), exit_code=0, test_count=1))
    assert not domain.snapshot().ready_for_completion


def test_covered_plan_check_is_not_scheduled_again():
    from nz_coder.runtime.verification.verification_scheduler import VerificationScheduler

    action = VerificationScheduler().action("yellow", verification_status={
        "verification_pipeline": {"stages": [{"name": "static", "commands": [
            {"command": "python -m py_compile app.py", "required": True, "status": "covered"},
        ]}]},
    }, unresolved_requirements=(), has_exact_contract=True, mutation_generation=1)
    assert action.kind == "none"


def test_plan_adapter_expands_aggregate_static_capability(tmp_path):
    from nz_coder.runtime.adapters.verification_plans import register_verification_plan

    domain = _domain(tmp_path)
    register_verification_plan(domain, {"changed_files": ["app.py", "app.js"], "stages": [
        {"name": "static", "required": True, "commands": [{"command": "verify_changed_files"}]},
    ]})
    specs = domain.snapshot().specs
    assert {(spec.capability, spec.targets) for spec in specs} == {("syntax", ("app.py",)), ("type", ("app.js",))}
    assert all(spec.coverage_mode == "capability_targets" for spec in specs)


def test_contract_projection_uses_start_generation_and_one_verdict(tmp_path):
    domain = _domain(tmp_path)
    spec = CheckSpec("acceptance", command_identity("pytest tests/test_a.py", str(tmp_path)), "test", required=True, source="user_declared")
    domain.register_check(spec)
    domain.begin_execution("drift", (spec.id,))
    domain.observe_execution(ExecutionFact("drift", domain.scope, spec.command, "succeeded", WorkspaceVersion(),
        WorkspaceVersion(1, 1, 1), capabilities=("test",), exit_code=0, test_count=1))
    progress = domain.contract_projection(spec.command.original, ())
    assert progress["attempted_generation"] == 0
    assert progress["attempts"] == 1
    assert progress["passed"] is None


def test_contract_attempt_count_survives_compacted_checkpoint(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import restore_domain

    domain = _domain(tmp_path)
    spec = CheckSpec("acceptance", command_identity("pytest tests/test_a.py", str(tmp_path)), "test", required=True, source="user_declared")
    domain.register_check(spec)
    for index in range(4):
        _run(domain, spec, f"pass-{index}", test_count=1)
    saved = domain.checkpoint()
    scope = replace(domain.scope, interaction_run_id="resumed")
    restored = restore_domain(saved, lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    progress = restored.contract_projection(spec.command.original, ())
    assert progress["attempts"] == 4
    assert progress["attempted_generation"] == -1
    assert progress["passed"] is None


def test_capability_coverage_reports_actual_execution_not_reference_command(tmp_path):
    from nz_coder.runtime.verification.completion_gate import verification_appendix

    domain = _domain(tmp_path)
    planned = CheckSpec("syntax", command_identity("verify_changed_files", str(tmp_path)), "syntax",
        targets=("app.py",), required=True, source="planner", stage="static", coverage_mode="capability_targets")
    domain.register_check(planned)
    version = domain.snapshot().version
    domain.begin_execution("ast", (planned.id,))
    domain.observe_execution(ExecutionFact("ast", domain.scope,
        command_identity("python_symbol_check app.py", str(tmp_path)), "succeeded", version, version,
        capabilities=("syntax",), targets=("app.py",), in_process=True))
    snapshot = domain.snapshot().to_dict()
    assert snapshot["last_verification"]["command"] == "python_symbol_check app.py"
    assert snapshot["verification_pipeline"]["stages"][0]["commands"][0]["last_command"] == "python_symbol_check app.py"
    text = verification_appendix(snapshot)
    assert "python_symbol_check app.py" in text
    assert "verify_changed_files" not in text
    from nz_coder.runtime.observability.run_evidence import RunEvidence
    report = RunEvidence(run_id="report", verification_domain=domain).verification_results
    assert report[0]["command"] == "python_symbol_check app.py"
    assert report[0]["check_command"] == "verify_changed_files"


def test_strict_acceptance_keeps_required_static_check_pending(tmp_path):
    import threading

    domain = VerificationDomain(lock=threading.RLock(), scope=_domain(tmp_path).scope,
        version=WorkspaceVersion(), require_targeted=True)
    static = CheckSpec("syntax", command_identity("python -m py_compile app.py", str(tmp_path)), "syntax", required=True, source="planner", stage="static")
    domain.register_check(static)
    contract = CheckSpec("user", command_identity("pytest tests/test_app.py", str(tmp_path)), "test",
        required=True, source="user_declared", covers=(static.id,))
    domain.register_check(contract)
    _run(domain, contract, "accepted", test_count=1)
    assert domain.snapshot().decision(static.id).status == "pending"
    assert not domain.snapshot().ready_for_completion


def test_only_host_contract_can_create_user_checks_and_planner_stays_optional(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.verification.domain.requirements import TaskContract, Requirement
    (tmp_path / 'app.py').write_text('def broken(:\n')
    scenario = VerificationScenario(workspace=tmp_path, plan={'stages': [
        {'name': 'static', 'commands': [{'command': 'python -m py_compile app.py', 'required': True}]}]})
    scenario.state.set_task_contract(TaskContract(requirements=(Requirement('R1', 'behave correctly', 'behavior'),),
        acceptance_commands=('true',)))
    scenario.runtime.synchronize_task()
    scenario.settle_shell('true')
    snapshot = scenario.domain.snapshot()
    assert not snapshot.ready_for_completion  # 显式命令已过，业务语义仍需独立评审。
    assert snapshot.assess_completion("completed").review_required
    assert {spec.command.original for spec in snapshot.specs if spec.source == 'user_declared'} == {'true'}
    assert all(not spec.required for spec in snapshot.specs if spec.source == 'planner')
