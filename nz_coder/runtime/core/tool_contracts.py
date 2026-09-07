"""Narrow tool ports; raw wire dictionaries are not a second domain model."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol, TypedDict
from dataclasses import dataclass, field

from nz_coder.runtime.agent.handoffs import HandoffSignal
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.tool_platform.execution import ToolExecutionResult


DispatchedTool = tuple[int, dict, ToolExecutionResult]
DispatchedTools = list[DispatchedTool]
ToolCheckpoint = Callable[[list[dict], str], Awaitable[None]]
ToolDisplay = Callable[[str, str], None]
TextDisplay = Callable[[str], None]
MetadataReporter = Callable[[str, dict], None]


@dataclass
class ApprovedToolBatch:
    """Tool calls that crossed repair/guardrail admission exactly once."""

    calls: list[dict]
    blocked: dict[int, ToolExecutionResult] = field(default_factory=dict)


class ToolTrace(Protocol):
    def __call__(self, event: str, /, **payload: object) -> None: ...


class ToolExecutorPort(Protocol):
    """Executor settles execution facts; it does not admit output or append history."""

    def execute_one(self, tool_call: dict, index: int) -> ToolExecutionResult: ...


class ToolPolicyPermissions(Protocol):
    """Interactive policy permission; per-call authorization belongs to Executor."""

    def ask_special(self, kind: str, metadata: dict, /) -> bool: ...


class ToolRecoveryPort(Protocol):
    """Run-owned repeat observations, without importing coding recovery rules."""

    def consume_tool_streak_event(self) -> dict | None: ...
    def observe_tool_call(
        self, tool_name: str, tool_input: object, *, threshold: int
    ) -> dict: ...
    def reset_tool_call_history(self, reason: str = "manual") -> None: ...


class ToolStallPort(Protocol):
    """Optional sidecar observations; scheduling/ownership remain outside core."""

    def consume_pending_nudge(self) -> str | None: ...
    def record_tool_use(self, call: dict, *, cache_hit: bool = False) -> bool: ...
    def record_tool_result(self, call_id: str, content: str) -> None: ...


class ToolTransactionPort(Protocol):
    """Compensation can remain partial/active; finish must not invent success."""

    @property
    def active(self) -> bool: ...

    def begin(self) -> None: ...

    def finish(
        self, has_write: bool, all_succeeded: bool, messages: list[dict]
    ) -> None: ...


class ToolBatchState(TypedDict):
    """Transient result projection only, not authoritative execution history."""

    manual_compact: bool
    used_todo: bool
    all_succeeded: bool
    write_total: int
    write_denied: int
    blocked: bool
    handoff_signal: HandoffSignal | None
    agent_transition: dict | None
    terminal: bool


class ToolResultTrace(Protocol):
    def __call__(
        self,
        result: ToolExecutionResult,
        output: str,
        /,
        tool_call_id: str = "",
        index: int | None = None,
    ) -> None: ...


class ToolResultConsumer(Protocol):
    """Outer compatibility override, never selected by native composition."""

    def __call__(
        self,
        dispatched: DispatchedTools,
        messages: list[dict],
        *,
        on_tool: ToolDisplay | None = None,
        processor: SessionProcessor | None = None,
    ) -> ToolBatchState: ...


class ToolBatchObserver(Protocol):
    """Committed-write enrichment and optional product effects, in order."""

    def post_write(self, dispatched: DispatchedTools, messages: list[dict]) -> None: ...
    def after_batch(
        self, messages: list[dict], state: ToolBatchState, on_text: TextDisplay | None
    ) -> None: ...
    def apply_plan_mode(self) -> None: ...
    async def capture_snapshot(self, processor: SessionProcessor) -> str | None: ...
    def record_patch(
        self, messages: list[dict], processor: SessionProcessor, snapshot: str | None
    ) -> None: ...


class ToolTaskState(Protocol):
    """Read view of existing task/verification owner; no execution-layer import."""

    mutation_generation: int
    work_phase: str

    def task_constraint_action(self, name: str, tool_input: dict, /) -> str: ...
    def closure_phase_action(self, name: str, tool_input: dict, /) -> str: ...
    def closure_phase_decision(
        self, name: str, tool_input: dict, /
    ) -> tuple[str, str]: ...
