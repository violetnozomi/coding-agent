"""Checkpoint receipts at real controlled file tool boundaries."""
from __future__ import annotations

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.state.workdir import scoped_workdir
from nz_coder.state.tool_ledger import ToolLedger


def execution(ledger, tool="write_file", call="call-a"):
    return ledger.register(session_id="session-a", interaction_id="interaction-a",
                           assistant_step_id="msg-a", agent_id="agent-a",
                           call_id=call, tool=tool, tool_input={"path": "a.txt"})


def test_actual_write_boundary_preserves_binary_before_and_deleted_state(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution

    (tmp_path / "a.txt").write_bytes(b"\xff\x00\r\n")
    ledger = ToolLedger(tmp_path)
    record = execution(ledger)
    access = WorkspaceFileAccess(tmp_path)
    with checkpoint_execution(ledger, record["execution_id"]), scoped_workdir(tmp_path):
        access.write_text("a.txt", "after")
        access.delete("a.txt")
    mutations = ledger.mutations("session-a")
    assert len(mutations) == 2
    assert ledger.state_bytes(mutations[0]["before_ref"]) == b"\xff\x00\r\n"
    assert ledger.state_bytes(mutations[0]["after_ref"]) == b"after"
    assert mutations[1]["before_ref"] == mutations[0]["after_ref"]
    assert ledger.state_bytes(mutations[1]["after_ref"]) is None


def test_before_persistence_failure_prevents_real_write(tmp_path, monkeypatch):
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution

    ledger = ToolLedger(tmp_path)
    record = execution(ledger)
    (tmp_path / "a.txt").write_text("original")

    def full(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "prepare_mutation", full)
    with checkpoint_execution(ledger, record["execution_id"]), pytest.raises(OSError):
        WorkspaceFileAccess(tmp_path).write_text("a.txt", "replacement")
    assert (tmp_path / "a.txt").read_text() == "original"


def test_after_persistence_failure_leaves_recognizable_uncertainty(tmp_path, monkeypatch):
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution

    ledger = ToolLedger(tmp_path)
    record = execution(ledger)

    def full(*args, **kwargs):
        raise OSError("disk full after write")

    monkeypatch.setattr(ledger, "confirm_mutation", full)
    with checkpoint_execution(ledger, record["execution_id"]), pytest.raises(OSError):
        WorkspaceFileAccess(tmp_path).write_text("a.txt", "written")
    assert (tmp_path / "a.txt").read_text() == "written"
    mutation, = ToolLedger(tmp_path).mutations("session-a")
    assert mutation["status"] in {"prepared", "uncertain"}
    assert mutation["after_ref"] is None
    assert mutation["disposition"] == "unknown"


@pytest.mark.parametrize("tool,args", [
    ("write_file", {"path": "a.txt", "content": "replacement"}),
    ("edit_file", {"path": "a.txt", "old_text": "original", "new_text": "replacement"}),
    ("write_files_batch", {"files": [{"path": "a.txt", "content": "replacement"}, {"path": "b.txt", "content": "new"}], "overwrite": True}),
    ("apply_patch", {"changes": [{"path": "a.txt", "old_text": "original", "new_text": "replacement"}]}),
])
def test_registered_file_tools_reach_ledger(tmp_path, tool, args):
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
    from nz_coder.tools import dispatch
    from nz_coder.tools import files  # noqa: F401

    ledger = ToolLedger(tmp_path)
    record = execution(ledger, tool)
    (tmp_path / "a.txt").write_text("original\n")
    with scoped_workdir(tmp_path), checkpoint_execution(ledger, record["execution_id"]):
        output = dispatch(tool, args)
    assert not str(output).startswith("Error:"), str(output)
    assert (tmp_path / "a.txt").read_text().strip() == "replacement"
    mutations = ledger.mutations("session-a")
    assert len(mutations) == (2 if tool == "write_files_batch" else 1)
    assert {m["status"] for m in mutations} == {"confirmed"}


def test_transaction_compensation_updates_same_mutation_authority(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
    from nz_coder.state.transaction import TransactionManager

    ledger = ToolLedger(tmp_path)
    record = execution(ledger)
    with scoped_workdir(tmp_path), checkpoint_execution(ledger, record["execution_id"]):
        txn = TransactionManager()
        txn.begin()
        access = WorkspaceFileAccess(tmp_path)
        access.write_text("a.txt", "first", transaction=txn)
        access.write_text("a.txt", "second", transaction=txn)
        txn.rollback()
    assert not (tmp_path / "a.txt").exists()
    assert [m["disposition"] for m in ledger.mutations("session-a")] == ["compensated", "compensated"]


@pytest.mark.parametrize("interleaved", [False, True])
def test_transaction_compensation_refuses_foreign_file_changes(tmp_path, interleaved):
    from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
    from nz_coder.state.transaction import TransactionManager

    ledger = ToolLedger(tmp_path)
    record = execution(ledger)
    target = tmp_path / "a.txt"
    target.write_text("base")
    with scoped_workdir(tmp_path), checkpoint_execution(ledger, record["execution_id"]):
        txn = TransactionManager()
        txn.begin()
        access = WorkspaceFileAccess(tmp_path)
        access.write_text("a.txt", "agent first", transaction=txn)
        target.write_text("user change")
        if interleaved:
            access.write_text("a.txt", "agent second", transaction=txn)
        before = target.read_bytes()
        result = txn.rollback()
    assert target.read_bytes() == before
    assert "failed" in result and txn.state == "rollback_partial"
    assert all(m["disposition"] != "compensated" for m in ledger.mutations("session-a"))


def test_compensation_metadata_retry_does_not_restore_twice(tmp_path, monkeypatch):
    from nz_coder.runtime.process import checkpoint_runtime
    from nz_coder.state.transaction import TransactionManager

    ledger = ToolLedger(tmp_path)
    record = execution(ledger)
    target = tmp_path / "a.txt"
    target.write_text("base")
    with scoped_workdir(tmp_path), checkpoint_runtime.checkpoint_execution(ledger, record["execution_id"]):
        txn = TransactionManager()
        txn.begin()
        WorkspaceFileAccess(tmp_path).write_text("a.txt", "after", transaction=txn)
        with monkeypatch.context() as patch:
            def fail(*args):
                raise OSError("transient ledger failure")
            patch.setattr(ledger, "record_compensation", fail)
            assert "failed" in txn.rollback()
        assert target.read_text() == "base" and txn.state == "rollback_partial"
        monkeypatch.setattr(txn, "_restore_backup", lambda *args: pytest.fail("restoration must not run twice"))
        txn.rollback()
        assert txn.state == "rolled_back"
    assert ledger.mutations("session-a")[0]["disposition"] == "compensated"
