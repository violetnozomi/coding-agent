"""Owned-file Undo/Redo and durable cross-process recovery behavior."""
from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
from nz_coder.runtime.process.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore
from nz_coder.runtime.session.session_revert import SessionReverter
from nz_coder.state.sessions import load_session, save_session
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir


def history():
    return [{"role": "user", "content": "Edit and verify", "_nz_message_id": "msg-user", "_nz_session_id": "session-a"},
            {"role": "assistant", "content": "Changes recorded", "_nz_message_id": "msg-step", "_nz_session_id": "session-a"}]


def edit(ledger, path, data, *, call="call-a", step="msg-step", session="session-a"):
    execution = ledger.register(session_id=session, interaction_id="interaction-a", assistant_step_id=step,
                                agent_id="agent-a", call_id=call, tool="apply_patch", tool_input={"path": path})
    with checkpoint_execution(ledger, execution["execution_id"]):
        access = WorkspaceFileAccess(ledger.workspace)
        if data is None:
            access.delete(path)
        else:
            access.write_bytes(path, data)


def reverter(root):
    return SessionReverter(WorkspaceSnapshotStore(root, root / ".nz-coder" / "snapshots"),
                           root / ".nz-coder" / "message_revert.json", session_id="session-a")


def test_undo_only_owned_paths_then_restart_redo_preserves_exact_bytes(tmp_path):
    ledger = ToolLedger(tmp_path)
    (tmp_path / "a.bin").write_bytes(b"\xff\x00\r\n")
    edit(ledger, "a.bin", b"first")
    edit(ledger, "a.bin", b"second", call="call-b")
    edit(ledger, "new.txt", b"", call="call-new")
    (tmp_path / "user.txt").write_bytes(b"user modification")
    messages = history()
    undone = reverter(tmp_path).revert(messages)
    assert undone.status == "completed" and not undone.recovery_required
    assert undone.files == ("a.bin", "new.txt")
    assert (tmp_path / "a.bin").read_bytes() == b"\xff\x00\r\n"
    assert not (tmp_path / "new.txt").exists()
    assert (tmp_path / "user.txt").read_bytes() == b"user modification"
    assert messages == []
    redone = reverter(tmp_path).unrevert(messages)
    assert redone.status == "completed"
    assert (tmp_path / "a.bin").read_bytes() == b"second"
    assert (tmp_path / "new.txt").read_bytes() == b""
    assert [m["content"] for m in messages] == [m["content"] for m in history()]


def test_undo_delete_and_permissions(tmp_path):
    ledger = ToolLedger(tmp_path)
    target = tmp_path / "executable.bin"
    target.write_bytes(b"\x00\xff")
    target.chmod(0o755)
    edit(ledger, "executable.bin", None)
    messages = history()
    reverter(tmp_path).revert(messages)
    assert target.read_bytes() == b"\x00\xff"
    if __import__("os").name != "nt":
        assert target.stat().st_mode & 0o777 == 0o755
    reverter(tmp_path).unrevert(messages)
    assert not target.exists()


@pytest.mark.parametrize("other_session", [False, True])
def test_interleaved_user_or_other_agent_change_is_not_erased(tmp_path, other_session):
    ledger = ToolLedger(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"base")
    edit(ledger, "a.txt", b"agent first")
    if other_session:
        edit(ledger, "a.txt", b"other agent", session="session-b", call="call-other")
    else:
        (tmp_path / "a.txt").write_bytes(b"user middle")
    edit(ledger, "a.txt", b"agent second", call="call-second")
    messages = history()
    with pytest.raises(SnapshotError, match="conflict"):
        reverter(tmp_path).revert(messages)
    assert (tmp_path / "a.txt").read_bytes() == b"agent second"
    assert messages == history()


def test_redo_refuses_new_user_edit_and_keeps_recovery_record(tmp_path):
    ledger = ToolLedger(tmp_path)
    edit(ledger, "a.txt", b"agent")
    messages = history()
    reverter(tmp_path).revert(messages)
    (tmp_path / "a.txt").write_bytes(b"user after undo")
    with pytest.raises(SnapshotError, match="conflict"):
        reverter(tmp_path).unrevert(messages)
    assert (tmp_path / "a.txt").read_bytes() == b"user after undo"
    assert messages == []


def test_legacy_snapshot_history_does_not_forge_ownership(tmp_path):
    target = tmp_path / "user.txt"
    target.write_bytes(b"user")
    with pytest.raises(SnapshotError, match="ownership|owned"):
        reverter(tmp_path).revert(history())
    assert target.read_bytes() == b"user"


