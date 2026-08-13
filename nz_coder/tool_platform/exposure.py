"""Context-budget-aware progressive exposure with run-owned unlock state."""
from __future__ import annotations

import copy
from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock

from nz_coder.tool_platform.catalog import ToolCatalog


RESIDENT_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "apply_patch", "bash",
    "grep_search", "glob", "list_directory", "task", "todo_write",
    "question", "load_skill", "tool_search", "compact_context",
})
DEFERRED_PREFIXES = (
    "mcp_", "lsp_", "workflow_", "memory_", "project_", "semantic_",
)
DEFERRED_NAMES = frozenset({
    "lsp", "repo_context", "repo_map", "smart_search", "read_symbol",
    "find_symbol_callers", "create_project", "verify_changed_files",
})


@dataclass(frozen=True)
class ToolExposurePlan:
    visible_names: tuple[str, ...]
    deferred_names: tuple[str, ...]
    estimated_tokens_before: int
    estimated_tokens_after: int


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

    def __init__(self, metadata: dict) -> None:
        section = metadata.setdefault("tool_exposure", {})
        existing = section.get("unlocked") if isinstance(section, dict) else ()
        self._metadata = metadata
        self._unlocked = set(existing if isinstance(existing, list) else ())
        self._catalog_specs: tuple[dict, ...] = ()

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
        self._metadata["tool_exposure"] = {"unlocked": sorted(self._unlocked)}
        return tuple(added)

    @property
    def catalog_specs(self) -> tuple[dict, ...]:
        return self._catalog_specs

    def bind_catalog(self, specs: list[dict]) -> None:
        """Remember the request-scoped catalog that discovery may unlock."""
        self._catalog_specs = tuple(copy.deepcopy(specs))

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
        except (TypeError, ValueError):
            return None


class ToolExposurePlanner:
    """Conservative policy that defers only known rare/dynamic surfaces."""

    def __init__(
        self, schema_budget_tokens: int = 6000, minimum_deferred_tools: int = 20,
    ) -> None:
        self.schema_budget_tokens = max(1, int(schema_budget_tokens))
        self.minimum_deferred_tools = max(1, int(minimum_deferred_tools))

    def plan(
        self, catalog: ToolCatalog, unlocked=(), pressure: ContextPressure | None = None,
    ) -> ToolExposurePlan:
        unlocked_names = set(unlocked)
        before = catalog.schema_tokens
        real_pressure = pressure is None or (
            pressure.remaining_tokens < max(2_048, before * 2)
            or before / max(1, pressure.context_window) >= 0.20
        )
        if before <= self.schema_budget_tokens and not (
            pressure is not None and real_pressure
        ):
            names = catalog.names()
            return ToolExposurePlan(names, (), before, before)
        if pressure is not None and not real_pressure:
            names = catalog.names()
            return ToolExposurePlan(names, (), before, before)
        visible = []
        deferred = []
        after = 0
        for definition in catalog.definitions():
            is_deferred = (
                definition.name not in RESIDENT_TOOLS
                and (
                    definition.name in DEFERRED_NAMES
                    or definition.name.startswith(DEFERRED_PREFIXES)
                )
            )
            if is_deferred and definition.name not in unlocked_names:
                deferred.append(definition.name)
            else:
                visible.append(definition.name)
                after += definition.schema_tokens
        mcp_deferred = [name for name in deferred if name.startswith("mcp_")]
        if len(mcp_deferred) < self.minimum_deferred_tools:
            for name in mcp_deferred:
                deferred.remove(name)
                visible.append(name)
                after += catalog.require(name).schema_tokens
        # A catalog made only of ordinary local tools remains fully visible.
        if len(deferred) < self.minimum_deferred_tools:
            return ToolExposurePlan(catalog.names(), (), before, before)
        return ToolExposurePlan(tuple(visible), tuple(deferred), before, after)


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


def expose_specs(specs: list[dict]) -> list[dict]:
    state = _CURRENT_STATE.get()
    planner = _CURRENT_PLANNER.get()
    if state is None or planner is None:
        return copy.deepcopy(list(specs))
    state.bind_catalog(specs)
    catalog = ToolCatalog.from_specs(specs)
    plan = planner.plan(catalog, state.unlocked, pressure=state.pressure())
    visible = set(plan.visible_names)
    return [definition.spec() for definition in catalog.definitions() if definition.name in visible]


class ToolExposureMiddleware:
    """Bind one RunContext's exposure state for model and tool execution."""

    def __init__(self, planner: ToolExposurePlanner | None = None) -> None:
        self.planner = planner or ToolExposurePlanner()
        self._tokens: dict[int, tuple[Token, Token]] = {}
        self._lock = RLock()

    async def before_run(self, context) -> None:
        state = ToolExposureState(context.metadata)
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


__all__ = [
    "ContextPressure", "RESIDENT_TOOLS", "ToolExposureMiddleware", "ToolExposurePlan",
    "ToolExposurePlanner", "ToolExposureState", "current_exposure_state",
    "expose_specs",
]
