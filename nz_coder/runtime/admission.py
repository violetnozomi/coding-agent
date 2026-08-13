"""Admission and runtime capability clamps for untrusted Agent graphs."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import re

from nz_coder.runtime.handoffs import AgentGraph, AgentSpec


TOOL_CAPABILITIES = frozenset({
    "read",
    "edit",
    "bash:test",
    "bash:read-only",
    "bash:mutating",
    "bash:network",
    "subagent",
})

_READ_TOOLS = frozenset({
    "read_file", "list_directory", "grep_search", "glob_search", "repo_map",
    "code_references", "diff_status", "verify_changed_files", "read_symbol",
    "find_symbol_callers", "python_symbol_check", "lsp", "question", "todo",
    "read_scratchpad", "update_scratchpad", "plan_enter", "write_plan",
    "plan_exit", "compact", "load_optional_tools", "project_profile",
    "analyze_impact", "review_run_evidence", "inspect_generated_project",
    "check_project_completeness", "plan_project_acceptance",
})
_EDIT_TOOLS = frozenset({
    "write_file", "write_files_batch", "edit_file", "apply_patch",
    "replace_lines", "python_structural_edit", "scaffold_project",
})
_SUBAGENT_TOOLS = frozenset({
    "task", "agent_manager", "apply_agent_changes", "background_task_start",
    "background_task_apply", "send_message",
})
_NETWORK_TOOLS = frozenset({"webfetch", "web_search"})
_NETWORK_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:curl|wget|ssh|scp|rsync)\b|"
    r"(?:^|[;&|]\s*)git\s+(?:clone|fetch|pull|push)\b|"
    r"(?:^|[;&|]\s*)(?:pip|pip3|npm|pnpm|yarn|cargo|go)\s+"
    r"(?:install|add|get)\b",
    flags=re.IGNORECASE,
)
_TEST_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:pytest|tox|nox|jest|vitest|ctest)\b|"
    r"(?:^|[;&|]\s*)python(?:3(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest)\b|"
    r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn)\s+(?:test|run\s+test)\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class SystemAgentCap:
    """System ceiling applied to one untrusted declared runtime."""

    allowed_tool_capabilities: frozenset[str]
    max_iterations: int = 80

    def __post_init__(self) -> None:
        unknown = set(self.allowed_tool_capabilities) - TOOL_CAPABILITIES
        if unknown:
            raise ValueError(
                "Unknown Agent tool capabilities: " + ", ".join(sorted(unknown))
            )
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or self.max_iterations <= 0
        ):
            raise ValueError("Agent system max_iterations must be positive")

    def effective_capabilities(self) -> frozenset[str]:
        """Apply the documented bash capability implication convention."""
        allowed = set(self.allowed_tool_capabilities)
        if "bash:mutating" in allowed:
            allowed.update({"bash:test", "bash:read-only"})
        if "bash:test" in allowed:
            allowed.add("bash:read-only")
        return frozenset(allowed)


@dataclass(frozen=True)
class AdmittedAgentHandle:
    """Opaque executable result of a successful graph admission audit."""

    graph: AgentGraph
    system_cap: SystemAgentCap
    clamp_notes: tuple[str, ...]
    invariant_bindings: tuple[str, ...] = (
        "finalOwner",
        "handoffLegality",
        "boundedRevise",
        "toolPermission",
        "evidenceTrail",
    )


@dataclass(frozen=True)
class AdmissionVerdict:
    """Typed success/failure result; only success carries an executable handle."""

    ok: bool
    handle: AdmittedAgentHandle | None = None
    reason: str = ""
    retryable: bool = False


@dataclass
class AdmissionInvariantSession:
    """Per-run observe/assert-terminal state for one admitted graph."""

    handle: AdmittedAgentHandle
    mutation_files: set[str] = field(default_factory=set)
    mutation_count: int = 0
    evidence_artifacts: list[str] = field(default_factory=list)
    handoffs: list[tuple[str, str]] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    terminal_ran: bool = False

    def record_tool_result(self, result) -> None:
        """Observe only settled, successful effects and verification evidence."""
        if (
            not bool(getattr(result, "executed", False))
            or bool(getattr(result, "dispatch_failed", False))
            or bool(getattr(result, "command_failed", False))
        ):
            return
        name = str(getattr(result, "name", "") or "")
        tool_input = getattr(result, "tool_input", {})
        if not isinstance(tool_input, dict):
            tool_input = {}
        if name in {"verify_changed_files", "python_symbol_check"}:
            self._add_evidence(name)
        elif name == "bash":
            from nz_coder.verification_planner import classify_verification_segments

            command = str(tool_input.get("command") or "").strip()
            if classify_verification_segments(command):
                self._add_evidence(command)

    def record_committed_mutation(self, result) -> None:
        """Record one write only after its surrounding transaction commits."""
        if (
            not bool(getattr(result, "is_write", False))
            or not bool(getattr(result, "executed", False))
            or bool(getattr(result, "dispatch_failed", False))
        ):
            return
        tool_input = getattr(result, "tool_input", {})
        if not isinstance(tool_input, dict) or bool(tool_input.get("dry_run")):
            return
        self.mutation_count += 1
        for path in _mutation_paths(tool_input):
            self.mutation_files.add(path)

    def record_handoff(self, source: str, target: str) -> None:
        self.handoffs.append((str(source), str(target)))

    def assert_terminal(self, current_agent: str, status: str) -> tuple[str, ...]:
        """Run terminal invariants once and return new reject violations."""
        if self.terminal_ran:
            return tuple(self.violations)
        self.terminal_ran = True
        if status not in {"completed", "completed_unverified"}:
            return ()
        graph = self.handle.graph
        current = graph.agent(current_agent)
        if current.handoffs:
            self.violations.append(
                "finalOwner: run completed while the active Agent still has "
                "declared outgoing handoffs"
            )
        if self.mutation_count > 0 and not self.evidence_artifacts:
            self.violations.append(
                "evidenceTrail: mutating run produced no successful verification artifact"
            )
        return tuple(self.violations)

    def _add_evidence(self, artifact: str) -> None:
        value = str(artifact or "").strip()[:1000]
        if value and value not in self.evidence_artifacts:
            self.evidence_artifacts.append(value)


def _mutation_paths(tool_input: dict) -> tuple[str, ...]:
    paths: list[str] = []
    direct = tool_input.get("path")
    if isinstance(direct, str) and direct.strip():
        paths.append(direct.strip())
    for key in ("files", "changes"):
        values = tool_input.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            value = item.get("path") if isinstance(item, dict) else item
            if isinstance(value, str) and value.strip() and value.strip() not in paths:
                paths.append(value.strip())
    return tuple(paths)


def resolve_tool_capability(name: str, tool_input: dict | None = None) -> str:
    """Classify concrete NZ tools; unknown tools use the strictest tier."""
    selected = str(name or "").strip()
    if selected == "bash":
        if tool_input is None:
            return "bash:mutating"
        command = str(tool_input.get("command") or "")
        if _NETWORK_COMMAND.search(command):
            return "bash:network"
        if _TEST_COMMAND.search(command):
            return "bash:test"
        from nz_coder.command_policy import is_known_read_only_command

        return (
            "bash:read-only"
            if is_known_read_only_command(command)
            else "bash:mutating"
        )
    if selected == "process":
        if tool_input is None:
            return "bash:mutating"
        operation = str(tool_input.get("operation") or "").strip().lower()
        if operation == "start":
            return resolve_tool_capability(
                "bash", {"command": str(tool_input.get("command") or "")}
            )
        if operation == "write":
            return "bash:mutating"
        return "read"
    if selected in _READ_TOOLS:
        return "read"
    if selected in _EDIT_TOOLS:
        return "edit"
    if selected in _SUBAGENT_TOOLS or selected == "emit_handoff":
        return "subagent"
    if selected in _NETWORK_TOOLS or selected.startswith("mcp_"):
        return "bash:network"
    return "subagent"


def admit_agent_graph(graph: AgentGraph, system_cap: SystemAgentCap) -> AdmissionVerdict:
    """Audit and clamp an untrusted graph without mutating its declaration."""
    allowed_caps = system_cap.effective_capabilities()
    if any(graph.agent(name).handoffs for name in graph.names()) and "subagent" not in allowed_caps:
        return AdmissionVerdict(
            ok=False,
            reason="handoffLegality: declared handoffs require the subagent capability",
            retryable=True,
        )

    admitted_agents: list[AgentSpec] = []
    notes: list[str] = []
    for name in graph.names():
        agent = graph.agent(name)
        if agent.allowed_tools is None:
            return AdmissionVerdict(
                ok=False,
                reason=(
                    f"toolPermission: untrusted Agent '{name}' must declare an "
                    "explicit tool allowlist"
                ),
                retryable=True,
            )
        admitted_tools = []
        removed = []
        for tool_name in agent.allowed_tools:
            capability = resolve_tool_capability(tool_name)
            if capability in allowed_caps:
                admitted_tools.append(tool_name)
            else:
                removed.append(f"{tool_name}={capability}")
        if removed:
            notes.append(
                f"[toolPermission] {name}: removed " + ", ".join(removed)
            )
        admitted_agents.append(replace(agent, allowed_tools=tuple(admitted_tools)))

    admitted_graph = AgentGraph(admitted_agents, start=graph.start)
    return AdmissionVerdict(
        ok=True,
        handle=AdmittedAgentHandle(
            graph=admitted_graph,
            system_cap=system_cap,
            clamp_notes=tuple(notes),
        ),
    )
