"""Tests for message-level Session workspace revert and unrevert."""
from __future__ import annotations

import pytest

from nz_coder.message_schema import attach_message_identity
from nz_coder.runtime.session_processor import SessionProcessor
from nz_coder.runtime.session_revert import SessionReverter
from nz_coder.runtime.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore


def _history(tmp_path):
    store = WorkspaceSnapshotStore(tmp_path, tmp_path / ".nz-coder" / "snapshots")
    app = tmp_path / "app.py"
    app.write_text("before\n", encoding="utf-8")
    start = store.track()

    user = {"role": "user", "content": "change app"}
    attach_message_identity(user, "msg-user", session_id="session-a")
    assistant = {"role": "assistant", "content": "done"}
    attach_message_identity(assistant, "msg-assistant", session_id="session-a")
    processor = SessionProcessor(assistant)
    processor.start_step(start)
    app.write_text("after\n", encoding="utf-8")
    finish = store.track()
    processor.finish_step("stop", snapshot=finish)
    return store, app, [user, assistant]


def test_message_revert_and_unrevert_restore_workspace_and_history(tmp_path):
    store, app, messages = _history(tmp_path)
    original = [dict(message) for message in messages]
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json")

    undone = reverter.revert(messages)

    assert undone.message_id == "msg-user"
    assert undone.files == ("app.py",)
    assert messages == []
    assert app.read_text(encoding="utf-8") == "before\n"

    redone = reverter.unrevert(messages)

    assert redone.files == ("app.py",)
    assert messages == original
    assert app.read_text(encoding="utf-8") == "after\n"


def test_message_revert_refuses_later_edit_without_truncating_history(tmp_path):
    store, app, messages = _history(tmp_path)
    original = list(messages)
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json")
    app.write_text("user edit\n", encoding="utf-8")

    with pytest.raises(SnapshotError, match="workspace changed"):
        reverter.revert(messages)

    assert messages == original
    assert app.read_text(encoding="utf-8") == "user edit\n"
    assert not reverter.state_path.exists()


def test_unrevert_refuses_after_conversation_advances(tmp_path):
    store, _, messages = _history(tmp_path)
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json")
    reverter.revert(messages)
    messages.append({"role": "user", "content": "new task"})

    with pytest.raises(SnapshotError, match="conversation advanced"):
        reverter.unrevert(messages)


def test_revert_persistence_failure_restores_workspace_and_history(tmp_path, monkeypatch):
    store, app, messages = _history(tmp_path)
    original = list(messages)
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json")

    def fail_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        "nz_coder.runtime.session_revert.write_session_runtime_json",
        fail_write,
    )

    with pytest.raises(SnapshotError, match="transition rolled back"):
        reverter.revert(messages)

    assert messages == original
    assert app.read_text(encoding="utf-8") == "after\n"
