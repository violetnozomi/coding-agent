"""Context-budget-aware progressive exposure with run-owned unlock state."""
from __future__ import annotations

import copy
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock

from nz_coder.tool_platform.catalog import ToolCatalog, estimate_schema_tokens
from nz_coder.runtime.conversation.continuation_context import continuation_task_text
from nz_coder.tools import get_tool_policy_snapshot


RESIDENT_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "apply_patch", "bash",
    "replace_lines", "write_files_batch", "grep_search", "glob_search",
    "list_directory", "process", "task", "todo", "question", "load_skill",
    "tool_search", "compact", "diff_status", "verify_changed_files",
    "verify_project_build", "review_run_evidence", "read_scratchpad",
    "update_scratchpad",
})
DEFERRED_PREFIXES = (
    "mcp_", "lsp_", "workflow_", "memory_", "project_", "semantic_",
)
DEFERRED_NAMES = frozenset({
    "lsp", "repo_context", "repo_map", "smart_search", "read_symbol",
    "find_symbol_callers", "create_project", "verify_changed_files",
})
TOOL_FAMILY_NAMES = {
    "agent_manager": "orchestration",
    "apply_agent_changes": "orchestration",
    "web_search": "web",
    "webfetch": "web",
    "lsp": "lsp",
    "repo_context": "repo_intelligence",
    "repo_map": "repo_intelligence",
    "smart_search": "repo_intelligence",
    "read_symbol": "repo_intelligence",
    "find_symbol_callers": "repo_intelligence",
    "code_references": "repo_intelligence",
    "analyze_impact": "repo_intelligence",
    "project_profile": "repo_intelligence",
    "semantic_search": "repo_intelligence",
    "save_memory": "memory",
    "recall_memory": "memory",
    "list_memories": "memory",
    "delete_memory": "memory",
    "load_optional_tools": "optional",
    "plan_enter": "planning",
    "plan_exit": "planning",
    "write_plan": "planning",
    "plan_verification": "planning",
    "analyze_project_requirements": "project_creation",
    "create_project_blueprint": "project_creation",
    "scaffold_project": "project_creation",
    "inspect_generated_project": "project_creation",
    "check_project_completeness": "project_creation",
    "plan_project_acceptance": "project_creation",
    "create_project": "project_creation",
}
TOOL_FAMILY_PREFIXES = (
    ("workflow_", "workflow"),
    ("mcp_", "mcp"),
    ("lsp_", "lsp"),
    ("memory_", "memory"),
    ("project_", "project_creation"),
    ("semantic_", "repo_intelligence"),
)
TASK_FAMILY_KEYWORDS = {
    "workflow": (
        "workflow", "runbook", "orchestrat", "工作流", "编排",
    ),
    "orchestration": (
        "agent manager", "agent_manager", "subagent", "child agent",
        "parallel agent", "子 agent", "子agent", "并行 agent", "并行智能体",
    ),
    "memory": (
        "memory", "remember", "recall", "记忆", "记住", "回忆",
    ),
    "project_creation": (
        "new project", "create project", "scaffold", "greenfield",
        "新项目", "创建项目", "从零", "项目脚手架",
    ),
    "repo_intelligence": (
        "architecture", "call graph", "control flow", "caller", "reference", "impact",
        "repo map", "refactor", "架构", "调用链", "调用者", "引用",
        "影响分析", "控制流", "代码流程", "重构", "仓库地图",
    ),
    "web": (
        "http://", "https://", "web search", "internet", "online",
        "网页", "联网", "上网", "最新资料",
    ),
    "mcp": ("mcp",),
    "lsp": ("lsp", "language server", "语言服务器", "跳转定义"),
    "planning": ("plan mode", "write a plan", "计划模式", "写计划", "方案"),
    "optional": ("optional tool", "optional pack", "可选工具"),
}
BROAD_TOOL_KEYWORDS = (
    "all tools", "any tools", "whatever tools", "所有工具", "任意工具",
)
DEFERRED_TOOL_PURPOSES = {
    "find_symbol_callers": "Python callers/references.",
    "project_profile": "Languages, packages, roots, commands.",
    "read_symbol": "Read/list Python symbols.",
    "repo_context": "Repository structure, calls, dependencies, impact.",
    "repo_map": "Cross-file source map.",
    "semantic_search": "Business-intent semantic code search.",
    "verify_changed_files": "Low-noise changed-file checks.",
    "workflow_builtin": "Built-in workflows.",
    "workflow_generate": "Generate workflow capsule.",
    "workflow_generation": "Parse/repair workflow envelope.",
    "workflow_host": "Workflow host contracts.",
    "workflow_library": "Saved workflow capsules.",
    "workflow_library_mutate": "Mutate saved workflow capsule.",
    "workflow_review_packet": "Workflow review packet.",
    "workflow_run": "Run multi-agent workflow.",
    "workflow_run_archive": "Archive workflow run.",
    "workflow_run_rename": "Rename workflow run.",
    "workflow_runs": "Workflow run history/artifacts.",
    "workflow_save": "Save workflow capsule.",
}


