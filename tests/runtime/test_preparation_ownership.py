"""真实manager的准备归属、停止竞态及A2交接。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
import time
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

import pytest

from tests.fixtures.process_lifecycle import wait_until
from tests.runtime.test_subagent_preparation import preparation_case as preparation_case


def _manager(workspace, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import agent_manager, process_supervisor
    from tests.fixtures.subagent_process_targets import managed_child
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", True)
    monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", 2)
    monkeypatch.setattr(process_supervisor, "_run_subagent_process", managed_child)
    return agent_manager.BackgroundAgentManager(workspace, "parent")


def _start(manager, path="app.py"):
    result = manager.start([{"prompt":"complete", "isolation":"process", "target_paths":[path]}])
    assert hasattr(result, "metadata"), result
    return result.metadata["task_ids"][0]


def _finish(manager):
    manager.close(timeout=3)
    assert all(not job.thread.is_alive() for job in manager._jobs.values())


def _blocked_preparation(workspace, monkeypatch):
    from nz_coder.runtime.agent import agent_manager
    from nz_coder.runtime.process.preparation import PreparationScope
    manager = _manager(workspace, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = PreparationScope._execute
    def blocked(scope, record):
        original(scope, record)
        entered.set()
        release.wait()
    monkeypatch.setattr(PreparationScope, "_execute", blocked)
    frozen, submitted = [], []
    make = agent_manager.freeze_settlement
    def freeze(*a, **k):
        value = make(*a, **k)
        frozen.append(value)
        return value
    submit = manager._settlements.submit
    def send(value):
        submitted.append(value)
        return submit(value)
    monkeypatch.setattr(agent_manager, "freeze_settlement", freeze)
    monkeypatch.setattr(manager._settlements, "submit", send)
    return manager, entered, release, frozen, submitted


def test_preparation_scope_registered_before_first_helper_process(tmp_path, monkeypatch):
    from nz_coder.runtime.process import preparation
    manager = _manager(tmp_path, monkeypatch)
    observed = []
    original = preparation.subprocess.Popen
    def create(argv, **kwargs):
        if argv[0] == "git":
            job = next(iter(manager._jobs.values()))
            scope = job.preparation_scope
            assert scope is not None and scope._cancel is job.cancel_event
            assert scope.settlement_id == job.settlement_id
            assert scope._record is not None
            observed.append(scope)
        return original(argv, **kwargs)
    monkeypatch.setattr(preparation.subprocess, "Popen", create)
    try:
        task = _start(manager)
        assert manager._jobs[task].done_event.wait(4)
        assert observed and all(scope is observed[0] for scope in observed)
        assert manager._load_raw(task)["status"] == "completed"
        assert not manager._load_raw(task).get("worktree_preparing")
    finally:
        _finish(manager)


@pytest.mark.parametrize("phase", ["target", "complete"])
def test_target_save_failure_prevents_mutation_and_complete_save_failure_prevents_spawn(tmp_path, monkeypatch, phase):
    from nz_coder.runtime.agent import agent_manager
    manager = _manager(tmp_path, monkeypatch)
    failed = []
    original = manager._save
    def save(state):
        if state.get("worktree") and not next(iter(manager._jobs.values())).frozen:
            pending = state.get("worktree_preparing") is True
            if pending == (phase == "target"):
                failed.append(copy.deepcopy(state))
                raise OSError("controlled disk failure")
        return original(state)
    monkeypatch.setattr(manager, "_save", save)
    def forbidden(supervisor):
        pytest.fail("prepare save failure reached spawn")
    monkeypatch.setattr(agent_manager.SubagentProcessSupervisor, "start", forbidden)
    try:
        task = _start(manager)
        assert manager._jobs[task].done_event.wait(3)
        assert failed
        state = manager._load_raw(task)
        assert state["status"] == "error"
        target = Path(failed[0]["worktree"]["path"])
        assert target.exists() is (phase == "complete")
        if phase == "complete":
            assert state["worktree_preparing"] is True
            assert state["worktree"]["path"] == str(target)
    finally:
        _finish(manager)


def test_target_and_stop_diagnostics_merge_latest_state(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.process.preparation import PreparationScope, PreparationInterrupted
    from nz_coder.runtime.worktree import Worktree, WorktreeManager
    manager = _manager(tmp_path, monkeypatch)
    ready, signalled, saved, diagnose = (threading.Event() for _ in range(4))
    original = PreparationScope.request_stop
    def stop(scope):
        original(scope)
        if threading.current_thread().name == "test-stop":
            signalled.set()
            diagnose.wait()
    monkeypatch.setattr(PreparationScope, "request_stop", stop)
    def prepare(prompt, state, **kwargs):
        ready.set()
        assert signalled.wait(2)
        target = WorktreeManager(tmp_path).worktree_dir / state["session_id"]
        kwargs["preparation"]._on_target(Worktree(state["session_id"], str(target), "subagent-" + state["session_id"], "HEAD", ""))
        saved.set()
        raise PreparationInterrupted("cancelled")
    monkeypatch.setattr(subagent, "prepare_subagent_command_context", prepare)
    stopper = None
    try:
        task = _start(manager)
        assert ready.wait(1)
        stopper = threading.Thread(target=lambda: manager.stop([task], reason="test stop", timeout_ms=0), name="test-stop")
        stopper.start()
        assert saved.wait(2)
        assert manager._jobs[task].done_event.wait(2)
        diagnose.set()
        stopper.join(2)
        state = manager._load_raw(task)
        assert state["status"] == "cancelled"
        assert state["stop_reason"] == "test stop"
        assert state["worktree_preparing"] is True
        assert state["worktree"]["branch"] == "subagent-" + task
        assert state["settlement_id"] == manager._jobs[task].settlement_id
        assert state["cancel_requested_at"] == manager._jobs[task].cancel_requested_at
        events = manager.events().metadata["workflow_events"]
        cancellations = [e for e in events if e["type"] == "task_cancel_requested"]
        assert len(cancellations) == 1
        assert cancellations[0]["data"]["message"] == f"stop requested for {task}: test stop"
        assert sum(e["type"] == "task_terminal" for e in events) == 1
        assert manager._workflow.snapshot()["items"][0]["status"] == "cancelled"
    finally:
        signalled.set()
        diagnose.set()
        if stopper is not None:
            stopper.join(2)
        _finish(manager)


def test_concurrent_stops_keep_first_cause_and_one_diagnostic(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.process.preparation import PreparationScope, PreparationInterrupted
    manager = _manager(tmp_path, monkeypatch)
    ready, signalled, diagnose, finish = (threading.Event() for _ in range(4))
    original = PreparationScope.request_stop
    def stop(scope):
        original(scope)
        if threading.current_thread().name == "first-stop":
            signalled.set()
            assert diagnose.wait(3)
    monkeypatch.setattr(PreparationScope, "request_stop", stop)
    def prepare(*args, **kwargs):
        ready.set()
        assert finish.wait(3)
        raise PreparationInterrupted("cancelled")
    monkeypatch.setattr(subagent, "prepare_subagent_command_context", prepare)
    results = []
    stopper = None
    try:
        task = _start(manager)
        assert ready.wait(1)
        stopper = threading.Thread(target=lambda: results.append(manager.stop([task], reason="first cause", timeout_ms=0)), name="first-stop")
        stopper.start()
        assert signalled.wait(1)
        accepted_at = manager._jobs[task].cancel_requested_at
        second = manager.stop([task], reason="second cause", timeout_ms=0)
        state = manager._load_raw(task)
        assert state["stop_reason"] == "first cause"
        assert state["cancel_requested_at"] == accepted_at
        assert second.metadata["requested_task_ids"] == []
        finish.set()
        assert manager._jobs[task].done_event.wait(2)
        diagnose.set()
        stopper.join(2)
        assert results[0].metadata["requested_task_ids"] == [task]
        events = manager.events().metadata["workflow_events"]
        cancellations = [e for e in events if e["type"] == "task_cancel_requested"]
        assert len(cancellations) == 1
        assert cancellations[0]["data"]["message"] == f"stop requested for {task}: first cause"
        assert sum(e["type"] == "task_terminal" for e in events) == 1
        state = manager._load_raw(task)
        assert state["status"] == "cancelled"
        assert state["stop_reason"] == "first cause" and state["cancel_requested_at"] == accepted_at
    finally:
        diagnose.set()
        finish.set()
        if stopper is not None:
            stopper.join(3)
        _finish(manager)


@pytest.mark.parametrize("reason", ["cancelled", "timeout", "both", "failure"])
def test_prepare_interrupt_maps_to_cancel_or_timeout_without_public_error(tmp_path, monkeypatch, reason):
    from nz_coder.protocol.public_error import PublicError
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.process.preparation import PreparationInterrupted, PreparationFailure
    manager = _manager(tmp_path, monkeypatch)
    def prepare(*args, **kwargs):
        if reason == "both":
            kwargs["preparation"]._cancel.set()
        if reason == "failure":
            raise PreparationFailure(PublicError("preparation_test_failure", "Preparation failed."))
        raise PreparationInterrupted("timeout" if reason in {"timeout", "both"} else "cancelled")
    monkeypatch.setattr(subagent, "prepare_subagent_command_context", prepare)
    try:
        task = _start(manager)
        assert manager._jobs[task].done_event.wait(2)
        state = manager._load_raw(task)
        assert state["status"] == {"cancelled":"cancelled", "timeout":"timeout", "both":"cancelled", "failure":"error"}[reason]
        if reason == "failure":
            assert state["public_error"]["code"] == "preparation_test_failure"
        else:
            assert not state.get("public_error")
    finally:
        _finish(manager)


def test_freeze_precedes_preparation_cleanup_and_submits_once(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    manager, entered, release, frozen, submitted = _blocked_preparation(tmp_path, monkeypatch)
    try:
        task = _start(manager)
        assert entered.wait(2)
        job = manager._jobs[task]
        manager.stop([task], timeout_ms=0)
        wait_until(lambda: job.frozen)
        owner = job.preparation_scope._record.thread
        assert owner.is_alive()
        assert len(frozen) == 1 and submitted == []
        assert manager._active_jobs == 1 and not job.done_event.is_set()
        assert capability_leases().get(job.capability_lease_id) is not None
        release.set()
        assert job.done_event.wait(2)
        assert len(submitted) == len(frozen) == 1 and submitted[0].state is frozen[0]
        assert manager._active_jobs == 0 and not job.capability_lease_id
        assert not owner.is_alive()
    finally:
        release.set()
        _finish(manager)


def test_frozen_stop_preserves_candidate_and_signals_both_scopes(tmp_path, monkeypatch):
    from tests.test_agent_manager import _blocked_command_manager, _start_process, _finish_process_manager
    manager, backend, _, frozen, submitted, _ = _blocked_command_manager(tmp_path, monkeypatch)
    try:
        task = _start_process(manager, "complete")
        assert backend.close_entered.wait(6)
        job = manager._jobs[task]
        wait_until(lambda: job.frozen)
        before = copy.deepcopy(frozen[0])
        calls = []
        for name, scope in [("preparation", job.preparation_scope), ("command", job.command_scope)]:
            original = scope.request_stop
            def stop(_name=name, _original=original):
                calls.append((_name, threading.get_ident()))
                _original()
            monkeypatch.setattr(scope, "request_stop", stop)
        manager.stop([task], timeout_ms=0)
        assert ("preparation", threading.get_ident()) in calls
        assert ("command", threading.get_ident()) in calls
        assert not job.cancel_event.is_set()
        assert frozen[0] == before and submitted == []
        backend.recover()
        assert job.done_event.wait(3)
        assert manager._load_raw(task)["child_result"]["cost"] == .125
    finally:
        backend.recover()
        _finish_process_manager(manager)


def test_shared_close_deadline_does_not_reset_per_domain(tmp_path, monkeypatch):
    manager, entered, release, _, _ = _blocked_preparation(tmp_path, monkeypatch)
    rounds = []
    class OtherScope:
        def request_stop(self):
            pass
        def close(self, *, deadline):
            rounds.append(("command", deadline))
            return True
    try:
        task = _start(manager)
        assert entered.wait(2)
        job = manager._jobs[task]
        job.command_scope = OtherScope()
        original = job.preparation_scope.close
        def close(*, deadline):
            rounds.append(("preparation", deadline))
            return original(deadline=deadline)
        monkeypatch.setattr(job.preparation_scope, "close", close)
        manager.stop([task], timeout_ms=0)
        wait_until(lambda: any(name == "command" for name, _ in rounds))
        assert rounds[0][0] == "preparation" and rounds[1][0] == "command"
        assert rounds[0][1] == rounds[1][1]
    finally:
        release.set()
        _finish(manager)


def test_lease_revoke_and_short_close_keep_original_pending_job(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.agent import agent_manager
    manager, entered, release, _, _ = _blocked_preparation(tmp_path, monkeypatch)
    monkeypatch.setitem(agent_manager._INSTANCES, (tmp_path.resolve(), "parent"), manager)
    try:
        task = _start(manager)
        assert entered.wait(2)
        job = manager._jobs[task]
        scope = job.preparation_scope
        with pytest.raises(RuntimeError):
            manager.close(timeout=.01)
        assert agent_manager._INSTANCES[(tmp_path.resolve(), "parent")] is manager
        revoked = capability_leases().revoke_workspace(tmp_path)
        assert revoked.failed >= 1
        assert job.preparation_scope is scope and not job.done_event.is_set()
        assert capability_leases().get(job.capability_lease_id) is not None
        release.set()
        assert job.done_event.wait(2)
    finally:
        release.set()
        _finish(manager)


def test_sibling_preparations_and_parent_context_do_not_cross(tmp_path, monkeypatch):
    from nz_coder.runtime.core.run_settings import RunSettings, scoped_run_settings
    from nz_coder.runtime.process.preparation import PreparationScope
    manager = _manager(tmp_path, monkeypatch)
    parent_context = ContextVar("parent-preparation-test", default=None)
    context_token = parent_context.set("private-parent-context")
    entered, release, seen = {}, {}, {}
    original = PreparationScope._execute
    def blocked(scope, record):
        seen[scope.child_session_id] = (parent_context.get(), scope._settings.process_kill_grace)
        entered.setdefault(scope.child_session_id, threading.Event()).set()
        release.setdefault(scope.child_session_id, threading.Event()).wait()
        original(scope, record)
    monkeypatch.setattr(PreparationScope, "_execute", blocked)
    try:
        with scoped_run_settings(replace(RunSettings.from_legacy_globals(), process_kill_grace=.02)):
            first = _start(manager, "a.py")
        wait_until(lambda: first in seen)
        with scoped_run_settings(replace(RunSettings.from_legacy_globals(), process_kill_grace=.03)):
            second = _start(manager, "b.py")
        wait_until(lambda: second in seen)
        assert seen[first] == (None, .02) and seen[second] == (None, .03)
        manager.stop([first], timeout_ms=0)
        assert not manager._jobs[second].cancel_event.is_set()
        assert manager._jobs[first].preparation_scope is not manager._jobs[second].preparation_scope
    finally:
        for gate in release.values():
            gate.set()
        _finish(manager)
        parent_context.reset(context_token)


def _block_real_stage(case, monkeypatch, stage):
    from nz_coder.runtime.agent import agent_manager
    root, log, configure = case
    (root / ".husky").mkdir()
    (root / ".worktreeinclude").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("local fixture\n")
    (root / "ignored.txt").write_text("test ignored content")
    ready, release = root.parent / "helper-ready.json", root.parent / "helper-release"
    configure(block_stage=stage, ready=ready, release=release)
    manager = _manager(root, monkeypatch)
    attempts = []
    def forbidden(supervisor):
        attempts.append(supervisor)
        raise AssertionError("Agent spawn is forbidden in this preparation failure case")
    monkeypatch.setattr(agent_manager.SubagentProcessSupervisor, "start", forbidden)
    return manager, ready, release, log, attempts


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable fixture; no Windows native claim")
@pytest.mark.parametrize("stage", ["repo", "add", "ignored"])
@pytest.mark.parametrize("reason", ["cancelled", "timeout"])
def test_real_prepare_stage_cancel_and_total_deadline(preparation_case, monkeypatch, stage, reason):
    from nz_coder.foundation import config
    manager, ready, release, log, attempts = _block_real_stage(preparation_case, monkeypatch, stage)
    if reason == "timeout":
        monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", 1)
    try:
        task = _start(manager)
        wait_until(ready.exists)
        job = manager._jobs[task]
        record = job.preparation_scope._record
        pid = json.loads(ready.read_text())["pid"]
        assert record.process.pid == pid
        before = log.read_bytes()
        if reason == "cancelled":
            manager.stop([task], timeout_ms=0)
        assert job.done_event.wait(3)
        state = manager._load_raw(task)
        assert state["status"] == reason
        assert attempts == [] and job.supervisor is None
        assert record.process.returncode is not None
        assert not record.thread.is_alive()
        assert all(not stream.thread.is_alive() and stream.closed for stream in record.streams)
        assert log.read_bytes() == before
        assert manager._active_jobs == 0 and not job.capability_lease_id
        if stage != "repo":
            assert state["worktree_preparing"] is True
            assert state["worktree"]["branch"] == "subagent-" + task
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        release.touch()
        _finish(manager)


@pytest.mark.parametrize("mode", ["add_partial", "add_absent", "add_timeout"])
def test_real_add_partial_failure_retains_target_and_git_ownership(preparation_case, monkeypatch, mode):
    from nz_coder.runtime.agent import agent_manager
    from nz_coder.runtime.process.preparation import PreparationScope
    root, _, configure = preparation_case
    configure(mode)
    manager = _manager(root, monkeypatch)
    attempts = []
    monkeypatch.setattr(agent_manager.SubagentProcessSupervisor, "start", lambda self: attempts.append(self))
    original = PreparationScope.run_git
    def run(scope, args, *, cwd, timeout):
        return original(scope, args, cwd=cwd, timeout=.04 if args[0] == "worktree" else timeout)
    monkeypatch.setattr(PreparationScope, "run_git", run)
    try:
        task = _start(manager)
        assert manager._jobs[task].done_event.wait(3)
        state = manager._load_raw(task)
        assert state["status"] == "error" and attempts == []
        assert state["worktree_preparing"] is True
        assert state["worktree"]["mode"] == "git"
        assert state["worktree"]["branch"] == "subagent-" + task
        assert not (Path(state["worktree"]["path"]) / "app.py").exists()
    finally:
        _finish(manager)


def test_real_preparation_environment_is_sanitized_at_all_launch_sites(preparation_case, monkeypatch):
    root, log, _ = preparation_case
    (root / ".husky").mkdir()
    (root / ".worktreeinclude").write_text("ignored.txt\n")
    (root / "ignored.txt").write_text("local fixture\n")
    monkeypatch.setenv("TEST_API_KEY", "worthless-sentinel")
    manager = _manager(root, monkeypatch)
    try:
        task = _start(manager)
        assert manager._jobs[task].done_event.wait(4)
        assert manager._load_raw(task)["status"] == "completed"
        rows = [json.loads(line) for line in log.read_text().splitlines()]
        assert {"rev-parse", "worktree", "ls-files", "ls-tree"} <= {row["args"][0] for row in rows}
        assert not any(row["args"][0] == "config" for row in rows)
        assert all(row["sentinel"] is None and row["prompt"] == "0" and row["askpass"] == "" for row in rows)
    finally:
        _finish(manager)


@pytest.mark.parametrize("fault", ["group", "reader", "close"])
def test_cleanup_recovery_needs_no_status_polling_and_never_replays_prepare(preparation_case, monkeypatch, fault):
    from nz_coder.runtime.process import preparation
    manager, ready, release, log, attempts = _block_real_stage(preparation_case, monkeypatch, "repo")
    recovered = threading.Event()
    real_terminate = preparation.terminate_process_tree
    real_popen = preparation.subprocess.Popen
    class Stream:
        def __init__(self, wrapped):
            self.wrapped = wrapped
        def read(self, size):
            data = self.wrapped.read(size)
            if not data and fault == "reader":
                recovered.wait()
            return data
        def close(self):
            if fault == "close" and not recovered.is_set():
                raise OSError("controlled stream close failure")
            return self.wrapped.close()
    def popen(*a, **k):
        process = real_popen(*a, **k)
        process.stdout, process.stderr = Stream(process.stdout), Stream(process.stderr)
        return process
    monkeypatch.setattr(preparation.subprocess, "Popen", popen)
    if fault == "group":
        monkeypatch.setattr(preparation, "terminate_process_tree", lambda *a, **k: real_terminate(*a, **k) if recovered.is_set() else False)
    try:
        task = _start(manager)
        wait_until(ready.exists)
        job = manager._jobs[task]
        record = job.preparation_scope._record
        before = log.read_bytes()
        result = manager.stop([task], timeout_ms=10)
        assert result.metadata["unsettled_task_ids"] == [task]
        wait_until(lambda: job.frozen)
        assert manager._active_jobs == 1 and job.capability_lease_id
        with pytest.raises(RuntimeError):
            manager.close(timeout=.01)
        recovered.set()
        assert job.done_event.wait(2)
        assert log.read_bytes() == before and attempts == []
        assert not record.thread.is_alive() and record.process.returncode is not None
        assert all(not stream.thread.is_alive() and stream.closed for stream in record.streams)
    finally:
        recovered.set()
        release.touch()
        _finish(manager)


def test_a2_disk_retry_after_preparation_does_not_replay_or_double_charge(preparation_case, monkeypatch):
    from nz_coder.runtime.agent import agent_manager
    root, log, _ = preparation_case
    manager = _manager(root, monkeypatch)
    blocked, failed = threading.Event(), threading.Event()
    blocked.set()
    original = manager._save
    def save(state):
        if state.get("background_result") and blocked.is_set():
            failed.set()
            raise OSError("controlled terminal disk failure")
        return original(state)
    monkeypatch.setattr(manager, "_save", save)
    captured = []
    freeze = agent_manager.FrozenSettlement
    def capture(*a, **k):
        value = freeze(*a, **k)
        captured.append(value)
        return value
    monkeypatch.setattr(agent_manager, "FrozenSettlement", capture)
    try:
        task = _start(manager)
        assert failed.wait(4)
        job = manager._jobs[task]
        assert job.frozen and not job.done_event.is_set()
        before = log.read_bytes()
        assert len(captured) == 1
        candidate = copy.deepcopy(captured[0].state)
        blocked.clear()
        manager._settlements.wake()
        assert job.done_event.wait(3)
        assert log.read_bytes() == before
        assert len(captured) == 1 and captured[0].state == candidate
        assert manager._load_raw(task)["child_result"]["cost"] == .125
        assert len(list(root.glob("*.transport"))) == 1
    finally:
        blocked.clear()
        manager._settlements.wake()
        _finish(manager)


def test_real_handoff_to_b2_preserves_workspace_policy_and_settlement(tmp_path, monkeypatch):
    from nz_coder.foundation.subprocess_env import build_sanitized_subprocess_env
    from nz_coder.runtime.agent import process_supervisor
    from nz_coder.runtime.worktree import Worktree, WorktreeManager
    from tests.fixtures.subagent_process_targets import prepared_worktree_probe
    root = tmp_path / "repo"
    root.mkdir()
    environment = build_sanitized_subprocess_env()
    def git(*args):
        return subprocess.run(["git", *args], cwd=root, env=environment, capture_output=True, text=True, check=True)
    git("init", "-q")
    git("-c", "user.name=Offline", "-c", "user.email=offline@invalid", "commit", "--allow-empty", "-qm", "initial")
    git("branch", "subagent-keep")
    (root / "app.py").write_text("dirty parent source\n")
    manager = _manager(root, monkeypatch)
    monkeypatch.setattr(process_supervisor, "_run_subagent_process", prepared_worktree_probe)
    try:
        task = _start(manager)
        assert manager._jobs[task].done_event.wait(6)
        state = manager._load_raw(task)
        assert state["status"] in {"completed", "completed_unverified"}, state
        worktree = Worktree(**state["worktree"])
        assert worktree.mode == "git" and not state.get("worktree_preparing")
        assert (Path(worktree.path) / "app.py").read_text() == "dirty parent source\n"
        facts = json.loads((root / ".nz-coder" / "model-probe.json").read_text())
        assert facts["cwd"] == worktree.path and facts["model_calls"] == 2
        assert any(worktree.path in output for output in facts["tool_outputs"])
        assert manager._jobs[task].preparation_scope.close(deadline=time.monotonic())
        assert WorktreeManager(root).remove(worktree, preparing=True)
        assert not Path(worktree.path).exists()
        git("show-ref", "--verify", "refs/heads/subagent-keep")
        assert worktree.branch not in git("branch", "--format=%(refname:short)").stdout.splitlines()
    finally:
        _finish(manager)
