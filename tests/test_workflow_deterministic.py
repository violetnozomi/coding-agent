"""Deterministic scheduling and cleanup tests for declarative workflows."""
from __future__ import annotations

from concurrent.futures import Future
import threading
from typing import Callable

import pytest


_WATCHDOG_SECONDS = 10.0
_STAGES = [
    {"prompt": "stage-1 {item}", "read_only": True},
    {"prompt": "stage-2 {item} after {previous}", "read_only": True},
]


def _manager(tmp_path, monkeypatch, *, max_tasks=4, concurrency=2):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_TASKS", max_tasks)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", concurrency)
    return BackgroundAgentManager(tmp_path, "parent")


def _install_controlled_child(
    monkeypatch,
    workspace,
    on_call: Callable[[str, threading.Event], None],
) -> list[str]:
    import nz_coder.runtime.agent.subagent as subagent

    calls: list[str] = []
    calls_lock = threading.Lock()

    def fake_run(prompt, *, session_id, cancel_event=None, **_kwargs):
        assert cancel_event is not None
        with calls_lock:
            calls.append(prompt)
        on_call(prompt, cancel_event)
        state = subagent._load_subagent_state("parent", session_id, workspace)
        state["status"] = "cancelled" if cancel_event.is_set() else "completed"
        state["tokens"] = {"output": 1, "total": 1}
        subagent._save_subagent_state("parent", state, workspace)
        return "cancelled" if cancel_event.is_set() else f"RESULT: {prompt}"

    monkeypatch.setattr(subagent, "run_subagent", fake_run)
    return calls


def _start_call(call: Callable[[], object]) -> tuple[Future, threading.Thread]:
    future: Future = Future()

    def run() -> None:
        try:
            future.set_result(call())
        except BaseException as exc:
            future.set_exception(exc)

    thread = threading.Thread(target=run, name="workflow-test-controller", daemon=True)
    thread.start()
    return future, thread


def _release_and_drain(
    *,
    releases: list[threading.Event],
    future: Future,
    thread: threading.Thread,
    manager,
) -> None:
    for release in releases:
        release.set()
    if not future.done():
        try:
            future.result(timeout=_WATCHDOG_SECONDS)
        except BaseException:
            pass
    thread.join(_WATCHDOG_SECONDS)
    manager.close(timeout=_WATCHDOG_SECONDS)
    assert not thread.is_alive(), "workflow controller did not drain"


def _wait(event: threading.Event, description: str) -> None:
    assert event.wait(_WATCHDOG_SECONDS), f"deadlock waiting for {description}"


def test_pipeline_advances_ready_item_without_global_stage_barrier(
    tmp_path,
    monkeypatch,
):
    """A ready item reaches stage two while its sibling remains in stage one."""
    from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime

    first_started = threading.Event()
    second_started = threading.Event()
    first_release = threading.Event()
    second_release = threading.Event()
    second_stage_started = threading.Event()

    def on_call(prompt: str, _cancel_event: threading.Event) -> None:
        if prompt.startswith('stage-1 "first"'):
            first_started.set()
            _wait(first_release, "first item stage-one release")
        elif prompt.startswith('stage-1 "second"'):
            second_started.set()
            _wait(second_release, "second item stage-one release")
        elif prompt.startswith('stage-2 "second"'):
            second_stage_started.set()

    manager = _manager(tmp_path, monkeypatch)
    _install_controlled_child(monkeypatch, tmp_path, on_call)
    runtime = WorkflowRuntime(manager, run_id="pipeline-no-stage-barrier")
    future, thread = _start_call(
        lambda: runtime.pipeline(["first", "second"], _STAGES, phase="pipeline")
    )
    try:
        _wait(first_started, "first item stage one")
        _wait(second_started, "second item stage one")
        second_release.set()
        _wait(second_stage_started, "second item stage two")
        assert not first_release.is_set()
        first_release.set()
        results = future.result(timeout=_WATCHDOG_SECONDS)
        assert [item["status"] for item in results] == ["completed", "completed"]
    finally:
        _release_and_drain(
            releases=[first_release, second_release],
            future=future,
            thread=thread,
            manager=manager,
        )


