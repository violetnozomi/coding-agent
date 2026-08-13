"""Declarative in-process Agent handoff graph and transition tool."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from nz_coder.runtime.structured_output import assert_supported_output_schema
from nz_coder.tools import ToolOutput


HistoryFilter = Callable[[tuple[dict, ...]], Iterable[dict]]


@dataclass(frozen=True)
class AgentReasoningProfile:
    """Default/ceiling reasoning policy with optional revise escalation."""

    default: str
    max: str | None = None
    escalate_on_revise: bool = False


@dataclass(frozen=True)
class HandoffSpec:
    """One declared continuation edge between two Agent roles."""

    target: str
    kind: str = "continuation"
    description: str = ""
    input_filter: HistoryFilter | None = None


@dataclass(frozen=True)
class AgentSpec:
    """Agent-as-data: identity, instructions, and legal handoff edges."""

    name: str
    instructions: str
    description: str = ""
    allowed_tools: tuple[str, ...] | None = None
    model: str | None = None
    provider: str | None = None
    effort: str | None = None
    reasoning: AgentReasoningProfile | None = None
    guardrails: tuple[object, ...] = field(default_factory=tuple)
    handoffs: tuple[HandoffSpec, ...] = field(default_factory=tuple)
    output_schema: dict | None = None


@dataclass(frozen=True)
class HandoffSignal:
    """Normalized tool signal consumed after the complete tool batch settles."""

    source: str
    target: str = ""
    terminal: bool = False
    summary: str = ""


class AgentGraph:
    """Validated acyclic continuation graph used by one AgentLoop run."""

    def __init__(self, agents: Iterable[AgentSpec], start: str):
        items = tuple(agents)
        self._agents = {agent.name: agent for agent in items}
        self.start = str(start or "").strip()
        if not items or len(self._agents) != len(items):
            raise ValueError("Agent graph requires unique non-empty agents")
        if any(not agent.name.strip() or not agent.instructions.strip() for agent in items):
            raise ValueError("Agent names and instructions must be non-empty")
        for agent in items:
            for field_name in ("model", "provider", "effort"):
                value = getattr(agent, field_name)
                if value is not None and (not isinstance(value, str) or not value.strip()):
                    raise ValueError(
                        f"Agent '{agent.name}' has an invalid {field_name}"
                    )
            if agent.reasoning is not None and (
                not isinstance(agent.reasoning.default, str)
                or not agent.reasoning.default.strip()
                or (
                    agent.reasoning.max is not None
                    and (
                        not isinstance(agent.reasoning.max, str)
                        or not agent.reasoning.max.strip()
                    )
                )
            ):
                raise ValueError(f"Agent '{agent.name}' has an invalid reasoning profile")
            if agent.output_schema is not None:
                assert_supported_output_schema(agent.output_schema)
                if agent.handoffs:
                    raise ValueError(
                        f"Agent '{agent.name}' with output_schema must be a terminal owner"
                    )
            if any(
                getattr(guardrail, "kind", None) not in {"input", "output", "tool"}
                or not isinstance(getattr(guardrail, "name", None), str)
                or not getattr(guardrail, "name", "").strip()
                for guardrail in agent.guardrails
            ):
                raise ValueError(f"Agent '{agent.name}' has an invalid guardrail")
            if agent.allowed_tools is not None and any(
                not isinstance(name, str) or not name.strip()
                for name in agent.allowed_tools
            ):
                raise ValueError(f"Agent '{agent.name}' has an invalid tool allowlist")
        if self.start not in self._agents:
            raise ValueError(f"Unknown start agent: {self.start}")
        for agent in items:
            targets: set[str] = set()
            for handoff in agent.handoffs:
                if handoff.kind not in {"continuation", "as-tool"}:
                    raise ValueError("Handoff kind must be continuation or as-tool")
                if handoff.target not in self._agents:
                    raise ValueError(
                        f"Agent '{agent.name}' declares unknown handoff target '{handoff.target}'"
                    )
                if handoff.target in targets:
                    raise ValueError(
                        f"Agent '{agent.name}' declares duplicate target '{handoff.target}'"
                    )
                targets.add(handoff.target)
        self._reject_cycles()

    def agent(self, name: str) -> AgentSpec:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise ValueError(f"Unknown agent: {name}") from exc

    def handoff(self, source: str, target: str) -> HandoffSpec | None:
        return next(
            (item for item in self.agent(source).handoffs if item.target == target),
            None,
        )

    def names(self) -> tuple[str, ...]:
        return tuple(self._agents)

    def tool_definition(self, current_agent: Callable[[], str]) -> dict:
        """Build one execution-local emit tool without polluting global tools."""

        def emit_handoff(target: str = "", summary: str = "", terminal: bool = False) -> str:
            source = current_agent()
            outgoing = self.agent(source).handoffs
            selected = str(target or "").strip()
            note = str(summary or "").strip()[:4000]
            if terminal:
                if selected:
                    return "Error: terminal handoff signal cannot also specify target"
                if outgoing:
                    return "Error: only an Agent with no declared handoffs may emit terminal=true"
                return ToolOutput(
                    note or f"{source} completed its terminal role.",
                    title="Agent terminal signal",
                    metadata={
                        "isTerminal": True,
                        "handoffSource": source,
                        "terminalSummary": note,
                    },
                )
            edge = self.handoff(source, selected)
            if edge is None:
                allowed = ", ".join(item.target for item in outgoing) or "(none)"
                return f"Error: undeclared handoff '{source}' -> '{selected}'; allowed: {allowed}"
            return ToolOutput(
                note or f"Handing off from {source} to {selected}.",
                title=f"Handoff: {source} -> {selected}",
                metadata={
                    "handoffTarget": selected,
                    "handoffSource": source,
                    "handoffKind": edge.kind,
                    "handoffDescription": edge.description,
                    "handoffInput": note,
                },
            )

        return {
            "name": "emit_handoff",
            "description": (
                "Transfer this run to a declared Agent role after the current tool batch "
                "settles, or emit terminal=true when this terminal role is complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "enum": list(self.names())},
                    "summary": {"type": "string"},
                    "terminal": {"type": "boolean"},
                },
            },
            "handler": emit_handoff,
            "execution": "serial",
            "transactional": False,
        }

    def _reject_cycles(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visiting:
                raise ValueError(f"Agent handoff graph contains a cycle at '{name}'")
            if name in visited:
                return
            visiting.add(name)
            for edge in self.agent(name).handoffs:
                visit(edge.target)
            visiting.remove(name)
            visited.add(name)

        for name in self._agents:
            visit(name)
