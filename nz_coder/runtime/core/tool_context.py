"""Focused run-scoped capabilities consumed by the production Tool Runtime."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from nz_coder.runtime.core.run_context import RunContext


def _empty_observability() -> dict[str, float | int]:
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
class ToolPolicyContext:
    """Policy inputs and mutable observations for exactly one run."""

    agent_name: str
    agent_graph: object | None
    tool_allowlist: frozenset[str] | None
    admission_handle: object | None
    runtime_state: object
    recovery: object
    permissions: object
    stall_orchestrator: object | None
    parse_input: Callable[[object], dict]
    trace: Callable[..., None]
    observability: dict = field(default_factory=_empty_observability)
    _batch_sequence: int = 0

    def __post_init__(self) -> None:
        if not self.agent_name.strip():
            raise ValueError("ToolPolicyContext agent_name must be non-empty")
        if not callable(self.parse_input) or not callable(self.trace):
            raise TypeError("ToolPolicyContext callbacks must be callable")

    def next_batch_id(self) -> str:
        self._batch_sequence += 1
        return f"batch-{self._batch_sequence}"


@dataclass(frozen=True)
class ToolLifecycleContext:
    """Persistence and execution lifecycle operations for one tool run."""

    checkpoint: Callable[[str], Awaitable[None]]
    processor_for_messages: ToolCallback
    write_override: ToolCallback | None
    begin_transaction: ToolCallback
    transaction_active: ToolCallback
    finish_transaction: ToolCallback
    metadata_reporter: ToolCallback
    question_reporter: ToolCallback
    dispatch_override_async: ToolCallback | None
    consume_override: ToolCallback | None
    model_capabilities: object | None
    describe_read_results: ToolCallback
    strict_completed: ToolCallback
    apply_transition: ToolCallback
    observer: object
    has_pre_tool_hooks: ToolCallback
    executor: object
    execute_one: ToolCallback
    before_tool: ToolCallback
    after_tool: ToolCallback
    trace: ToolCallback

    def __post_init__(self) -> None:
        for name in (
            "checkpoint",
            "processor_for_messages",
            "begin_transaction",
            "transaction_active",
            "finish_transaction",
            "metadata_reporter",
            "question_reporter",
            "describe_read_results",
            "strict_completed",
            "apply_transition",
            "has_pre_tool_hooks",
            "execute_one",
            "before_tool",
            "after_tool",
            "trace",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"ToolLifecycleContext {name} must be callable")
        for name in (
            "post_write", "after_batch", "apply_plan_mode",
            "capture_snapshot", "record_patch",
        ):
            if not callable(getattr(self.observer, name, None)):
                raise TypeError(f"ToolLifecycleContext observer must implement {name}")


@dataclass(frozen=True)
class ToolProjectionContext:
    """Stable result projection operations separated from AgentLoop."""

    signal_from_metadata: Callable[[dict | None], object | None]
    record_result: Callable[[object], bool]
    trace_result: Callable[..., None]
    stall_orchestrator: object | None
    after_result: Callable[[list[dict], object, str], None]
    available_result_tokens: Callable[[list[dict]], int] | None = None
    runtime_state: object | None = None

    def __post_init__(self) -> None:
        for name in (
            "signal_from_metadata",
            "record_result",
            "trace_result",
            "after_result",
        ):
            if not callable(getattr(self, name)):
                raise TypeError(f"ToolProjectionContext {name} must be callable")


@dataclass(frozen=True)
class ToolExecutionContext:
    """Complete focused input for production asynchronous tool batches."""

    run: RunContext | None
    policy: ToolPolicyContext
    lifecycle: ToolLifecycleContext
    projection: ToolProjectionContext

    def __post_init__(self) -> None:
        if self.run is not None and not isinstance(self.run, RunContext):
            raise TypeError("ToolExecutionContext run must be RunContext or None")


ToolCallback = Callable[..., Any]