def test_pipeline_returns_input_order_after_reverse_completion(
    tmp_path,
    monkeypatch,
):
    """Completion order does not change logical output order."""
    from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime

    first_final_started = threading.Event()
    first_final_release = threading.Event()
    second_final_started = threading.Event()
    second_final_completed = threading.Event()
    completion_order: list[str] = []
    completion_lock = threading.Lock()

    def on_call(prompt: str, _cancel_event: threading.Event) -> None:
        if prompt.startswith('stage-2 "first"'):
            first_final_started.set()
            _wait(first_final_release, "first item final-stage release")
        elif prompt.startswith('stage-2 "second"'):
            second_final_started.set()

    manager = _manager(tmp_path, monkeypatch)
    _install_controlled_child(monkeypatch, tmp_path, on_call)
    runtime = WorkflowRuntime(manager, run_id="pipeline-input-order")
    original_run_agent = runtime.run_agent

    def observed_run_agent(task: dict) -> dict | None:
        result = original_run_agent(task)
        prompt = str(task.get("prompt") or "")
        completed = (
            "first" if prompt.startswith('stage-2 "first"')
            else "second" if prompt.startswith('stage-2 "second"')
            else ""
        )
        if completed and result is not None:
            with completion_lock:
                completion_order.append(completed)
            if completed == "second":
                second_final_completed.set()
        return result

    monkeypatch.setattr(runtime, "run_agent", observed_run_agent)
    future, thread = _start_call(
        lambda: runtime.pipeline(["first", "second"], _STAGES, phase="pipeline")
    )
    try:
        _wait(first_final_started, "first item final stage")
        _wait(second_final_started, "second item final stage")
        _wait(second_final_completed, "second item run-agent completion")
        first_final_release.set()
        results = future.result(timeout=_WATCHDOG_SECONDS)
        with completion_lock:
            assert completion_order == ["second", "first"]
        assert results[0]["final_text"].startswith('RESULT: stage-2 "first"')
        assert results[1]["final_text"].startswith('RESULT: stage-2 "second"')
    finally:
        _release_and_drain(
            releases=[first_final_release],
            future=future,
            thread=thread,
            manager=manager,
        )


@pytest.mark.parametrize("failed_stage", ["stage-1", "stage-2"])
def test_pipeline_isolates_stage_failure_in_logical_input_slot(
    tmp_path,
    monkeypatch,
    failed_stage,
):
    """An ordinary stage failure stops only that item chain and keeps its slot."""
    from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime

    def on_call(prompt: str, _cancel_event: threading.Event) -> None:
        if prompt.startswith(f'{failed_stage} "first"'):
            raise RuntimeError(f"controlled {failed_stage} failure")

    manager = _manager(tmp_path, monkeypatch)
    calls = _install_controlled_child(monkeypatch, tmp_path, on_call)
    try:
        results = WorkflowRuntime(
            manager,
            run_id=f"pipeline-{failed_stage}-failure",
        ).pipeline(["first", "second"], _STAGES, phase="pipeline")

        assert results[0] is None
        assert results[1]["final_text"].startswith('RESULT: stage-2 "second"')
        if failed_stage == "stage-1":
            assert not any(call.startswith('stage-2 "first"') for call in calls)
    finally:
        manager.close(timeout=_WATCHDOG_SECONDS)


def test_pipeline_caller_cancel_stops_started_tasks_and_skips_later_stages(
    tmp_path,
    monkeypatch,
):
    """Caller cancellation settles active children before the pipeline returns."""
    from nz_coder.runtime.workflows.workflow_runtime import (
        WorkflowAbortError,
        WorkflowRuntime,
    )

    caller_cancel = threading.Event()
    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def on_call(prompt: str, task_cancel: threading.Event) -> None:
        if prompt.startswith('stage-1 "first"'):
            first_started.set()
        elif prompt.startswith('stage-1 "second"'):
            second_started.set()
        else:
            return
        while not release.wait(0.05):
            if task_cancel.is_set():
                return

    manager = _manager(tmp_path, monkeypatch)
    calls = _install_controlled_child(monkeypatch, tmp_path, on_call)
    runtime = WorkflowRuntime(
        manager,
        run_id="pipeline-caller-cancel",
        cancel_event=caller_cancel,
    )
    future, thread = _start_call(
        lambda: runtime.pipeline(["first", "second"], _STAGES, phase="pipeline")
    )
    try:
        _wait(first_started, "first active item")
        _wait(second_started, "second active item")
        caller_cancel.set()
        with pytest.raises(WorkflowAbortError, match="aborted by caller"):
            future.result(timeout=_WATCHDOG_SECONDS)
        assert not any(call.startswith("stage-2") for call in calls)
        assert runtime._active_task_ids == set()
        assert all(job.done_event.is_set() for job in manager._jobs.values())
        assert {state["status"] for state in manager._states()} == {"cancelled"}
    finally:
        _release_and_drain(
            releases=[release],
            future=future,
            thread=thread,
            manager=manager,
        )


