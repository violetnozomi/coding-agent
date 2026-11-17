"""Durability and replay contracts for background workflow snapshots."""
from __future__ import annotations

import json

import pytest


def _state(status: str = "queued") -> dict:
    return {
        "session_id": "child-1",
        "agent_id": "agent-1",
        "display_name": "worker",
        "background": True,
        "status": status,
        "created_at": 10.0,
        "queued_at": 11.0,
        "changed_files": [],
    }


def test_workflow_snapshot_tracks_revision_counts_and_progress(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = WorkflowProcessStore(
        tmp_path / "workflow",
        "parent",
        agent_cap=8,
        concurrency_cap=3,
    )
    queued = _state()
    first = store.record_task("task_queued", queued)
    running = dict(queued, status="running", run_started_at=12.0)
    second = store.record_task("task_started", running)
    completed = dict(running, status="completed", finished_at=13.0)
    completed["child_result"] = {
        "digest": "implemented",
        "summary_kind": "excerpt",
        "usage": {"total": 21},
    }
    final = store.record_task("task_terminal", completed)

    assert first["revision"] == 2
    assert second["counts"]["running"] == 1
    assert final["status"] == "completed"
    assert final["counts"]["completed"] == 1
    assert final["progress"]["finished_agents"] == 1
    assert final["progress"]["peak_active_agents"] == 1
    assert final["progress"]["agent_cap"] == 8
    assert final["progress"]["concurrency_cap"] == 3
    assert final["tokens"]["spent"] == 21
    assert final["items"][0]["summary"] == "implemented"
    assert final["items"][0]["summary_status"] == "notice"


def test_workflow_event_log_replays_when_snapshot_is_missing(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    root = tmp_path / "workflow"
    store = WorkflowProcessStore(root, "parent", agent_cap=4)
    store.record_task("task_queued", _state())
    expected = store.snapshot()
    (root / "snapshot.json").unlink()

    restored = WorkflowProcessStore(root, "parent", agent_cap=4)

    assert restored.snapshot() == expected
    assert json.loads((root / "snapshot.json").read_text())["revision"] == 2


def test_workflow_event_log_ignores_only_truncated_tail(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    root = tmp_path / "workflow"
    store = WorkflowProcessStore(root, "parent", agent_cap=4)
    store.record_task("task_queued", _state())
    with (root / "events.jsonl").open("ab") as handle:
        handle.write(b'{"incomplete":')

    restored = WorkflowProcessStore(root, "parent", agent_cap=4)

    assert restored.snapshot()["revision"] == 2


def test_workflow_event_log_rejects_corrupt_complete_middle(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    root = tmp_path / "workflow"
    WorkflowProcessStore(root, "parent", agent_cap=4)
    with (root / "events.jsonl").open("ab") as handle:
        handle.write(b'{"bad":true}\n')

    with pytest.raises(ValueError, match="event chain"):
        WorkflowProcessStore(root, "parent", agent_cap=4)


def test_workflow_reconcile_is_idempotent(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = WorkflowProcessStore(tmp_path / "workflow", "parent", agent_cap=4)
    store.reconcile([_state()])
    revision = store.snapshot()["revision"]

    store.reconcile([_state()])

    assert store.snapshot()["revision"] == revision


def _invocation(status="completed", identity="round-1"):
    return {**_state(status), "settlement_id": identity, "finished_at": 13.0,
            "child_result_settlement_id": identity,
            "child_result": {"task_id": "child-1", "name": "offline", "status": status,
                             "digest": "done", "usage": {"total": 18}}}


def _store(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = WorkflowProcessStore(tmp_path / "workflow", "parent", agent_cap=4)
    store.record_task("task_queued", _invocation("queued"))
    return store


def test_short_writes_are_completed_for_journal_and_snapshot(tmp_path, monkeypatch):
    import os
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = _store(tmp_path)
    original = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: original(fd, data[:17]))
    store.record_task("task_terminal", _invocation())
    assert json.loads(store.snapshot_path.read_text())["counts"]["completed"] == 1
    assert WorkflowProcessStore(store.root, "parent", agent_cap=4).snapshot()["counts"]["completed"] == 1


def test_zero_write_cannot_be_acknowledged(tmp_path, monkeypatch):
    import os

    store = _store(tmp_path)
    with monkeypatch.context() as fault:
        fault.setattr(os, "write", lambda fd, data: 0)
        with pytest.raises(OSError):
            store.record_task("task_terminal", _invocation())
    assert store.snapshot()["counts"]["completed"] == 0


@pytest.mark.parametrize("partial", [False, True], ids=["complete-unacked", "partial-line"])
def test_uncertain_append_is_verified_before_terminal_retry(tmp_path, monkeypatch, partial):
    import os
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = _store(tmp_path)
    original_write, original_sync = os.write, os.fsync
    identity = store.events_path.stat().st_ino
    writes = 0

    def write(fd, data):
        nonlocal writes
        if os.fstat(fd).st_ino == identity:
            writes += 1
            if partial:
                if writes == 1:
                    return original_write(fd, data[:19])
                raise OSError("injected partial write")
        return original_write(fd, data)

    def sync(fd):
        original_sync(fd)
        if os.fstat(fd).st_ino == identity:
            raise OSError("injected ambiguous acknowledgement")

    with monkeypatch.context() as fault:
        fault.setattr(os, "write", write)
        fault.setattr(os, "fsync", sync)
        with pytest.raises(OSError):
            store.commit_terminal(_invocation(), event_id="terminal-fixed")
    commit = store.commit_terminal(_invocation(), event_id="terminal-fixed")
    assert commit.event_id == "terminal-fixed"
    assert len([event for event in store.events() if event["type"] == "task_terminal"]) == 1
    restored = WorkflowProcessStore(store.root, "parent", agent_cap=4)
    assert restored.snapshot()["counts"]["completed"] == 1


def test_startup_tail_is_removed_before_first_append(tmp_path):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = _store(tmp_path)
    with store.events_path.open("ab") as stream:
        stream.write(b'{"incomplete":')
    restored = WorkflowProcessStore(store.root, "parent", agent_cap=4)
    restored.record_task("task_terminal", _invocation())
    again = WorkflowProcessStore(store.root, "parent", agent_cap=4)
    assert again.snapshot()["counts"]["completed"] == 1


@pytest.mark.parametrize("kind", ["bytes", "entries"])
def test_append_and_reload_share_capacity_limits(tmp_path, monkeypatch, kind):
    from nz_coder.runtime.workflows import workflow_process as module

    store = _store(tmp_path)
    before = store.events_path.read_bytes()
    if kind == "bytes":
        monkeypatch.setattr(module, "_MAX_JOURNAL_BYTES", len(before) + 20)
    else:
        monkeypatch.setattr(module, "_MAX_JOURNAL_ENTRIES", 2, raising=False)
    with pytest.raises(ValueError, match="capacity|exceeds"):
        store.record_event("workflow_log", data={"message": "x" * 100})
    assert store.events_path.read_bytes() == before
    assert module.WorkflowProcessStore(store.root, "parent", agent_cap=4).snapshot()["revision"] == 2


def test_snapshot_failure_preserves_commit_and_can_be_repaired(tmp_path, monkeypatch):
    store = _store(tmp_path)
    notifications = []
    store.set_recovery_wakeup(lambda: notifications.append("dirty"))
    with monkeypatch.context() as fault:
        fault.setattr(store, "_persist_snapshot", lambda: (_ for _ in ()).throw(OSError("snapshot unavailable")))
        commit = store.commit_terminal(_invocation(), event_id="terminal-fixed")
        assert commit.snapshot_dirty is True
        assert store.snapshot()["counts"]["completed"] == 1
        assert store.needs_recovery is True
    store.recover()
    assert store.needs_recovery is False
    assert json.loads(store.snapshot_path.read_text())["counts"]["completed"] == 1
    assert notifications
    assert store.commit_terminal(_invocation(), event_id="unused-on-retry").event_id == "terminal-fixed"
    assert len([event for event in store.events() if event["type"] == "task_terminal"]) == 1


def test_reload_survives_snapshot_cache_failure(tmp_path, monkeypatch):
    from nz_coder.runtime.workflows.workflow_process import WorkflowProcessStore

    store = _store(tmp_path)
    store.record_task("task_terminal", _invocation())
    with monkeypatch.context() as fault:
        fault.setattr(WorkflowProcessStore, "_persist_snapshot", lambda self: (_ for _ in ()).throw(OSError("snapshot unavailable")))
        restored = WorkflowProcessStore(store.root, "parent", agent_cap=4)
        assert restored.snapshot()["counts"]["completed"] == 1
        assert restored.needs_recovery
    restored.recover()
    assert not restored.needs_recovery


def test_terminal_is_not_downgraded_by_raw_without_new_admission(tmp_path):
    store = _store(tmp_path)
    store.commit_terminal(_invocation(), event_id="terminal-fixed")
    for raw in [_invocation("running"), _invocation("interrupted"), _invocation("error"), _invocation("running", "unadmitted")]:
        raw.pop("child_result")
        snapshot = store.reconcile([raw])
        assert snapshot["counts"]["completed"] == 1
        assert snapshot["tokens"]["spent"] == 18
    assert store.current_invocation("child-1") == "round-1"
    store.record_task("task_queued", _invocation("queued", "round-2"))
    assert store.current_invocation("child-1") == "round-2"
    assert store.snapshot()["counts"]["pending"] == 1
    store.commit_terminal(_invocation(identity="round-2"), event_id="terminal-second")
    assert len([event for event in store.events() if event["type"] == "task_terminal"]) == 2
    assert store.committed_items()[0]["settlement_id"] == "round-2"


def test_old_canonical_is_not_used_in_new_invocation_projection(tmp_path):
    store = _store(tmp_path)
    new = _invocation("queued", "round-2")
    new["child_result_settlement_id"] = "round-1"
    store.record_task("task_queued", new)
    item = store.snapshot()["items"][0]
    assert item.get("summary") != "done"
    assert item["usage"] == {}


def test_callback_failure_is_not_a_commit_failure(tmp_path):
    store = _store(tmp_path)
    store._on_event = lambda *_: (_ for _ in ()).throw(OSError("presentation unavailable"))
    commit = store.commit_terminal(_invocation(), event_id="terminal-fixed")
    assert commit.sequence == 3
    assert store.snapshot()["counts"]["completed"] == 1
