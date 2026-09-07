"""Focused run-scoped capabilities consumed by the production Tool Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from nz_coder.runtime.agent.admission import AdmittedAgentHandle
from nz_coder.runtime.agent.handoffs import AgentGraph, HandoffSignal
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.core.tool_contracts import (
    DispatchedTools,
    MetadataReporter,
    ToolBatchObserver,
    ToolCheckpoint,
    ToolExecutorPort,
    ToolResultConsumer,
    ToolResultTrace,
    ToolTaskState,
    ToolTrace,
    ToolTransactionPort,
    ToolPolicyPermissions,
    ToolRecoveryPort,
    ToolStallPort,
)

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
    agent_graph: AgentGraph | None
    tool_allowlist: frozenset[str] | None
    admission_handle: AdmittedAgentHandle | None
    runtime_state: ToolTaskState | None
    recovery: ToolRecoveryPort
    permissions: ToolPolicyPermissions
    stall_orchestrator: ToolStallPort | None
    parse_input: Callable[[object], dict]
    trace: ToolTrace
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

    checkpoint: ToolCheckpoint
    drain_progress: Callable[[], Awaitable[None]]
    processor_for_messages: Callable[[list[dict]], SessionProcessor | None]
    transaction: ToolTransactionPort
    metadata_reporter: Callable[[SessionProcessor | None, list[dict]], MetadataReporter]
    question_reporter: Callable[[SessionProcessor | None, list[dict]], MetadataReporter]
    model_capabilities: ModelCapabilities | None
    describe_read_results: Callable[[DispatchedTools, list[dict]], Awaitable[bool]]
    strict_completed: Callable[[DispatchedTools], bool]
    apply_transition: Callable[
        [HandoffSignal, list[dict], SessionProcessor | None], Awaitable[dict | None]
    ]
    observer: ToolBatchObserver
    has_pre_tool_hooks: Callable[[], bool]
    executor: ToolExecutorPort
    execute_one: Callable[[dict, int, list[dict]], ToolExecutionResult]
    before_tool: Callable[
        [dict, list[dict]], Awaitable[tuple[dict, ToolExecutionResult | None]]
    ]
    after_tool: Callable[
        [dict, ToolExecutionResult, list[dict]], Awaitable[ToolExecutionResult]
    ]
    trace: ToolTrace
    # Explicit legacy-only overrides. Native composition leaves these empty.
    write_override: Callable[[list[dict]], bool] | None = None
    dispatch_override_async: (
        Callable[[list[dict], bool, list[dict]], Awaitable[DispatchedTools]] | None
    ) = None
    dispatch_override_sync: (
        Callable[[list[dict], bool, list[dict]], DispatchedTools] | None
    ) = None
    consume_override: ToolResultConsumer | None = None

    def __post_init__(self) -> None:
        for name in (
            "checkpoint",
            "drain_progress",
            "processor_for_messages",
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
        if self.executor is None or not callable(
            getattr(self.executor, "execute_one", None)
        ):
            raise TypeError("ToolLifecycleContext requires an executor")
        if self.transaction is None or not all(
            callable(getattr(self.transaction, name, None))
            for name in ("begin", "finish")
        ):
            raise TypeError("ToolLifecycleContext requires transaction begin/finish")
        for name in (
            "post_write",
            "after_batch",
            "apply_plan_mode",
            "capture_snapshot",
            "record_patch",
        ):
            if not callable(getattr(self.observer, name, None)):
                raise TypeError(f"ToolLifecycleContext observer must implement {name}")


@dataclass(frozen=True)
class ToolProjectionContext:
    """Stable result projection operations separated from AgentLoop."""

    signal_from_metadata: Callable[[dict | None], HandoffSignal | None]
    record_result: Callable[[ToolExecutionResult], bool]
    trace_result: ToolResultTrace
    stall_orchestrator: ToolStallPort | None
    after_result: Callable[[list[dict], ToolExecutionResult, str], None]
    available_result_tokens: Callable[[list[dict]], int] | None = None
    runtime_state: ToolTaskState | None = None

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
