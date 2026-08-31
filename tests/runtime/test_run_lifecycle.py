"""Production lifecycle terminal-content ownership contracts."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.protocol.message_schema import ASSISTANT_FINISH_KEY, COMPACTION_KEY, PARTS_KEY
from nz_coder.runtime.core.lifecycle_context import LifecycleRunState
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle, last_user_text
from nz_coder.runtime.execution.runner import _typed_result
from nz_coder.runtime.session.model import Session


class _EmptyEvidence:
    def is_empty(self) -> bool:
        return True


def _lifecycle_context(events: list[str], runtime_state=None, run_evidence=None):
    host = SimpleNamespace(
        _emit_session_event=lambda *_args, **_kwargs: events.append("message-event"),
        _checkpoint_messages=lambda *_args, **_kwargs: events.append("checkpoint"),
        runtime_state=runtime_state,
        run_evidence=run_evidence,
    )

    def persist_assistant_end(messages, status, content_text=""):
        events.append("persist")
        return AgentLoop._persist_assistant_end_state(
            host, messages, status, content_text,
        )

    return SimpleNamespace(
        run_state=LifecycleRunState(),
        stall_orchestrator=None,
        recovery=SimpleNamespace(consecutive_errors=0, last_error=""),
        vm=SimpleNamespace(status=lambda: {}),
        assert_terminal=lambda status: status,
        finish_lineage=lambda *_args: None,
        persist_assistant_end=persist_assistant_end,
        runtime_summary=lambda: {},
        current_agent_name=lambda: "worker",
        structured_outputs=lambda: {},
        commit=lambda: events.append("commit"),
        run_evidence=lambda: _EmptyEvidence(),
        trace_evidence_summary=lambda: {},
        trace=lambda *_args, **_kwargs: None,
        publish_event=lambda *_args, **_kwargs: events.append("publish"),
        persist_runtime_state=lambda **_kwargs: None,
        save_learnings=lambda _messages: None,
    )


def test_lifecycle_clears_read_cache_at_each_run_boundary():
    events: list[str] = []
    runtime_state = SimpleNamespace(turn_count=0, replan_count=0)
    context = SimpleNamespace(
        run_state=LifecycleRunState(),
        clear_reverter=lambda: None,
        vm=SimpleNamespace(reset=lambda: None),
        recovery=SimpleNamespace(start_tool_call_run=lambda: None),
        clear_read_cache=lambda: events.append("read-cache-cleared"),
        stall_orchestrator=None,
        admission_handle=None,
        reset_hooks=lambda: None,
        clear_reasoning_escalation=lambda: None,
        commit=lambda: None,
        restore_agent_role=lambda: None,
        session_id="read-cache-run-boundary",
        bind_user_messages=lambda _messages: None,
        prepare_runtime_state=(
            lambda _task, _turns, _timeout, _resume, _round: False
        ),
        runtime_state=runtime_state,
        start_run_evidence=lambda: None,
        persist_runtime_state=lambda **_kwargs: None,
        publish_started=lambda _messages, _stream, _turns: None,
    )

    ProductionRunLifecycle().initialize(
        context,
        [{"role": "user", "content": "read sample.py"}],
        stream=False,
    )

    assert events == ["read-cache-cleared"]


def test_lifecycle_persists_boundary_content_before_completion(tmp_path):
    events: list[str] = []
    context = _lifecycle_context(events)
    messages = [
        {"role": "user", "content": "update app.py"},
        {
            "role": "assistant",
            "content": "",
            ASSISTANT_FINISH_KEY: "tool-calls",
        },
    ]

    result = ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "completed",
        stream=False,
        content_text="Completed the requested changes in app.py.",
    )

    assert result["content"] == "Completed the requested changes in app.py."
    assert messages[-1]["content"] == result["content"]
    assert events.index("persist") < events.index("commit") < events.index("publish")

    request = RunRequest(
        agent=AgentDefinition(name="worker", instructions="update the file"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "update app.py"},),
        workspace=tmp_path,
        session_id="terminal-content",
    )
    run_context = RunContext(
        request,
        Session.create(request.session_id, messages, workspace=tmp_path),
        request.agent.name,
    )
    typed = _typed_result(request, run_context, result)
    assert typed.final_text == result["content"]


def test_lifecycle_closes_sync_runtime_accounting_after_terminal_learning():
    """Synchronous memory calls belong to the run's final Provider ledger."""
    events: list[str] = []
    traces: list[tuple[str, dict]] = []
    provider_calls = {"count": 0}
    context = _lifecycle_context(events)
    context.runtime_summary = lambda: {"provider_calls": provider_calls["count"]}

    def save_learnings(_messages):
        events.append("learn")
        provider_calls["count"] += 1

    context.save_learnings = save_learnings
    context.trace = lambda event, **payload: traces.append((event, payload))

    result = ProductionRunLifecycle().finalize_sync(
        context,
        [{"role": "user", "content": "remember this fix"}],
        "completed",
        stream=False,
        content_text="Done.",
    )

    run_end = next(payload for event, payload in traces if event == "run_end")
    assert result["runtime"]["provider_calls"] == 1
    assert run_end["runtime"]["provider_calls"] == 1
    assert events.index("learn") < events.index("publish")


