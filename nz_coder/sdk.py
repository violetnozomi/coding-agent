"""Public Python SDK backed by NZ-Coder's production AgentRunner chain."""
from __future__ import annotations

import copy
import warnings
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from nz_coder.protocol.message_schema import ASSISTANT_USAGE_KEY
from nz_coder.runtime.core.request import AgentDefinition, AgentHandoff, RunOptions, RunRequest
from nz_coder.runtime.core.profiles import READ_CHILD_PROFILE, RunProfile
from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage
from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec, HandoffSpec
from nz_coder.runtime.execution.runner import AgentRunner
from nz_coder.runtime.execution.native_sdk import build_native_sdk_runner
from nz_coder.runtime.session import Session
from nz_coder.state.workdir import scoped_workdir


AgentFactory = Callable[[RunRequest], object]
Agent = AgentDefinition


@runtime_checkable
class Tool(Protocol):
    """Public callable contract accepted by the registry's tool handlers."""

    def __call__(self, **kwargs) -> str: ...


@dataclass(frozen=True)
class AgentClient:
    """Reusable host-neutral client entering the same chain as CLI and HTTP."""

    agent_factory: AgentFactory | None = None
    runner: object | None = None

    def __post_init__(self) -> None:
        if self.agent_factory is not None and not callable(self.agent_factory):
            raise TypeError("AgentClient agent_factory must be callable")
        if self.runner is not None and not callable(getattr(self.runner, "run_result", None)):
            raise TypeError("AgentClient runner must expose async run_result")
        if self.runner is not None and self.agent_factory is not None:
            raise ValueError("AgentClient accepts runner or agent_factory, not both")

    async def run(
        self,
        request: RunRequest,
        *,
        cancel_event=None,
        on_tool=None,
        on_text=None,
        on_token=None,
        on_final_text=None,
        on_event=None,
        permission_asker=None,
        question_asker=None,
        workflow_approval_asker=None,
        event_bus=None,
        config_snapshot=None,
    ) -> RunResult:
        """Execute one immutable request through the production Agent runtime."""
        if not isinstance(request, RunRequest):
            raise TypeError("AgentClient.run requires RunRequest")
        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_result(request)
        if on_token is not None:
            warnings.warn(
                "on_token is deprecated; it emits committed final text, not raw "
                "Provider tokens. Use on_final_text or structured on_event.",
                DeprecationWarning,
                stacklevel=2,
            )
        if on_token is not None and on_final_text is not None:
            raise ValueError("Use either on_token or on_final_text, not both")
        committed_text_callback = on_final_text or on_token

        native_runner = self.runner
        if native_runner is None and self.agent_factory is None:
            native_runner = build_native_sdk_runner()
        if native_runner is not None:
            return await native_runner.run_result(request, RunOptions(
                stream=request.stream,
                on_tool=on_tool,
                on_text=on_text,
                on_token=committed_text_callback,
                on_event=on_event,
                cancellation=cancel_event,
                permission_asker=permission_asker,
                question_asker=question_asker,
                workflow_approval_asker=workflow_approval_asker,
                event_bus=event_bus,
                config_snapshot=config_snapshot,
            ))

        messages = copy.deepcopy(list(request.messages))
        factory = self.agent_factory
        if factory is None:
            raise AssertionError("default SDK execution must select the native runner")
        with scoped_workdir(request.workspace):
            agent = factory(request)
            try:
                status = await agent.run(
                    messages,
                    on_tool=on_tool,
                    on_text=on_text,
                    on_token=committed_text_callback,
                    stream=request.stream,
                )
            finally:
                close = getattr(agent, "close", None)
                if callable(close):
                    close()
        return _normalize_result(request, agent, messages, status)

    async def run_child(
        self,
        *,
        parent: RunRequest,
        agent: AgentDefinition,
        prompt: str,
        session_id: str,
        profile: RunProfile = READ_CHILD_PROFILE,
        stream: bool = False,
        cancel_event=None,
    ) -> RunResult:
        """Run or resume one Session-owned child through the same native Runner."""
        if not isinstance(parent, RunRequest):
            raise TypeError("AgentClient.run_child parent must be RunRequest")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("AgentClient.run_child prompt must be non-empty")
        child = RunRequest(
            agent=agent,
            profile=profile,
            messages=({"role": "user", "content": prompt},),
            workspace=parent.workspace,
            session_id=session_id,
            stream=stream,
            parent_interaction_run_id=parent.interaction_run_id,
            parent_run_id=parent.session_id,
            parent_agent_id=parent.agent.name,
            provider=agent.provider or parent.provider,
            model=agent.model or parent.model,
            reasoning_effort=agent.reasoning_effort or parent.reasoning_effort,
            metadata={
                **copy.deepcopy(parent.metadata),
                "parent_session_id": parent.session_id,
            },
        )
        return await self.run(child, cancel_event=cancel_event)


async def run_agent(
    request: RunRequest,
    *,
    agent_factory: AgentFactory | None = None,
    cancel_event=None,
    on_tool=None,
    on_text=None,
    on_token=None,
    on_final_text=None,
    on_event=None,
) -> RunResult:
    """One-shot SDK entry using the exact production Agent execution chain."""
    return await AgentClient(agent_factory=agent_factory).run(
        request,
        cancel_event=cancel_event,
        on_tool=on_tool,
        on_text=on_text,
        on_token=on_token,
        on_final_text=on_final_text,
        on_event=on_event,
    )


