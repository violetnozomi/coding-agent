"""提案审阅使用同一SQLite事务和显式版本。"""
from nz_coder.state.memory import MemoryManager
from nz_coder.state.memory_control import MemoryControlPlane


def _control(tmp_path, capability="manage"):
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    control = MemoryControlPlane(manager, trusted_context={"capability": capability,
        "scope_id": "scope", "session_id": "session", "actor_id": "user",
        "operation_namespace": "scope:user:session"})
    return manager, control


def _command():
    return {"action": "create", "name": "规则", "type": "project", "description": "说明", "content": "明确内容"}


def test_submit_is_always_proposal_and_approval_is_transactional(tmp_path):
    manager, control = _control(tmp_path)
    proposed = control.submit(_command(), operation_id="submit")
    assert proposed["status"] == "pending_review"
    assert manager.list_records()["items"] == []
    proposal = control.get(proposed["proposal_id"])
    assert proposal["revision"] == 1
    accepted = control.approve(proposal["id"], expected_proposal_revision=1,
                               reviewer="user", operation_id="accept")
    assert accepted["status"] == "saved"
    assert control.pending() == []
    assert control.get(proposal["id"])["status"] == "applied"
    assert manager.read(accepted["memory_id"])["content"] == "明确内容"
    assert not (manager.memory_dir / "memory-control").exists()


def test_review_requires_displayed_proposal_and_target_versions(tmp_path):
    manager, control = _control(tmp_path)
    first = manager.mutate(_command(), trusted_context={"capability": "manage", "scope_id": "scope",
        "operation_namespace": "test", "operation_id": "create"})
    proposed = control.submit({"action": "update", "memory_id": first["memory_id"],
        "expected_revision": 1, "content": "候选"}, operation_id="submit")
    rejected = control.approve(proposed["proposal_id"], expected_proposal_revision=9,
        expected_revision=1, reviewer="user", operation_id="stale-proposal")
    assert rejected["status"] == "conflict"
    missing_target = control.approve(proposed["proposal_id"], expected_proposal_revision=1,
        reviewer="user", operation_id="missing-target")
    assert missing_target["status"] == "error"
    accepted = control.approve(proposed["proposal_id"], expected_proposal_revision=1,
        expected_revision=1, reviewer="user", operation_id="accept")
    assert accepted["status"] == "saved"
    assert manager.read(first["memory_id"])["revision"] == 2


def test_duplicate_operation_receipt_and_read_capability_are_preserved(tmp_path):
    manager, control = _control(tmp_path)
    first = control.submit(_command(), operation_id="same")
    assert control.submit(_command(), operation_id="same") == first
    assert control.submit({**_command(), "content": "另一内容"}, operation_id="same")["status"] == "operation_conflict"
    read = MemoryControlPlane(manager, trusted_context={"capability": "read", "scope_id": "scope",
        "session_id": "session", "operation_namespace": "read"})
    assert read.submit(_command(), operation_id="denied")["status"] == "denied"
    assert read.approve(first["proposal_id"], expected_proposal_revision=1,
        reviewer="user", operation_id="denied-accept")["status"] == "denied"
    rejected = control.reject(first["proposal_id"], expected_proposal_revision=1,
        reviewer="user", reason="不适用", operation_id="reject")
    assert rejected["status"] == "saved"
    assert manager.list_records()["items"] == []
def test_rejection_reason_is_persisted_in_same_decision_transaction(tmp_path):
    from nz_coder.state.memory import MemoryManager
    from nz_coder.state.memory_control import MemoryControlPlane
    manager = MemoryManager(tmp_path)
    control = MemoryControlPlane(manager)
    candidate = control.submit({"action": "create", "name": "candidate", "content": "a note"},
                               operation_id="proposal-reason", source_session="child")
    rejected = control.reject(candidate["proposal_id"], expected_proposal_revision=1,
                              operation_id="reject-reason", reviewer="user", reason="does not apply to this project")
    assert rejected["status"] == "saved"
    assert any(event.get("reason") == "does not apply to this project" for event in control.ledger())
