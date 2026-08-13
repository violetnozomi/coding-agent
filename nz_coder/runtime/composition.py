"""Single composition owner for production and declared Agent runtimes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from nz_coder.runtime.admission import AdmittedAgentHandle
from nz_coder.runtime.handoffs import AgentGraph
from nz_coder.runtime.services import build_runtime_services


AgentConstructor = Callable[..., Any]


@dataclass(frozen=True)
class AgentRuntimeAssembly:
    """Validated boundary between the native coding loop and Agent graphs.

    The production coding profile deliberately keeps NZ-Coder's mature
    planning/reflection/verification loop as its only control plane. Declared
    graphs are explicit: their Agent instructions, handoffs, models, and
    guardrails become authoritative inside the same Session/tool runtime.
    """

    profile: str
    system_prompt: str
    agent_graph: AgentGraph | None = None
    admission_handle: AdmittedAgentHandle | None = None

    def __post_init__(self) -> None:
        if self.profile not in {"coding", "declared"}:
            raise ValueError(f"Unknown Agent runtime profile: {self.profile}")
        if not isinstance(self.system_prompt, str) or not self.system_prompt.strip():
            raise ValueError("Agent runtime requires a non-empty system prompt")
        if self.profile == "coding" and self.agent_graph is not None:
            raise ValueError("The coding profile cannot also install a declared Agent graph")
        if self.profile == "declared" and self.agent_graph is None:
            raise ValueError("The declared profile requires an Agent graph")
        if self.admission_handle is not None:
            if self.profile != "declared":
                raise ValueError("Only a declared runtime can carry admission")
            if self.agent_graph is not self.admission_handle.graph:
                raise ValueError("Admission handle does not own this Agent graph")

    @property
    def control_plane(self) -> str:
        return "native-coding-loop" if self.profile == "coding" else "declared-agent-graph"

    def build(
        self,
        *,
        agent_cls: AgentConstructor | None = None,
        **kwargs,
    ):
        """Construct one AgentLoop without letting entry points re-compose it."""
        owned = {"agent_graph", "admission_handle"}.intersection(kwargs)
        if owned:
            raise TypeError(
                ", ".join(sorted(owned)) + " is owned by AgentRuntimeAssembly"
            )
        if agent_cls is None:
            from nz_coder.runtime.loop import ProductRunEnvironment

            agent_cls = ProductRunEnvironment
        runtime_services = kwargs.pop("runtime_services", None)
        if runtime_services is None:
            runtime_services = build_runtime_services()
        agent = agent_cls(
            self.system_prompt,
            agent_graph=self.agent_graph,
            admission_handle=self.admission_handle,
            runtime_services=runtime_services,
            **kwargs,
        )
        agent.runtime_profile = self.profile
        agent.runtime_control_plane = self.control_plane
        return agent


def coding_runtime(system_prompt: str) -> AgentRuntimeAssembly:
    """Assemble the default terminal/HTTP/evaluation coding runtime."""
    return AgentRuntimeAssembly("coding", system_prompt)


def declared_runtime(agent_graph: AgentGraph) -> AgentRuntimeAssembly:
    """Assemble an explicit multi-role graph on the common NZ runtime."""
    start = agent_graph.agent(agent_graph.start)
    return AgentRuntimeAssembly("declared", start.instructions, agent_graph)


def admitted_runtime(handle: AdmittedAgentHandle) -> AgentRuntimeAssembly:
    """Assemble an untrusted graph only from an opaque successful verdict."""
    if not isinstance(handle, AdmittedAgentHandle):
        raise TypeError("admitted_runtime requires an AdmittedAgentHandle")
    start = handle.graph.agent(handle.graph.start)
    return AgentRuntimeAssembly(
        "declared",
        start.instructions,
        handle.graph,
        admission_handle=handle,
    )


def build_coding_agent(system_prompt: str, **kwargs):
    """Explicit compatibility factory retaining the historical AgentLoop API."""
    from nz_coder.runtime.loop import AgentLoop

    return coding_runtime(system_prompt).build(agent_cls=AgentLoop, **kwargs)


def build_product_environment(system_prompt: str, **kwargs):
    """Construct the canonical complete environment for product surfaces."""
    return coding_runtime(system_prompt).build(**kwargs)


def build_declared_agent(agent_graph: AgentGraph, **kwargs):
    """Canonical factory for SDK/tests that explicitly choose a role graph."""
    return declared_runtime(agent_graph).build(**kwargs)


def build_admitted_agent(handle: AdmittedAgentHandle, **kwargs):
    """Canonical factory for externally supplied, successfully admitted graphs."""
    return admitted_runtime(handle).build(**kwargs)
