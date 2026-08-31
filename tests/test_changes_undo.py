"""Tests for session-scoped, reversible change-set undo and redo."""
from __future__ import annotations

import os

import pytest

from nz_coder.state.changes import ChangeTracker, revert_change_file
from nz_coder.runtime.process.workdir import scoped_workdir


def _record_change(
    tracker: ChangeTracker,
    path,
    before: str,
    after: str,
) -> None:
    target = path / "app.py"
    tracker.record_before("app.py", target.exists(), before)
    target.write_text(after, encoding="utf-8")
    tracker.record_after("app.py", True, after)


def test_current_snapshot_uses_disk_state_after_external_rollback(tmp_path):
    change_dir = tmp_path / ".nz-coder" / "runtime" / "changes"
    with scoped_workdir(tmp_path):
        target = tmp_path / "app.py"
        target.write_text("before\n", encoding="utf-8")
        tracker = ChangeTracker(run_id="risk-snapshot", change_dir=change_dir)
        tracker.record_before("app.py", True, "before\n")
        target.write_text("after\n", encoding="utf-8")
        tracker.record_after("app.py", True, "after\n")

        assert tracker.current_changed_paths() == ["app.py"]
        assert tracker.current_deleted_paths() == []
        assert "+after" in tracker.render_current_diff()

        target.write_text("before\n", encoding="utf-8")

        assert tracker.changed_paths() == ["app.py"]
        assert tracker.current_changed_paths() == []
        assert tracker.current_deleted_paths() == []
        assert "+after" not in tracker.render_current_diff()


def test_current_snapshot_reports_deleted_files_without_git(tmp_path):
    change_dir = tmp_path / ".nz-coder" / "runtime" / "changes"
    with scoped_workdir(tmp_path):
        target = tmp_path / "app.py"
        target.write_text("before\n", encoding="utf-8")
        tracker = ChangeTracker(run_id="deleted-snapshot", change_dir=change_dir)
        tracker.record_before("app.py", True, "before\n")
        target.unlink()
        tracker.record_after("app.py", False, "")

        assert tracker.current_changed_paths() == ["app.py"]
        assert tracker.current_deleted_paths() == ["app.py"]


def test_change_manifest_replace_failure_preserves_last_checkpoint(
    tmp_path,
    monkeypatch,
):
    import nz_coder.state.sessions as sessions

    change_dir = tmp_path / ".nz-coder" / "runtime" / "changes"
    with scoped_workdir(tmp_path):
        tracker = ChangeTracker(run_id="atomic", change_dir=change_dir)
        tracker.record_before("app.py", True, "before\n")
        prior = tracker.path.read_text(encoding="utf-8")
        monkeypatch.setattr(
            sessions.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("commit failed")),
        )

        with pytest.raises(OSError, match="commit failed"):
            tracker.record_after("app.py", True, "after\n")

    assert tracker.path.read_text(encoding="utf-8") == prior
    assert not list(change_dir.glob(".atomic.json.*.tmp"))


def test_multi_level_undo_redo_restores_files_and_history(tmp_path):
    change_dir = tmp_path / ".nz-coder" / "runtime" / "changes"
    history = [
        {"role": "user", "content": "turn one"},
        {"role": "assistant", "content": "done one"},
        {"role": "user", "content": "turn two"},
        {"role": "assistant", "content": "done two"},
        {"role": "user", "content": "<reminder>save memory</reminder>"},
    ]
    original_history = list(history)

    with scoped_workdir(tmp_path):
        app = tmp_path / "app.py"
        app.write_text("zero\n", encoding="utf-8")

        first = ChangeTracker(run_id="turn-1", change_dir=change_dir)
        first.history_start = 0
        _record_change(first, tmp_path, "zero\n", "one\n")
        os.utime(first.path, (1, 1))

        second = ChangeTracker(run_id="turn-2", change_dir=change_dir)
        second.history_start = 2
        _record_change(second, tmp_path, "one\n", "two\n")
        os.utime(second.path, (2, 2))

        assert second.undo(history).startswith("Undid agent changes:")
        assert app.read_text(encoding="utf-8") == "one\n"
        assert history == original_history[:2]

        assert second.undo(history).startswith("Undid agent changes:")
        assert app.read_text(encoding="utf-8") == "zero\n"
        assert history == []

        assert second.redo(history).startswith("Redid agent changes:")
        assert app.read_text(encoding="utf-8") == "one\n"
        assert history == original_history[:2]

        assert second.redo(history).startswith("Redid agent changes:")
        assert app.read_text(encoding="utf-8") == "two\n"
        assert history == original_history