def test_lifecycle_closes_async_runtime_accounting_after_terminal_learning():
    """Awaited memory calls must settle before publishing the run boundary."""
    events: list[str] = []
    traces: list[tuple[str, dict]] = []
    provider_calls = {"count": 0}
    context = _lifecycle_context(events)
    context.runtime_summary = lambda: {"provider_calls": provider_calls["count"]}

    async def save_learnings(_messages):
        events.append("learn")
        provider_calls["count"] += 1

    context.save_learnings_async = save_learnings
    context.trace = lambda event, **payload: traces.append((event, payload))

    result = asyncio.run(ProductionRunLifecycle().finalize(
        context,
        [{"role": "user", "content": "remember this fix"}],
        "completed",
        stream=False,
        content_text="Done.",
    ))

    run_end = next(payload for event, payload in traces if event == "run_end")
    assert result["runtime"]["provider_calls"] == 1
    assert run_end["runtime"]["provider_calls"] == 1
    assert events.index("learn") < events.index("publish")


def test_aborted_lifecycle_preserves_runtime_accounting():
    """Failure terminals need the same diagnostic ledger as successful runs."""
    events: list[str] = []
    traces: list[tuple[str, dict]] = []
    context = _lifecycle_context(events)
    context.recovery.consecutive_errors = 3
    context.recovery.last_error = "provider unavailable"
    context.runtime_summary = lambda: {
        "provider_calls": 4,
        "provider_usage": {"total": 1200},
    }
    context.vm.status = lambda: {"vm_state": "idle"}
    context.trace = lambda event, **payload: traces.append((event, payload))

    result = ProductionRunLifecycle().finalize_sync(
        context,
        [{"role": "user", "content": "repair the parser"}],
        "aborted",
        stream=False,
    )

    run_end = next(payload for event, payload in traces if event == "run_end")
    assert result["runtime"]["provider_calls"] == 4
    assert result["vm_state"] == "idle"
    assert run_end["runtime"] == result["runtime"]
    assert run_end["vm_state"] == "idle"


def test_cancelled_lifecycle_does_not_start_terminal_memory_work():
    """Ctrl+C must not launch fresh local or Provider work during teardown."""
    sync_events: list[str] = []
    sync_context = _lifecycle_context(sync_events)
    sync_context.save_learnings = lambda _messages: sync_events.append("learn")

    sync_result = ProductionRunLifecycle().finalize_sync(
        sync_context,
        [{"role": "user", "content": "stop now"}],
        "cancelled",
        stream=False,
    )

    async_events: list[str] = []
    async_context = _lifecycle_context(async_events)

    async def save_learnings(_messages):
        async_events.append("learn")

    async_context.save_learnings_async = save_learnings
    async_result = asyncio.run(ProductionRunLifecycle().finalize(
        async_context,
        [{"role": "user", "content": "stop now"}],
        "cancelled",
        stream=False,
    ))

    assert sync_result["status"] == "cancelled"
    assert async_result["status"] == "cancelled"
    assert "learn" not in sync_events
    assert "learn" not in async_events


def test_terminal_memory_failure_is_observable_but_non_fatal():
    """A broken optional memory store must not erase completed task output."""
    sync_traces: list[tuple[str, dict]] = []
    sync_context = _lifecycle_context([])
    sync_context.save_learnings = lambda _messages: (_ for _ in ()).throw(
        OSError("memory disk full")
    )
    sync_context.trace = (
        lambda event, **payload: sync_traces.append((event, payload))
    )

    sync_result = ProductionRunLifecycle().finalize_sync(
        sync_context,
        [{"role": "user", "content": "finish the repair"}],
        "completed",
        stream=False,
        content_text="Repair complete.",
    )

    async_traces: list[tuple[str, dict]] = []
    async_context = _lifecycle_context([])

    async def fail_memory(_messages):
        raise RuntimeError("memory index corrupt")

    async_context.save_learnings_async = fail_memory
    async_context.trace = (
        lambda event, **payload: async_traces.append((event, payload))
    )
    async_result = asyncio.run(ProductionRunLifecycle().finalize(
        async_context,
        [{"role": "user", "content": "finish the repair"}],
        "completed",
        stream=False,
        content_text="Repair complete.",
    ))

    assert sync_result["status"] == "completed"
    assert async_result["status"] == "completed"
    assert any(event == "terminal_learning_failed" for event, _ in sync_traces)
    assert any(event == "terminal_learning_failed" for event, _ in async_traces)
    assert any(event == "run_end" for event, _ in sync_traces)
    assert any(event == "run_end" for event, _ in async_traces)


