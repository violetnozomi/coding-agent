"""Workflows consume committed results without a second wait or mailbox drain."""
from __future__ import annotations

from concurrent.futures import CancelledError, ThreadPoolExecutor
import threading
import time

import pytest

from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
from nz_coder.runtime.agent.child_result import CHILD_RESULT_KEY, ChildAgentResult
from nz_coder.runtime.workflows.workflow_runtime import WorkflowAbortError, WorkflowRuntime


_TEXT = "evidence: " + "x" * 5000


@pytest.fixture
def owned_child(tmp_path, monkeypatch):
    import nz_coder.runtime.agent.subagent as subagent

    release = threading.Event()

    def run(_prompt, *, session_id, **_kwargs):
        assert release.wait(5), "test did not release its child"
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        result = ChildAgentResult(
            task_id=session_id, name="investigation", status="completed",
            session_id=session_id, parent_session_id="parent", final_text=_TEXT,
            structured={"finding": "parser"}, structured_present=True,
            usage={"output": 7, "total": 11}, cost=0.125,
            verification={"summary": "targeted check passed"},
        )
        state.update(status="completed", child_result=result.to_dict(),
                     child_result_settlement_id=state["settlement_id"])
        subagent._save_subagent_state("parent", state, tmp_path)
        return _TEXT

    monkeypatch.setattr(subagent, "run_subagent", run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    started = manager.start([{"prompt": "investigate", "read_only": True}])
    assert hasattr(started, "metadata"), started
    task_id = started.metadata["task_ids"][0]
    try:
        yield manager, task_id, release
    finally:
        release.set()
        manager.close(timeout=3)


def _observe_wait(monkeypatch, job):
    reached = threading.Event()
    original = job.done_event.wait

    def wait(timeout=None):
        reached.set()
        return original(timeout)

    monkeypatch.setattr(job.done_event, "wait", wait)
    return reached


def test_result_wait_returns_full_envelope_and_preserves_parent_mail(owned_child):
    manager, task_id, release = owned_child
    release.set()

    result = manager.wait_for_result(task_id)

    assert isinstance(result, ChildAgentResult)
    assert result.final_text == _TEXT
    assert result.structured == {"finding": "parser"}
    assert result.usage == {"output": 7, "total": 11}
    assert result.cost == 0.125
    assert result.verification == {"summary": "targeted check passed"}
    mail = manager.drain_messages("worker")
    assert len(mail) == 1
    assert mail[0]["kind"] == "task_completed"
    assert len(mail[0]["content"]) == 4000
    assert manager._jobs[task_id].done_event.is_set()


def test_pending_result_wait_does_not_reread_persistent_state(owned_child, monkeypatch):
    manager, task_id, release = owned_child
    reached = _observe_wait(monkeypatch, manager._jobs[task_id])
    reads = []
    original = manager._load

    def load(session_id):
        reads.append(session_id)
        return original(session_id)

    monkeypatch.setattr(manager, "_load", load)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(manager.wait_for_result, task_id, threading.Event())
        try:
            assert reached.wait(1)
            initial_reads = len(reads)
            time.sleep(0.15)
            assert len(reads) == initial_reads, "pending wait reread persistent state"
            release.set()
            assert waiting.result(timeout=2).final_text == _TEXT
        finally:
            release.set()


def test_result_wait_cancellation_leaves_task_stop_to_workflow(owned_child, monkeypatch):
    manager, task_id, release = owned_child
    reached = _observe_wait(monkeypatch, manager._jobs[task_id])
    cancelled = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(manager.wait_for_result, task_id, cancelled)
        try:
            assert reached.wait(1)
            cancelled.set()
            with pytest.raises(CancelledError):
                waiting.result(timeout=1)
            assert not manager._jobs[task_id].cancel_event.is_set()
            assert manager.drain_messages("worker") == []
        finally:
            cancelled.set()
            release.set()


def test_result_wait_does_not_publish_before_terminal_commit(owned_child, monkeypatch):
    manager, task_id, release = owned_child
    reached = _observe_wait(monkeypatch, manager._jobs[task_id])
    saving = threading.Event()
    save_release = threading.Event()
    original = manager._save

    def save(state):
        if state.get("background_result"):
            saving.set()
            assert save_release.wait(3)
        original(state)

    monkeypatch.setattr(manager, "_save", save)
    with ThreadPoolExecutor(max_workers=1) as pool:
        try:
            waiting = pool.submit(manager.wait_for_result, task_id)
            assert reached.wait(1)
            release.set()
            assert saving.wait(1)
            assert not waiting.done()
            assert not manager._jobs[task_id].done_event.is_set()
            save_release.set()
            assert waiting.result(timeout=2).cost == 0.125
        finally:
            save_release.set()
            release.set()


def test_result_wait_does_not_fabricate_missing_committed_envelope(owned_child):
    manager, task_id, release = owned_child
    release.set()
    assert manager._jobs[task_id].done_event.wait(2)
    state = manager._load_raw(task_id)
    state.pop(CHILD_RESULT_KEY)
    manager._save(state)

    assert manager.wait_for_result(task_id) is None
    assert manager._load(task_id)["_checkpoint_unavailable"] is True
    assert len(manager.drain_messages("worker")) == 1


def test_result_wait_does_not_adopt_a_relaunched_invocation(owned_child, monkeypatch):
    manager, task_id, release = owned_child
    original_job = manager._jobs[task_id]
    original_wait = original_job.done_event.wait
    entered = threading.Event()
    completed = threading.Event()
    read_release = threading.Event()

    def wait(timeout=None):
        entered.set()
        finished = original_wait(timeout)
        if finished:
            completed.set()
            assert read_release.wait(3)
        return finished

    monkeypatch.setattr(original_job.done_event, "wait", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(manager.wait_for_result, task_id)
        try:
            assert entered.wait(1)
            release.set()
            assert completed.wait(2)
            state = manager._load_raw(task_id)
            manager._launch(state, {"prompt": "new invocation", "read_only": True})
            new_job = manager._jobs[task_id]
            assert new_job.settlement_id != original_job.settlement_id
            assert new_job.done_event.wait(2)
            read_release.set()
            assert waiting.result(timeout=1) is None
        finally:
            release.set()
            read_release.set()


def test_result_wait_rejects_unknown_task(tmp_path):
    manager = BackgroundAgentManager(tmp_path, "parent")
    try:
        with pytest.raises(ValueError, match="unknown workflow task"):
            manager.wait_for_result("unknown")
    finally:
        manager.close()


def test_all_workflow_consumers_use_direct_results_without_public_wait(tmp_path, monkeypatch):
    from tests.test_workflow_runtime import _install_fake_child, _manager

    calls = _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=3)

    def public_wait(*_args, **_kwargs):
        pytest.fail("workflow entered the second public wait to obtain a result")

    monkeypatch.setattr(manager, "wait", public_wait)
    runtime = WorkflowRuntime(manager, run_id="direct-results")
    try:
        investigated = runtime.run_agent({"prompt": "inspect parser", "read_only": True})
        synthesis = runtime.synthesize([investigated], "merge evidence", phase="merge")
        verdict = runtime.verify_synthesis(synthesis, [investigated], phase="merge", criteria="supported")

        assert investigated["final_text"] == "RESULT: inspect parser"
        assert synthesis["final_text"] == "SYNTHESIZED RESULT"
        assert verdict["verdict"] == "accept"
        assert runtime.spent_tokens == 21
        assert len(calls) == 3
        assert len(manager.drain_messages("worker")) == 3
        assert not runtime._active_task_ids
    finally:
        manager.close()


@pytest.mark.parametrize("consumer,reason", [
    ("task", "workflow aborted by caller"),
    ("synthesis", "workflow aborted during synthesis"),
    ("verifier", "workflow verifier aborted"),
])
def test_workflow_cancellation_stops_child_and_retires_tracking(tmp_path, monkeypatch, consumer, reason):
    import nz_coder.runtime.agent.subagent as subagent

    started = threading.Event()
    caller_cancelled = threading.Event()

    def run(_prompt, *, session_id, cancel_event, **_kwargs):
        started.set()
        assert cancel_event.wait(2)
        state = subagent._load_subagent_state("parent", session_id, tmp_path)
        state["status"] = "cancelled"
        subagent._save_subagent_state("parent", state, tmp_path)
        return "cancelled"

    monkeypatch.setattr(subagent, "run_subagent", run)
    manager = BackgroundAgentManager(tmp_path, "parent")
    runtime = WorkflowRuntime(manager, run_id="cancel-results", cancel_event=caller_cancelled)

    def consume():
        if consumer == "task":
            return runtime.run_agent({"prompt": "inspect", "read_only": True})
        if consumer == "synthesis":
            return runtime.synthesize([], "merge", phase="merge")
        return runtime.verify_synthesis({}, [], phase="merge", criteria="supported")

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(consume)
            try:
                assert started.wait(1)
                caller_cancelled.set()
                with pytest.raises(WorkflowAbortError, match=reason):
                    waiting.result(timeout=3)
                assert not runtime._active_task_ids
                assert runtime.spent_tokens == 0
                state = manager._states()[0]
                assert manager._jobs[state["session_id"]].done_event.is_set()
                assert state["status"] == "cancelled"
            finally:
                caller_cancelled.set()
    finally:
        manager.close()