def deferred_tool_hint(name: str) -> str:
    """Return the compact, self-teaching description for a deferred schema."""
    purpose = DEFERRED_TOOL_PURPOSES.get(name)
    if purpose is None:
        label = str(name).replace("_", " ")
        purpose = f"Deferred {label}."
    return f"{purpose} Schema: `tool_search select:{name}`."


@dataclass(frozen=True)
class ToolExposurePlan:
    visible_names: tuple[str, ...]
    deferred_names: tuple[str, ...]
    estimated_tokens_before: int
    estimated_tokens_after: int
    hidden_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextPressure:
    """Input-budget facts used to decide whether schemas create real pressure."""

    context_window: int
    used_tokens: int = 0
    reserve_tokens: int = 0

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.context_window - self.used_tokens - self.reserve_tokens)


class ToolExposureState:
    """Mutable unlock set owned by one RunContext metadata dictionary."""

    def __init__(self, metadata: dict, task_text: str = "") -> None:
        section = metadata.setdefault("tool_exposure", {})
        existing = section.get("unlocked") if isinstance(section, dict) else ()
        self._metadata = metadata
        self._unlocked = set(existing if isinstance(existing, list) else ())
        self._catalog_specs: tuple[dict, ...] = ()
        self._task_text = str(task_text or "").strip()
        self._last_plan: ToolExposurePlan | None = None

    @property
    def unlocked(self) -> frozenset[str]:
        return frozenset(self._unlocked)

    def unlock(self, names) -> tuple[str, ...]:
        added = []
        for name in names:
            value = str(name).strip()
            if value and value not in self._unlocked:
                self._unlocked.add(value)
                added.append(value)
        section = self._metadata.setdefault("tool_exposure", {})
        section["unlocked"] = sorted(self._unlocked)
        return tuple(added)

    @property
    def catalog_specs(self) -> tuple[dict, ...]:
        return self._catalog_specs

    def bind_catalog(self, specs: list[dict]) -> None:
        """Remember the request-scoped catalog that discovery may unlock."""
        self._catalog_specs = tuple(copy.deepcopy(specs))

    @property
    def task_text(self) -> str:
        return self._task_text

    @property
    def last_plan(self) -> ToolExposurePlan | None:
        return self._last_plan

    def record_plan(self, plan: ToolExposurePlan) -> None:
        self._last_plan = plan
        section = self._metadata.setdefault("tool_exposure", {})
        section["last_plan"] = {
            "visible_names": list(plan.visible_names),
            "deferred_names": list(plan.deferred_names),
            "hidden_names": list(plan.hidden_names),
            "estimated_tokens_before": plan.estimated_tokens_before,
            "estimated_tokens_after": plan.estimated_tokens_after,
        }

    def pressure(self) -> ContextPressure | None:
        """Read the latest run-owned pressure snapshot, if supplied."""
        value = self._metadata.get("context_pressure")
        if not isinstance(value, dict):
            return None
        try:
            return ContextPressure(
                context_window=max(1, int(value.get("context_window") or 0)),
                used_tokens=max(0, int(value.get("used_tokens") or 0)),
                reserve_tokens=max(0, int(value.get("reserve_tokens") or 0)),
            )
        except (TypeError, ValueError, OverflowError):
            return None


