"""Settlement retries own real files, frozen facts and a bounded-close worker."""
from __future__ import annotations

import importlib
import json
import threading
import time

import pytest

from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore
from nz_coder.state.sessions import write_session_runtime_json


def _module():
    return importlib.import_module("nz_coder.runtime.agent.settlement")


def _state(task="child", identity="round"):
    return {"session_id": task, "parent_session_id": "parent", "display_name": "offline",
            "status": "completed", "settlement_id": identity, "child_result_settlement_id": identity,
            "child_result": {"task_id": task, "session_id": task, "parent_session_id": "parent", "name": "offline",
                             "status": "completed", "final_text": "done", "usage": {"total": 18},
                             "cost": 0.125, "structured": {"answer": 42}}}


def _coordinator(tmp_path, save):
    module = _module()
    store = WorkflowProcessStore(tmp_path / "workflow", "parent", agent_cap=4)
    return module, store, module.SettlementCoordinator(store, save, threading.RLock())


def _submit(module, store, coordinator, callback, task="child", identity="round"):
    raw = _state(task, identity)
    store.record_task("task_queued", {**raw, "status": "queued"})
    frozen = module.freeze_settlement(raw, "done", settlement_id=identity)
    record = module.FrozenSettlement(identity, "event-" + identity, frozen, None, callback)
    coordinator.submit(record)
    return record


def _until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def _close(coordinator):
    coordinator.wake(recheck=True)
    assert coordinator.close(deadline=time.monotonic() + 2)
    if coordinator._thread is not None:
        assert not coordinator._thread.is_alive()


def test_state_write_failure_retries_only_frozen_settlement(tmp_path):
    saved = []
    finished = threading.Event()

    def save(state):
        saved.append(state)
        if len(saved) == 1:
            raise OSError("transient state failure")
        write_session_runtime_json(tmp_path / "state.json", state)

    module, store, coordinator = _coordinator(tmp_path, save)
    try:
        record = _submit(module, store, coordinator, finished.set)
        assert finished.wait(2)
        assert len(saved) == 2
        assert saved[0] == saved[1] == record.state
        durable = json.loads((tmp_path / "state.json").read_text())
        assert durable["child_result"]["usage"]["total"] == 18
        assert durable["child_result"]["cost"] == 0.125
        assert len([e for e in store.events() if e["type"] == "task_terminal"]) == 1
    finally:
        _close(coordinator)


def test_persistent_write_failure_keeps_worker_and_candidate_until_recovery(tmp_path):
    blocked = threading.Event()
    blocked.set()
    finished = threading.Event()

    def save(state):
        if blocked.is_set():
            raise OSError("storage unavailable")
        write_session_runtime_json(tmp_path / "state.json", state)

    module, store, coordinator = _coordinator(tmp_path, save)
    try:
        _submit(module, store, coordinator, finished.set)
        _until(lambda: coordinator.error_for("round") is not None)
        assert coordinator.close(deadline=time.monotonic() + 0.02) is False
        assert coordinator.is_pending("round") and coordinator._thread.is_alive()
        assert not finished.is_set()
        blocked.clear()
        coordinator.wake(recheck=True)
        assert finished.wait(2)
    finally:
        blocked.clear()
        _close(coordinator)


def test_failing_task_does_not_starve_other_due_settlement(tmp_path):
    blocked = threading.Event()
    blocked.set()
    completed = []

    def save(state):
        if state["session_id"] == "first" and blocked.is_set():
            raise OSError("first state unavailable")
        write_session_runtime_json(tmp_path / (state["session_id"] + ".json"), state)

    module, store, coordinator = _coordinator(tmp_path, save)
    try:
        _submit(module, store, coordinator, lambda: completed.append("first"), "first", "one")
        _submit(module, store, coordinator, lambda: completed.append("second"), "second", "two")
        _until(lambda: "second" in completed)
        assert completed == ["second"]
        blocked.clear()
        _until(lambda: "first" in completed)
    finally:
        blocked.clear()
        _close(coordinator)


def test_ordinary_event_snapshot_recovers_without_candidate(tmp_path, monkeypatch):
    module, store, coordinator = _coordinator(tmp_path, lambda state: None)
    try:
        with monkeypatch.context() as fault:
            fault.setattr(store, "_persist_snapshot", lambda: (_ for _ in ()).throw(OSError("snapshot unavailable")))
            store.record_event("workflow_log", data={"message": "offline"})
            _until(lambda: coordinator._thread is not None and coordinator._thread.is_alive())
            assert store.needs_recovery
        _until(lambda: not store.needs_recovery)
        assert json.loads(store.snapshot_path.read_text())["revision"] == store.snapshot()["revision"]
    finally:
        _close(coordinator)


def test_committed_task_releases_despite_snapshot_and_callback_failure(tmp_path, monkeypatch):
    released = threading.Event()
    module, store, coordinator = _coordinator(tmp_path, lambda state: None)

    def callback():
        released.set()
        raise OSError("presentation unavailable")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(store, "_persist_snapshot", lambda: (_ for _ in ()).throw(OSError("snapshot unavailable")))
            _submit(module, store, coordinator, callback)
            assert released.wait(2)
            _until(lambda: not coordinator.is_pending("round"))
            assert store.snapshot()["counts"]["completed"] == 1
            assert coordinator.close(deadline=time.monotonic() + 1)
        assert len([e for e in store.events() if e["type"] == "task_terminal"]) == 1
    finally:
        _close(coordinator)


