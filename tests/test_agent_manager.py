"""Tests for Session-owned background write-subagent orchestration."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest


def test_background_task_events_project_standard_child_lifecycle(tmp_path):
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    from nz_coder.session_events import SessionEventBus

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


def test_background_manager_starts_parallel_non_overlapping_tasks(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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


def test_background_manager_rejects_overlapping_requested_scopes(tmp_path):
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    result = manager.start([
        {"prompt": "first", "target_paths": ["src"]},
        {"prompt": "second", "target_paths": ["src/api.py"]},
    ])

    assert result.startswith("Error: task 1 overlaps")
    assert manager._states() == []


def test_background_manager_routes_bounded_peer_messages(tmp_path):
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

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


def test_background_manager_broadcasts_to_live_siblings_and_worker(tmp_path):
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

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
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

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
    import nz_coder.runtime.subagent as subagent
    from nz_coder.message_schema import SYNTHETIC_USER_KEY
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder import config
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder import config
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.subagent as subagent

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
    from nz_coder.runtime.loop import AgentLoop

    order = []
    loop = AgentLoop.__new__(AgentLoop)
    loop.background_agents = type(
        "Background",
        (),
        {"close": lambda self, timeout=5.0: order.append(("background", timeout))},
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


def test_dispose_keeps_manager_reachable_when_children_do_not_settle(
    tmp_path,
    monkeypatch,
):
    from nz_coder.runtime.agent_manager import (
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


def test_manager_marks_orphaned_live_state_interrupted(tmp_path):
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

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


def test_worktree_manager_rejects_state_symlink_escape(tmp_path):
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.worktree import WorktreeError, WorktreeManager

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / ".nz-coder").symlink_to(outside, target_is_directory=True)

    try:
        WorktreeManager(workspace)
    except WorktreeError as exc:
        assert "escapes repository root" in str(exc)
    else:
        raise AssertionError("worktree state symlink escape was not rejected")
    try:
        BackgroundAgentManager(workspace, "parent")
    except ValueError as exc:
        assert "state path escapes workspace" in str(exc)
    else:
        raise AssertionError("subagent state symlink escape was not rejected")
    assert not (outside / "sessions").exists()


def test_apply_agent_changes_requires_exact_review_and_preserves_transaction(tmp_path):
    import nz_coder.tools.files  # noqa: F401
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import (
        BackgroundAgentManager,
        apply_agent_changes,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.transaction import TransactionManager
    from nz_coder.changes import ChangeTracker

    (tmp_path / "a.py").write_text("old\n", encoding="utf-8")
    (tmp_path / "gone.py").write_text("remove\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    baseline = manager._baseline(["a.py", "gone.py", "new.py"])
    child = tmp_path / ".nz-coder" / "worktrees" / "child"
    child.mkdir(parents=True)
    (child / "a.py").write_text("new\n", encoding="utf-8")
    (child / "new.py").write_text("created\n", encoding="utf-8")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["a.py", "gone.py", "new.py"],
        "changed_files": ["a.py", "gone.py", "new.py"],
        "baseline_hashes": baseline,
        "worktree": {"path": str(child), "mode": "copy"},
    })
    manager._save(state)
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
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

    target = tmp_path / "app.py"
    target.write_text("base\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    baseline = manager._baseline(["app.py"])
    child = tmp_path / ".nz-coder" / "worktrees" / "child"
    child.mkdir(parents=True)
    (child / "app.py").write_text("child\n", encoding="utf-8")
    target.write_text("parent changed\n", encoding="utf-8")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["app.py"],
        "changed_files": ["app.py"],
        "baseline_hashes": baseline,
        "worktree": {"path": str(child), "mode": "copy"},
    })
    manager._save(state)

    writes, deletes, error = manager.application_changes(state["session_id"], ["app.py"])

    assert writes == [] and deletes == []
    assert "parent changed since child snapshot" in error
    assert target.read_text(encoding="utf-8") == "parent changed\n"


def test_applied_child_changes_follow_parent_transaction_rollback(tmp_path):
    import nz_coder.tools.files  # noqa: F401
    import nz_coder.runtime.subagent as subagent
    from nz_coder.runtime.agent_manager import (
        BackgroundAgentManager,
        apply_agent_changes,
        scoped_background_agent_manager,
    )
    from nz_coder.runtime.workdir import scoped_workdir
    from nz_coder.tools.files import bind_tool_state
    from nz_coder.transaction import TransactionManager
    from nz_coder.changes import ChangeTracker

    target = tmp_path / "app.py"
    target.write_text("parent\n", encoding="utf-8")
    manager = BackgroundAgentManager(tmp_path, "parent")
    baseline = manager._baseline(["app.py"])
    child = tmp_path / ".nz-coder" / "worktrees" / "rollback-child"
    child.mkdir(parents=True)
    (child / "app.py").write_text("child\n", encoding="utf-8")
    state = subagent._new_subagent_state("parent", "general-purpose", None)
    state.update({
        "background": True,
        "status": "completed",
        "claimed_paths": ["app.py"],
        "changed_files": ["app.py"],
        "baseline_hashes": baseline,
        "worktree": {"path": str(child), "mode": "copy"},
    })
    manager._save(state)
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
def _blocking_process_child(connection, payload):
    """Pickle-safe target proving hard-stop does not require cooperation."""
    del connection, payload
    time.sleep(30)


def test_process_isolation_hard_stops_uncooperative_child(tmp_path, monkeypatch):
    from nz_coder.runtime.agent_manager import BackgroundAgentManager
    import nz_coder.runtime.agent_manager as agent_manager_module

    monkeypatch.setattr(
        agent_manager_module,
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
    while manager._load(session_id).get("status") != "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)

    stopped = manager.stop([session_id], reason="hard stop test", timeout_ms=2000)

    assert stopped.metadata["unsettled_task_ids"] == []
    assert stopped.metadata["child_results"][0]["status"] == "cancelled"
    assert manager._jobs[session_id].process.exitcode is not None


def test_process_isolation_can_be_disabled(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

    monkeypatch.setattr(config, "SUBAGENT_PROCESS_ISOLATION_ENABLED", False)
    manager = BackgroundAgentManager(tmp_path, "parent")
    result = manager.start([{
        "prompt": "work",
        "target_paths": ["app.py"],
        "isolation": "process",
    }])

    assert result == "Error: process isolation is disabled by configuration"


def test_managed_workflow_retains_only_latest_500_terminal_runs(tmp_path):
    from nz_coder.runtime.agent_manager import BackgroundAgentManager

    manager = BackgroundAgentManager(tmp_path, "parent")
    for index in range(503):
        run_id = f"run-{index}"
        manager.begin_workflow_run(run_id, run_id)
        manager.finish_workflow_run(run_id, "completed")

    snapshots = manager.workflow_run_snapshots()
    assert len(snapshots) == 500
    assert snapshots[0]["run_id"] == "run-502"
    assert snapshots[-1]["run_id"] == "run-3"