class ToolExposurePlanner:
    """Replace rich rare-tool descriptions with callable compact hints."""

    def __init__(
        self,
        schema_budget_tokens: int = 6000,
        minimum_deferred_tools: int = 8,
        maximum_eager_tools: int = 32,
    ) -> None:
        self.schema_budget_tokens = max(1, int(schema_budget_tokens))
        self.minimum_deferred_tools = max(1, int(minimum_deferred_tools))
        self.maximum_eager_tools = max(1, int(maximum_eager_tools))

    def plan(
        self,
        catalog: ToolCatalog,
        unlocked=(),
        pressure: ContextPressure | None = None,
        task_text: str | None = None,
    ) -> ToolExposurePlan:
        unlocked_names = set(unlocked)
        before = catalog.schema_tokens
        real_pressure = pressure is not None and (
            pressure.remaining_tokens < max(2_048, before * 2)
            or before / max(1, pressure.context_window) >= 0.20
        )
        if (
            before <= self.schema_budget_tokens
            and len(catalog.names()) <= self.maximum_eager_tools
            and not real_pressure
        ):
            names = catalog.names()
            return ToolExposurePlan(names, (), before, before)
        active_families = _active_task_families(task_text)
        task_aware = active_families is not None
        mcp_candidates = {
            definition.name
            for definition in catalog.definitions()
            if definition.name.startswith("mcp_")
            and definition.name not in unlocked_names
        }
        direct_mcp = (
            mcp_candidates
            if len(mcp_candidates) < self.minimum_deferred_tools
            else set()
        )
        deferred = []
        hidden = []
        for definition in catalog.definitions():
            family = _tool_family(definition.name)
            is_deferred = definition.name not in RESIDENT_TOOLS and family is not None
            if (
                not is_deferred
                or definition.name in unlocked_names
                or definition.name in direct_mcp
            ):
                continue
            if task_aware and family not in active_families:
                hidden.append(definition.name)
            else:
                deferred.append(definition.name)
        # A catalog made only of ordinary local tools remains fully visible.
        if len(deferred) + len(hidden) < self.minimum_deferred_tools:
            return ToolExposurePlan(catalog.names(), (), before, before)
        deferred_names = set(deferred)
        hidden_names = set(hidden)
        visible_names = tuple(
            name for name in catalog.names() if name not in hidden_names
        )
        after = 0
        for definition in catalog.definitions():
            if definition.name in hidden_names:
                continue
            spec = definition.spec()
            if definition.name in deferred_names:
                spec["function"]["description"] = deferred_tool_hint(definition.name)
            after += estimate_schema_tokens(spec)
        return ToolExposurePlan(
            visible_names,
            tuple(deferred),
            before,
            after,
            tuple(hidden),
        )


_CURRENT_STATE: ContextVar[ToolExposureState | None] = ContextVar(
    "nz_coder_tool_exposure_state", default=None,
)
_CURRENT_PLANNER: ContextVar[ToolExposurePlanner | None] = ContextVar(
    "nz_coder_tool_exposure_planner", default=None,
)


def current_exposure_state() -> ToolExposureState:
    state = _CURRENT_STATE.get()
    if state is None:
        raise RuntimeError("No active Tool exposure run context")
    return state


