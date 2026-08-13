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
    from nz_coder.runtime.workflow_process import WorkflowProcessStore

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
    from nz_coder.runtime.workflow_process import WorkflowProcessStore

    root = tmp_path / "workflow"
    store = WorkflowProcessStore(root, "parent", agent_cap=4)
    store.record_task("task_queued", _state())
    expected = store.snapshot()
    (root / "snapshot.json").unlink()

    restored = WorkflowProcessStore(root, "parent", agent_cap=4)

    assert restored.snapshot() == expected
    assert json.loads((root / "snapshot.json").read_text())["revision"] == 2


def test_workflow_event_log_ignores_only_truncated_tail(tmp_path):
    from nz_coder.runtime.workflow_process import WorkflowProcessStore

    root = tmp_path / "workflow"
    store = WorkflowProcessStore(root, "parent", agent_cap=4)
    store.record_task("task_queued", _state())
    with (root / "events.jsonl").open("ab") as handle:
        handle.write(b'{"incomplete":')

    restored = WorkflowProcessStore(root, "parent", agent_cap=4)

    assert restored.snapshot()["revision"] == 2


def test_workflow_event_log_rejects_corrupt_complete_middle(tmp_path):
    from nz_coder.runtime.workflow_process import WorkflowProcessStore

    root = tmp_path / "workflow"
    WorkflowProcessStore(root, "parent", agent_cap=4)
    with (root / "events.jsonl").open("ab") as handle:
        handle.write(b'{"bad":true}\n')

    with pytest.raises(ValueError, match="event chain"):
        WorkflowProcessStore(root, "parent", agent_cap=4)


def test_workflow_reconcile_is_idempotent(tmp_path):
    from nz_coder.runtime.workflow_process import WorkflowProcessStore

    store = WorkflowProcessStore(tmp_path / "workflow", "parent", agent_cap=4)
    store.reconcile([_state()])
    revision = store.snapshot()["revision"]

    store.reconcile([_state()])

    assert store.snapshot()["revision"] == revision
