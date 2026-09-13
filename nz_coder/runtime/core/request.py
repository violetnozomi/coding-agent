"""Immutable Agent declaration and input contract for one Runner frame."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from nz_coder.runtime.core.profiles import RunProfile


@dataclass(frozen=True)
class AgentHandoff:
    """Runnable SDK handoff edge carrying its complete target declaration."""

    target: object
    kind: str = "continuation"
    description: str = ""
    input_filter: object | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.target, AgentDefinition):
            raise TypeError("AgentHandoff target must be an AgentDefinition")
        if self.kind not in {"continuation", "as-tool"}:
            raise ValueError("AgentHandoff kind must be continuation or as-tool")
        if self.input_filter is not None and not callable(self.input_filter):
            raise TypeError("AgentHandoff input_filter must be callable")


@dataclass(frozen=True)
class AgentDefinition:
    """Declarative Agent identity and policy overrides without live services."""

    name: str
    instructions: str
    description: str = ""
    allowed_tools: tuple[str, ...] | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    guardrails: tuple[object, ...] = field(default_factory=tuple)
    handoffs: tuple[AgentHandoff, ...] = field(default_factory=tuple)
    output_schema: dict | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Agent definition name must be non-empty")
        if not isinstance(self.instructions, str) or not self.instructions.strip():
            raise ValueError("Agent definition instructions must be non-empty")
        if self.allowed_tools is not None:
            names = tuple(str(name).strip() for name in self.allowed_tools)
            if any(not name for name in names):
                raise ValueError("Agent allowed_tools must contain non-empty names")
            object.__setattr__(self, "allowed_tools", names)
        object.__setattr__(self, "guardrails", tuple(self.guardrails))
        object.__setattr__(self, "handoffs", tuple(self.handoffs))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", copy.deepcopy(self.output_schema))


@dataclass(frozen=True)
class RunOptions:
    """Caller controls; ``on_token`` receives committed final text only.

    Reversible incremental consumers must use ``on_event`` plus RunViewReducer.
    Raw Provider deltas never cross this callback before output policy approval.
    """

    stream: bool | None = None
    on_tool: Callable[..., object] | None = None
    on_text: Callable[..., object] | None = None
    on_token: Callable[..., object] | None = None
    on_event: Callable[..., object] | None = None
    cancellation: object | None = None
    permission_asker: Callable[..., object] | None = None
    question_asker: Callable[..., object] | None = None
    workflow_approval_asker: Callable[..., object] | None = None
    event_bus: object | None = None
    config_snapshot: object | None = None

    def __post_init__(self) -> None:
        if self.stream is not None and not isinstance(self.stream, bool):
            raise TypeError("RunOptions stream must be bool or None")
        for name in (
            "on_tool", "on_text", "on_token", "on_event",
            "permission_asker", "question_asker", "workflow_approval_asker",
        ):
            value = getattr(self, name)
            if value is not None and not callable(value):
                raise TypeError(f"RunOptions {name} must be callable or None")


@dataclass(frozen=True)
class RunRequest:
    """Complete immutable input supplied to one future AgentRunner invocation."""

    agent: AgentDefinition
    profile: RunProfile
    messages: tuple[dict, ...] | list[dict]
    workspace: Path | str
    session_id: str
    tool_names: tuple[str, ...] | list[str] = field(default_factory=tuple)
    stream: bool = True
    interaction_run_id: str | None = None
    parent_interaction_run_id: str | None = None
    parent_run_id: str | None = None
    parent_agent_id: str | None = None
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.agent, AgentDefinition):
            raise TypeError("RunRequest agent must be an AgentDefinition")
        if not isinstance(self.profile, RunProfile):
            raise TypeError("RunRequest profile must be a RunProfile")
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("RunRequest session_id must be non-empty")

        messages = copy.deepcopy(tuple(self.messages))
        for index, message in enumerate(messages):
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("role"), str)
                or "content" not in message
            ):
                raise ValueError(f"Invalid message at index {index}")

        names = tuple(str(name).strip() for name in self.tool_names)
        if any(not name for name in names):
            raise ValueError("RunRequest tool_names must contain non-empty names")

        object.__setattr__(self, "messages", messages)
        object.__setattr__(self, "tool_names", names)
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())
        object.__setattr__(self, "metadata", copy.deepcopy(self.metadata))
        for name in ("interaction_run_id", "parent_interaction_run_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise ValueError(f"RunRequest {name} must be non-empty or None")
