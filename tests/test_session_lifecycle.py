"""Session and child-worktree lifecycle tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir


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
        state = sessions.session_dir()

    assert state in hardened
    assert path in hardened


def test_session_persistence_repairs_nonfinite_extension_metadata(tmp_path):
    from nz_coder.state.sessions import load_session, save_session

    messages = [{
        "role": "assistant",
        "content": "done",
        "_nz_extension": {"score": float("nan"), "latency": float("inf")},
    }]
    with scoped_workdir(tmp_path):
        path = save_session(messages, session_id="strict-json", activate=False)
        restored = load_session("strict-json")

    assert restored["messages"][0]["_nz_extension"] == {
        "score": None,
        "latency": None,
    }
    json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


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


def test_worktree_create_rejects_managed_child_symlink(tmp_path, monkeypatch):
    from nz_coder.runtime.worktree import WorktreeError, WorktreeManager

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    manager = WorktreeManager(workspace)
    manager.worktree_dir.mkdir(parents=True)
    (manager.worktree_dir / "child").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(manager, "is_git_repo", lambda: False)

    with pytest.raises(WorktreeError, match="symbolic link"):
        manager.create("child")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("base_ref", ["--detach", "HEAD\n--force", "", "a" * 501])
def test_worktree_create_rejects_unsafe_base_ref(tmp_path, base_ref):
    from nz_coder.runtime.worktree import WorktreeError, WorktreeManager

    with pytest.raises(WorktreeError, match="base ref"):
        WorktreeManager(tmp_path).create("child", base_ref)


def test_delete_session_removes_artifacts_and_child_worktree(tmp_path):
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import (
        load_session,
        save_session,
        session_artifact_dir,
        session_subagent_dir,
    )
    from nz_coder.runtime.worktree import WorktreeManager

    with scoped_workdir(tmp_path):
        save_session([{"role": "user", "content": "old"}], session_id="old")
        child = WorktreeManager(tmp_path).worktree_dir / "subagent-child"
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
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import (
        activate_session,
        active_session_id,
        load_session,
        save_session,
        session_dir,
    )

    with scoped_workdir(tmp_path):
        save_session([], session_id="newer", activate=False)
        save_session([], session_id="active-old", activate=True)
        # Make newer.json the newest remaining durable Session without changing
        # the active alias.
        newer = session_dir() / "newer.json"
        newer.touch()
        activate_session("active-old")

        assert delete_session("active-old") is True
        assert active_session_id() == "newer"
        assert load_session("active")["session_id"] == "newer"


def test_delete_session_kills_owned_persistent_process(tmp_path):
    import shlex
    import sys

    from nz_coder.runtime.process.process_service import workspace_process_service
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import save_session

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
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import load_session, save_session

    with scoped_workdir(tmp_path):
        save_session([], session_id="session")
        with pytest.raises(ValueError, match="exact persisted"):
            delete_session("../../session")
        with pytest.raises(ValueError, match="exact persisted"):
            delete_session("latest")
        assert load_session("session")["session_id"] == "session"


@pytest.mark.parametrize("session_id", ["latest", "active", "../../session", "bad/id"])
def test_session_mutations_reject_reserved_or_sanitized_identity(
    tmp_path,
    session_id,
):
    """User IDs must never alias another Session or convenience pointer."""
    from nz_coder.state.sessions import activate_session, save_session, session_dir

    with scoped_workdir(tmp_path):
        save_session([], session_id="session", activate=False)
        with pytest.raises(ValueError, match="exact non-reserved"):
            save_session([], session_id=session_id, activate=False)
        with pytest.raises(ValueError, match="exact non-reserved"):
            activate_session(session_id)

        assert (session_dir() / "session.json").exists()


def test_session_read_rejects_lossy_identity_instead_of_aliasing(tmp_path):
    from nz_coder.state.sessions import load_session, save_session

    with scoped_workdir(tmp_path):
        save_session([], session_id="session", activate=False)

        with pytest.raises(ValueError, match="exact non-reserved"):
            load_session("../../session")
        assert load_session("latest")["session_id"] == "session"


def test_corrupt_active_alias_cannot_select_sanitized_session(tmp_path):
    from nz_coder.state.sessions import active_session_id, save_session, session_dir

    with scoped_workdir(tmp_path):
        save_session([], session_id="session", activate=False)
        alias = session_dir() / "active.json"
        alias.write_text(
            json.dumps({"session_id": "../../session"}),
            encoding="utf-8",
        )

        assert active_session_id() is None


def test_non_object_session_json_is_treated_as_corrupt_state(tmp_path):
    """Valid JSON with the wrong root type must not crash Session callers."""
    from nz_coder.state.sessions import active_session_id, load_session, save_session, session_dir

    with scoped_workdir(tmp_path):
        base = session_dir()
        base.mkdir(parents=True)
        (base / "active.json").write_text("[]\n", encoding="utf-8")
        (base / "latest.json").write_text("[]\n", encoding="utf-8")
        (base / "session.json").write_text("[]\n", encoding="utf-8")

        assert active_session_id() is None
        assert load_session("session") == {}
        path = save_session([], session_id="session", activate=False)
        assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == "session"


def test_failed_session_activation_does_not_publish_context_identity(
    tmp_path,
    monkeypatch,
):
    """The ContextVar commit point must follow durable active-alias success."""
    import nz_coder.state.sessions as sessions

    with scoped_workdir(tmp_path):
        monkeypatch.setattr(
            sessions,
            "_write_json",
            lambda _path, _payload: (_ for _ in ()).throw(OSError("disk full")),
        )

        with pytest.raises(OSError, match="disk full"):
            sessions.activate_session("failed")

        assert sessions.active_session_id() is None


def test_list_sessions_tolerates_concurrent_file_deletion(tmp_path, monkeypatch):
    import nz_coder.state.sessions as sessions

    with scoped_workdir(tmp_path):
        stale = sessions.save_session([], session_id="stale", activate=False)
        keep = sessions.save_session([], session_id="keep", activate=False)
        original_stat = Path.stat

        def racing_stat(path, *args, **kwargs):
            if path == stale:
                raise FileNotFoundError(str(path))
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", racing_stat)

        assert sessions.list_sessions() == [keep]


def test_session_uses_first_real_user_as_default_title_without_overwriting_rename(tmp_path):
    from nz_coder.state.sessions import load_session, rename_session, save_session

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
    from nz_coder.state.sessions import load_session, save_session

    with scoped_workdir(tmp_path):
        save_session(
            [{"role": "user", "content": "x" * 120}],
            session_id="session",
        )
        title = load_session("session")["title"]

    assert len(title) == 100
    assert title == "x" * 97 + "..."
