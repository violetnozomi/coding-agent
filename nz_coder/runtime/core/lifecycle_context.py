"""Focused mutable state and capabilities for one Agent run lifecycle."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


def _empty_tool_observability() -> dict:
    return {
        "batches": 0,
        "calls": 0,
        "wall_ms": 0.0,
        "tool_duration_ms": 0.0,
        "peak_concurrency": 0,
        "parallel_segments": 0,
        "serial_segments": 0,
        "barrier_wait_ms": 0.0,
        "streak_resets": 0,
    }


@dataclass
class LifecycleRunState:
    """Ephemeral lifecycle facts reset for every Runner invocation."""

    tool_calls_this_run: int = 0
    used_save_memory: bool = False
    tool_batch_sequence: int = 0
    tool_observability: dict = field(default_factory=_empty_tool_observability)
    sidecar_risky_shell_ops: int = 0
    sidecar_unattributed_write_ops: int = 0
    last_status: dict = field(default_factory=lambda: {"status": "running", "errors": 0})
    restored_state: bool = False
    replan_count: int = 0
    reflection_signature: str = ""
    reflection_attempts: int = 0
    cached_reflection_review: object | None = None
    last_reflection_review: object | None = None
    last_terminal_summary: str = ""
    structured_output_attempted: set[str] = field(default_factory=set)
    structured_output_active_repair: str = ""
    structured_outputs: dict = field(default_factory=dict)
    structured_output_evaluations: dict = field(default_factory=dict)
    admission_terminal_violations: tuple = ()
    admission_session: object | None = None
    lineage_finished: bool = False

    def reset(self) -> None:
        """Reset all run-lifetime facts without sharing mutable containers."""
        fresh = type(self)()
        for name, value in vars(fresh).items():
            setattr(self, name, value)


LifecycleCallback = Callable[..., object]


@dataclass(frozen=True)
class LifecycleExecutionContext:
    """Explicit state objects and operations used by ProductionRunLifecycle."""

    run_state: LifecycleRunState
    session_id: str
    vm: object
    recovery: object
    stall_orchestrator: object | None
    admission_handle: object | None
    runtime_state: object
    permissions_mode: str
    provider_id: str
    model_id: str
    model_variant: str | None
    model_capabilities: object
    current_agent_name: LifecycleCallback
    structured_outputs: LifecycleCallback
    clear_reverter: LifecycleCallback
    reset_hooks: LifecycleCallback
    clear_reasoning_escalation: LifecycleCallback
    restore_agent_role: LifecycleCallback
    bind_user_messages: LifecycleCallback
    scratchpad_plan: LifecycleCallback
    prepare_runtime_state: LifecycleCallback
    start_run_evidence: LifecycleCallback
    persist_runtime_state: LifecycleCallback
    publish_started: LifecycleCallback
    assert_terminal: LifecycleCallback
    finish_lineage: LifecycleCallback
    persist_assistant_end: LifecycleCallback
    runtime_summary: LifecycleCallback
    run_evidence: LifecycleCallback
    trace_evidence_summary: LifecycleCallback
    trace: LifecycleCallback
    publish_event: LifecycleCallback
    save_learnings: LifecycleCallback
    save_learnings_async: LifecycleCallback
    commit: LifecycleCallback

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("LifecycleExecutionContext session_id must be non-empty")
        for name, value in vars(self).items():
            if name in {
                "run_state", "session_id", "vm", "recovery", "stall_orchestrator",
                "admission_handle", "runtime_state", "permissions_mode", "provider_id",
                "model_id", "model_variant", "model_capabilities",
            }:
                continue
            if not callable(value):
                raise TypeError(f"LifecycleExecutionContext {name} must be callable")