def test_terminal_boundary_cancels_stall_sidecar_before_runtime_summary():
    """A late L2 observer must settle inside the run that launched it."""
    events: list[str] = []
    context = _lifecycle_context(events)

    class Stall:
        def cancel_and_settle(self, timeout=0):
            events.append(f"stall-settled:{timeout}")
            return True

    context.stall_orchestrator = Stall()

    def runtime_summary():
        assert any(item.startswith("stall-settled:") for item in events)
        return {"provider_calls": 2}

    context.runtime_summary = runtime_summary

    result = ProductionRunLifecycle().finalize_sync(
        context,
        [{"role": "user", "content": "finish"}],
        "completed",
        stream=False,
        content_text="Done.",
    )

    assert result["runtime"]["provider_calls"] == 2


def test_lifecycle_preserves_nonempty_provider_content():
    events: list[str] = []
    context = _lifecycle_context(events)
    messages = [
        {"role": "user", "content": "update app.py"},
        {
            "role": "assistant",
            "content": "provider summary",
            ASSISTANT_FINISH_KEY: "stop",
        },
    ]

    result = ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "completed",
        stream=False,
        content_text="deterministic fallback",
    )

    assert result["content"] == "deterministic fallback"
    assert messages[-1]["content"] == "provider summary"


def test_lifecycle_surfaces_factual_max_turns_content_instead_of_generic_notice():
    events: list[str] = []
    visible: list[str] = []
    context = _lifecycle_context(events)
    messages = [
        {"role": "user", "content": "update package docs"},
        {
            "role": "assistant",
            "content": "Everything is complete.",
            ASSISTANT_FINISH_KEY: "stop",
        },
    ]
    summary = (
        "Stopped at the work limit without claiming completion. "
        "Unresolved requirements: R5."
    )

    result = ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "max_turns",
        on_text=visible.append,
        stream=False,
        content_text=summary,
        max_turns=20,
    )

    assert result["status"] == "max_turns"
    assert result["content"] == summary
    assert visible == [summary]
    assert not any("max_turns=20" in item for item in visible)
    assert messages[-1]["content"] == summary


def test_max_turns_keeps_runtime_state_resumable():
    """An unfinished terminal boundary must not close its task state."""
    persisted = []
    context = _lifecycle_context([])
    context.persist_runtime_state = lambda **payload: persisted.append(payload)
    messages = [{"role": "user", "content": "finish the parser fix"}]

    ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "max_turns",
        stream=False,
        content_text="Stopped with parser verification still pending.",
        max_turns=20,
    )

    assert persisted[-1] == {"active": True}


def test_last_user_text_preserves_late_acceptance_scope_for_runtime_policy():
    """Long continuation instructions must not lose their trailing test scope."""
    from nz_coder.runtime.agent.task_policy import declared_test_scopes

    prompt = (
        "Continue the previous run in this same Session; do not restart or "
        "broadly re-explore. The previous max_turns completion claim was not "
        "trustworthy. Close only the original unresolved acceptance-critical "
        "work: update the explicitly requested cron_engine/README.md (not the "
        "workspace-root README), preserve all numeric API and 0/7 Sunday "
        "compatibility while correcting any remaining semantic issue in named "
        "weekday wrap ranges and steps, add focused regression coverage where "
        "needed, and run the exact acceptance command "
        "python -m pytest -q cron_engine/tests."
    )

    task_text = last_user_text([{"role": "user", "content": prompt}])

    assert task_text == prompt
    assert declared_test_scopes(task_text) == ("cron_engine/tests",)


def test_last_user_text_ignores_structured_compaction_summary():
    """Derived summaries must never replace the canonical User task."""
    messages = [{
        "role": "user",
        "content": "Derived summary, not a new instruction",
        COMPACTION_KEY: {"version": 1},
    }]

    assert last_user_text(messages) == ""


