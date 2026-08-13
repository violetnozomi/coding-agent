"""Progressive tool exposure behavior and Session isolation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.tool_platform.catalog import ToolCatalog
from nz_coder.tool_platform.exposure import (
    ContextPressure,
    ToolExposureMiddleware,
    ToolExposurePlanner,
    current_exposure_state,
    expose_specs,
)
from nz_coder.runtime.core.middleware import MiddlewarePipeline
from nz_coder.tools.tool_search import search_and_unlock


def _spec(name: str, description: str = "long detailed description") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description * 20,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _context(session_id: str):
    return SimpleNamespace(
        request=SimpleNamespace(session_id=session_id),
        metadata={},
    )


def test_planner_keeps_resident_and_defers_rare_tools_under_pressure() -> None:
    specs = [
        _spec("read_file"), _spec("edit_file"), _spec("tool_search"),
        *[_spec(f"mcp_remote_{index}") for index in range(80)],
    ]
    plan = ToolExposurePlanner(schema_budget_tokens=1200).plan(
        ToolCatalog.from_specs(specs), unlocked=(),
    )

    assert "read_file" in plan.visible_names
    assert "edit_file" in plan.visible_names
    assert "tool_search" in plan.visible_names
    assert "mcp_remote_79" in plan.deferred_names
    assert plan.estimated_tokens_after < plan.estimated_tokens_before


def test_unlock_is_visible_next_turn_and_isolated_between_sessions() -> None:
    specs = [_spec("read_file"), _spec("tool_search"), _spec("mcp_remote_1")]
    planner = ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1)
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(planner),))
    first = _context("one")
    second = _context("two")

    async def run_first():
        assert "mcp_remote_1" not in {
            item["function"]["name"] for item in expose_specs(specs)
        }
        current_exposure_state().unlock(("mcp_remote_1",))
        return {item["function"]["name"] for item in expose_specs(specs)}

    async def run_second():
        return {item["function"]["name"] for item in expose_specs(specs)}

    first_visible = asyncio.run(pipeline.run("run", first, run_first))
    second_visible = asyncio.run(pipeline.run("run", second, run_second))

    assert "mcp_remote_1" in first_visible
    assert "mcp_remote_1" not in second_visible
    assert first.metadata["tool_exposure"]["unlocked"] == ["mcp_remote_1"]


def test_tool_search_returns_full_schema_and_unlocks_match() -> None:
    specs = [_spec("read_file"), _spec("tool_search"), _spec("mcp_issue_lookup")]
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1),
    ),))
    context = _context("search-session")

    async def execute():
        output = search_and_unlock("select:mcp_issue_lookup", specs=specs)
        return output, {item["function"]["name"] for item in expose_specs(specs)}

    output, visible = asyncio.run(pipeline.run("run", context, execute))

    assert '"name": "mcp_issue_lookup"' in output
    assert '"parameters"' in output
    assert "mcp_issue_lookup" in visible


def test_tool_search_cannot_discover_tools_outside_run_catalog() -> None:
    specs = [_spec("read_file"), _spec("tool_search"), _spec("mcp_allowed")]
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1),
    ),))
    context = _context("scoped-search-session")

    async def execute():
        expose_specs(specs)
        return search_and_unlock("select:repo_context")

    output = asyncio.run(pipeline.run("run", context, execute))

    assert output.startswith("No tools matched")
    assert context.metadata["tool_exposure"].get("unlocked", []) == []


def test_default_policy_does_not_defer_a_small_role_or_mcp_surface() -> None:
    specs = [_spec("repo_map"), _spec("emit_handoff"), _spec("mcp_ping")]
    plan = ToolExposurePlanner(schema_budget_tokens=1).plan(
        ToolCatalog.from_specs(specs), unlocked=(),
    )

    assert set(plan.visible_names) == {"repo_map", "emit_handoff", "mcp_ping"}
    assert plan.deferred_names == ()


def test_large_catalog_remains_visible_without_real_context_pressure() -> None:
    specs = [_spec("read_file"), *[_spec(f"mcp_remote_{i}") for i in range(100)]]
    pressure = ContextPressure(context_window=200_000, used_tokens=1_000, reserve_tokens=8_000)

    plan = ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1).plan(
        ToolCatalog.from_specs(specs), pressure=pressure,
    )

    assert len(plan.visible_names) == 101
    assert plan.deferred_names == ()


def test_schema_ratio_triggers_deferral_under_real_pressure() -> None:
    specs = [_spec("read_file"), *[_spec(f"mcp_remote_{i}") for i in range(200)]]
    pressure = ContextPressure(context_window=20_000, used_tokens=15_000, reserve_tokens=3_000)

    plan = ToolExposurePlanner(schema_budget_tokens=100_000, minimum_deferred_tools=1).plan(
        ToolCatalog.from_specs(specs), pressure=pressure,
    )

    assert "read_file" in plan.visible_names
    assert len(plan.deferred_names) == 200


def test_middleware_wires_run_owned_pressure_into_actual_spec_exposure() -> None:
    specs = [_spec("read_file"), *[_spec(f"mcp_remote_{i}") for i in range(30)]]
    planner = ToolExposurePlanner(schema_budget_tokens=100_000, minimum_deferred_tools=1)
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(planner),))
    context = _context("pressure-session")
    context.metadata["context_pressure"] = {
        "context_window": 10_000, "used_tokens": 8_000, "reserve_tokens": 1_000,
    }

    async def execute():
        return [item["function"]["name"] for item in expose_specs(specs)]

    visible = asyncio.run(pipeline.run("run", context, execute))

    assert visible == ["read_file"]
