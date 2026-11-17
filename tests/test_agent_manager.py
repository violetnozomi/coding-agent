"""Tests for Session-owned background write-subagent orchestration."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


def test_manager_startup_sweep_retains_stale_preparing_target(tmp_path):
    from pathlib import Path
    from tests.fixtures.process_lifecycle import persist_preparing_worktree
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    state = persist_preparing_worktree(tmp_path, mode="copy", workflow_run_id="run")
    manager = BackgroundAgentManager(tmp_path, "parent")
    try:
        assert Path(state["worktree"]["path"]).exists()
        assert manager._load_raw("child")["worktree_preparing"] is True
    finally:
        manager.close()

def _process_manager(tmp_path, monkeypatch, *, timeout=10, concurrency=1):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import agent_manager as module
    from nz_coder.runtime.agent import process_supervisor
    from tests.fixtures import subagent_process_targets as targets

    monkeypatch.setattr(config, "SUBAGENT_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", concurrency)
    monkeypatch.setattr(process_supervisor, "_run_subagent_process", targets.managed_child)
    return module.BackgroundAgentManager(tmp_path, "parent")


def _process_ready(manager, session_id, workspace):
    deadline = time.monotonic() + 3
    while not (workspace / f"{session_id}.ready").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _finish_process_manager(manager):
    for job in list(manager._jobs.values()):
        job.cancel_event.set()
        process = job.process
        if process is not None:
            try:
                if process.is_alive():
                    process.kill()
                process.join(2)
            except ValueError:
                pass
        job.thread.join(3)
        assert not job.thread.is_alive()
    manager.close(timeout=0.1)


def _start_process(manager, prompt="complete-and-block", path="app.py", **extra):
    result = manager.start([{"prompt": prompt, "target_paths": [path], "isolation": "process", **extra}])
    assert hasattr(result, "metadata"), result
    return result.metadata["task_ids"][0]


def _blocked_command_manager(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.agent import agent_manager as module, process_supervisor
    from nz_coder.runtime.process import process_service
    from tests.fixtures import subagent_process_targets as targets
    from tests.fixtures.process_lifecycle import ControlledBackend

    manager = _process_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(config, 'SUBAGENT_WORKTREE_ENABLED', True)
    monkeypatch.setattr(process_supervisor, '_run_subagent_process', targets.owned_process_then_complete)
    backend = ControlledBackend()
    backend.auto_stop = True
    backend.close_release.clear()
    backend_creates, frozen, submits, releases = [], [], [], []
    def create(*args, **kwargs):
        backend_creates.append(kwargs['cwd'])
        return backend
    original_frozen = module.freeze_settlement
    def freeze(*args, **kwargs):
        record = original_frozen(*args, **kwargs)
        frozen.append(record)
        return record
    original_submit = manager._settlements.submit
    def submit(record):
        submits.append(record)
        return original_submit(record)
    original_release = capability_leases().release
    def release(identity):
        releases.append(identity)
        return original_release(identity)
    monkeypatch.setattr(process_service, 'create_process_backend', create)
    monkeypatch.setattr(module, 'freeze_settlement', freeze)
    monkeypatch.setattr(manager._settlements, 'submit', submit)
    monkeypatch.setattr(capability_leases(), 'release', release)
    return manager, backend, backend_creates, frozen, submits, releases


def test_freezes_before_command_cleanup_and_submits_same_candidate_once(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    from tests.fixtures.process_lifecycle import wait_until

    manager, backend, creates, frozen, submits, releases = _blocked_command_manager(tmp_path, monkeypatch)
    try:
        task = _start_process(manager, 'complete')
        assert backend.close_entered.wait(6)
        job = manager._jobs[task]
        wait_until(lambda: job.frozen)
        assert job.supervisor.exited and not job.process.is_alive()
        assert len(frozen) == 1 and submits == []
        assert not job.done_event.is_set() and manager._active_jobs == 1
        lease_id = job.capability_lease_id
        assert capability_leases().get(lease_id) is not None
        assert manager._load(task)['status'] == 'running'
        scope = job.command_scope
        records = list(scope._service._records.values())
        assert any(record.waiter.is_alive() for record in records)
        backend.recover()
        assert job.done_event.wait(3)
        assert job.command_scope is scope
        assert len(submits) == len(frozen) == 1 and submits[0].state is frozen[0]
        assert releases.count(lease_id) == 1
        assert len(creates) == 1
        assert manager._load(task)['child_result']['cost'] == .125
        assert all(not record.waiter.is_alive() and not record.reader.is_alive() for record in records)
        assert backend.closed and manager._active_jobs == 0
    finally:
        backend.recover()
        _finish_process_manager(manager)


def test_stop_after_freeze_only_requests_command_cleanup(tmp_path, monkeypatch):
    import copy
    from nz_coder.foundation.capability_lease import capability_leases
    from tests.fixtures.process_lifecycle import wait_until

    manager, backend, _, frozen, submits, _ = _blocked_command_manager(tmp_path, monkeypatch)
    try:
        task = _start_process(manager, 'complete')
        assert backend.close_entered.wait(6)
        job = manager._jobs[task]
        wait_until(lambda: job.frozen)
        before = copy.deepcopy(frozen[0])
        scope = job.command_scope
        callers = []
        original_stop = scope.request_stop
        def stop():
            callers.append(threading.get_ident())
            original_stop()
        monkeypatch.setattr(scope, 'request_stop', stop)
        result = manager.stop([task], timeout_ms=0)
        assert threading.get_ident() in callers
        assert result.metadata['requested_task_ids'] == []
        assert result.metadata['unsettled_task_ids'] == [task]
        assert not job.cancel_event.is_set()
        assert frozen[0] == before and submits == []
        lease_id = job.capability_lease_id
        revoked = capability_leases().revoke_workspace(tmp_path)
        assert revoked.failed >= 1
        assert capability_leases().get(lease_id) is not None
        assert frozen[0] == before
        backend.recover()
        assert job.done_event.wait(3)
        assert manager._load(task)['status'] == 'completed'
        assert manager._load(task)['child_result']['usage']['total'] == 18
        assert manager._load(task)['child_result']['cost'] == .125
    finally:
        backend.recover()
        _finish_process_manager(manager)


def test_pre_freeze_cancel_keeps_existing_manager_precedence(tmp_path, monkeypatch):
    from tests.fixtures.process_lifecycle import wait_until

    manager, backend, _, frozen, submits, _ = _blocked_command_manager(tmp_path, monkeypatch)
    try:
        task = _start_process(manager, 'complete-and-block')
        _process_ready(manager, task, tmp_path)
        job = manager._jobs[task]
        assert job.process.is_alive() and not job.frozen
        manager.stop([task], timeout_ms=10)
        wait_until(lambda: job.frozen, timeout=3)
        assert frozen[0]['status'] == 'cancelled'
        assert frozen[0]['child_result']['cost'] == .125
        assert submits == [] and not job.done_event.is_set()
        backend.recover()
        assert job.done_event.wait(3)
        assert manager._load(task)['status'] == 'cancelled'
    finally:
        backend.recover()
        _finish_process_manager(manager)


def test_session_delete_and_manager_close_keep_original_pending_scope(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import agent_manager as module
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.lifecycle import delete_session
    from nz_coder.state.sessions import save_session
    from tests.fixtures.process_lifecycle import wait_until

    manager, backend, _, _, _, _ = _blocked_command_manager(tmp_path, monkeypatch)
    monkeypatch.setitem(module._INSTANCES, (tmp_path.resolve(), 'parent'), manager)
    with scoped_workdir(tmp_path):
        parent_state = save_session([], session_id='parent', activate=False)
        task = _start_process(manager, 'complete')
        try:
            assert backend.close_entered.wait(6)
            job = manager._jobs[task]
            wait_until(lambda: job.frozen)
            scope = job.command_scope
            worktree = Path(manager._load_raw(task)['worktree']['path'])
            with pytest.raises(RuntimeError):
                manager.close(timeout=0)
            assert job.command_scope is scope and module._INSTANCES[(tmp_path.resolve(), 'parent')] is manager
            original_close = manager.close
            with monkeypatch.context() as fault:
                fault.setattr(manager, 'close', lambda timeout=5: original_close(timeout=.01))
                with pytest.raises(RuntimeError):
                    delete_session('parent')
            assert parent_state.exists() and worktree.exists()
            assert not job.done_event.is_set()
            backend.recover()
            assert job.done_event.wait(3)
            assert delete_session('parent')
            assert not parent_state.exists() and not worktree.exists()
        finally:
            backend.recover()
            _finish_process_manager(manager)


def test_settlement_disk_failure_after_commands_does_not_reexecute_or_double_charge(tmp_path, monkeypatch):
    from tests.fixtures.process_lifecycle import wait_until

    manager, backend, creates, frozen, submits, _ = _blocked_command_manager(tmp_path, monkeypatch)
    blocked, failed = threading.Event(), threading.Event()
    blocked.set()
    original = manager._save
    def save(state):
        if state.get('background_result') and blocked.is_set():
            failed.set()
            raise OSError('controlled A2 disk failure')
        return original(state)
    monkeypatch.setattr(manager, '_save', save)
    try:
        task = _start_process(manager, 'complete')
        assert backend.close_entered.wait(6)
        job = manager._jobs[task]
        wait_until(lambda: job.frozen)
        assert not failed.is_set() and submits == []
        backend.recover()
        assert failed.wait(3)
        assert backend.closed and not job.done_event.is_set()
        assert manager._active_jobs == 1 and job.capability_lease_id
        assert len(submits) == 1 and submits[0].state is frozen[0]
        assert len(creates) == len(list(tmp_path.glob('*.transport'))) == 1
        blocked.clear()
        manager._settlements.wake(recheck=True)
        assert job.done_event.wait(3)
        assert manager._load(task)['child_result']['cost'] == .125
        assert manager._load(task)['child_result']['usage']['total'] == 18
        assert len(submits) == len(creates) == 1
    finally:
        blocked.clear()
        backend.recover()
        manager._settlements.wake(recheck=True)
        _finish_process_manager(manager)


def test_event_callback_failure_keeps_resource_gate(tmp_path, monkeypatch):
    from tests.fixtures.process_lifecycle import wait_until

    manager, backend, _, _, submits, _ = _blocked_command_manager(tmp_path, monkeypatch)
    errors = []
    class Publisher:
        interaction_run_id = 'origin'
        agent_id = 'child'
        agent_invocation_id = 'child-invocation'
        parent_agent_invocation_id = 'parent-invocation'
        def for_child(self, child):
            return self
        def publish(self, event_type, properties=None):
            if event_type.startswith('process.'):
                errors.append(event_type)
                raise RuntimeError('controlled subscriber failure')
    manager.bind_event_publisher(Publisher())
    try:
        task = _start_process(manager, 'complete')
        assert backend.close_entered.wait(6)
        job = manager._jobs[task]
        wait_until(lambda: job.frozen)
        assert errors and submits == [] and not job.done_event.is_set()
        assert job.command_scope and manager._active_jobs == 1
        backend.recover()
        assert job.done_event.wait(3)
        assert manager._load(task)['status'] == 'completed'
    finally:
        backend.recover()
        _finish_process_manager(manager)


def test_process_parent_deadline_releases_capacity_after_exit(tmp_path, monkeypatch):
    manager = _process_manager(tmp_path, monkeypatch, timeout=2)
    terminal_capacity = []
    original_record = manager._workflow._on_event

    def record(event, snapshot):
        if event["type"] == "task_terminal":
            terminal_capacity.append(manager._active_jobs)
        return original_record(event, snapshot)

    monkeypatch.setattr(manager._workflow, "_on_event", record)
    try:
        started = manager.start([
            {"prompt": "block", "target_paths": ["one.py"], "isolation": "process"},
            {"prompt": "complete", "target_paths": ["two.py"], "isolation": "process"},
        ])
        first, second = started.metadata["task_ids"]
        assert manager._jobs[first].done_event.wait(4), "parent never enforced its deadline"
        assert manager._jobs[second].done_event.wait(3)
        assert manager._load(first)["status"] == "timeout"
        assert manager._load(second)["status"] == "completed"
        assert manager._jobs[first].process.exitcode is not None
        assert manager._active_jobs == 0
        assert terminal_capacity == [1, 1]
        assert len([event for event in manager._workflow.events() if event["type"] == "task_terminal"]) == 2
    finally:
        _finish_process_manager(manager)


def test_process_cleanup_pending_keeps_capacity_and_lease(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases

    manager = _process_manager(tmp_path, monkeypatch)
    try:
        session_id = _start_process(manager, "block")
        _process_ready(manager, session_id, tmp_path)
        job = manager._jobs[session_id]
        lease_id = job.capability_lease_id
        with monkeypatch.context() as fault:
            fault.setattr(job.process, "terminate", lambda: None)
            fault.setattr(job.process, "kill", lambda: None)
            stopped = manager.stop([session_id], timeout_ms=10)
            assert stopped.metadata["unsettled_task_ids"] == [session_id]
            with pytest.raises(RuntimeError):
                manager.close(timeout=0.01)
            assert job.process.is_alive()
            assert manager._active_jobs == 1
            assert capability_leases().get(lease_id) is not None
        assert job.done_event.wait(3)
        assert job.process.is_alive() is False
        assert manager._active_jobs == 0
        assert capability_leases().get(lease_id) is None
    finally:
        _finish_process_manager(manager)


def test_process_result_is_invocation_scoped(tmp_path, monkeypatch):
    from nz_coder.foundation.user_paths import user_storage_layout

    manager = _process_manager(tmp_path, monkeypatch, concurrency=2)
    outside = tmp_path / "do-not-touch.json"
    outside.write_text("sentinel")
    try:
        first = _start_process(manager, "complete", path="one.py", result_path=str(outside))
        second = _start_process(manager, "complete", path="two.py")
        assert manager._jobs[first].done_event.wait(3)
        assert manager._jobs[second].done_event.wait(3)
        manager._launch(manager._load_raw(first), {"prompt": "complete"})
        assert manager._jobs[first].done_event.wait(3)
        paths = [Path(p.read_text()) for p in tmp_path.glob("*.transport")]
        assert len(paths) == len(set(paths)) == 3
        expected_root = user_storage_layout(tmp_path).workspace_state / "child-process-results"
        assert all(path.is_relative_to(expected_root) and path.name == "result.json" for path in paths)
        _await_settlement(lambda: all(not path.parent.exists() for path in paths))
        assert all(not path.parent.exists() for path in paths)
        assert outside.read_text() == "sentinel"
    finally:
        _finish_process_manager(manager)


@pytest.mark.parametrize("operation", ["status", "events", "wait-result", "wait", "stop"])
def test_live_child_terminal_is_hidden_on_all_public_surfaces(tmp_path, monkeypatch, operation):
    from concurrent.futures import CancelledError
    from nz_coder.runtime.agent import agent_manager as module

    manager = _process_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "_STOP_SETTLE_SECONDS", 0.03)
    try:
        session_id = _start_process(manager)
        _process_ready(manager, session_id, tmp_path)
        raw = manager._load_raw(session_id)
        assert raw["status"] == "completed" and raw["child_result"]["usage"]["total"] == 18
        assert "tokens" not in raw
        if operation == "wait-result":
            cancel = threading.Event()
            result = []

            def wait_result():
                try:
                    manager.wait_for_result(session_id, cancel)
                except CancelledError:
                    result.append("cancelled")

            waiter = threading.Thread(target=wait_result)
            waiter.start()
            waiter.join(0.03)
            try:
                assert waiter.is_alive(), "wait returned while child was alive"
            finally:
                cancel.set()
                waiter.join(1)
            assert result == ["cancelled"]
        else:
            with monkeypatch.context() as fault:
                fault.setattr(manager._jobs[session_id].process, "terminate", lambda: None)
                fault.setattr(manager._jobs[session_id].process, "kill", lambda: None)
                if operation == "status":
                    result = manager.status([session_id])
                elif operation == "events":
                    result = manager.events()
                elif operation == "wait":
                    result = manager.wait([session_id], timeout_ms=10)
                else:
                    result = manager.stop([session_id], timeout_ms=10)
                assert result.metadata.get("child_results", []) == []
                snapshot = result.metadata["workflow_snapshot"]
                assert snapshot["counts"]["completed"] == 0
                assert snapshot["counts"]["running"] == 1
                if operation in {"wait", "stop"}:
                    assert result.metadata["unsettled_task_ids"] == [session_id]
                    assert manager._jobs[session_id].cancel_event.is_set()
                assert manager._jobs[session_id].process.is_alive()
                assert manager._active_jobs == 1
        assert manager._load_raw(session_id)["child_result"]["usage"]["total"] == 18
    finally:
        _finish_process_manager(manager)


@pytest.mark.parametrize("cancel", [False, True])
def test_process_terminal_preserves_durable_canonical_usage(tmp_path, monkeypatch, cancel):
    manager = _process_manager(tmp_path, monkeypatch)
    try:
        session_id = _start_process(manager, "complete-and-block" if cancel else "complete")
        _process_ready(manager, session_id, tmp_path)
        if cancel:
            manager.stop([session_id], timeout_ms=2000)
        assert manager._jobs[session_id].done_event.wait(3)
        state = manager._load(session_id)
        assert state["status"] == ("cancelled" if cancel else "completed")
        canonical = state["child_result"]
        assert canonical["usage"] == {"input": 11, "output": 7, "total": 18}
        assert canonical["cost"] == 0.125
        assert canonical["structured"] == {"answer": 42}
        assert canonical["status"] == state["status"]
        assert canonical["interrupted"] is cancel
    finally:
        _finish_process_manager(manager)


def test_thread_default_and_public_result_contract_remain_stable(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.agent import subagent

    def fake_run(prompt, **kwargs):
        state = subagent._load_subagent_state("parent", kwargs["session_id"], tmp_path)
        state["status"] = "completed"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "thread-done"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    try:
        started = manager.start([{"prompt": "offline", "target_paths": ["app.py"]}])
        session_id = started.metadata["task_ids"][0]
        assert manager._jobs[session_id].done_event.wait(2)
        assert manager._load_raw(session_id)["isolation"] == "thread"
        assert manager._jobs[session_id].process is None
        result = manager.status([session_id])
        assert result.metadata["child_results"][0]["final_text"] == "thread-done"
    finally:
        manager.close()


def test_cancelled_task_does_not_spawn_when_capacity_is_free(tmp_path, monkeypatch):
    manager = _process_manager(tmp_path, monkeypatch)
    try:
        with manager._lock:
            session_id = _start_process(manager, "block")
            manager.cancel([session_id])
        assert manager._jobs[session_id].done_event.wait(2)
        assert manager._jobs[session_id].process is None
        assert manager._load(session_id)["status"] == "cancelled"
        assert not list(tmp_path.glob("*.transport"))
    finally:
        _finish_process_manager(manager)


@pytest.mark.parametrize("case", ["crash", "invalid-transport", "cancel", "timeout"])
def test_parent_outcome_replaces_success_text_but_keeps_settled_usage(tmp_path, monkeypatch, case):
    manager = _process_manager(tmp_path, monkeypatch, timeout=1 if case == "timeout" else 10)
    prompts = {"crash": "complete-and-crash", "invalid-transport": "complete-with-invalid-transport"}
    try:
        session_id = _start_process(manager, prompts.get(case, "complete-and-block"))
        _process_ready(manager, session_id, tmp_path)
        if case == "cancel":
            manager.stop([session_id], timeout_ms=2000)
        assert manager._jobs[session_id].done_event.wait(3)
        state = manager._load(session_id)
        expected = {"crash": "error", "invalid-transport": "error", "cancel": "cancelled", "timeout": "timeout"}[case]
        assert state["status"] == expected
        for response in [manager.status([session_id]), manager.wait([session_id]), manager.stop([session_id])]:
            result = response.metadata["child_results"][0]
            assert result["status"] == expected
            assert result["final_text"] != "done"
            assert result["digest"] != "successful summary"
            assert result["digest"] in result["final_text"]
            assert result["summary_kind"] == "excerpt"
            assert result["final_text_truncated"] is False
            assert result["usage"]["total"] == 18 and result["cost"] == 0.125
            assert result["structured"] == {"answer": 42}
            if case in {"crash", "invalid-transport"}:
                code = "child_process_exit" if case == "crash" else "child_process_protocol_error"
                assert result["public_error"]["code"] == code
            else:
                assert "public_error" not in result
            assert "successful summary" not in repr(response.metadata["workflow_snapshot"])
        assert state["digest"] == state["child_result"]["digest"]
    finally:
        _finish_process_manager(manager)


def test_stop_after_supervisor_result_before_parent_handoff_is_cancelled(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.process_supervisor import SubagentProcessSupervisor

    manager = _process_manager(tmp_path, monkeypatch)
    reached = threading.Event()
    release = threading.Event()
    original_wait = SubagentProcessSupervisor.wait

    def gated_wait(supervisor, *args, **kwargs):
        outcome = original_wait(supervisor, *args, **kwargs)
        reached.set()
        assert release.wait(3)
        return outcome

    monkeypatch.setattr(SubagentProcessSupervisor, "wait", gated_wait)
    try:
        session_id = _start_process(manager, "complete")
        assert reached.wait(3)
        stopped = manager.stop([session_id], timeout_ms=0)
        assert stopped.metadata["requested_task_ids"] == [session_id]
        release.set()
        assert manager._jobs[session_id].done_event.wait(3)
        result = manager.status([session_id]).metadata["child_results"][0]
        assert result["status"] == "cancelled"
        assert result["interrupted"] is True
        assert result["usage"]["total"] == 18
    finally:
        release.set()
        _finish_process_manager(manager)


@pytest.mark.parametrize("operation", ["workflow-stop", "parent-wait"])
def test_live_canonical_terminal_remains_a_lifecycle_source(tmp_path, monkeypatch, operation):
    manager = _process_manager(tmp_path, monkeypatch)
    try:
        manager.begin_workflow_run("flow", "offline")
        session_id = _start_process(manager, workflow_run_id="flow")
        _process_ready(manager, session_id, tmp_path)
        assert manager._load_raw(session_id)["status"] == "completed"
        assert manager._load(session_id)["status"] == "running"
        if operation == "parent-wait":
            assert manager.has_worker_wake_source() is True
        else:
            assert manager.stop_workflow_run("flow") is True
            assert manager._jobs[session_id].cancel_event.is_set()
            assert manager._jobs[session_id].done_event.wait(3)
            assert manager._load(session_id)["status"] == "cancelled"
    finally:
        _finish_process_manager(manager)


def _await_settlement(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_parent_state_save_failure_retries_without_changing_business_outcome(tmp_path, monkeypatch):
    manager = _process_manager(tmp_path, monkeypatch)
    original = manager._save
    attempts = []

    def save(state):
        if state.get("background_result"):
            attempts.append(state.copy())
            if len(attempts) == 1:
                raise OSError("injected transient parent save failure")
        return original(state)

    monkeypatch.setattr(manager, "_save", save)
    try:
        task = _start_process(manager, "complete")
        assert manager._jobs[task].done_event.wait(3)
        result = manager.status([task]).metadata["child_results"][0]
        assert result["status"] == "completed"
        assert result["usage"]["total"] == 18 and result["cost"] == 0.125
        assert len(attempts) == 2
        assert attempts[0]["finished_at"] == attempts[1]["finished_at"]
        assert len(list(tmp_path.glob("*.transport"))) == 1
    finally:
        _finish_process_manager(manager)


def test_frozen_pending_stop_and_close_keep_resources_until_commit(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases

    manager = _process_manager(tmp_path, monkeypatch)
    blocked = threading.Event()
    blocked.set()
    failed = threading.Event()
    original = manager._save

    def save(state):
        if state.get("background_result") and blocked.is_set():
            failed.set()
            raise OSError("injected persistent parent save failure")
        return original(state)

    monkeypatch.setattr(manager, "_save", save)
    try:
        task = _start_process(manager, "complete")
        assert failed.wait(3)
        job = manager._jobs[task]
        assert not job.done_event.is_set()
        assert job.frozen and not job.process.is_alive()
        lease_id = job.capability_lease_id
        stopped = manager.stop([task], timeout_ms=10)
        assert stopped.metadata["requested_task_ids"] == []
        assert stopped.metadata["unsettled_task_ids"] == [task]
        assert not job.cancel_event.is_set()
        cancellation = manager.cancel([task])
        assert cancellation.metadata["requested_task_ids"] == []
        assert "cancellation requested" not in str(cancellation).lower()
        with pytest.raises(RuntimeError):
            manager.close(timeout=0.02)
        assert manager._active_jobs == 1
        assert capability_leases().get(lease_id) is not None
        assert manager._settlements._thread.is_alive()
        with pytest.raises(ValueError, match="settle|active"):
            manager._launch(manager._load_raw(task), {"prompt": "early-error"})
        blocked.clear()
        manager._settlements.wake(recheck=True)
        assert job.done_event.wait(3)
        assert manager._load(task)["status"] == "completed"
        assert manager._active_jobs == 0 and capability_leases().get(lease_id) is None
    finally:
        blocked.clear()
        if hasattr(manager, "_settlements"):
            manager._settlements.wake(recheck=True)
        _finish_process_manager(manager)


def test_committed_snapshot_failure_does_not_rewrite_task_or_hold_slot(tmp_path, monkeypatch):
    manager = _process_manager(tmp_path, monkeypatch)
    original = manager._workflow._persist_snapshot

    def persist():
        if manager._workflow.committed_items():
            raise OSError("injected snapshot failure after terminal")
        return original()

    try:
        with monkeypatch.context() as fault:
            fault.setattr(manager._workflow, "_persist_snapshot", persist)
            task = _start_process(manager, "complete")
            assert manager._jobs[task].done_event.wait(3)
            assert manager._load_raw(task)["status"] == "completed"
            assert manager._active_jobs == 0
            assert manager.status([task]).metadata["workflow_snapshot"]["counts"]["completed"] == 1
        _await_settlement(lambda: not manager._workflow.needs_recovery)
        assert len([e for e in manager._workflow.events() if e["type"] == "task_terminal"]) == 1
    finally:
        _finish_process_manager(manager)


def test_manager_adopts_same_transport_canonical_when_child_save_failed(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import process_supervisor
    from tests.fixtures import subagent_process_targets as targets

    manager = _process_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(process_supervisor, "_run_subagent_process", targets.canonical_result)
    try:
        task = _start_process(manager, "complete")
        assert manager._jobs[task].done_event.wait(3)
        result = manager.status([task]).metadata["child_results"][0]
        assert result["status"] == "completed"
        assert result["usage"]["total"] == 18 and result["structured"] == {"answer": 42}
    finally:
        _finish_process_manager(manager)


def test_new_invocation_early_error_does_not_adopt_old_result_or_lose_cost_baseline(tmp_path, monkeypatch):
    manager = _process_manager(tmp_path, monkeypatch)
    try:
        task = _start_process(manager, "complete")
        assert manager._jobs[task].done_event.wait(3)
        first_id = manager._load_raw(task)["settlement_id"]
        for _ in range(2):
            previous = manager._load_raw(task)
            manager._launch(previous, {"prompt": "early-error"})
            assert manager._jobs[task].done_event.wait(3)
            state = manager._load_raw(task)
            assert state["settlement_id"] != previous["settlement_id"]
            result = manager.status([task]).metadata["child_results"][0]
            assert result["status"] == "error"
            assert "structured" not in result and result["usage"] == {}
            assert "cost" not in result
            assert (tmp_path / f"{task}.prior-cost").read_text() == "0.125"
        terminals = [e for e in manager._workflow.events() if e["type"] == "task_terminal"]
        assert len({e["data"]["settlement_id"] for e in terminals}) == 3
        assert terminals[0]["data"]["settlement_id"] == first_id
    finally:
        _finish_process_manager(manager)


@pytest.mark.parametrize("case", ["running", "error", "missing", "uncommitted", "new-admission", "unadmitted-id",
                                  "foreign-task", "foreign-session", "foreign-parent",
                                  "uncommitted-foreign-task", "uncommitted-foreign-session", "uncommitted-foreign-parent"])
def test_startup_arbitrates_journal_before_checkpoint_without_query_writes(tmp_path, monkeypatch, case):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    setup = BackgroundAgentManager(tmp_path, "parent")
    state = {"session_id": "historical", "parent_session_id": "parent", "background": True,
             "display_name": "offline", "status": "completed", "settlement_id": "one",
             "child_result_settlement_id": "one",
             "child_result": {"task_id": "historical", "session_id": "historical", "parent_session_id": "parent",
                              "name": "offline", "status": "completed", "final_text": "done", "usage": {"total": 18}}}
    setup._workflow.record_task("task_queued", {**state, "status": "queued"})
    if not case.startswith("uncommitted"):
        setup._workflow.commit_terminal(state, event_id="terminal-one")
    if case == "new-admission":
        state["settlement_id"] = "two"
        setup._workflow.record_task("task_queued", {**state, "status": "queued"})
    if case == "running":
        state["status"] = "running"
    elif case == "error":
        state["status"] = "error"
        state["child_result"]["status"] = "error"
    elif case == "unadmitted-id":
        state["settlement_id"] = "not-admitted"
    elif "foreign-" in case:
        field = {"task": "task_id", "session": "session_id", "parent": "parent_session_id"}[case.rsplit("-", 1)[-1]]
        state["child_result"][field] = "another-owner"
        state["child_result"]["cost"] = 12.5
        state["child_result"]["structured"] = {"foreign": True}
        if case.endswith("foreign-parent"):
            state["parent_session_id"] = "another-owner"
    if case != "missing":
        setup._save(state)
    setup.close()
    actual_save = subagent._save_subagent_state
    manager = None

    def blocked_save(*args, **kwargs):
        raise OSError("query repair writes unavailable")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(subagent, "_save_subagent_state", blocked_save)
            manager = BackgroundAgentManager(tmp_path, "parent")
            responses = [manager.status(["historical"]), manager.events(),
                         manager.wait(["historical"], timeout_ms=10), manager.stop(["historical"], timeout_ms=0)]
            completed = not case.startswith("uncommitted") and case != "new-admission"
            for response in responses:
                assert hasattr(response, "metadata"), response
                snapshot = response.metadata["workflow_snapshot"]
                assert snapshot["counts"]["completed"] == int(completed)
                if completed:
                    assert snapshot["tokens"]["spent"] == 18
                else:
                    assert all(result["status"] != "completed" for result in response.metadata.get("child_results", []))
            if case in {"missing", "error", "unadmitted-id"} or "foreign-" in case:
                assert responses[0].metadata["public_error"]["code"] == "child_result_unavailable"
                assert responses[0].metadata["child_results"] == []
            if case == "running":
                assert responses[0].metadata["child_results"][0]["status"] == "completed"
        if manager is not None and hasattr(manager, "_settlements"):
            manager._settlements.wake(recheck=True)
    finally:
        monkeypatch.setattr(subagent, "_save_subagent_state", actual_save)
        if manager is not None:
            manager.close(timeout=2)


def test_pending_startup_repair_blocks_new_invocation_until_recovered(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.state.memory_scope import MemoryScope

    setup = BackgroundAgentManager(tmp_path, "parent")
    state = subagent._new_subagent_state("parent", "general-purpose", None,
                                         memory_scope=MemoryScope.for_workspace(tmp_path))
    state.update(background=True, settlement_id="old", status="queued", claimed_paths=["app.py"])
    setup._workflow.record_task("task_queued", state)
    setup._save(state)
    setup.close()
    actual_save = subagent._save_subagent_state
    blocked = threading.Event()
    blocked.set()
    failed = threading.Event()
    release_execution = threading.Event()

    def save(parent, candidate, workspace):
        if candidate.get("status") == "interrupted" and candidate.get("settlement_id") == "old" and blocked.is_set():
            failed.set()
            raise OSError("startup repair unavailable")
        return actual_save(parent, candidate, workspace)

    def run(*args, **kwargs):
        assert release_execution.wait(2)
        return "offline execution"

    monkeypatch.setattr(subagent, "_save_subagent_state", save)
    monkeypatch.setattr(subagent, "run_subagent", run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    try:
        assert failed.wait(1)
        with pytest.raises(ValueError, match="settle"):
            manager._launch(manager._load_raw(state["session_id"]), {"prompt": "offline"})
        assert manager._load_raw(state["session_id"])["settlement_id"] == "old"
        assert not manager._jobs
        blocked.clear()
        manager._settlements.wake(recheck=True)
        _await_settlement(lambda: not manager._settlements._pending)
        assert manager._load_raw(state["session_id"])["status"] == "interrupted"
    finally:
        blocked.clear()
        release_execution.set()
        manager._settlements.wake(recheck=True)
        manager.close(timeout=2)


def test_close_rechecks_repaired_hard_block_before_waiting_for_jobs(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.workflows import workflow_process
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.tools import ToolOutput

    manager = BackgroundAgentManager(tmp_path, "parent")
    original_limit = workflow_process._MAX_JOURNAL_ENTRIES

    def run(*args, **kwargs):
        task = kwargs["session_id"]
        raw = manager._load_raw(task)
        monkeypatch.setattr(workflow_process, "_MAX_JOURNAL_ENTRIES", len(manager._workflow.events()))
        return ToolOutput("done", metadata={"child_result_settlement_id": raw["settlement_id"], "child_result": {
            "task_id": task, "session_id": task, "parent_session_id": "parent", "name": "offline",
            "status": "completed", "final_text": "done", "usage": {"total": 18}}})

    monkeypatch.setattr(subagent, "run_subagent", run)
    try:
        started = manager.start([{"prompt": "offline", "target_paths": ["app.py"]}])
        task = started.metadata["task_ids"][0]
        identity = manager._jobs[task].settlement_id
        _await_settlement(lambda: manager._settlements.error_for(identity) is not None)
        assert manager._settlements.error_for(identity).code == "workflow_journal_capacity"
        assert not manager._jobs[task].done_event.is_set()
        monkeypatch.setattr(workflow_process, "_MAX_JOURNAL_ENTRIES", original_limit)
        manager.close(timeout=1)
        assert manager._closed and not manager._settlements._thread.is_alive()
        assert not manager._settlements._pending
        assert manager._load_raw(task)["status"] == "completed"
    finally:
        monkeypatch.setattr(workflow_process, "_MAX_JOURNAL_ENTRIES", original_limit)
        manager._settlements.wake(recheck=True)
        manager.close(timeout=2)


def test_draining_recovery_owner_rejects_new_execution_before_admission(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    release = threading.Event()
    exiting = threading.Event()
    original = manager._settlements._run

    def run():
        original()
        exiting.set()
        assert release.wait(2)

    monkeypatch.setattr(manager._settlements, "_run", run)
    monkeypatch.setattr(subagent, "run_subagent", lambda *args, **kwargs: "offline")
    try:
        with pytest.raises(RuntimeError, match="recovery"):
            manager.close(timeout=0.03)
        assert exiting.is_set() and manager._settlements._thread.is_alive()
        assert manager.start([{"prompt": "offline", "target_paths": ["app.py"]}]) == "Error: background Agent manager is closed"
        assert not manager._jobs
        assert not [event for event in manager._workflow.events() if event["type"] == "task_queued"]
    finally:
        release.set()
        manager._settlements._thread.join(2)
        manager._settlements.wake(recheck=True)
        manager.close(timeout=2)


def test_live_stop_signals_before_failed_cancel_checkpoint(tmp_path, monkeypatch):
    manager = _process_manager(tmp_path, monkeypatch)
    original = manager._save

    def save(state):
        if state.get("status") == "cancel_requested":
            raise OSError("cancellation checkpoint unavailable")
        return original(state)

    try:
        task = _start_process(manager, "block")
        _process_ready(manager, task, tmp_path)
        monkeypatch.setattr(manager, "_save", save)
        result = manager.stop([task], timeout_ms=2000)
        assert result.metadata["requested_task_ids"] == [task]
        assert manager._jobs[task].cancel_event.is_set()
        assert manager._jobs[task].done_event.wait(3)
        assert manager._load(task)["status"] == "cancelled"
    finally:
        _finish_process_manager(manager)


def test_invalid_thread_canonical_settles_as_safe_error(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.tools import ToolOutput

    def run(_prompt, **kwargs):
        state = subagent._load_subagent_state("parent", kwargs["session_id"], tmp_path)
        return ToolOutput("sentinel-secret", metadata={"child_result_settlement_id": state["settlement_id"],
                          "child_result": {"task_id": "foreign", "name": "offline", "status": "completed", "final_text": "sentinel-secret"}})

    monkeypatch.setattr(subagent, "run_subagent", run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    try:
        started = manager.start([{"prompt": "offline", "target_paths": ["app.py"]}])
        task = started.metadata["task_ids"][0]
        assert manager._jobs[task].done_event.wait(1)
        result = manager.status([task]).metadata["child_results"][0]
        assert result["status"] == "error" and result["public_error"]
        assert "sentinel-secret" not in repr(result)
        assert manager._active_jobs == 0
    finally:
        # RED 阶段损坏结果会让执行线程退出；清理实际已结束的测试线程及租约。
        for live in manager._jobs.values():
            live.thread.join(2)
            if not live.done_event.is_set() and not live.thread.is_alive():
                capability_leases().release(live.capability_lease_id)
                live.capability_lease_id = ""
                manager._active_jobs = 0
                live.done_event.set()
        manager.close(timeout=2)


def test_direct_launch_registration_is_serialized_with_admission(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    reached, release = threading.Event(), threading.Event()
    original_limits = manager._run_limits
    original_start = threading.Thread.start
    results, owned = [], []
    count = 0

    def limits(snapshot=None):
        nonlocal count
        count += 1
        if count == 1:
            reached.set()
            assert release.wait(2)
        return original_limits(snapshot)

    def start(thread):
        if thread.name.startswith("nz-subagent-"):
            return None
        return original_start(thread)

    def launch():
        try:
            manager._launch({"session_id": "shared", "background": True, "agent_type": "explore"}, {"prompt": "offline"})
        except ValueError:
            results.append("rejected")
        else:
            owned.append(manager._jobs["shared"])
            results.append("started")

    monkeypatch.setattr(manager, "_run_limits", limits)
    monkeypatch.setattr(threading.Thread, "start", start)
    first, second = threading.Thread(target=launch), threading.Thread(target=launch)
    try:
        first.start()
        assert reached.wait(1)
        second.start()
        time.sleep(0.04)
        assert not results, "second admission overtook registration of the first"
        release.set()
        first.join(2)
        second.join(2)
        assert sorted(results) == ["rejected", "started"]
    finally:
        release.set()
        first.join(2)
        if second.ident is not None:
            second.join(2)
        for live in owned:
            capability_leases().release(live.capability_lease_id)
            live.capability_lease_id = ""
            live.done_event.set()
        manager.close(timeout=2)


def test_background_task_events_project_standard_child_lifecycle(tmp_path):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.protocol.session_events import SessionEventBus

    bus = SessionEventBus(session_id="parent")
    subscription = bus.subscribe()
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.bind_event_bus(bus)
    snapshot = {"status": "running"}
    manager._bridge_workflow_event(
        {"type": "task_started", "task_id": "child-1"}, snapshot,
    )
    manager._bridge_workflow_event(
        {"type": "task_terminal", "task_id": "child-1"}, snapshot,
    )

    assert [subscription.get(timeout=0.1).type for _ in range(4)] == [
        "workflow.task.started",
        "session.child.started",
        "workflow.task.terminal",
        "session.child.finished",
    ]


def _wait_for_status(manager, session_id: str, expected: str, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = manager._load(session_id)
        if state.get("status") == expected:
            return state
        time.sleep(0.01)
    raise AssertionError(f"{session_id} did not reach {expected}: {manager._load(session_id)}")


def _worktree_root(workspace: Path) -> Path:
    from nz_coder.runtime.worktree import WorktreeManager

    return WorktreeManager(workspace).worktree_dir


def test_background_manager_starts_parallel_non_overlapping_tasks(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    gate = threading.Barrier(2)

    def fake_run(prompt, *, session_id, cancel_event, **kwargs):
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "running"
        subagent._save_subagent_state("parent", state, tmp_path)
        gate.wait(timeout=2)
        state["status"] = "completed"
        state["changed_files"] = list(state["claimed_paths"])
        subagent._save_subagent_state("parent", state, tmp_path)
        return f"finished {prompt}"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")

    result = manager.start([
        {"name": "one", "prompt": "edit a", "target_paths": ["a.py"]},
        {"name": "two", "prompt": "edit b", "target_paths": ["b.py"]},
    ])

    states = [state for state in manager._states() if state.get("background")]
    assert result.startswith("Started 2 background")
    assert len(states) == 2
    for state in states:
        _wait_for_status(manager, state["session_id"], "completed")
        deadline = time.monotonic() + 2
        settled = manager._load(state["session_id"])
        while "background_result" not in settled and time.monotonic() < deadline:
            time.sleep(0.01)
            settled = manager._load(state["session_id"])
        assert settled["background_result"].startswith("finished")
        assert settled["child_result"]["status"] == "completed"
    status = manager.status()
    assert status.count("[completed]") == 2
    assert len(status.metadata["child_results"]) == 2
    assert all(
        outcome["status"] == "completed"
        for outcome in status.metadata["child_results"]
    )
    assert "summary:" in status
    snapshot = status.metadata["workflow_snapshot"]
    assert snapshot["status"] == "completed"
    assert snapshot["counts"]["completed"] == 2
    assert snapshot["progress"]["spawned_agents"] == 2

    events = manager.events(after_sequence=1)
    assert events.metadata["workflow_events"]
    assert events.metadata["workflow_snapshot"]["revision"] == snapshot["revision"]


def test_background_completion_leaves_worker_wake_for_fast_child(tmp_path, monkeypatch):
    """A child that finishes before the parent yields must not lose its banner."""
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    def fake_run(prompt, *, session_id, **_kwargs):
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "completed"
        subagent._save_subagent_state("parent", state, tmp_path)
        return f"finished {prompt}"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.start([{
        "name": "probe",
        "prompt": "inspect parser",
        "target_paths": ["parser.py"],
    }])
    state = next(item for item in manager._states() if item.get("background"))
    _wait_for_status(manager, state["session_id"], "completed")
    deadline = time.monotonic() + 2
    wake = []
    while not wake and time.monotonic() < deadline:
        wake = manager.drain_messages("worker")
        if not wake:
            time.sleep(0.01)

    assert len(wake) == 1
    assert wake[0]["kind"] == "task_completed"
    assert wake[0]["sender"] == state["session_id"]
    assert "finished inspect parser" in wake[0]["content"]
    assert manager.has_worker_wake_source() is False


def test_background_manager_rejects_overlapping_requested_scopes(tmp_path):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    result = manager.start([
        {"prompt": "first", "target_paths": ["src"]},
        {"prompt": "second", "target_paths": ["src/api.py"]},
    ])

    assert result.startswith("Error: task 1 overlaps")
    assert manager._states() == []


def test_background_manager_routes_bounded_peer_messages(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    first = subagent._new_subagent_state("parent", "general-purpose", None)
    second = subagent._new_subagent_state("parent", "general-purpose", None)
    first.update({"background": True, "status": "running", "display_name": "api"})
    second.update({"background": True, "status": "queued", "display_name": "tests"})
    manager._save(first)
    manager._save(second)

    delivered = manager.send_message(
        sender=first["session_id"],
        recipient="tests",
        content="The endpoint now returns 204; update the assertion.",
    )
    peer_mail = manager.drain_messages(second["session_id"])

    assert delivered.startswith("Message peer-000001 delivered")
    assert len(peer_mail) == 1
    assert peer_mail[0]["sender"] == first["session_id"]
    assert peer_mail[0]["seen_by"] == [first["session_id"]]
    assert manager.drain_messages(second["session_id"]) == []


def test_worker_send_message_routes_coordinator_instruction_to_live_child(tmp_path):
    import nz_coder.runtime.agent.agent_manager as manager_module
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir

    manager = BackgroundAgentManager(tmp_path, "parent")
    child = subagent._new_subagent_state("parent", "general-purpose", None)
    child.update({
        "background": True,
        "status": "running",
        "display_name": "tests",
        "isolation": "thread",
    })
    manager._save(child)
    handler = getattr(manager_module, "send_message", None)

    assert callable(handler)
    with scoped_workdir(tmp_path), scoped_background_agent_manager(manager):
        delivered = handler(
            to="tests",
            content="Limit the repair to the warning order assertion.",
        )
    messages = manager.drain_messages(child["session_id"])

    assert delivered.startswith("Message peer-000001 delivered")
    assert len(messages) == 1
    assert messages[0]["sender"] == "worker"
    assert messages[0]["kind"] == "coordinator_instruction"


def test_worker_send_message_rejects_process_isolated_child(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    child = subagent._new_subagent_state("parent", "general-purpose", None)
    child.update({
        "background": True,
        "status": "running",
        "isolation": "process",
    })
    manager._save(child)

    result = manager.send_message(
        sender="worker",
        recipient=child["session_id"],
        content="Check the focused test before editing.",
    )

    assert result == "Error: online steering is unavailable for process-isolated children"


def test_shared_send_message_uses_bound_child_sender_identity(tmp_path):
    import nz_coder.runtime.agent.agent_manager as manager_module
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir

    manager = BackgroundAgentManager(tmp_path, "parent")
    sender = subagent._new_subagent_state("parent", "general-purpose", None)
    recipient = subagent._new_subagent_state("parent", "general-purpose", None)
    sender.update({"background": True, "status": "running", "isolation": "thread"})
    recipient.update({"background": True, "status": "running", "isolation": "thread"})
    manager._save(sender)
    manager._save(recipient)
    bind_sender = getattr(manager_module, "scoped_agent_message_sender", None)

    assert callable(bind_sender)
    with (
        scoped_workdir(tmp_path),
        scoped_background_agent_manager(manager),
        bind_sender(manager, sender["session_id"]),
    ):
        delivered = manager_module.send_message(
            to=recipient["session_id"],
            content="The parser contract changed.",
        )
    messages = manager.drain_messages(recipient["session_id"])

    assert delivered.startswith("Message peer-000001 delivered")
    assert messages[0]["sender"] == sender["session_id"]
    assert messages[0]["kind"] == "peer_message"


def test_child_loop_drains_worker_message_as_coordinator_instruction(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.execution.loop import AgentLoop

    manager = BackgroundAgentManager(tmp_path, "parent")
    child = subagent._new_subagent_state("parent", "general-purpose", None)
    child.update({"background": True, "status": "running", "isolation": "thread"})
    manager._save(child)
    manager.send_message(
        sender="worker",
        recipient=child["session_id"],
        content="Do not modify unrelated tests.",
    )
    events = []
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_checkpoint_lock = __import__("threading").RLock()
    loop._session_checkpoint_writer = None
    loop.verification_runtime = None
    loop.background_agents = object()
    loop._background_message_manager = manager
    loop._background_message_recipient = child["session_id"]
    loop.tracer = type(
        "Tracer",
        (),
        {"log": lambda self, event, **data: events.append((event, data))},
    )()
    messages = [{"role": "user", "content": "implement feature"}]

    drained = loop._drain_background_agent_messages(messages)

    assert drained == 1
    assert messages[-1][SYNTHETIC_USER_KEY] is True
    assert messages[-1]["_nz_coordinator_instruction"] is True
    assert '<coordinator-instruction id="peer-000001"' in messages[-1]["content"]
    assert "Do not modify unrelated tests." in messages[-1]["content"]
    assert events == [(
        "peer_messages_drained",
        {"recipient": child["session_id"], "count": 1},
    )]


def test_child_identity_binds_parent_manager_as_message_inbox(tmp_path):
    from types import SimpleNamespace

    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.agent.subagent import _bind_child_session_identity

    manager = BackgroundAgentManager(tmp_path, "parent")
    agent = SimpleNamespace(session_id="child-1", background_agents=object())

    with scoped_background_agent_manager(manager):
        _bind_child_session_identity(agent, "parent")

    assert agent.parent_session_id == "parent"
    assert agent._background_message_manager is manager
    assert agent._background_message_recipient == "child-1"


def test_background_manager_broadcasts_to_live_siblings_and_worker(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    sender = subagent._new_subagent_state("parent", "general-purpose", None)
    sibling = subagent._new_subagent_state("parent", "general-purpose", None)
    completed = subagent._new_subagent_state("parent", "general-purpose", None)
    sender.update({"background": True, "status": "running"})
    sibling.update({"background": True, "status": "running"})
    completed.update({"background": True, "status": "completed"})
    for state in (sender, sibling, completed):
        manager._save(state)

    result = manager.send_message(
        sender=sender["session_id"],
        recipient="*",
        content="Shared schema changed.",
    )

    assert sibling["session_id"] in result
    assert len(manager.drain_messages(sibling["session_id"])) == 1
    assert len(manager.drain_messages("worker")) == 1
    assert manager.drain_messages(completed["session_id"]) == []


def test_background_manager_rejects_forwarding_cycles_and_oversized_content(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    sender = subagent._new_subagent_state("parent", "general-purpose", None)
    target = subagent._new_subagent_state("parent", "general-purpose", None)
    sender.update({"background": True, "status": "running"})
    target.update({"background": True, "status": "running"})
    manager._save(sender)
    manager._save(target)

    cycle = manager.send_message(
        sender=sender["session_id"],
        recipient=target["session_id"],
        content="forward",
        seen_by=[sender["session_id"]],
    )
    oversized = manager.send_message(
        sender=sender["session_id"],
        recipient=target["session_id"],
        content="x" * 4001,
    )

    assert cycle == "Error: forwarding cycle detected for sender"
    assert oversized == "Error: content exceeds 4000 characters"


def test_agent_loop_drains_worker_mail_only_at_explicit_boundary(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.protocol.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.execution.loop import AgentLoop

    manager = BackgroundAgentManager(tmp_path, "parent")
    sender = subagent._new_subagent_state("parent", "general-purpose", None)
    sender.update({"background": True, "status": "running"})
    manager._save(sender)
    manager.send_message(
        sender=sender["session_id"],
        recipient="worker",
        content="Tests expose a backwards-compatibility failure.",
    )
    events = []
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_checkpoint_lock = __import__("threading").RLock()
    loop._session_checkpoint_writer = None
    loop.verification_runtime = None
    loop.background_agents = manager
    loop.tracer = type("Tracer", (), {"log": lambda self, event, **data: events.append((event, data))})()
    messages = [{"role": "user", "content": "implement feature"}]

    drained = loop._drain_background_agent_messages(messages)

    assert drained == 1
    assert len(messages) == 2
    assert messages[-1][SYNTHETIC_USER_KEY] is True
    assert messages[-1]["_nz_peer_message"] is True
    assert "untrusted child-Agent context" in messages[-1]["content"]
    assert events == [("peer_messages_drained", {"recipient": "worker", "count": 1})]
    assert loop._drain_background_agent_messages(messages) == 0


def test_background_manager_requests_cooperative_cancel(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    running = threading.Event()

    def fake_run(*args, session_id, cancel_event, **kwargs):
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "running"
        subagent._save_subagent_state("parent", state, tmp_path)
        running.set()
        assert cancel_event.wait(2)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "cancelled"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "cancelled"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.start([{"prompt": "work", "target_paths": ["app.py"]}])
    session_id = manager._states()[0]["session_id"]
    assert running.wait(1)

    result = manager.cancel([session_id])
    state = _wait_for_status(manager, session_id, "cancelled")

    assert "cancellation requested" in result
    assert state["background_result"] == "cancelled"
    assert state["child_result"]["status"] == "cancelled"


def test_workflow_wait_preserves_requested_result_order(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    def fake_run(prompt, *, session_id, **_kwargs):
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "completed"
        subagent._save_subagent_state("parent", state, tmp_path)
        return f"finished {prompt}"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.start([
        {"name": "one", "prompt": "first", "target_paths": ["a.py"]},
        {"name": "two", "prompt": "second", "target_paths": ["b.py"]},
    ])
    ids = [state["session_id"] for state in manager._states() if state.get("background")]

    result = manager.wait(list(reversed(ids)), timeout_ms=1000)

    assert result.metadata["timed_out_task_ids"] == []
    assert [item["task_id"] for item in result.metadata["child_results"]] == list(
        reversed(ids)
    )


def test_background_fanout_separates_lifetime_and_concurrency_caps(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_TASKS", 4)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", 2)
    active = 0
    peak = 0
    guard = threading.Lock()

    def fake_run(prompt, *, session_id, **_kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "error" if prompt == "fail" else "completed"
        subagent._save_subagent_state("parent", state, tmp_path)
        with guard:
            active -= 1
        if prompt == "fail":
            raise RuntimeError("isolated failure")
        return f"finished {prompt}"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    started = manager.start([
        {"prompt": "one", "target_paths": ["one.py"]},
        {"prompt": "fail", "target_paths": ["fail.py"]},
        {"prompt": "three", "target_paths": ["three.py"]},
    ])
    first_ids = started.metadata["task_ids"]
    waited = manager.wait(first_ids, timeout_ms=2000)

    assert peak == 2
    assert [item["task_id"] for item in waited.metadata["child_results"]] == first_ids
    assert [item["status"] for item in waited.metadata["child_results"]] == [
        "completed", "error", "completed",
    ]
    snapshot = waited.metadata["workflow_snapshot"]
    assert snapshot["progress"]["peak_active_agents"] == 2
    assert snapshot["progress"]["concurrency_cap"] == 2

    fourth = manager.start([
        {"prompt": "four", "target_paths": ["four.py"]},
    ])
    manager.wait(fourth.metadata["task_ids"], timeout_ms=1000)
    rejected = manager.start([
        {"prompt": "five", "target_paths": ["five.py"]},
    ])

    assert rejected.startswith("Error: maxAgents lifetime cap (4)")
    assert len([state for state in manager._states() if state.get("background")]) == 4


def test_concurrent_fanout_admission_cannot_oversubscribe_lifetime_cap(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_TASKS", 1)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", 1)
    release = threading.Event()

    def fake_run(*_args, session_id, **_kwargs):
        release.wait(1)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "completed"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "done"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    barrier = threading.Barrier(2)
    results = []

    def submit(path):
        barrier.wait(timeout=1)
        results.append(manager.start([{"prompt": path, "target_paths": [path]}]))

    threads = [
        threading.Thread(target=submit, args=("a.py",)),
        threading.Thread(target=submit, args=("b.py",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    release.set()

    assert sum(str(result).startswith("Started 1") for result in results) == 1
    assert sum(str(result).startswith("Error: maxAgents lifetime cap") for result in results) == 1
    assert len([state for state in manager._states() if state.get("background")]) == 1
    manager.close(timeout=2)


def test_workflow_wait_timeout_stops_and_settles_child(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    started = threading.Event()

    def fake_run(*_args, session_id, cancel_event, **_kwargs):
        started.set()
        assert cancel_event.wait(2)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "cancelled"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "stopped after wait timeout"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.start([{"prompt": "slow", "target_paths": ["app.py"]}])
    session_id = manager._states()[0]["session_id"]
    assert started.wait(1)

    result = manager.wait([session_id], timeout_ms=10)

    assert result.metadata["timed_out_task_ids"] == [session_id]
    assert result.metadata["unsettled_task_ids"] == []
    assert result.metadata["child_results"][0]["status"] == "cancelled"


def test_workflow_stop_is_idempotent_and_emits_one_terminal(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    started = threading.Event()

    def fake_run(*_args, session_id, cancel_event, **_kwargs):
        started.set()
        assert cancel_event.wait(2)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "cancelled"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "stopped"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.start([{"prompt": "slow", "target_paths": ["app.py"]}])
    session_id = manager._states()[0]["session_id"]
    assert started.wait(1)

    first = manager.stop([session_id], reason="test stop", timeout_ms=1000)
    second = manager.stop([session_id], reason="duplicate", timeout_ms=1000)
    events = manager.events().metadata["workflow_events"]

    assert first.metadata["requested_task_ids"] == [session_id]
    assert second.metadata["requested_task_ids"] == []
    assert sum(event["type"] == "task_terminal" for event in events) == 1
    assert sum(event["type"] == "task_cancel_requested" for event in events) == 1


def test_manager_close_settles_unawaited_children(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.subagent as subagent

    started = threading.Event()

    def fake_run(*_args, session_id, cancel_event, **_kwargs):
        started.set()
        assert cancel_event.wait(2)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "cancelled"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "closed"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    manager.start([{"prompt": "slow", "target_paths": ["app.py"]}])
    session_id = manager._states()[0]["session_id"]
    assert started.wait(1)

    manager.close(timeout=1)

    state = manager._load_raw(session_id)
    assert state["status"] == "cancelled"
    assert state["stop_reason"] == "parent Session closed"
    assert manager._jobs == {}


def test_agent_loop_close_cleans_background_before_other_resources():
    from nz_coder.runtime.execution.loop import AgentLoop

    order = []
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_checkpoint_lock = __import__("threading").RLock()
    loop._session_checkpoint_writer = None
    loop.verification_runtime = None
    loop.background_agents = type(
        "Background",
        (),
        {"request_retirement": lambda self: None,
         "close": lambda self, timeout=5.0: order.append(("background", timeout))},
    )()
    loop.event_bus = type(
        "Events", (), {"close": lambda self: order.append(("events", None))}
    )()
    loop.tracer = type(
        "Tracer", (), {"close": lambda self: order.append(("tracer", None))}
    )()

    loop.close()

    assert order == [
        ("background", 5.0),
        ("events", None),
        ("tracer", None),
    ]


def test_agent_loop_close_records_child_failure_and_continues_cleanup():
    """One failing cleanup stage cannot mask or prevent later cleanup stages."""
    from nz_coder.runtime.execution.loop import AgentLoop

    order = []
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_checkpoint_lock = __import__("threading").RLock()
    loop._session_checkpoint_writer = None
    loop.verification_runtime = None
    loop.background_agents = type(
        "Background",
        (),
        {"request_retirement": lambda self: None,
         "close": lambda self, timeout=5.0: (_ for _ in ()).throw(
            RuntimeError("child still running")
        )},
    )()
    loop._close_repo_intelligence = lambda: order.append("repo")
    loop.event_bus = type(
        "Events", (), {"close": lambda self: order.append("events")}
    )()
    loop.tracer = type(
        "Tracer", (), {"close": lambda self: order.append("tracer")}
    )()

    loop.close()

    assert order == ["repo", "events", "tracer"]


def test_dispose_keeps_manager_reachable_when_children_do_not_settle(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent.agent_manager import (
        background_agent_manager,
        dispose_background_agent_manager,
    )

    manager = background_agent_manager(tmp_path, "parent-dispose")

    def fail_close(timeout=5.0):
        raise RuntimeError("child still running")

    monkeypatch.setattr(manager, "close", fail_close)
    with pytest.raises(RuntimeError, match="child still running"):
        dispose_background_agent_manager(tmp_path, "parent-dispose")
    assert background_agent_manager(tmp_path, "parent-dispose") is manager

    monkeypatch.setattr(manager, "close", lambda timeout=5.0: None)
    dispose_background_agent_manager(tmp_path, "parent-dispose")
    replacement = background_agent_manager(tmp_path, "parent-dispose")
    assert replacement is not manager
    dispose_background_agent_manager(tmp_path, "parent-dispose")


def test_dispose_does_not_block_unrelated_workspace_registry(tmp_path, monkeypatch):
    """One slow Session close must not freeze every workspace manager lookup."""
    import threading

    from nz_coder.runtime.agent.agent_manager import (
        background_agent_manager,
        dispose_background_agent_manager,
    )

    (tmp_path / "first").mkdir()
    (tmp_path / "second").mkdir()
    first = background_agent_manager(tmp_path / "first", "parent")
    close_entered = threading.Event()
    close_release = threading.Event()
    lookup_finished = threading.Event()

    def slow_close(timeout=5.0):
        close_entered.set()
        assert close_release.wait(timeout=2)

    monkeypatch.setattr(first, "close", slow_close)
    disposer = threading.Thread(
        target=dispose_background_agent_manager,
        args=(tmp_path / "first", "parent"),
    )
    lookup = threading.Thread(
        target=lambda: (
            background_agent_manager(tmp_path / "second", "parent"),
            lookup_finished.set(),
        )
    )
    disposer.start()
    try:
        assert close_entered.wait(timeout=1)
        lookup.start()
        assert lookup_finished.wait(timeout=0.25)
    finally:
        close_release.set()
        disposer.join(timeout=2)
        lookup.join(timeout=2)
        dispose_background_agent_manager(tmp_path / "second", "parent")


def test_closed_background_manager_rejects_stale_start_reference(
    tmp_path,
    monkeypatch,
):
    """A reference retained across Session disposal must not orphan new jobs."""
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.foundation import config

    manager = BackgroundAgentManager(tmp_path, "closed-parent")
    manager.close()
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", False)

    assert manager.start([{
        "prompt": "must not start",
        "target_paths": ["src"],
    }]) == "Error: background Agent manager is closed"


def test_agent_loop_close_releases_registered_background_manager(tmp_path):
    """Closing and reopening one Session must receive a live manager owner."""
    from nz_coder.runtime.agent.agent_manager import (
        background_agent_manager,
        dispose_background_agent_manager,
    )
    from nz_coder.runtime.execution.loop import AgentLoop

    manager = background_agent_manager(tmp_path, "reopen-session")
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_checkpoint_lock = __import__("threading").RLock()
    loop._session_checkpoint_writer = None
    loop.verification_runtime = None
    loop.workdir = tmp_path
    loop.session_id = "reopen-session"
    loop.background_agents = manager
    loop._close_repo_intelligence = lambda: None
    loop._provider_runtimes = {}
    loop._owns_event_bus = True
    loop.event_bus = type("Events", (), {"close": lambda self: None})()
    loop.tracer = type("Tracer", (), {"close": lambda self: None})()

    loop.close()
    replacement = background_agent_manager(tmp_path, "reopen-session")

    assert replacement is not manager
    assert replacement.start([]) != "Error: background Agent manager is closed"
    dispose_background_agent_manager(tmp_path, "reopen-session")


def test_manager_marks_orphaned_live_state_interrupted(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state.update({"background": True, "status": "running"})
    subagent._save_subagent_state("parent", state, tmp_path)

    manager = BackgroundAgentManager(tmp_path, "parent")

    assert manager._load(state["session_id"])["status"] == "interrupted"


def test_copy_worktree_snapshots_dirty_workspace_without_git(tmp_path, monkeypatch):
    from nz_coder.runtime.worktree import WorktreeManager

    (tmp_path / "app.py").write_text("parent\n", encoding="utf-8")
    hidden = tmp_path / ".nz-coder"
    hidden.mkdir()
    (hidden / "secret.txt").write_text("state\n", encoding="utf-8")
    manager = WorktreeManager(tmp_path)
    monkeypatch.setattr(manager, "is_git_repo", lambda: False)

    worktree = manager.create("child")
    child_file = Path(worktree.path) / "app.py"
    child_file.write_text("child\n", encoding="utf-8")

    assert worktree.mode == "copy"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "parent\n"
    assert not (Path(worktree.path) / ".nz-coder" / "secret.txt").exists()


def test_worktree_manager_ignores_repository_state_symlink(tmp_path):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.worktree import WorktreeManager

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)

    manager = WorktreeManager(workspace)
    background = BackgroundAgentManager(workspace, "parent")
    assert not manager.worktree_dir.is_relative_to(workspace)
    assert not background._workflow.root.is_relative_to(workspace)
    assert not (outside / "sessions").exists()


def _save_committed_apply_fixture(manager, state):
    from nz_coder.runtime.agent.child_result import child_result_from_state

    identity = "fixture-" + state["session_id"]
    state["delivery_baseline"] = {"version": 1, "files": {
        path: {"kind": "file", "sha256": digest}
        for path, digest in state.get("baseline_hashes", {}).items() if digest
    }}
    state.update(settlement_id=identity, child_result_settlement_id=identity)
    state["child_result"] = child_result_from_state(state, final_text="done", status="completed").to_dict()
    manager._workflow.record_task("task_queued", {**state, "status": "queued"})
    manager._save(state)
    manager._workflow.commit_terminal(state, event_id=identity)


def test_apply_rejects_unavailable_result_from_conflicting_checkpoint(tmp_path):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    (tmp_path / "app.py").write_text("parent\n")
    manager = BackgroundAgentManager(tmp_path, "parent")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    child = _worktree_root(tmp_path) / state["session_id"]
    child.mkdir(parents=True)
    (child / "app.py").write_text("uncommitted new invocation\n")
    state.update(background=True, status="completed", changed_files=["app.py"], claimed_paths=["app.py"],
                 baseline_hashes=manager._baseline(["app.py"]),
                 worktree={"id": state["session_id"], "path": str(child), "mode": "copy"})
    _save_committed_apply_fixture(manager, state)
    state.update(settlement_id="unconfirmed-new", status="queued")
    manager._save(state)
    try:
        visible = manager._load(state["session_id"])
        assert visible["status"] == "completed" and visible["_checkpoint_unavailable"]
        writes, deletes, error = manager.application_changes(state["session_id"], ["app.py"])
        assert writes == deletes == []
        assert "unavailable" in error.lower()
        assert (tmp_path / "app.py").read_text() == "parent\n"
    finally:
        manager.close()


def test_apply_agent_changes_requires_exact_review_and_preserves_transaction(tmp_path):
    import nz_coder.tools.files  # noqa: F401
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        apply_agent_changes,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.state.transaction import TransactionManager
    from nz_coder.state.changes import ChangeTracker

    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("remove\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    baseline = manager._baseline(["a.py", "gone.py", "new.py"])
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    child = _worktree_root(tmp_path) / state["session_id"]
    child.mkdir(parents=True)
    (child / "a.py").write_text("new\n", encoding="utf-8")
    (child / "new.py").write_text("created\n", encoding="utf-8")
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["a.py", "gone.py", "new.py"],
        "changed_files": ["a.py", "gone.py", "new.py"],
        "baseline_hashes": baseline,
        "worktree": {
            "id": state["session_id"], "path": str(child), "mode": "copy",
        },
    })
    _save_committed_apply_fixture(manager, state)
    txn = TransactionManager()
    tracker = ChangeTracker(run_id="apply-child", change_dir=tmp_path / "changes")

    with (
        scoped_workdir(tmp_path),
        scoped_background_agent_manager(manager),
        bind_tool_state(txn=txn, change_tracker=tracker),
    ):
        txn.begin()
        rejected = apply_agent_changes(state["session_id"], ["a.py"], confirm=True)
        applied = apply_agent_changes(
            state["session_id"],
            ["a.py", "gone.py", "new.py"],
            confirm=True,
        )
        txn.commit()

    assert rejected.startswith("Error: reviewed_files must exactly match")
    assert applied.startswith("Applied reviewed child changes")
    assert applied.metadata["child_result"]["status"] == "applied"
    assert applied.metadata["child_changed_files"] == ["a.py", "gone.py", "new.py"]
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new\n"
    assert (tmp_path / "new.py").read_text(encoding="utf-8") == "created\n"
    assert not (tmp_path / "gone.py").exists()


def test_apply_agent_changes_rejects_parent_baseline_conflict(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    target = tmp_path / "app.py"
    target.write_text("base\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    baseline = manager._baseline(["app.py"])
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    child = _worktree_root(tmp_path) / state["session_id"]
    child.mkdir(parents=True)
    (child / "app.py").write_text("child\n", encoding="utf-8")
    target.write_text("parent changed\n", encoding="utf-8")
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["app.py"],
        "changed_files": ["app.py"],
        "baseline_hashes": baseline,
        "worktree": {
            "id": state["session_id"], "path": str(child), "mode": "copy",
        },
    })
    _save_committed_apply_fixture(manager, state)

    writes, deletes, error = manager.application_changes(state["session_id"], ["app.py"])

    assert writes == [] and deletes == []
    assert "parent changed since child snapshot" in error
    assert target.read_text(encoding="utf-8") == "parent changed\n"


def test_apply_agent_changes_rejects_unowned_child_worktree(tmp_path):
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    target = tmp_path / "app.py"
    target.write_text("parent\n", encoding="utf-8")
    outside = tmp_path.parent / "unowned-apply-worktree"
    outside.mkdir(exist_ok=True)
    (outside / "app.py").write_text("untrusted\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["app.py"],
        "changed_files": ["app.py"],
        "baseline_hashes": manager._baseline(["app.py"]),
        "worktree": {
            "id": state["session_id"],
            "path": str(outside),
            "mode": "copy",
        },
    })
    _save_committed_apply_fixture(manager, state)

    writes, deletes, error = manager.application_changes(
        state["session_id"],
        ["app.py"],
    )

    assert writes == [] and deletes == []
    assert "worktree" in error.lower()
    assert target.read_text(encoding="utf-8") == "parent\n"


def test_applied_child_changes_follow_parent_transaction_rollback(tmp_path):
    import nz_coder.tools.files  # noqa: F401
    import nz_coder.runtime.agent.subagent as subagent
    from nz_coder.runtime.agent.agent_manager import (
        BackgroundAgentManager,
        apply_agent_changes,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.state.transaction import TransactionManager
    from nz_coder.state.changes import ChangeTracker

    target = tmp_path / "app.py"
    target.write_text("parent\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    baseline = manager._baseline(["app.py"])
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    child = _worktree_root(tmp_path) / state["session_id"]
    child.mkdir(parents=True)
    (child / "app.py").write_text("child\n", encoding="utf-8")
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["app.py"],
        "changed_files": ["app.py"],
        "baseline_hashes": baseline,
        "worktree": {
            "id": state["session_id"], "path": str(child), "mode": "copy",
        },
    })
    _save_committed_apply_fixture(manager, state)
    txn = TransactionManager()
    tracker = ChangeTracker(run_id="rollback-child", change_dir=tmp_path / "changes")

    with (
        scoped_workdir(tmp_path),
        scoped_background_agent_manager(manager),
        bind_tool_state(txn=txn, change_tracker=tracker),
    ):
        txn.begin()
        result = apply_agent_changes(state["session_id"], ["app.py"], confirm=True)
        txn.rollback()

    assert result.startswith("Applied reviewed child changes")
    assert target.read_text(encoding="utf-8") == "parent\n"
def _blocking_process_child(result_path, payload, endpoint):
    """Pickle-safe target proving hard-stop does not require cooperation."""
    from tests.fixtures.subagent_process_targets import _command_client
    with _command_client(payload, endpoint):
        (Path(payload['workspace']) / 'ready').touch()
        time.sleep(30)


def test_process_isolation_hard_stops_uncooperative_child(tmp_path, monkeypatch):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent.process_supervisor as process_supervisor_module

    monkeypatch.setattr(
        process_supervisor_module,
        "_run_subagent_process",
        _blocking_process_child,
    )
    manager = BackgroundAgentManager(tmp_path, "parent")
    started = manager.start([{
        "prompt": "block forever",
        "target_paths": ["app.py"],
        "isolation": "process",
    }])
    session_id = started.metadata["task_ids"][0]
    deadline = time.monotonic() + 3
    while not (tmp_path / 'ready').exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)

    stopped = manager.stop([session_id], reason="hard stop test", timeout_ms=2000)

    assert stopped.metadata["unsettled_task_ids"] == []
    assert stopped.metadata["child_results"][0]["status"] == "cancelled"
    assert manager._jobs[session_id].process.exitcode is not None


def test_process_isolation_can_be_disabled(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    monkeypatch.setattr(config, "SUBAGENT_PROCESS_ISOLATION_ENABLED", False)
    manager = BackgroundAgentManager(tmp_path, "parent")
    result = manager.start([{
        "prompt": "work",
        "target_paths": ["app.py"],
        "isolation": "process",
    }])

    assert result == "Error: process isolation is disabled by configuration"


def test_managed_workflow_retains_only_latest_500_terminal_runs(tmp_path):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    for index in range(503):
        run_id = f"run-{index}"
        manager.begin_workflow_run(run_id, run_id)
        manager.finish_workflow_run(run_id, "completed")

    snapshots = manager.workflow_run_snapshots()
    assert len(snapshots) == 500
    assert snapshots[0]["run_id"] == "run-502"
    assert snapshots[-1]["run_id"] == "run-3"


def test_thread_start_failure_settles_admitted_child_without_leak(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    actual_start = threading.Thread.start
    def fail_subagent(thread):
        if thread.name.startswith('nz-subagent-'):
            raise RuntimeError('injected start failure')
        return actual_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', fail_subagent)
    manager = BackgroundAgentManager(tmp_path, 'parent')
    try:
        result = manager.start([{'prompt': 'task', 'target_paths': ['app.py']}])
        task = result.metadata['task_ids'][0]
        job = manager._jobs[task]
        assert job.done_event.wait(3)
        assert manager._load(task)['status'] == 'error'
        assert manager._active_jobs == 0
        assert not job.capability_lease_id
        assert capability_leases().get(job.capability_lease_id) is None
    finally:
        manager.close(timeout=3)


def test_start_failure_storage_error_retains_lease_until_commit(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    actual_start = threading.Thread.start
    def start(thread):
        if thread.name.startswith('nz-subagent-'):
            raise RuntimeError('injected start failure')
        return actual_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    manager = BackgroundAgentManager(tmp_path, 'parent')
    blocked, failed = threading.Event(), threading.Event()
    blocked.set()
    save = manager._save
    def fail_save(state):
        if state.get('background_result') and blocked.is_set():
            failed.set()
            raise OSError('injected storage failure')
        return save(state)
    monkeypatch.setattr(manager, '_save', fail_save)
    try:
        result = manager.start([{'prompt': 'task', 'target_paths': ['app.py']}])
        job = manager._jobs[result.metadata['task_ids'][0]]
        assert failed.wait(3)
        lease = job.capability_lease_id
        assert capability_leases().get(lease) is not None
        assert not job.done_event.is_set()
        blocked.clear()
        manager._settlements.wake(recheck=True)
        assert job.done_event.wait(3)
        assert capability_leases().get(lease) is None
    finally:
        blocked.clear()
        manager._settlements.wake(recheck=True)
        manager.close(timeout=3)


def test_thread_started_then_start_raises_preserves_original_worker_owner(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    entered, release = threading.Event(), threading.Event()
    def run(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return 'normal worker result'
    monkeypatch.setattr(subagent, 'run_subagent', run)
    actual_start = threading.Thread.start
    def start(thread):
        result = actual_start(thread)
        if thread.name.startswith('nz-subagent-'):
            raise RuntimeError('raised after start')
        return result
    monkeypatch.setattr(threading.Thread, 'start', start)
    manager = BackgroundAgentManager(tmp_path, 'parent')
    try:
        result = manager.start([{'prompt': 'task', 'target_paths': ['app.py']}])
        task = result.metadata['task_ids'][0]
        job = manager._jobs[task]
        assert entered.wait(3)
        assert not job.done_event.is_set()
        assert job.capability_lease_id
        release.set()
        assert job.done_event.wait(3)
        assert manager._load(task)['background_result'] == 'normal worker result'
    finally:
        release.set()
        manager.close(timeout=3)


def test_second_fanout_start_failure_preserves_started_sibling(tmp_path, monkeypatch):
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    entered, release = threading.Event(), threading.Event()
    def run(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return 'first sibling finished'
    monkeypatch.setattr(subagent, 'run_subagent', run)
    actual_start = threading.Thread.start
    count = 0
    def start(thread):
        nonlocal count
        if thread.name.startswith('nz-subagent-'):
            count += 1
            if count == 2:
                raise RuntimeError('second start failed')
        return actual_start(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    manager = BackgroundAgentManager(tmp_path, 'parent')
    try:
        result = manager.start([{'prompt': 'first', 'target_paths': ['one']},
                                {'prompt': 'second', 'target_paths': ['two']}])
        first, second = result.metadata['task_ids']
        assert entered.wait(3)
        assert manager._jobs[second].done_event.wait(3)
        assert manager._load(second)['status'] == 'error'
        assert not manager._jobs[first].done_event.is_set()
        release.set()
        assert manager._jobs[first].done_event.wait(3)
        assert manager._load(first)['background_result'] == 'first sibling finished'
    finally:
        release.set()
        manager.close(timeout=3)
