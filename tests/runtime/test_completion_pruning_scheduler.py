"""阶段标签不授予执行权，参数不是需要猜测存在性的文件清单。"""
from dataclasses import replace
from types import SimpleNamespace

from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.verification.completion_gate import ordinary_verification_guidance
from nz_coder.runtime.verification.verification_scheduler import VerificationAction, VerificationScheduler


def test_required_regression_uses_original_check_identity(tmp_path):
    command = 'python -m unittest discover -s tests -p "test_store.py" -v'
    check = {"id": "real-check", "command": command, "cwd": str(tmp_path), "required": True,
        "status": "pending", "retry_eligible": True, "capability": "test", "automation_provenance": "user_declared"}
    status = {"verification_pipeline": {"stages": [{"name": "regression", "commands": [check]}]}}
    actions = VerificationScheduler().completion_actions(status, mutation_generation=3)
    assert len(actions) == 1
    assert actions[0].check_id == "real-check"
    assert actions[0].command == command


def test_unittest_pattern_guidance_does_not_need_same_name_root_file(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_store.py").write_text("import unittest\n")
    command = 'python -m unittest discover -s tests -p "test_store.py" -v'
    status = {"verification_pipeline": {"stages": [{"name": "regression", "commands": [
        {"command": command, "cwd": str(tmp_path), "required": True, "status": "failed"}]}]}}
    assert command in ordinary_verification_guidance(status, tmp_path)


def test_stage_change_does_not_create_another_execution_allowance(tmp_path):
    state = SimpleNamespace(mutation_generation=5, acceptance_mutation_generation=5)
    context = SimpleNamespace(interaction_run_id="run")
    action = VerificationAction(kind="stage", stage="targeted", command="python -m unittest discover", cwd=str(tmp_path))
    assert AgentRunner._verification_attempt_key(state, context, action) == AgentRunner._verification_attempt_key(
        state, context, replace(action, stage="regression"))
    assert AgentRunner._verification_attempt_key(state, context, action) == AgentRunner._verification_attempt_key(
        state, context, replace(action, command="python  -m unittest discover"))