def _agent_graph_from_definition(
    root: AgentDefinition,
    *,
    root_provider: str | None = None,
    root_model: str | None = None,
    root_effort: str | None = None,
) -> AgentGraph:
    """Resolve nested SDK declarations into the executable AgentGraph contract."""
    if not isinstance(root, AgentDefinition):
        raise TypeError("SDK graph root must be an AgentDefinition")
    definitions: dict[str, AgentDefinition] = {}
    visiting: set[int] = set()

    def collect(definition: AgentDefinition) -> None:
        identity = id(definition)
        if identity in visiting:
            raise ValueError(f"SDK Agent handoff graph contains a cycle at '{definition.name}'")
        existing = definitions.get(definition.name)
        if existing is not None:
            if existing != definition:
                raise ValueError(
                    f"SDK Agent graph declares conflicting definitions for '{definition.name}'"
                )
            return
        definitions[definition.name] = definition
        visiting.add(identity)
        for edge in definition.handoffs:
            if not isinstance(edge, AgentHandoff):
                raise TypeError("AgentDefinition handoffs must contain AgentHandoff values")
            collect(edge.target)
        visiting.remove(identity)

    collect(root)
    specs = []
    for definition in definitions.values():
        is_root = definition is root
        specs.append(AgentSpec(
            name=definition.name,
            instructions=definition.instructions,
            description=definition.description,
            allowed_tools=definition.allowed_tools,
            model=root_model if is_root and root_model is not None else definition.model,
            provider=(
                root_provider if is_root and root_provider is not None
                else definition.provider
            ),
            effort=(
                root_effort if is_root and root_effort is not None
                else definition.reasoning_effort
            ),
            guardrails=definition.guardrails,
            handoffs=tuple(HandoffSpec(
                target=edge.target.name,
                kind=edge.kind,
                description=edge.description,
                input_filter=edge.input_filter,
            ) for edge in definition.handoffs),
            output_schema=definition.output_schema,
        ))
    return AgentGraph(specs, start=root.name)


def _tool_allowlist(request: RunRequest) -> tuple[str, ...] | None:
    """Intersect request-selected tools with the SDK Agent declaration."""
    declared = request.agent.allowed_tools
    selected = request.tool_names
    if declared is None and not selected:
        return None
    if declared is None:
        return tuple(selected)
    if not selected:
        return tuple(declared)
    admitted = set(declared)
    return tuple(name for name in selected if name in admitted)


def _normalize_result(
    request: RunRequest,
    agent: object,
    messages: list[dict],
    raw_status: object,
) -> RunResult:
    status_payload = raw_status if isinstance(raw_status, dict) else {}
    raw_name = str(status_payload.get("status") or "error")
    status = {
        "completed": RunStatus.COMPLETED,
        "completed_unverified": RunStatus.COMPLETED,
        "interrupted": RunStatus.INTERRUPTED,
        "cancelled": RunStatus.CANCELLED,
        "max_turns": RunStatus.MAX_TURNS,
    }.get(raw_name, RunStatus.ERROR)
    runtime = status_payload.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    return RunResult(
        status=status,
        final_text=_last_assistant_text(messages),
        messages=messages,
        usage=_aggregate_usage(messages),
        session_id=request.session_id,
        active_agent=str(
            runtime.get("active_agent")
            or getattr(agent, "current_agent_name", "")
            or request.agent.name
        ),
        error=str(status_payload.get("last_error") or "") if status is RunStatus.ERROR else "",
        metadata={
            **copy.deepcopy(request.metadata),
            "trace_id": str(getattr(agent, "trace_id", "")),
            "runtime": copy.deepcopy(runtime),
            "raw_status": raw_name,
        },
    )


def _last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            if (
                message.get("_nz_internal") is True
                or message.get("_nz_visible") is False
            ):
                continue
            content = message.get("content", "")
            if isinstance(content, str) and content:
                return content
    return ""


def _aggregate_usage(messages: list[dict]) -> TokenUsage:
    fields = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0}
    for message in messages:
        usage = message.get(ASSISTANT_USAGE_KEY) if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            continue
        for name in fields:
            value = usage.get(name, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                fields[name] += max(0, int(value))
    return TokenUsage(
        input_tokens=fields["input"],
        output_tokens=fields["output"],
        reasoning_tokens=fields["reasoning"],
        cached_read_tokens=fields["cache_read"],
        cached_write_tokens=fields["cache_write"],
    )


def _cancelled_result(request: RunRequest) -> RunResult:
    return RunResult(
        status=RunStatus.CANCELLED,
        final_text="",
        messages=request.messages,
        usage=TokenUsage(),
        session_id=request.session_id,
        active_agent=request.agent.name,
        error="cancelled",
    )


__all__ = [
    "Agent",
    "AgentClient",
    "AgentRunner",
    "AgentDefinition",
    "AgentHandoff",
    "RunRequest",
    "RunResult",
    "Session",
    "Tool",
    "run_agent",
]