def test_partial_file_failure_records_progress_and_blocks_new_writes(tmp_path, monkeypatch):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal

    ledger = ToolLedger(tmp_path)
    for path in ("a.txt", "b.txt"):
        edit(ledger, path, b"agent", call="call-" + path)
    original = RecoveryJournal._apply_file

    def fail_second(self, item):
        if item["path"] == "b.txt":
            raise OSError("test failure")
        return original(self, item)

    messages = history()
    with monkeypatch.context() as patch:
        patch.setattr(RecoveryJournal, "_apply_file", fail_second)
        with pytest.raises(SnapshotError, match="recovery"):
            reverter(tmp_path).revert(messages)
    assert not (tmp_path / "a.txt").exists() and (tmp_path / "b.txt").exists()
    assert messages == history()
    with pytest.raises(SnapshotError, match="recovery"):
        edit(ledger, "c.txt", b"must not write", call="call-c")
    assert not (tmp_path / "c.txt").exists()
    result = reverter(tmp_path).recover(messages)
    assert result.status == "completed" and messages == []
    assert not (tmp_path / "b.txt").exists()


@pytest.mark.parametrize("after_commit", [False, True])
def test_history_failure_reconciles_idempotently(tmp_path, monkeypatch, after_commit):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal

    edit(ToolLedger(tmp_path), "a.txt", b"written")
    messages = history()
    original = RecoveryJournal._commit_history

    def fail(self, operation):
        if after_commit:
            original(self, operation)
        raise OSError("history failure")

    with monkeypatch.context() as patch:
        patch.setattr(RecoveryJournal, "_commit_history", fail)
        if after_commit:
            reverter(tmp_path).revert(messages)
        else:
            with pytest.raises(SnapshotError, match="recovery"):
                reverter(tmp_path).revert(messages)
    assert not (tmp_path / "a.txt").exists()
    reverter(tmp_path).recover(messages)
    assert messages == []
    reverter(tmp_path).unrevert(messages)
    assert len(messages) == 2 and (tmp_path / "a.txt").read_bytes() == b"written"


def _crash_worker(root, phase, direction, connection):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal

    root = Path(root)
    with scoped_workdir(root):
        messages = load_session("session-a")["messages"]
    if phase == "prepared":
        def pause(self, operation, messages):
            connection.send("prepared")
            connection.recv()

        RecoveryJournal.apply = pause
    elif phase == "file":
        original = RecoveryJournal._apply_file

        def pause(self, item):
            original(self, item)
            connection.send("file-written")
            connection.recv()

        RecoveryJournal._apply_file = pause
    else:
        original = RecoveryJournal._commit_history

        def pause(self, operation):
            if phase == "after-history":
                original(self, operation)
            connection.send(phase)
            connection.recv()

        RecoveryJournal._commit_history = pause
    (reverter(root).revert if direction == "undo" else reverter(root).unrevert)(messages)


def _restart_worker(root, direction, connection):
    root = Path(root)
    with scoped_workdir(root):
        restored = load_session("session-a")["messages"]
    result = reverter(root).recover(restored)
    repeated = reverter(root).recover(restored)
    connection.send((result.status, restored, repeated, (root / "a.txt").exists()))
    (reverter(root).unrevert if direction == "undo" else reverter(root).revert)(restored)
    connection.send((len(restored), (root / "a.txt").read_bytes() if (root / "a.txt").exists() else None))


@pytest.mark.parametrize("direction", ["undo", "redo"])
@pytest.mark.parametrize("phase", ["prepared", "file", "before-history", "after-history"])
def test_killed_process_recovery_uses_source_target_evidence(tmp_path, phase, direction):
    ledger = ToolLedger(tmp_path)
    edit(ledger, "a.txt", b"written")
    messages = history()
    with scoped_workdir(tmp_path):
        save_session(messages, session_id="session-a", activate=False)
    if direction == "redo":
        reverter(tmp_path).revert(messages)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_crash_worker, args=(str(tmp_path), phase, direction, child))
    process.start()
    try:
        assert parent.poll(20), "child did not reach the explicit crash boundary"
        assert parent.recv() in {"prepared", "file-written", "before-history", "after-history"}
        process.kill()
        process.join(10)
        assert process.exitcode != 0
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()
    parent, child = context.Pipe()
    restarted = context.Process(target=_restart_worker, args=(str(tmp_path), direction, child))
    restarted.start()
    try:
        assert parent.poll(20), "fresh process did not recover"
        status, recovered, repeated, exists = parent.recv()
        assert status == "completed" and repeated is None
        assert recovered == ([] if direction == "undo" else history())
        assert exists == (direction == "redo")
        assert parent.poll(20)
        assert parent.recv() == ((2, b"written") if direction == "undo" else (0, None))
        restarted.join(10)
        assert restarted.exitcode == 0
    finally:
        if restarted.is_alive():
            restarted.kill()
            restarted.join(10)
        parent.close()
        child.close()