def test_revert_preflight_prevents_partial_multi_file_rollback(tmp_path):
    with scoped_workdir(tmp_path):
        first = tmp_path / "first.py"
        second = tmp_path / "second.py"
        first.write_text("before-1\n", encoding="utf-8")
        second.write_text("before-2\n", encoding="utf-8")
        tracker = ChangeTracker(change_dir=tmp_path / "changes")
        tracker.record_before("first.py", True, "before-1\n")
        tracker.record_before("second.py", True, "before-2\n")
        first.write_text("after-1\n", encoding="utf-8")
        second.write_text("after-2\n", encoding="utf-8")
        tracker.record_after("first.py", True, "after-1\n")
        tracker.record_after("second.py", True, "after-2\n")

        second.write_text("user-edit\n", encoding="utf-8")
        result = revert_change_file(tracker.path)

        assert result.startswith("Refused to revert:")
        assert first.read_text(encoding="utf-8") == "after-1\n"
        assert second.read_text(encoding="utf-8") == "user-edit\n"


def test_redo_refuses_when_conversation_advanced_after_undo(tmp_path):
    change_dir = tmp_path / ".nz-coder" / "runtime" / "changes"
    history = [
        {"role": "user", "content": "edit"},
        {"role": "assistant", "content": "done"},
    ]

    with scoped_workdir(tmp_path):
        app = tmp_path / "app.py"
        app.write_text("before\n", encoding="utf-8")
        tracker = ChangeTracker(change_dir=change_dir)
        _record_change(tracker, tmp_path, "before\n", "after\n")

        assert tracker.undo(history).startswith("Undid agent changes:")
        history.append({"role": "user", "content": "new topic"})

        result = tracker.redo(history)

        assert result.startswith("Refused to redo:")
        assert app.read_text(encoding="utf-8") == "before\n"


def test_new_change_set_invalidates_redo_stack(tmp_path):
    change_dir = tmp_path / ".nz-coder" / "runtime" / "changes"
    history = [{"role": "user", "content": "edit"}]

    with scoped_workdir(tmp_path):
        app = tmp_path / "app.py"
        app.write_text("before\n", encoding="utf-8")
        first = ChangeTracker(run_id="first", change_dir=change_dir)
        _record_change(first, tmp_path, "before\n", "after\n")
        assert first.undo(history).startswith("Undid agent changes:")

        second = ChangeTracker(run_id="second", change_dir=change_dir)
        _record_change(second, tmp_path, "before\n", "replacement\n")

        assert second.redo(history) == "No agent change set available to redo."


def test_agent_rotates_owned_change_tracker_after_modifying_turn(tmp_path):
    from nz_coder.runtime.execution.loop import AgentLoop

    with scoped_workdir(tmp_path):
        change_dir = tmp_path / ".nz-coder" / "sessions" / "_artifacts" / "s1" / "runtime" / "changes"
        app = tmp_path / "app.py"
        app.write_text("before\n", encoding="utf-8")
        tracker = ChangeTracker(run_id="turn-one", change_dir=change_dir)
        _record_change(tracker, tmp_path, "before\n", "after\n")

        agent = AgentLoop.__new__(AgentLoop)
        agent._owns_change_tracker = True
        agent.change_tracker = tracker
        agent.session_id = "s1"

        agent._rotate_change_tracker_if_needed()

        assert agent.change_tracker is not tracker
        assert agent.change_tracker.change_dir == change_dir
        assert agent.change_tracker.changed_paths() == []
