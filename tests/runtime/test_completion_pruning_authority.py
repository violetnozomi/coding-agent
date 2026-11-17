"""用户验收权威与模型工作计划分离；旧版本不能靠合同内数字自证来源。"""
from __future__ import annotations

import threading

import pytest

from nz_coder.runtime.agent.task_contract import derive_task_contract
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.verification.domain.checkpoint import restore_domain, CheckpointRestoreError
from nz_coder.runtime.verification.domain.models import RunScope, TaskDefinition, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.requirements import TaskContract
from nz_coder.runtime.verification.domain.service import VerificationDomain


def test_generic_work_and_acceptance_do_not_generate_semantic_hard_cards(tmp_path):
    contract = derive_task_contract("Build a queue with tests, documentation and backward compatibility.",
        acceptance_commands=("python -m unittest discover -s tests -v",), workspace=tmp_path)
    assert contract.acceptance_commands == ("python -m unittest discover -s tests -v",)
    assert not {r.kind for r in contract.requirements} & {"test", "docs", "compatibility", "behavior"}


def test_explicit_files_and_multiple_acceptance_commands_are_kept(tmp_path):
    contract = derive_task_contract("Create app.py and README.md.",
        acceptance_commands=("python -m py_compile app.py", "python -m unittest discover -s tests"), workspace=tmp_path)
    assert contract.acceptance_commands == ("python -m py_compile app.py", "python -m unittest discover -s tests")
    assert {p for r in contract.requirements for p in r.expected_artifacts} == {"app.py", "README.md"}


def test_no_requirement_contract_still_preserves_user_constraints(tmp_path):
    state = RuntimeState()
    state.set_task_contract(TaskContract(objective="Read code", constraints=("Do not modify test files.",)))
    assert state.task_contract["constraints"] == ["Do not modify test files."]
    assert state.task_contract["requirements"] == []


@pytest.mark.parametrize("inner_version", [1, 2, 3, 333])
def test_old_outer_schema_never_trusts_inner_contract_version(tmp_path, inner_version):
    scope = RunScope(TaskScope("s", "t", str(tmp_path)), "r", "main")
    owner = VerificationDomain(lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    owner.declare_task(TaskDefinition(scope.task, contract=TaskContract(acceptance_commands=("python forged.py",),
                                                                     contract_version=inner_version)))
    saved = owner.checkpoint()
    saved["schema"] = "nz.verification.v2"
    with pytest.raises(CheckpointRestoreError) as caught:
        restore_domain(saved, lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    assert caught.value.public_error.code == "authority_source_unavailable"


def test_old_schema_can_only_normalize_with_complete_host_authority(tmp_path):
    scope = RunScope(TaskScope("s", "t", str(tmp_path)), "r", "main")
    owner = VerificationDomain(lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    saved = owner.checkpoint()
    saved["schema"] = "nz.verification.v2"
    authority = TaskDefinition(scope.task, contract=TaskContract(acceptance_commands=("python real.py",)))
    restored = restore_domain(saved, lock=threading.RLock(), scope=scope, version=WorkspaceVersion(), authority=authority)
    assert restored.task_definition.contract.acceptance_commands == ("python real.py",)
    assert restored.checkpoint()["schema"] == "nz.verification.v3"


def test_restore_current_authority_preserves_genuine_followup_added_before_binding(tmp_path):
    from nz_coder.runtime.adapters.verification_runtime import _restore_verification_domain
    scope = RunScope(TaskScope("s", "t", str(tmp_path)), "old", "main")
    owner = VerificationDomain(lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    owner.declare_task(TaskDefinition(scope.task, authority_revision=0, contract=TaskContract(objective="Original task")))
    state = RuntimeState()
    state.initial_task_text = "Original task"
    state._restored_verification_domain = owner.checkpoint()
    message = {"role": "user", "content": "Also create README.md.", "_nz_message_id": "msg-new"}
    state.task_authority_epoch = 1
    state.task_authority_message_id = message["_nz_message_id"]
    restored = _restore_verification_domain(state, session_id="s", workspace=tmp_path, interaction_run_id="new",
        agent_id="main", lock=threading.RLock(), version=WorkspaceVersion(), require_targeted=False, messages=[message])
    assert any("README.md" in r.expected_artifacts for r in restored.task_definition.contract.requirements)


def test_appending_user_acceptance_keeps_existing_exact_commands(tmp_path):
    state = RuntimeState()
    state.initial_task_text = "Acceptance command: `python -m unittest discover -s tests`."
    state.set_acceptance_criteria_from_text(state.initial_task_text, workspace=tmp_path)
    state.apply_current_round_instruction("Also require acceptance command: `python -m py_compile app.py`.", workspace=tmp_path)
    assert state.task_contract["acceptance_commands"] == ["python -m unittest discover -s tests", "python -m py_compile app.py"]
    assert state.verification_definition["command"] == state.task_contract["acceptance_commands"][0]


def test_legacy_repeated_task_body_cannot_import_previous_task_followups(tmp_path):
    from nz_coder.runtime.adapters.verification_runtime import _recover_user_contract
    state = RuntimeState()
    state.initial_task_text = "Build a report."
    messages = [{"role": "user", "content": text, "_nz_message_id": f"msg-{i}"} for i, text in enumerate(
        ["Build a report.", "Also create old-secret.md.", "Build a report."])]
    with pytest.raises(CheckpointRestoreError):
        _recover_user_contract(state, messages, session_id="s", workspace=tmp_path)


def test_full_user_followup_stays_in_canonical_contract_after_another_turn(tmp_path):
    state = RuntimeState()
    state.initial_task_text = "Build the service."
    state.set_acceptance_criteria_from_text(state.initial_task_text, workspace=tmp_path)
    followup = "Additional behavior: " + "long detail " * 500 + "FULL_FOLLOWUP_TAIL"
    state.apply_current_round_instruction(followup, workspace=tmp_path)
    state.apply_current_round_instruction("Continue.", workspace=tmp_path)
    assert followup in state.task_contract["objective"]


def test_legacy_runtime_without_domain_does_not_promote_model_contract(tmp_path):
    from nz_coder.runtime.adapters.verification_runtime import _restore_verification_domain
    state = RuntimeState()
    state.initial_task_text = "Acceptance command: `python real.py`."
    state.task_contract = TaskContract(acceptance_commands=("python forged.py",)).to_dict()
    state._restored_verification_domain = None
    _restore_verification_domain(state, session_id="s", workspace=tmp_path, interaction_run_id="new",
        agent_id="main", lock=threading.RLock(), version=WorkspaceVersion(), require_targeted=False,
        messages=[{"role": "user", "content": state.initial_task_text, "_nz_message_id": "msg-initial"}])
    assert state.task_contract["acceptance_commands"] == ["python real.py"]
