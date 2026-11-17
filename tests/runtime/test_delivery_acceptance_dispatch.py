"""用户明确的验收命令按完整集合执行，不按 runner 名称授予资格。"""
from __future__ import annotations


from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.verification.verification_scheduler import VerificationAction, VerificationScheduler
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import CheckSpec

from test_completion_pruning_domain import domain


def status(root, commands, *, source="user_declared", strict=False):
    return {"strict": strict, "verification_pipeline": {"stages": [{"name": "regression", "commands": [
        {"id": f"check-{i}", "command": command, "cwd": str(root), "required": True,
         "status": "pending", "retry_eligible": True, "capability": "custom_assert",
         "automation_provenance": source}
        for i, command in enumerate(commands)]}]}}


def test_declared_custom_commands_are_scheduled_without_runner_whitelist(tmp_path):
    commands = ("make check", "./verify.sh", "cmake --build build", "python check.py")
    selected = VerificationScheduler().completion_actions(status(tmp_path, commands), mutation_generation=0)
    assert [item.command for item in selected] == list(commands)


def test_optional_model_and_planner_checks_are_not_automatically_run(tmp_path):
    for source in ("planner", "model_execution", "failure_evidence"):
        assert not VerificationScheduler().completion_actions(status(tmp_path, ["python -m unittest"], source=source),
                                                             mutation_generation=1)


def test_first_declared_opaque_command_can_enter_execution(tmp_path):
    owner = domain(tmp_path)
    spec = CheckSpec("check", command_identity("python check.py 2>&1", str(tmp_path)),
                     "custom_assert", required=True, source="user_declared")
    owner.register_check(spec)
    assert owner.snapshot().retry_eligible(spec)


def test_all_capabilities_must_pass_for_declared_command(tmp_path):
    command = "python -m py_compile app.py && python -m unittest"
    current = status(tmp_path, [command, command])
    items = current["verification_pipeline"]["stages"][0]["commands"]
    items[0].update(capability="syntax", status="passed")
    items[1].update(capability="test")
    action = VerificationAction(command=command, cwd=str(tmp_path), check_id="check-1")
    assert not AgentRunner._scheduled_verification_passed(current, action)
    items[1]["status"] = "passed"
    assert AgentRunner._scheduled_verification_passed(current, action)


def test_more_than_six_declared_checks_remain_schedulable(tmp_path):
    commands = [f"python check_{i}.py" for i in range(8)]
    selected = VerificationScheduler().completion_actions(status(tmp_path, commands), mutation_generation=0)
    assert len(selected) == 8
    assert len({item.command for item in selected}) == 8


def test_same_command_multiple_capabilities_has_one_execution(tmp_path):
    current = status(tmp_path, ["python check.py", "python check.py"])
    current["verification_pipeline"]["stages"][0]["commands"][1]["capability"] = "test"
    assert len(VerificationScheduler().completion_actions(current, mutation_generation=0)) == 1


def test_real_runner_runs_all_seven_acceptance_commands_without_source_change(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run

    commands = tuple(f"python -c 'assert {i} == {i}'" for i in range(7))
    task = "Execute these acceptance checks. " + " ".join(f"Acceptance command: `{command}`." for command in commands)
    result, _state, trace, requests, _reviews, _permissions = _run(monkeypatch, tmp_path,
        ["The requested checks are ready for validation."], task=task, limit=20, allowed_commands=commands)
    executed = [entry.get("input", {}).get("command") for entry in trace
                if entry.get("event") == "tool_call" and entry.get("executed")]
    assert executed == list(commands), (result.error, result.final_text)
    assert len(requests) == 1
    assert result.status.value == "completed", (result.error, result.final_text)



def test_failed_acceptance_does_not_starve_other_declared_commands(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run
    commands = ("python -c 'raise SystemExit(1)'", "python -c 'assert 2 == 2'")
    task = " ".join(f"Acceptance command: `{command}`." for command in commands)
    result, _, trace, _, _, _ = _run(monkeypatch, tmp_path, ["Validate the requested commands."] * 4,
        task=task, limit=10, allowed_commands=commands)
    executed = [entry.get("input", {}).get("command") for entry in trace
                if entry.get("event") == "tool_call" and entry.get("executed")]
    assert executed == list(commands)
    assert result.status.value == "blocked"
