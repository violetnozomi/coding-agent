"""Dependency-inversion ports consumed by the production shared AgentRunner."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from nz_coder.runtime.core.events import RuntimeEventSink

if TYPE_CHECKING:
    from nz_coder.runtime.core.context import ContextExecutionContext
    from nz_coder.runtime.core.lifecycle_context import LifecycleExecutionContext
    from nz_coder.runtime.core.model_context import ModelExecutionContext
    from nz_coder.runtime.core.memory_context import MemoryExecutionContext
    from nz_coder.runtime.core.request import RunRequest
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.core.run_context import RunContext
    from nz_coder.runtime.core.tool_context import ToolExecutionContext
    from nz_coder.runtime.core.verification_context import VerificationExecutionContext
@runtime_checkable
class ModelGateway(Protocol):
    """Complete one coding turn through the provider-neutral gateway."""

    async def complete_turn(
        self, context: ModelExecutionContext, messages: list, **kwargs,
    ):
        """Settle one model turn as the production LLM result envelope."""
        ...


@runtime_checkable
class ToolRuntime(Protocol):
    """Complete batch dispatch boundary, including lifecycle policy."""

    async def execute_batch_async(
        self,
        context: ToolExecutionContext,
        calls: list,
        messages: list,
        **kwargs,
    ):
        """Settle every admitted tool call and return the batch transition."""
        ...


@runtime_checkable
class ContextManager(Protocol):
    """Context budgeting and compaction boundary invoked before Provider calls."""

    async def prepare_async(
        self,
        context: ContextExecutionContext,
        messages: list,
        **kwargs,
    ) -> bool:
        """Prepare the run-owned transcript required for the next call."""
        ...


@runtime_checkable
class SessionRepository(Protocol):
    """Storage-neutral session load/save boundary."""

    def checkpoint(self, host, messages: list, run_status: str) -> None:
        """Persist one settled state snapshot."""
        ...


@runtime_checkable
class SessionRuntimePort(Protocol):
    """Open and persist the Session-owned state for one Runner frame."""

    async def open(self, request: RunRequest) -> RunContext:
        """Load or create the Session and return its isolated RunContext."""
        ...

    async def checkpoint(self, context: RunContext, status: str) -> None:
        """Persist one settled run boundary without finalizing the context."""
        ...

    async def finalize(self, context: RunContext, status: RunStatus) -> None:
        """Persist the exactly-once terminal run state."""
        ...


@runtime_checkable
class MemoryService(Protocol):
    """Run-scoped memory recall and terminal learning boundary."""

    def prompt_block(self, context: MemoryExecutionContext, query: str) -> str:
        """Return prompt-safe relevant memory for the current run."""
        ...

    async def finalize(
        self, context: MemoryExecutionContext, messages: list, status: str,
    ) -> None:
        """Persist eligible terminal observations without changing the result."""
        ...


@runtime_checkable
class CompletionVerifier(Protocol):
    """Bounded completion check used after a natural model stop."""

    async def verify(
        self,
        context: VerificationExecutionContext,
        messages: list,
        status: str,
        content: str,
    ) -> str:
        """Return a host-neutral accept, revise, or abort decision."""
        ...


@runtime_checkable
class RuntimeHost(Protocol):
    """Run-scoped resource binding and cleanup boundary."""

    async def run(self, agent, messages: list, *, execute, **kwargs):
        """Invoke execute while all production resources are bound."""
        ...


@runtime_checkable
class RunLifecycle(Protocol):
    """Reset and restore one run before the first model turn."""

    def initialize(
        self, context: LifecycleExecutionContext, messages: list, stream: bool,
    ) -> tuple[int, int]:
        """Return the admitted maximum and resume turn."""
        ...

    async def finalize(self, host, messages: list, status: str, **kwargs):
        """Settle one terminal run and return its public result."""
        ...

    def finalize_sync(self, host, messages: list, status: str, **kwargs):
        """Compatibility terminal settlement for synchronous callers."""
        ...


@runtime_checkable
class GuardrailRuntime(Protocol):
    """Policy boundary for declared input, output, and tool guardrails."""

    async def run_input(self, host, messages: list) -> None:
        ...

    def has(self, host, kind: str) -> bool:
        ...

    async def run_output(self, host, content: str, messages: list) -> str:
        ...

    async def before_tool(self, host, tool_call: dict, messages: list):
        ...

    async def after_tool(self, host, tool_call: dict, result, messages: list):
        ...


@runtime_checkable
class InputPreflight(Protocol):
    """Provider-neutral preprocessing boundary for input and tool media."""

    async def prepare_user_images(self, host, messages: list, owner: dict) -> str:
        ...

    async def prepare_user_documents(self, host, messages: list, owner: dict) -> str:
        ...

    async def describe_read_results(self, host, dispatched: list, messages: list) -> bool:
        ...


@runtime_checkable
class AgentTransitionRuntime(Protocol):
    """Declared handoff, structured-output, and terminal policy boundary."""

    def signal_from_metadata(self, host, metadata: dict | None):
        ...

    def apply(self, host, signal, messages: list, processor):
        ...

    def resolve_structured_output(self, host, content: str, messages: list) -> bool:
        ...

    def return_from_as_tool(self, host, messages: list, summary: str = "") -> dict:
        ...

    async def terminal_content(self, host, fallback: str, messages: list) -> str:
        ...



@dataclass(frozen=True)
class RuntimeServices:
    """Validated live service graph created by the composition root."""

    model: ModelGateway
    tools: ToolRuntime
    context: ContextManager
    session_runtime: SessionRuntimePort
    events: RuntimeEventSink
    host: RuntimeHost
    memory: MemoryService
    verifier: CompletionVerifier
    lifecycle: RunLifecycle
    guardrails: GuardrailRuntime
    inputs: InputPreflight
    transitions: AgentTransitionRuntime
    middleware: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        required = (
            ("model", self.model, ModelGateway),
            ("tools", self.tools, ToolRuntime),
            ("context", self.context, ContextManager),
            ("session_runtime", self.session_runtime, SessionRuntimePort),
            ("events", self.events, RuntimeEventSink),
            ("host", self.host, RuntimeHost),
            ("memory", self.memory, MemoryService),
            ("verifier", self.verifier, CompletionVerifier),
            ("lifecycle", self.lifecycle, RunLifecycle),
            ("guardrails", self.guardrails, GuardrailRuntime),
            ("inputs", self.inputs, InputPreflight),
            ("transitions", self.transitions, AgentTransitionRuntime),
        )
        for name, value, contract in required:
            if value is None or not isinstance(value, contract):
                raise TypeError(f"RuntimeServices {name} must implement {contract.__name__}")
        object.__setattr__(self, "middleware", tuple(self.middleware))