def current_exposure_plan() -> ToolExposurePlan | None:
    state = _CURRENT_STATE.get()
    return state.last_plan if state is not None else None


def expose_specs(specs: list[dict]) -> list[dict]:
    state = _CURRENT_STATE.get()
    planner = _CURRENT_PLANNER.get()
    if state is None or planner is None:
        return copy.deepcopy(list(specs))
    state.bind_catalog(specs)
    catalog = ToolCatalog.from_specs(specs)
    plan = planner.plan(
        catalog,
        state.unlocked,
        pressure=state.pressure(),
        task_text=state.task_text,
    )
    state.record_plan(plan)
    deferred = set(plan.deferred_names)
    visible = set(plan.visible_names)
    result = []
    for definition in catalog.definitions():
        if definition.name not in visible:
            continue
        spec = definition.spec()
        if definition.name in deferred:
            spec["function"]["description"] = deferred_tool_hint(definition.name)
        result.append(spec)
    return result


def filter_specs_for_permission_mode(
    specs: list[dict],
    mode: str,
) -> list[dict]:
    """Apply fail-closed declarative Plan-mode visibility before projection.

    Bash remains visible because its permission boundary is command-aware and
    read-only inspection commands are valid during planning.
    """
    if str(mode or "") != "plan":
        return list(specs)
    policy = get_tool_policy_snapshot()
    return [
        spec for spec in specs
        if (
            str(spec.get("function", {}).get("name") or "") == "bash"
            or bool(policy.get(
                str(spec.get("function", {}).get("name") or ""),
                {},
            ).get("plan_mode_allowed"))
        )
    ]


class ToolExposureMiddleware:
    """Bind one RunContext's exposure state for model and tool execution."""

    def __init__(self, planner: ToolExposurePlanner | None = None) -> None:
        self.planner = planner or ToolExposurePlanner()
        self._tokens: dict[int, tuple[Token, Token]] = {}
        self._lock = RLock()

    async def before_run(self, context) -> None:
        state = ToolExposureState(
            context.metadata,
            task_text=_last_user_text(getattr(context.request, "messages", ())),
        )
        tokens = (_CURRENT_STATE.set(state), _CURRENT_PLANNER.set(self.planner))
        with self._lock:
            self._tokens[id(context)] = tokens

    async def after_run(self, context, _result) -> None:
        self._reset(context)

    async def on_run_error(self, context, _error) -> None:
        self._reset(context)

    def _reset(self, context) -> None:
        with self._lock:
            tokens = self._tokens.pop(id(context), None)
        if tokens is not None:
            _CURRENT_PLANNER.reset(tokens[1])
            _CURRENT_STATE.reset(tokens[0])


def _tool_family(name: str) -> str | None:
    value = str(name or "")
    direct = TOOL_FAMILY_NAMES.get(value)
    if direct is not None:
        return direct
    for prefix, family in TOOL_FAMILY_PREFIXES:
        if value.startswith(prefix):
            return family
    if value in DEFERRED_NAMES:
        return "repo_intelligence"
    if value.startswith(DEFERRED_PREFIXES):
        return "extended"
    return None


def _active_task_families(task_text: str | None) -> frozenset[str] | None:
    value = str(task_text or "").strip().lower()
    if not value or any(keyword in value for keyword in BROAD_TOOL_KEYWORDS):
        return None
    return frozenset(
        family
        for family, keywords in TASK_FAMILY_KEYWORDS.items()
        if any(keyword in value for keyword in keywords)
    )


def _last_user_text(messages) -> str:
    return continuation_task_text(list(messages or ()))[:2000]


__all__ = [
    "ContextPressure", "RESIDENT_TOOLS", "ToolExposureMiddleware", "ToolExposurePlan",
    "ToolExposurePlanner", "ToolExposureState", "current_exposure_state",
    "current_exposure_plan", "deferred_tool_hint", "expose_specs",
    "filter_specs_for_permission_mode",
]
