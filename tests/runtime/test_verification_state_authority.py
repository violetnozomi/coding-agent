"""各模块只能读域投影，不能再独立写入通过和需求满足状态。"""
from __future__ import annotations

import json

import pytest

from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.verification.domain.models import RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _state(root):
    request = RunRequest(agent=AgentDefinition(name="main", instructions="test"), profile=MAIN_PROFILE,
        workspace=root, session_id="authority", messages=())
    session = Session.create("authority", [], workspace=root)
    context = RunContext(request, session, "main")
    scope = RunScope(TaskScope("authority", "task", str(root)), context.interaction_run_id, "main")
    domain = VerificationDomain(lock=context._state_lock, scope=scope, version=WorkspaceVersion())
    context.attach_verification(domain)
    state = RuntimeState()
    state.bind_run_context(context)
    return state, context, domain


@pytest.mark.parametrize("name,value", [
    ("py_compile_ok", True), ("changed_files_verified", True), ("verification_generation", 4),
    ("last_verification_failure", "clear failure"), ("requirement_ledger", {"items": []}),
    ("verification_contract", {"passed": True}),
])
def test_runtime_verification_views_reject_independent_writes(tmp_path, name, value):
    state, _context, _domain = _state(tmp_path)
    with pytest.raises(AttributeError):
        setattr(state, name, value)


def test_runtime_snapshot_has_only_one_restorable_verification_state(tmp_path):
    state, _context, domain = _state(tmp_path)
    target = tmp_path / "runtime_state.json"
    state.save(target)
    saved = json.loads(target.read_text())
    assert saved["verification_domain"] == domain.checkpoint()
    assert "verification_definition" in saved
    assert not {"py_compile_ok", "changed_files_verified", "verification_generation", "last_verification_failure",
                "requirement_ledger", "verification_contract", "verification_checkpoint"}.intersection(saved)


def test_context_cannot_attach_a_second_domain(tmp_path):
    _state_object, context, domain = _state(tmp_path)
    with pytest.raises(RuntimeError, match="already"):
        context.attach_verification(domain)


def test_handoff_does_not_publish_over_new_authority_during_disk_write(monkeypatch, tmp_path):
    state, context, _domain = _state(tmp_path)
    target = tmp_path / "runtime_state.json"
    original = state._write_snapshot
    writes = []

    def write(path, snapshot):
        writes.append(snapshot)
        original(path, snapshot)
        if len(writes) == 1:
            with context._state_lock:
                state.task_authority_epoch += 1
                state.task_authority_message_id = "new-instruction"

    monkeypatch.setattr(state, "_write_snapshot", write)
    with pytest.raises(ValueError, match="authority"):
        state.commit_execution_handoff({"kind": "edit", "action": "old instruction"}, target)
    assert state.execution_handoff == {}
    saved = json.loads(target.read_text())
    assert saved["task_authority_epoch"] == 1
    assert saved["execution_handoff"] == {}
    assert "verification_domain" in saved


def test_context_finish_cannot_publish_completed_with_pending_required_check(tmp_path):
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import CheckSpec

    _state_object, context, domain = _state(tmp_path)
    domain.register_check(CheckSpec("required", command_identity("pytest test_a.py", str(tmp_path)), "test",
                                    required=True, source="user_declared"))
    with pytest.raises(ValueError, match="unconfirmed"):
        context.finish(RunStatus.COMPLETED)
    assert not context.finalized
    assert context.terminal_status is None


def test_public_state_does_not_embed_private_restoration_checkpoint(tmp_path):
    state, _context, _domain = _state(tmp_path)
    public = state.to_dict()
    assert "verification_domain" not in public
    assert "verification_contract" in public