def test_recovered_verification_cannot_be_restored_as_current(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.state.sessions import session_runtime_state_path, write_session_runtime_json

    edit(ToolLedger(tmp_path), "a.txt", b"written")
    with scoped_workdir(tmp_path):
        path = session_runtime_state_path("session-a")
        write_session_runtime_json(path, {"active": True, "verification_attempts": 5})
    reverter(tmp_path).revert(history())
    state = RuntimeState()
    assert state.load(path, allow_inactive=True) is False
    assert state.verification_attempts == 0


def test_undo_refreshes_live_code_index_without_watcher(tmp_path):
    from nz_coder.intelligence.service import workspace_repo_intelligence, release_repo_intelligence

    (tmp_path / "a.py").write_text("def before(): pass\n")
    edit(ToolLedger(tmp_path), "a.py", b"def after(): pass\n")
    service = workspace_repo_intelligence(tmp_path)
    try:
        assert service.wait_ready(5).status == "ready"
        assert {s["name"] for s in service.index.file_symbols("a.py")} == {"after"}
        generation = service.state.generation
        reverter(tmp_path).revert(history())
        assert service.state.generation > generation
        assert {s["name"] for s in service.index.file_symbols("a.py")} == {"before"}
    finally:
        release_repo_intelligence(tmp_path)


def test_pending_recovery_neither_source_nor_target_preserves_conflict(tmp_path, monkeypatch):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run

    ledger = ToolLedger(tmp_path)
    edit(ledger, "a.txt", b"written")
    messages = history()
    with monkeypatch.context() as patch:
        def fail(*args):
            raise OSError("before history")
        patch.setattr(RecoveryJournal, "_commit_history", fail)
        with pytest.raises(SnapshotError):
            reverter(tmp_path).revert(messages)
    (tmp_path / "a.txt").write_bytes(b"external edit")
    with pytest.raises(SnapshotError) as error:
        reverter(tmp_path).recover(messages)
    assert error.value.result.conflicts == ("a.txt",)
    assert error.value.result.recovery_required and error.value.result.operation_id
    assert (tmp_path / "a.txt").read_bytes() == b"external edit"
    assert messages == history()
    with pytest.raises(SnapshotError, match="recovery"):
        with recovery_run(tmp_path, "session-b"):
            pytest.fail("new run must not start while recovery is unresolved")


def test_new_run_invalidates_redo_for_all_native_entry_paths(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run

    edit(ToolLedger(tmp_path), "a.txt", b"written")
    messages = history()
    reverter(tmp_path).revert(messages)
    with recovery_run(tmp_path, "session-a"):
        pass
    with pytest.raises(SnapshotError, match="invalidated"):
        reverter(tmp_path).unrevert(messages)
    assert not (tmp_path / "a.txt").exists()


def test_undo_refuses_stale_in_memory_history_before_any_file_change(tmp_path):
    edit(ToolLedger(tmp_path), "a.txt", b"written")
    messages = history()
    advanced = [*messages, {"role": "user", "content": "New durable instruction", "_nz_message_id": "msg-new"}]
    with scoped_workdir(tmp_path):
        save_session(advanced, session_id="session-a", activate=False)
    with pytest.raises(SnapshotError, match="history"):
        reverter(tmp_path).revert(messages)
    assert (tmp_path / "a.txt").read_bytes() == b"written"
    with scoped_workdir(tmp_path):
        assert load_session("session-a")["messages"] == advanced


def test_redo_refuses_foreign_owned_changes_even_when_bytes_return_to_source(tmp_path):
    ledger = ToolLedger(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"base")
    edit(ledger, "a.txt", b"written")
    messages = history()
    reverter(tmp_path).revert(messages)
    edit(ledger, "a.txt", b"other intermediate", session="session-b", call="other-1")
    edit(ledger, "a.txt", b"base", session="session-b", call="other-2")
    with pytest.raises(SnapshotError, match="conflict"):
        reverter(tmp_path).unrevert(messages)
    assert (tmp_path / "a.txt").read_bytes() == b"base" and messages == []
