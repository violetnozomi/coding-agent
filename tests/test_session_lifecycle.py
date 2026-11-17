"""Session and child-worktree lifecycle tests."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir


@pytest.mark.parametrize("detached", [False, True])
def test_preparing_git_cleanup_preserves_carriage_return_path_bytes(tmp_path, detached):
    from nz_coder.runtime.worktree import Worktree, WorktreeManager
    root = tmp_path / "repo"
    root.mkdir()
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout
    git("init", "--quiet")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "base")
    git("branch", "subagent-child")
    manager = WorktreeManager(root, worktree_dir=tmp_path / "cache\rpart")
    target = manager.worktree_dir / "child"
    git("worktree", "add", "--quiet", *(["--detach"] if detached else []), str(target), "HEAD" if detached else "subagent-child")
    registration = b"worktree " + os.fsencode(target) + b"\0"
    assert registration in git("worktree", "list", "--porcelain", "-z")
    worktree = Worktree("child", str(target), "subagent-child", "HEAD", "", mode="git")
    assert manager.remove(worktree, preparing=True)
    assert not target.exists()
    assert registration not in git("worktree", "list", "--porcelain", "-z")
    assert git("for-each-ref", "--format=%(refname)", "--", "refs/heads/subagent-child") == b""


class _PreparationGitCleanup:
    def __init__(self, path, fault=""):
        self.path = path
        self.fault = fault
        self.registered = True
        self.branch_exists = True

    def run(self, args):
        import shutil
        import subprocess
        stdout = ""
        kind = ""
        if args[0] == "rev-parse":
            kind, stdout = "probe", "true\n"
        elif args[:2] == ["worktree", "list"]:
            kind = "list"
            stdout = f"worktree {self.path}\0HEAD abc\0branch refs/heads/subagent-child\0\0" if self.registered else ""
        elif args[:2] == ["worktree", "remove"]:
            kind = "remove"
        elif args[:2] == ["worktree", "prune"]:
            kind = "prune"
        elif args[0] == "for-each-ref":
            kind, stdout = "refs", "refs/heads/subagent-child\n" if self.branch_exists else ""
        elif args[:2] == ["branch", "-D"]:
            assert args[2] == "subagent-child"
            kind = "branch"
        else:
            raise AssertionError(args)
        failed = self.fault == kind or (kind == "probe" and self.fault in {"missing", "timeout"})
        if not failed and kind == "remove":
            shutil.rmtree(self.path, ignore_errors=True)
            self.registered = False
        if not failed and kind == "branch":
            self.branch_exists = False
        return subprocess.CompletedProcess(["git", *args], 1 if failed else 0, stdout if not failed else "", "private-detail" if failed else "")


@pytest.mark.parametrize("fault", ["missing", "timeout", "list", "remove", "prune", "refs", "branch"])
def test_delete_preparing_git_preserves_session_on_uncertain_cleanup(tmp_path, monkeypatch, fault):
    from tests.fixtures.process_lifecycle import persist_preparing_worktree
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.runtime.worktree import WorktreeManager
    from nz_coder.state.sessions import save_session, load_session
    from nz_coder.runtime.agent.subagent import _load_subagent_state
    with scoped_workdir(tmp_path):
        save_session([], session_id="old", activate=False)
        state = persist_preparing_worktree(tmp_path, parent="old")
        git = _PreparationGitCleanup(Path(state["worktree"]["path"]), fault)
        monkeypatch.setattr(WorktreeManager, "_run_git", lambda self, args, cwd=None: git.run(args))
        with pytest.raises(Exception) as caught:
            delete_session("old")
        assert "private-detail" not in str(caught.value)
        assert load_session("old")
        persisted = _load_subagent_state("old", "child", tmp_path)
        assert persisted["worktree_preparing"] is True
        assert persisted["worktree"]["path"] == str(git.path)
        assert persisted["worktree"]["branch"] == "subagent-child"


def test_delete_preparing_git_retries_confirmed_remaining_ownership(tmp_path, monkeypatch):
    from tests.fixtures.process_lifecycle import persist_preparing_worktree
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.runtime.worktree import WorktreeManager
    from nz_coder.state.sessions import save_session, load_session
    with scoped_workdir(tmp_path):
        save_session([], session_id="old", activate=False)
        state = persist_preparing_worktree(tmp_path, parent="old")
        git = _PreparationGitCleanup(Path(state["worktree"]["path"]), "branch")
        monkeypatch.setattr(WorktreeManager, "_run_git", lambda self, args, cwd=None: git.run(args))
        with pytest.raises(Exception):
            delete_session("old")
        assert load_session("old")
        assert not git.path.exists()
        assert git.branch_exists
        git.fault = ""
        assert delete_session("old") is True
        assert not git.registered and not git.branch_exists
        assert load_session("old") == {}


def test_delete_preparing_copy_keeps_original_copy_cleanup(tmp_path):
    from tests.fixtures.process_lifecycle import persist_preparing_worktree
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import save_session, load_session
    with scoped_workdir(tmp_path):
        save_session([], session_id="old", activate=False)
        state = persist_preparing_worktree(tmp_path, parent="old", mode="copy")
        assert delete_session("old") is True
        assert load_session("old") == {}
        assert not Path(state["worktree"]["path"]).exists()


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
    from nz_coder.state.sessions import SessionReadError, active_session_id, save_session, session_dir

    with scoped_workdir(tmp_path):
        target = save_session([], session_id="session", activate=False)
        original_target = target.read_bytes()
        alias = session_dir() / "active.json"
        alias.write_text(json.dumps({"session_id": "../../session"}), encoding="utf-8")
        original_alias = alias.read_bytes()
        with pytest.raises(SessionReadError) as error:
            active_session_id()
        assert error.value.kind == "corrupt"
        assert alias.read_bytes() == original_alias
        assert target.read_bytes() == original_target


def test_non_object_session_json_is_treated_as_corrupt_state(tmp_path):
    from nz_coder.state.sessions import SessionReadError, active_session_id, load_session, save_session, session_dir

    with scoped_workdir(tmp_path):
        base = session_dir()
        base.mkdir(parents=True)
        paths = [base / name for name in ("active.json", "latest.json", "session.json")]
        for path in paths:
            path.write_bytes(b"[]\n")
        for operation in (active_session_id, lambda: load_session("latest"),
                          lambda: load_session("session"),
                          lambda: save_session([], session_id="session", activate=False)):
            with pytest.raises(SessionReadError) as error:
                operation()
            assert error.value.kind == "corrupt"
        assert all(path.read_bytes() == b"[]\n" for path in paths)


def test_failed_session_activation_does_not_publish_context_identity(tmp_path, monkeypatch):
    """The ContextVar commit point must follow durable active-alias success."""
    import nz_coder.state.sessions as sessions

    with scoped_workdir(tmp_path):
        sessions.activate_session("prior")
        alias = sessions.session_dir() / "active.json"
        original_alias = alias.read_bytes()
        original_publish = sessions._publish_session_json
        attempted = []

        def fail_active(prepared, path):
            if path == alias:
                attempted.append(prepared.read_bytes())
                raise OSError("disk full")
            return original_publish(prepared, path)

        monkeypatch.setattr(sessions, "_publish_session_json", fail_active)
        with pytest.raises(OSError, match="disk full"):
            sessions.activate_session("failed")
        assert attempted and json.loads(attempted[0])["session_id"] == "failed"
        assert alias.read_bytes() == original_alias
        assert sessions.active_session_id() == "prior"


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


def test_session_delete_preserves_state_until_process_cleanup_done(
    tmp_path, monkeypatch
):
    from nz_coder.runtime.process import process_service as processes
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import save_session, session_artifact_dir
    from tests.fixtures.process_lifecycle import ControlledBackend

    backend = ControlledBackend()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    with scoped_workdir(tmp_path):
        state = save_session([], session_id="owned")
        keep = save_session([], session_id="keep")
        artifacts = session_artifact_dir("owned")
        artifacts.mkdir(parents=True, exist_ok=True)
        result_file = artifacts / "result.txt"
        result_file.write_text("keep until cleanup")
        service = processes.workspace_process_service(tmp_path)
        service._kill_grace_override = 0
        service.start("owned", cwd=tmp_path, owner_session_id="owned")
        try:
            with pytest.raises(processes.ProcessStateError):
                delete_session("owned")
            assert state.exists() and result_file.exists() and keep.exists()
            backend.recover()
            assert delete_session("owned") is True
            assert not state.exists() and not artifacts.exists()
            assert keep.exists()
        finally:
            backend.recover()
            processes.close_workspace_process_service(tmp_path)