def test_last_user_text_ignores_legacy_session_summary_wrapper():
    """Older persisted summaries receive the same canonical-task treatment."""
    messages = [
        {"role": "user", "content": "Fix the original parser regression"},
        {
            "role": "user",
            "content": "<session-summary>derived state</session-summary>",
        },
    ]

    assert last_user_text(messages) == "Fix the original parser regression"


def test_max_turns_after_tool_batch_appends_protocol_safe_terminal_assistant():
    """Terminal truth must follow tool results instead of mutating their owner."""
    events: list[str] = []
    context = _lifecycle_context(events)
    messages = [
        {"role": "user", "content": "update app.py"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "call-1",
                "type": "function",
                "function": {"name": "verify_changed_files", "arguments": "{}"},
            }],
            ASSISTANT_FINISH_KEY: "tool-calls",
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "Error: git diff failed",
        },
        {
            "role": "user",
            "content": (
                "<tool-failure-diagnostic>verification failed"
                "</tool-failure-diagnostic>"
            ),
        },
    ]
    summary = "Stopped at the work limit without claiming completion."

    result = ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "max_turns",
        stream=False,
        content_text=summary,
        max_turns=8,
    )

    assert result["content"] == summary
    assert messages[1]["content"] == ""
    assert messages[1]["tool_calls"][0]["id"] == "call-1"
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == summary
    assert not messages[-1].get("tool_calls")
    assert any(
        part.get("type") == "text" and part.get("text") == summary
        for part in messages[-1][PARTS_KEY]
    )


def test_terminal_lifecycle_cleans_orphan_tool_call_before_checkpoint():
    """Legacy hosts persist the same Provider-safe catch boundary as native."""
    events: list[str] = []
    context = _lifecycle_context(events)
    messages = [
        {"role": "user", "content": "inspect app.py"},
        {
            "role": "assistant",
            "content": "Inspection started.",
            "tool_calls": [{
                "id": "call-interrupted",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
            ASSISTANT_FINISH_KEY: "tool-calls",
        },
    ]

    result = ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "error",
        stream=False,
        content_text="The inspection failed before the tool settled.",
    )

    assert result["status"] == "error"
    assert messages[-1]["content"] == "Inspection started."
    assert "tool_calls" not in messages[-1]
    assert events.index("persist") < events.index("checkpoint")


def test_max_turns_persists_deterministic_continuation_boundary():
    """Terminal persistence must retain exact unfinished work without an LLM call."""
    events: list[str] = []
    acceptance = "python -m pytest -q cron_engine/tests"
    task = (
        "Continue the existing parser work and preserve numeric compatibility. "
        + "detail " * 500
        + f"Finally run the exact acceptance command {acceptance}."
    )
    runtime_state = SimpleNamespace(
        # Legacy sessions may already contain the old 300-character policy
        # projection.  The durable real User message remains authoritative.
        initial_task_text=task[:300],
        changed_files=["cron_engine/parser.py", "cron_engine/README.md"],
        acceptance_criteria=["numeric expressions remain compatible"],
        requested_paths=["cron_engine/README.md"],
        task_contract={"objective": "Finish named weekday range support"},
        requirement_ledger={
            "items": [{
                "requirement": {
                    "id": "R3",
                    "description": "Run focused regression tests",
                },
                "status": "pending",
            }],
        },
        verification_contract={
            "command": acceptance,
            "passed": False,
            "output": "1 failed",
        },
        last_verification_failure="1 failed",
        recovery_repair_targets=["cron_engine/parser.py"],
        open_todo_items=1,
    )
    evidence = SimpleNamespace(
        verification_results=[{
            "command": acceptance,
            "status": "failed",
            "summary": "1 failed",
        }],
        limitations=["verify_changed_files requires Git"],
        tool_failures=[],
    )
    context = _lifecycle_context(events, runtime_state, evidence)
    messages = [{"role": "user", "content": task}]

    ProductionRunLifecycle().finalize_sync(
        context,
        messages,
        "max_turns",
        stream=False,
        content_text="Stopped with R3 unresolved.",
        max_turns=8,
    )

    boundary = messages[-1]["_nz_continuation"]
    assert boundary["status"] == "max_turns"
    assert boundary["version"] == 1
    assert "Finish named weekday range support" in boundary["summary"]
    assert "R3 [pending] Run focused regression tests" in boundary["summary"]
    assert acceptance in boundary["summary"]
    assert task[-120:] in boundary["summary"]
    assert len(boundary["summary"]) <= 6_000
