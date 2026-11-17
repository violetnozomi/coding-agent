"""受控工作树准备的Git、回退和逐阶段停止语义。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from tests.fixtures.process_lifecycle import make_preparation_scope


@pytest.fixture
def preparation_case(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("dirty source\n")
    executable = tmp_path / "bin" / "git"
    executable.parent.mkdir()
    events = tmp_path / "events.jsonl"

    def configure(mode="normal", *, block_stage="", ready=None, release=None):
        script = f'''#!{sys.executable}
import json,os,sys,time
from pathlib import Path
args=sys.argv[1:]
with open({str(events)!r},"a") as f:
    f.write(json.dumps({{"args":args,"cwd":os.getcwd(),"sentinel":os.environ.get("TEST_API_KEY"),"prompt":os.environ.get("GIT_TERMINAL_PROMPT"),"askpass":os.environ.get("GIT_ASKPASS")}})+"\\n")
mode={mode!r}
stage={{"rev-parse":"repo","worktree":"add","config":"hooks","ls-files":"ignored"}}.get(args[0],"")
if stage and stage=={block_stage!r}:
    Path({str(ready)!r}).write_text(json.dumps({{"pid":os.getpid(),"stage":stage}}))
    while not Path({str(release)!r}).exists():time.sleep(.01)
if args[0]=="rev-parse":
    if mode=="repo_nonzero":sys.exit(1)
    if mode=="repo_timeout":time.sleep(30)
    print("true" if "--is-inside-work-tree" in args else "f"*40)
elif args[:2]==["worktree","add"]:
    target=Path(args[args.index("--")+1])
    if mode in ("normal","add_partial","add_timeout"):target.mkdir(parents=True)
    if mode=="add_timeout":time.sleep(30)
    if mode in ("add_partial","add_absent"):sys.exit(1)
elif args[0]=="ls-files" and "--stage" in args:
    print("100644 " + "f"*40 + " 0\\tapp.py",end="\\0")
elif args[0]=="ls-tree":
    print("100644 blob " + "f"*40 + "\\tapp.py",end="\\0")
elif args[0]=="ls-files" and "--ignored" not in args:
    for p in Path.cwd().rglob("*"):
        if p.is_file() and p.name!="ignored.txt":print(str(p.relative_to(Path.cwd())),end="\\0")
elif args[0] in ("ls-files","--literal-pathspecs"):print("ignored.txt",end="\\0")
'''
        compile(script, str(executable), "exec")
        executable.write_text(script)
        executable.chmod(0o700)
    configure()
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ.get("PATH", ""))
    return root, events, configure


def test_all_three_git_launch_sites_use_preparation_scope(preparation_case, monkeypatch):
    from nz_coder.runtime.worktree import WorktreeManager
    root, log, _ = preparation_case
    (root / ".husky").mkdir()
    (root / ".worktreeinclude").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("copied by setup\n")
    monkeypatch.setenv("TEST_API_KEY", "worthless-sentinel")
    saved = []
    scope = make_preparation_scope(root, on_target=saved.append)
    calls = []
    run = scope.run_git
    def traced(args, *, cwd, timeout):
        calls.append((args, cwd, timeout))
        return run(args, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(scope, "run_git", traced)
    try:
        worktree = WorktreeManager(root, preparation=scope).create("child")
        assert worktree.mode == "git"
        assert (Path(worktree.path) / "app.py").read_text() == "dirty source\n"
        assert (Path(worktree.path) / "ignored.txt").read_text() == "copied by setup\n"
        assert saved[0].branch == "subagent-child"
        assert calls and all(call[2] in (60, 30) for call in calls)
        events = [json.loads(line) for line in log.read_text().splitlines()]
        assert {"rev-parse", "worktree", "ls-files", "ls-tree"} <= {e["args"][0] for e in events}
        assert all(e["sentinel"] is None and e["prompt"] == "0" and e["askpass"] == "" for e in events)
        assert any(e["cwd"] == worktree.path for e in events)
        assert any(e["cwd"] == str(root) for e in events)
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("mode", ["repo_nonzero", "repo_os", "repo_timeout", "add_partial", "add_absent", "add_os", "add_timeout"])
def test_pre_add_failure_can_copy_but_admitted_add_failure_retains_git(preparation_case, monkeypatch, mode):
    import time
    from nz_coder.runtime.process import preparation
    from nz_coder.runtime.worktree import WorktreeManager
    root, _, configure = preparation_case
    configure(mode)
    saved = []
    scope = make_preparation_scope(root, on_target=saved.append)
    original = preparation.subprocess.Popen
    def popen(argv, **kwargs):
        if (mode == "repo_os" and argv[1] == "rev-parse") or (mode == "add_os" and argv[1] == "worktree"):
            raise FileNotFoundError("controlled executable unavailable")
        return original(argv, **kwargs)
    monkeypatch.setattr(preparation.subprocess, "Popen", popen)
    run = scope.run_git
    monkeypatch.setattr(scope, "run_git", lambda args, *, cwd, timeout: run(args, cwd=cwd, timeout=.04 if mode.endswith("timeout") else timeout))
    manager = WorktreeManager(root, preparation=scope)
    try:
        if mode.startswith("repo_"):
            worktree = manager.create("child")
            assert worktree.mode == "copy"
            assert (Path(worktree.path) / "app.py").read_text() == "dirty source\n"
            assert saved[0].mode == "copy"
        else:
            with pytest.raises(preparation.PreparationFailure):
                manager.create("child")
            assert saved[0].mode == "git"
            assert saved[0].branch == "subagent-child"
            assert not (Path(saved[0].path) / "app.py").exists()
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_unknown_existing_target_is_not_ready(preparation_case):
    import time
    from nz_coder.runtime.worktree import WorktreeManager
    root, _, _ = preparation_case
    scope = make_preparation_scope(root)
    manager = WorktreeManager(root, preparation=scope)
    target = manager.worktree_dir / "child"
    target.mkdir(parents=True)
    (target / "user.txt").write_text("keep")
    try:
        with pytest.raises(Exception):
            manager.create("child")
        assert (target / "user.txt").read_text() == "keep"
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("mode", ["normal", "repo_nonzero"])
def test_target_appearing_during_repository_probe_is_not_reused(preparation_case, monkeypatch, mode):
    from nz_coder.runtime.worktree import WorktreeError, WorktreeManager
    root, log, configure = preparation_case
    configure(mode)
    saved = []
    scope = make_preparation_scope(root, on_target=saved.append)
    manager = WorktreeManager(root, preparation=scope)
    target = manager.worktree_dir / "child"
    probe = manager.is_git_repo
    def raced_probe():
        result = probe()
        target.mkdir(parents=True)
        (target / "user.txt").write_text("keep")
        return result
    monkeypatch.setattr(manager, "is_git_repo", raced_probe)
    monkeypatch.setattr(manager, "read_worktree_head_sha", lambda path: "abc" if path.exists() else None)
    try:
        with pytest.raises(WorktreeError, match="completed ownership record"):
            manager.create("child")
        assert saved == []
        assert [p.name for p in target.iterdir()] == ["user.txt"]
        assert (target / "user.txt").read_text() == "keep"
        assert len(log.read_text().splitlines()) == 1
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def test_target_is_recorded_before_first_mutation(preparation_case):
    import time
    from nz_coder.runtime.worktree import WorktreeManager
    root, _, _ = preparation_case
    saved = []
    def reject(worktree):
        saved.append(worktree)
        assert not Path(worktree.path).exists()
        raise OSError("disk unavailable")
    scope = make_preparation_scope(root, on_target=reject)
    try:
        with pytest.raises(OSError, match="disk unavailable"):
            WorktreeManager(root, preparation=scope).create("child")
        assert len(saved) == 1
        assert not Path(saved[0].path).exists()
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("stage", ["repo", "add", "snapshot", "configs", "symlinks", "ignored"])
@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_interruption_never_enters_fallback_or_optional_setup(preparation_case, monkeypatch, stage, reason):
    import time
    from nz_coder.runtime.process.preparation import PreparationInterrupted
    from nz_coder.runtime.worktree import WorktreeManager, setup
    root, _, _ = preparation_case
    cancelled = threading.Event()
    scope = make_preparation_scope(root, cancel_event=cancelled)
    def interrupt():
        if reason == "cancelled":
            cancelled.set()
        else:
            scope._deadline = time.monotonic() - 1
    if stage in {"repo", "add"}:
        run = scope.run_git
        def finish(args, *, cwd, timeout):
            result = run(args, cwd=cwd, timeout=timeout)
            if (stage == "repo" and args[0] == "rev-parse") or (stage == "add" and args[0] == "worktree"):
                interrupt()
            return result
        monkeypatch.setattr(scope, "run_git", finish)
    elif stage == "snapshot":
        snapshot = WorktreeManager._sync_snapshot
        def finish(self, *a, **k):
            snapshot(self, *a, **k)
            interrupt()
        monkeypatch.setattr(WorktreeManager, "_sync_snapshot", finish)
    else:
        name = {"configs":"_copy_local_configs", "hooks":"_setup_git_hooks", "symlinks":"_create_symlinks", "ignored":"_copy_ignored_files"}[stage]
        original = getattr(setup, name)
        def finish(*a, **k):
            original(*a, **k)
            interrupt()
        monkeypatch.setattr(setup, name, finish)
    try:
        with pytest.raises(PreparationInterrupted) as caught:
            WorktreeManager(root, preparation=scope).create("child")
        assert caught.value.reason == reason
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("error", [OSError("local error"), subprocess.TimeoutExpired("git", 10)])
def test_declared_include_errors_fail_preparation(preparation_case, monkeypatch, error):
    import time
    from nz_coder.runtime.worktree import setup
    root, _, _ = preparation_case
    (root / ".husky").mkdir()
    (root / ".worktreeinclude").write_text("*.txt")
    scope = make_preparation_scope(root)
    monkeypatch.setattr(scope, "run_git", lambda *a, **k: (_ for _ in ()).throw(error))
    try:
        with pytest.raises(type(error)):
            setup.perform_post_creation_setup(root, root, preparation=scope)
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


def _child_state():
    from nz_coder.runtime.agent import subagent
    state = subagent._new_subagent_state("parent", "explore", ["bash"])
    state.update(session_id="child", settlement_id="settlement-child")
    return state


@pytest.mark.parametrize("stage", ["ensure", "scratch", "policy"])
def test_prepare_requires_same_scope_and_checks_scratch_policy_boundaries(preparation_case, monkeypatch, stage):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings
    from nz_coder.runtime.process.preparation import PreparationInterrupted
    root, _, _ = preparation_case
    cancelled = threading.Event()
    scope = make_preparation_scope(root, cancel_event=cancelled)
    name = {"ensure":"_ensure_subagent_worktree", "scratch":"_prepare_subagent_workspace", "policy":"_preview_subagent_command_policy"}[stage]
    original = getattr(subagent, name)
    def finish(*a, **k):
        result = original(*a, **k)
        cancelled.set()
        return result
    monkeypatch.setattr(subagent, name, finish)
    try:
        with pytest.raises(PreparationInterrupted):
            subagent.prepare_subagent_command_context(
                "offline", _child_state(), workspace=root, config_snapshot=None,
                settings=RunSettings.from_legacy_globals(), settlement_id="settlement-child", preparation=scope,
            )
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("mode", ["git", "copy"])
def test_preparing_state_cannot_resume_or_clone(preparation_case, mode):
    from dataclasses import asdict
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.worktree import Worktree, WorktreeManager
    root, _, _ = preparation_case
    state = _child_state()
    manager = WorktreeManager(root)
    target = manager.worktree_dir / "child"
    target.mkdir(parents=True)
    (target / "user.txt").write_text("keep")
    state.update(worktree=asdict(Worktree("child", str(target), "subagent-child", "HEAD", "", mode=mode)), worktree_preparing=True)
    with pytest.raises(Exception, match="preparation|prepar"):
        subagent._ensure_subagent_worktree(root, state)
    clone = dict(state, session_id="clone")
    with pytest.raises(Exception, match="preparation|prepar"):
        subagent._clone_child_worktree(root, state, clone)
    assert (target / "user.txt").read_text() == "keep"
    assert not (manager.worktree_dir / "clone").exists()


@pytest.mark.parametrize("direct", [False, True])
def test_complete_persisted_worktree_remains_reusable(preparation_case, direct):
    from dataclasses import asdict, replace
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.core.run_settings import RunSettings
    from nz_coder.runtime.worktree import Worktree, WorktreeManager
    root, log, _ = preparation_case
    state = _child_state()
    if not direct:
        target = WorktreeManager(root).worktree_dir / "child"
        target.mkdir(parents=True)
        state["worktree"] = asdict(Worktree("child", str(target), "", "HEAD", "", mode="copy"))
    settings = replace(RunSettings.from_legacy_globals(), subagent_worktree_enabled=not direct)
    scope = make_preparation_scope(root, settings=settings)
    try:
        prepared, authority = subagent.prepare_subagent_command_context(
            "offline", state, workspace=root, config_snapshot=None, settings=settings,
            settlement_id="settlement-child", preparation=scope,
        )
        assert prepared.worktree.mode == ("direct" if direct else "copy")
        assert authority.execution_root == (root if direct else target)
        assert not state.get("worktree_preparing")
        # 恢复可以读取模块边界，但不能重建工作树或修改共享配置。
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        assert not any(row["args"][0] in {"worktree", "config"} for row in rows)
    finally:
        assert scope.close(deadline=time.monotonic() + 2)