def test_hard_capacity_blocks_without_periodic_rewrite_and_can_recheck(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows import workflow_process

    released = threading.Event()
    module, store, coordinator = _coordinator(tmp_path, lambda state: None)
    original = store.commit_terminal
    calls = []

    def commit(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_terminal", commit)
    try:
        with monkeypatch.context() as fault:
            # Leave room for admission but no room for its terminal.
            fault.setattr(workflow_process, "_MAX_JOURNAL_ENTRIES", 2)
            _submit(module, store, coordinator, released.set)
            _until(lambda: coordinator.error_for("round") is not None)
            assert coordinator.error_for("round").retryable is False
            count = len(calls)
            time.sleep(0.12)
            assert len(calls) == count
            assert not released.is_set()
        coordinator.wake(recheck=True)
        assert released.wait(2)
    finally:
        _close(coordinator)


def test_freeze_rejects_old_canonical_and_preserves_input_accounting_baseline():
    module = _module()
    raw = _state()
    raw["settlement_id"] = "new-round"
    raw["status"] = "error"
    frozen = module.freeze_settlement(raw, "current failure", settlement_id="new-round")
    assert "structured" not in frozen["child_result"]
    assert frozen["child_result"]["usage"] == {}
    assert "cost" not in frozen["child_result"]
    assert raw["child_result"]["cost"] == 0.125
    assert frozen["child_result_settlement_id"] == "new-round"


def test_close_timeout_keeps_draining_until_native_worker_exit(tmp_path, monkeypatch):
    module, store, coordinator = _coordinator(tmp_path, lambda state: None)
    exiting = threading.Event()
    release = threading.Event()
    original = coordinator._run

    def run():
        original()
        exiting.set()
        assert release.wait(2)

    monkeypatch.setattr(coordinator, "_run", run)
    try:
        assert coordinator.close(deadline=time.monotonic() + 0.03) is False
        assert exiting.is_set() and coordinator._thread.is_alive()
        with pytest.raises(RuntimeError, match="closing"):
            coordinator.submit(module.FrozenSettlement("new", "event-new", _state(identity="new"), None, lambda: None))
        assert not coordinator.is_pending("new")
    finally:
        release.set()
        coordinator._thread.join(2)
        assert coordinator.close(deadline=time.monotonic() + 1)
        assert not coordinator._pending and not coordinator._thread.is_alive()


@pytest.mark.parametrize("bad", ["type", "item", "usage", "created_at"])
def test_bad_complete_journal_keeps_confirmed_projection_and_recovery_owner(tmp_path, monkeypatch, bad):
    saved = threading.Event()
    released = threading.Event()
    module, store, coordinator = _coordinator(tmp_path, lambda state: saved.set())
    raw = _state()
    store.record_task("task_queued", {**raw, "status": "queued"})
    confirmed = store.snapshot()
    prefix = store.events_path.read_bytes()
    malformed = {**store.events()[-1], "id": "bad-complete", "sequence": 3,
                 "parent_id": store.events()[-1]["id"]}
    if bad == "type":
        malformed["type"] = []
    elif bad == "item":
        malformed["data"] = {"item": {"task_id": "child"}}
    elif bad == "usage":
        malformed["data"]["item"]["usage"] = {"total": "broken"}
    else:
        malformed["data"]["item"]["created_at"] = "broken"
    store.events_path.write_bytes(prefix + json.dumps(malformed).encode() + b"\n")
    store._journal_uncertain = True
    frozen = module.freeze_settlement(raw, "done", settlement_id="round")
    coordinator.submit(module.FrozenSettlement("round", "terminal-round", frozen, None, released.set))
    try:
        assert saved.wait(1)
        _until(lambda: coordinator.error_for("round") is not None or not coordinator._thread.is_alive())
        assert coordinator._thread.is_alive(), "bad complete record killed recovery owner"
        assert coordinator.error_for("round").retryable is False
        assert store.snapshot() == confirmed
        assert coordinator.close(deadline=time.monotonic() + 0.02) is False
        assert coordinator.is_pending("round") and not released.is_set()
        store.events_path.write_bytes(prefix)
        coordinator.wake(recheck=True)
        assert released.wait(2)
        assert store.snapshot()["counts"]["completed"] == 1
    finally:
        store.events_path.write_bytes(prefix if coordinator.is_pending("round") else store.events_path.read_bytes())
        _close(coordinator)


def test_obsolete_checkpoint_repair_cannot_save_over_confirmed_new_admission(tmp_path):
    failed = threading.Event()
    blocked = threading.Event()
    blocked.set()
    saved = []

    def save(state):
        if blocked.is_set():
            failed.set()
            raise OSError("old repair save unavailable")
        saved.append(state)

    module, store, coordinator = _coordinator(tmp_path, save)
    old = {**_state(identity="old"), "status": "interrupted"}
    store.record_task("task_queued", {**old, "status": "queued"})
    coordinator.submit(module.FrozenSettlement("checkpoint-repair-old", "repair-old", old, None, lambda: None, reconcile_only=True))
    try:
        assert failed.wait(1)
        store.record_task("task_queued", {**old, "settlement_id": "new", "status": "queued"})
        blocked.clear()
        coordinator.wake(recheck=True)
        _until(lambda: not coordinator.is_pending("checkpoint-repair-old"))
        assert saved == [], "obsolete repair overwrote the current checkpoint"
        assert store.current_invocation("child") == "new"
        assert store.snapshot()["items"][0]["raw_status"] == "queued"
    finally:
        blocked.clear()
        _close(coordinator)
