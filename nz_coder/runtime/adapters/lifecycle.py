"""Legacy Agent host adapter for the focused production run lifecycle."""
from __future__ import annotations

import copy

from nz_coder import config
from nz_coder.message_schema import bind_user_context
from nz_coder.run_evidence import RunEvidence
from nz_coder.runtime.core.lifecycle_context import (
    LifecycleExecutionContext,
    LifecycleRunState,
)
from nz_coder.runtime.session_revert import SessionReverter
from nz_coder.runtime.workdir import current_workdir
from nz_coder.sessions import session_runtime_state_path


def lifecycle_context_from_legacy_host(host) -> LifecycleExecutionContext:
    """Project explicit lifecycle state and commit it back at settled boundaries."""
    state = _state_from_host(host)
    tracer = host.tracer

    def commit() -> None:
        mapping = {
            "tool_calls_this_run": "tool_calls_this_run",
            "used_save_memory": "used_save_memory",
            "tool_batch_sequence": "_tool_batch_sequence",
            "tool_observability": "_tool_observability",
            "sidecar_risky_shell_ops": "_sidecar_risky_shell_ops",
            "sidecar_unattributed_write_ops": "_sidecar_unattributed_write_ops",
            "last_status": "last_status",
            "restored_state": "_restored_state",
            "replan_count": "_replan_count",
            "reflection_signature": "_reflection_signature",
            "reflection_attempts": "_reflection_attempts",
            "cached_reflection_review": "_cached_reflection_review",
            "last_reflection_review": "_last_reflection_review",
            "last_terminal_summary": "_last_terminal_summary",
            "structured_output_attempted": "_structured_output_attempted",
            "structured_output_active_repair": "_structured_output_active_repair",
            "structured_outputs": "_structured_outputs",
            "structured_output_evaluations": "_structured_output_evaluations",
            "admission_terminal_violations": "_admission_terminal_violations",
            "admission_session": "_admission_session",
            "lineage_finished": "_lineage_finished",
        }
        for source, target in mapping.items():
            setattr(host, target, getattr(state, source))

    def prepare_runtime_state(task_text: str, max_turns: int, timeout: float) -> bool:
        host._runtime_state_path = session_runtime_state_path(host.session_id)
        legacy_path = current_workdir() / ".nz-coder" / "runtime_state.json"
        host.runtime_state.reset(max_turns=max_turns, timeout_seconds=timeout)
        host.runtime_state.set_acceptance_criteria_from_text(task_text)
        host.runtime_state.initial_task_text = task_text
        restored = False
        if config.RUNTIME_STATE_PERSIST:
            restore_path = host._runtime_state_path
            if not restore_path.exists() and legacy_path.exists():
                restore_path = legacy_path
            restored = host.runtime_state.load(restore_path)
            if restored:
                host.runtime_state.max_turns = max_turns
                host.runtime_state.timeout_seconds = timeout
                if not host.runtime_state.initial_task_text:
                    host.runtime_state.initial_task_text = task_text
                if host.runtime_state.plan_text:
                    host._sp.replace_category("plan", host.runtime_state.plan_text)
        return bool(restored)

    return LifecycleExecutionContext(
        run_state=state,
        session_id=host.session_id,
        vm=host.vm,
        recovery=host.recovery,
        stall_orchestrator=getattr(host, "stall_orchestrator", None),
        admission_handle=host.admission_handle,
        runtime_state=host.runtime_state,
        permissions_mode=host.permissions.mode,
        provider_id=host.provider_id,
        model_id=host.model_id,
        model_variant=host.model_variant,
        model_capabilities=host.model_capabilities,
        current_agent_name=lambda: host.current_agent_name,
        structured_outputs=lambda: host._structured_outputs,
        clear_reverter=lambda: _clear_reverter(host),
        reset_hooks=host.hooks.reset_run_state,
        clear_reasoning_escalation=host._agent_reasoning_escalated.clear,
        restore_agent_role=lambda: _restore_agent_role(host),
        bind_user_messages=lambda messages: _bind_new_user_messages(host, messages),
        scratchpad_plan=lambda text: host._sp.replace_category("plan", text),
        prepare_runtime_state=prepare_runtime_state,
        start_run_evidence=lambda: _start_run_evidence(host),
        persist_runtime_state=host._persist_runtime_state,
        publish_started=lambda messages, stream, max_turns: _publish_started(
            host, messages, stream, max_turns,
        ),
        assert_terminal=host._assert_admission_terminal,
        finish_lineage=host._finish_lineage,
        persist_assistant_end=host._persist_assistant_end_state,
        runtime_summary=host._runtime_summary,
        run_evidence=lambda: host.run_evidence,
        trace_evidence_summary=host._run_evidence_summary,
        trace=tracer.log,
        publish_event=host._emit_session_event,
        save_learnings=host._maybe_save_learnings,
        save_learnings_async=host._maybe_save_learnings_async,
        commit=commit,
    )


