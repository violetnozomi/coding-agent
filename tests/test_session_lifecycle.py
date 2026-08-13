"""Session and child-worktree lifecycle tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nz_coder.runtime.workdir import scoped_workdir


def test_session_persistence_hardens_state_directories_and_final_json(tmp_path, monkeypatch):
    import nz_coder.state.sessions as sessions

    hardened = []
    monkeypatch.setattr(
        sessions,
        "harden_private_path",
        lambda path: hardened.append(Path(path)),
    )
    with scoped_workdir(tmp_path):
        path = sessions.save_session([], session_id="private", activate=False)

    state = tmp_path / ".nz-coder"
    assert state in hardened
    assert state / "sessions" in hardened
    assert path in hardened


def test_worktree_remove_deletes_only_one_managed_copy(tmp_path, monkeypatch):
    from nz_coder.runtime.worktree import WorktreeError, WorktreeManager

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("parent\n", encoding="utf-8")
    manager = WorktreeManager(workspace)
    monkeypatch.setattr(manager, "is_git_repo", lambda: False)
    worktree = manager.create("child")
    target = Path(worktree.path)

    assert target.exists()
    assert manager.remove(worktree) is True
    assert not target.exists()
    assert (workspace / "app.py").exists()

    with pytest.raises(WorktreeError, match="escapes"):
        manager.remove(tmp_path / "outside")
    with pytest.raises(WorktreeError, match="one managed child"):
        manager.remove(manager.worktree_dir)


def test_delete_session_removes_artifacts_and_child_worktree(tmp_path):
    from nz_coder.sessions import (
        delete_session,
        load_session,
        save_session,
        session_artifact_dir,
        session_subagent_dir,
    )

    with scoped_workdir(tmp_path):
        save_session([{"role": "user", "content": "old"}], session_id="old")
        child = tmp_path / ".nz-coder" / "worktrees" / "subagent-child"
        child.mkdir(parents=True)
        (child / "result.py").write_text("done\n", encoding="utf-8")
        state_dir = session_subagent_dir("old") / "subagent-child"
        state_dir.mkdir(parents=True)
        (state_dir / "state.json").write_text(
            json.dumps({
                "session_id": "subagent-child",
                "status": "completed",
                "worktree": {
                    "id": "subagent-child",
                    "path": str(child),
                    "branch": "",
                    "mode": "copy",
                },
            }),
            encoding="utf-8",
        )
        save_session([{"role": "user", "content": "keep"}], session_id="keep")

        assert delete_session("old") is True
        assert delete_session("old") is False
        assert load_session("old") == {}
        assert load_session("latest")["session_id"] == "keep"
        assert not session_artifact_dir("old").exists()
        assert not child.exists()


def test_delete_active_session_repairs_alias_to_latest_remaining(tmp_path):
    from nz_coder.sessions import (
        activate_session,
        active_session_id,
        delete_session,
        load_session,
        save_session,
    )

    with scoped_workdir(tmp_path):
        save_session([], session_id="newer", activate=False)
        save_session([], session_id="active-old", activate=True)
        # Make newer.json the newest remaining durable Session without changing
        # the active alias.
        newer = tmp_path / ".nz-coder" / "sessions" / "newer.json"
        newer.touch()
        activate_session("active-old")

        assert delete_session("active-old") is True
        assert active_session_id() == "newer"
        assert load_session("active")["session_id"] == "newer"


def test_delete_session_kills_owned_persistent_process(tmp_path):
    import shlex
    import sys

    from nz_coder.runtime.process_service import workspace_process_service
    from nz_coder.sessions import delete_session, save_session

    with scoped_workdir(tmp_path):
        save_session([], session_id="process-owner")
        service = workspace_process_service(tmp_path)
        handle = service.start(
            f"{shlex.quote(sys.executable)} -c 'import time; time.sleep(60)'",
            cwd=tmp_path,
            owner_session_id="process-owner",
            tty=False,
        )
        assert service.get(handle.process_id).status == "running"
        assert delete_session("process-owner") is True
        assert service.get(handle.process_id).status == "cancelled"
        assert service.list(active_only=True) == []


def test_delete_session_rejects_sanitized_alias_or_escape(tmp_path):
    from nz_coder.sessions import delete_session, load_session, save_session

    with scoped_workdir(tmp_path):
        save_session([], session_id="session")
        with pytest.raises(ValueError, match="exact persisted"):
            delete_session("../../session")
        with pytest.raises(ValueError, match="exact persisted"):
            delete_session("latest")
        assert load_session("session")["session_id"] == "session"


def test_session_uses_first_real_user_as_default_title_without_overwriting_rename(tmp_path):
    from nz_coder.sessions import load_session, rename_session, save_session

    with scoped_workdir(tmp_path):
        save_session([], session_id="session")
        assert load_session("session")["title"] == "New Session"

        save_session(
            [
                {"role": "user", "content": "<reminder>hidden</reminder>", "_nz_synthetic": True},
                {"role": "user", "content": "  Fix   parser\nedge cases  "},
            ],
            session_id="session",
        )
        assert load_session("session")["title"] == "Fix parser edge cases"

        rename_session("session", "Manual title")
        save_session(
            [{"role": "user", "content": "A newer prompt"}],
            session_id="session",
        )
        assert load_session("session")["title"] == "Manual title"


def test_session_fallback_title_matches_infcode_length_limit(tmp_path):
    from nz_coder.sessions import load_session, save_session

    with scoped_workdir(tmp_path):
        save_session(
            [{"role": "user", "content": "x" * 120}],
            session_id="session",
        )
        title = load_session("session")["title"]

    assert len(title) == 100
    assert title == "x" * 97 + "..."