def test_pipeline_manager_stop_drains_started_tasks_and_skips_later_stages(
    tmp_path,
    monkeypatch,
):
    """A consumer stop settles active pipeline work and remains terminal."""
    from nz_coder.runtime.workflows.workflow_runtime import (
        WorkflowAbortError,
        WorkflowRuntime,
    )

    first_started = threading.Event()
    second_started = threading.Event()
    release = threading.Event()

    def on_call(prompt: str, task_cancel: threading.Event) -> None:
        if prompt.startswith('stage-1 "first"'):
            first_started.set()
        elif prompt.startswith('stage-1 "second"'):
            second_started.set()
        else:
            return
        while not release.wait(0.05):
            if task_cancel.is_set():
                return

    manager = _manager(tmp_path, monkeypatch)
    calls = _install_controlled_child(monkeypatch, tmp_path, on_call)
    runtime = WorkflowRuntime(manager, run_id="pipeline-manager-stop")
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "pipeline",
            "mode": "pipeline",
            "items": ["first", "second"],
            "stages": _STAGES,
        }],
    }
    future, thread = _start_call(lambda: runtime.execute(plan))
    try:
        _wait(first_started, "first active item")
        _wait(second_started, "second active item")
        assert manager.stop_workflow_run(runtime.run_id, "consumer stopped")
        with pytest.raises(WorkflowAbortError, match="stopped by manager"):
            future.result(timeout=_WATCHDOG_SECONDS)
        assert not any(call.startswith("stage-2") for call in calls)
        assert runtime._active_task_ids == set()
        assert all(job.done_event.is_set() for job in manager._jobs.values())
        assert {state["status"] for state in manager._states()} == {"cancelled"}
        assert manager.workflow_run_snapshots()[0]["status"] == "stopped"
    finally:
        _release_and_drain(
            releases=[release],
            future=future,
            thread=thread,
            manager=manager,
        )


def test_pipeline_control_failure_stops_peer_before_waiting_for_executor_shutdown(
    tmp_path,
    monkeypatch,
):
    """A structural failure stops active peers before joining item chains."""
    from nz_coder.runtime.workflows.workflow_runtime import (
        WorkflowBudgetError,
        WorkflowRuntime,
    )

    blocked_started = threading.Event()
    blocked_cancelled = threading.Event()
    blocked_release = threading.Event()

    def on_call(prompt: str, task_cancel: threading.Event) -> None:
        if prompt.startswith('stage-1 "blocked"'):
            blocked_started.set()
            while not blocked_release.wait(0.05):
                if task_cancel.is_set():
                    blocked_cancelled.set()
                    return
        elif prompt.startswith('stage-1 "budget"'):
            _wait(blocked_started, "blocked peer to start")

    manager = _manager(tmp_path, monkeypatch)
    calls = _install_controlled_child(monkeypatch, tmp_path, on_call)
    runtime = WorkflowRuntime(
        manager,
        run_id="pipeline-control-failure",
        token_budget=1,
    )
    plan = {
        "require_synthesis": False,
        "phases": [{
            "name": "pipeline",
            "mode": "pipeline",
            "items": ["blocked", "budget", "queued"],
            "stages": _STAGES,
        }],
    }
    future, thread = _start_call(lambda: runtime.execute(plan))
    try:
        _wait(blocked_started, "blocked peer")
        _wait(blocked_cancelled, "structural failure to cancel its active peer")
        with pytest.raises(WorkflowBudgetError, match="tokenBudget cap"):
            future.result(timeout=_WATCHDOG_SECONDS)
        assert not any(call.startswith("stage-2") for call in calls)
        assert not any('"queued"' in call for call in calls)
        assert manager.spawned_count() == 2
        assert runtime._active_task_ids == set()
        assert all(job.done_event.is_set() for job in manager._jobs.values())
    finally:
        _release_and_drain(
            releases=[blocked_release],
            future=future,
            thread=thread,
            manager=manager,
        )


@pytest.mark.parametrize("private_type", [False, True])
def test_parallel_cleanup_failure_does_not_replace_control_error(
    tmp_path,
    monkeypatch,
    private_type,
):
    """Best-effort peer cleanup preserves the structural failure as primary."""
    from nz_coder.runtime.workflows.workflow_runtime import (
        WorkflowBudgetError,
        WorkflowRuntime,
    )

    manager = _manager(tmp_path, monkeypatch)
    runtime = WorkflowRuntime(manager, run_id="parallel-primary-error")
    error_type = type("SENTINEL_PRIVATE_TYPE", (Exception,), {}) if private_type else RuntimeError

    def fail_cleanup(_reason: str) -> None:
        raise error_type("SENTINEL-private-cleanup")

    monkeypatch.setattr(runtime, "stop_active", fail_cleanup)

    def fail() -> None:
        raise WorkflowBudgetError("primary budget failure")

    try:
        with pytest.raises(WorkflowBudgetError, match="primary budget failure"):
            runtime.parallel([fail])
        cleanup_events = [
            event for event in manager.events().metadata["workflow_events"]
            if event["type"] == "workflow_log"
            and event["data"].get("message") == "parallel peer cleanup failed"
        ]
        assert cleanup_events[-1]["data"]["data"] == {
            "error_type": "Exception" if private_type else "RuntimeError",
            "stage": "parallel-control-cleanup",
        }
    finally:
        manager.close(timeout=_WATCHDOG_SECONDS)