def _state_from_host(host) -> LifecycleRunState:
    return LifecycleRunState(
        tool_calls_this_run=int(getattr(host, "tool_calls_this_run", 0)),
        used_save_memory=bool(getattr(host, "used_save_memory", False)),
        tool_batch_sequence=int(getattr(host, "_tool_batch_sequence", 0)),
        tool_observability=copy.deepcopy(getattr(host, "_tool_observability", {})),
        sidecar_risky_shell_ops=int(getattr(host, "_sidecar_risky_shell_ops", 0)),
        sidecar_unattributed_write_ops=int(
            getattr(host, "_sidecar_unattributed_write_ops", 0)
        ),
        last_status=copy.deepcopy(getattr(host, "last_status", {})),
        restored_state=bool(getattr(host, "_restored_state", False)),
        replan_count=int(getattr(host, "_replan_count", 0)),
        reflection_signature=str(getattr(host, "_reflection_signature", "")),
        reflection_attempts=int(getattr(host, "_reflection_attempts", 0)),
        cached_reflection_review=getattr(host, "_cached_reflection_review", None),
        last_reflection_review=getattr(host, "_last_reflection_review", None),
        last_terminal_summary=str(getattr(host, "_last_terminal_summary", "")),
        structured_output_attempted=set(
            getattr(host, "_structured_output_attempted", set())
        ),
        structured_output_active_repair=str(
            getattr(host, "_structured_output_active_repair", "")
        ),
        structured_outputs=copy.deepcopy(getattr(host, "_structured_outputs", {})),
        structured_output_evaluations=copy.deepcopy(
            getattr(host, "_structured_output_evaluations", {})
        ),
        admission_terminal_violations=tuple(
            getattr(host, "_admission_terminal_violations", ())
        ),
        admission_session=getattr(host, "_admission_session", None),
        lineage_finished=bool(getattr(host, "_lineage_finished", False)),
    )


def _clear_reverter(host) -> None:
    reverter = getattr(host, "session_reverter", None)
    if isinstance(reverter, SessionReverter):
        reverter.clear()


def _start_run_evidence(host) -> None:
    host.run_evidence = RunEvidence(run_id=host.tracer.run_id)
    host.run_evidence.task_mode = host.runtime_state.task_mode


def _bind_new_user_messages(host, messages: list) -> None:
    last_assistant = max(
        (
            index for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "assistant"
        ),
        default=-1,
    )
    for message in messages[last_assistant + 1:]:
        if isinstance(message, dict) and message.get("role") == "user":
            bind_user_context(
                message,
                agent="plan" if host.permissions.mode == "plan" else "build",
                provider_id=host.provider_id,
                model_id=host.model_id,
                variant=host.model_variant,
            )


def _restore_agent_role(host) -> None:
    graph = getattr(host, "agent_graph", None)
    lineage = getattr(host, "lineage", None)
    if graph is None:
        if lineage is not None:
            lineage.append("run_started", {
                "agent": str(getattr(host, "agent_id", "worker") or "worker"),
                "graph": [], "resumed": False,
                "runtime_profile": str(getattr(host, "runtime_profile", "direct")),
                "control_plane": str(getattr(
                    host, "runtime_control_plane", "native-coding-loop"
                )),
            })
        return
    recovered, depth = (
        lineage.recover_open_agent_state(graph.start)
        if lineage is not None else (graph.start, 0)
    )
    host.current_agent_name = recovered if recovered in graph.names() else graph.start
    stack = host.agent_call_stack_store.load()
    if len(stack) < depth:
        raise RuntimeError(
            "Interrupted Agent handoff cannot resume: durable caller stack "
            "is missing or shorter than the lineage journal"
        )
    if len(stack) > depth:
        stack = stack[:depth]
        host.agent_call_stack_store.save(stack)
    if depth and stack[-1].get("target") != host.current_agent_name:
        raise RuntimeError(
            "Interrupted Agent handoff cannot resume: durable caller stack "
            "is missing or does not match the active Agent"
        )
    host._agent_call_stack = stack if depth else []
    host._activate_agent_runtime(host.current_agent_name)
    host._handoff_count = 0
    if lineage is not None:
        lineage.append("run_started", {
            "agent": host.current_agent_name,
            "graph": list(graph.names()),
            "resumed": host.current_agent_name != graph.start,
            "runtime_profile": str(getattr(host, "runtime_profile", "direct")),
            "control_plane": str(getattr(
                host, "runtime_control_plane", "declared-agent-graph"
            )),
            "admitted": host.admission_handle is not None,
            "invariant_bindings": list(
                host.admission_handle.invariant_bindings
                if host.admission_handle is not None else ()
            ),
            "admission_clamps": list(
                host.admission_handle.clamp_notes
                if host.admission_handle is not None else ()
            ),
        })


def _publish_started(host, messages: list, stream: bool, max_turns: int) -> None:
    capabilities = host.model_capabilities
    host.tracer.log(
        "run_start", message_count=len(messages), stream=stream,
        mode=host.permissions.mode, change_set=str(host.change_tracker.path),
        max_turns=max_turns, restored_runtime_state=host._restored_state,
        model=host.model_id, request_model=host._active_model_id(),
        model_family=capabilities.family, prompt_family=capabilities.prompt_family,
        context_tokens=capabilities.context_tokens,
        output_tokens=capabilities.output_tokens,
        supports_tools=capabilities.supports_tools,
        supports_streaming=capabilities.supports_streaming,
        supports_reasoning=capabilities.supports_reasoning,
        model_variant=capabilities.selected_variant,
        available_variants=capabilities.available_variants,
        capability_source=capabilities.source,
        runtime_profile=str(getattr(host, "runtime_profile", "direct")),
        control_plane=str(getattr(
            host, "runtime_control_plane", "native-coding-loop"
        )),
        active_agent=str(host.current_agent_name or "worker"),
        admitted=host.admission_handle is not None,
        admitted_capabilities=sorted(
            host.admission_handle.system_cap.effective_capabilities()
            if host.admission_handle is not None else ()
        ),
    )
    host._emit_session_event("session.run.started", {
        "message_count": len(messages), "stream": bool(stream),
        "permission_mode": host.permissions.mode,
        "model": host.model_id, "max_turns": max_turns,
    })
