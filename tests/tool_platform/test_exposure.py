"""Progressive tool exposure behavior and Session isolation."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.tool_platform.catalog import ToolCatalog
from nz_coder.tool_platform.exposure import (
    ContextPressure,
    ToolExposureMiddleware,
    ToolExposurePlanner,
    ToolExposureState,
    current_exposure_state,
    expose_specs,
    filter_specs_for_permission_mode,
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
        request=SimpleNamespace(session_id=session_id, messages=()),
        metadata={},
    )


def test_pressure_ignores_nonfinite_runtime_metrics() -> None:
    """Corrupt context telemetry must not break tool-schema planning."""
    metadata = {
        "context_pressure": {
            "context_window": float("inf"),
            "used_tokens": 1,
            "reserve_tokens": 1,
        },
    }

    assert ToolExposureState(metadata).pressure() is None


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
        initial = {
            item["function"]["name"]: item["function"]["description"]
            for item in expose_specs(specs)
        }
        assert "select:mcp_remote_1" in initial["mcp_remote_1"]
        current_exposure_state().unlock(("mcp_remote_1",))
        return {
            item["function"]["name"]: item["function"]["description"]
            for item in expose_specs(specs)
        }

    async def run_second():
        return {
            item["function"]["name"]: item["function"]["description"]
            for item in expose_specs(specs)
        }

    first_visible = asyncio.run(pipeline.run("run", first, run_first))
    second_visible = asyncio.run(pipeline.run("run", second, run_second))

    assert first_visible["mcp_remote_1"] == specs[2]["function"]["description"]
    assert "select:mcp_remote_1" in second_visible["mcp_remote_1"]
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


def test_deferred_tool_stays_callable_with_hint_until_full_schema_is_unlocked() -> None:
    description_unit = "Run a bounded workflow with detailed phase and synthesis contracts. "
    full_description = description_unit * 20
    specs = [
        _spec("read_file"),
        _spec("tool_search"),
        *[
            _spec(f"workflow_rare_{index}", description_unit)
            for index in range(12)
        ],
    ]
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1),
    ),))
    context = _context("hinted-search-session")

    async def execute():
        initial = {
            item["function"]["name"]: item["function"]
            for item in expose_specs(specs)
        }
        output = search_and_unlock("select:workflow_rare_0")
        unlocked = {
            item["function"]["name"]: item["function"]
            for item in expose_specs(specs)
        }
        return initial, output, unlocked

    initial, output, unlocked = asyncio.run(pipeline.run("run", context, execute))

    assert "workflow_rare_0" in initial
    assert "select:workflow_rare_0" in initial["workflow_rare_0"]["description"]
    assert len(initial["workflow_rare_0"]["description"]) < len(full_description)
    assert initial["workflow_rare_0"]["parameters"] == specs[2]["function"]["parameters"]
    assert '"name": "workflow_rare_0"' in output
    assert unlocked["workflow_rare_0"]["description"] == full_description


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


def test_schema_budget_defers_large_catalog_even_with_large_context() -> None:
    specs = [_spec("read_file"), *[_spec(f"mcp_remote_{i}") for i in range(100)]]
    pressure = ContextPressure(context_window=200_000, used_tokens=1_000, reserve_tokens=8_000)

    plan = ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1).plan(
        ToolCatalog.from_specs(specs), pressure=pressure,
    )

    assert len(plan.visible_names) == 101
    assert len(plan.deferred_names) == 100
    assert plan.estimated_tokens_after < plan.estimated_tokens_before


def test_default_policy_defers_realistic_local_rare_surface() -> None:
    specs = [
        _spec("read_file"),
        _spec("tool_search"),
        *[_spec(f"workflow_rare_{index}") for index in range(12)],
    ]
    pressure = ContextPressure(
        context_window=1_000_000,
        used_tokens=3_000,
        reserve_tokens=64_000,
    )

    plan = ToolExposurePlanner(schema_budget_tokens=1).plan(
        ToolCatalog.from_specs(specs),
        pressure=pressure,
    )

    assert len(plan.visible_names) == 14
    assert len(plan.deferred_names) == 12


def test_streaming_model_path_uses_run_scoped_tool_exposure() -> None:
    from nz_coder.runtime.execution.provider_stream import _active_tools

    specs = [_spec("read_file"), _spec("tool_search"), _spec("mcp_remote_1")]
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1),
    ),))
    context = _context("streaming-exposure")
    host = SimpleNamespace(
        model_capabilities=SimpleNamespace(supports_tools=True),
        _active_tool_specs=lambda: specs,
    )

    async def execute():
        return {
            item["function"]["name"]: item["function"]["description"]
            for item in _active_tools(host)
        }

    visible = asyncio.run(pipeline.run("run", context, execute))

    assert set(visible) == {"mcp_remote_1", "read_file", "tool_search"}
    assert "select:mcp_remote_1" in visible["mcp_remote_1"]


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
        return {
            item["function"]["name"]: item["function"]["description"]
            for item in expose_specs(specs)
        }

    visible = asyncio.run(pipeline.run("run", context, execute))

    assert len(visible) == 31
    assert "select:mcp_remote_0" in visible["mcp_remote_0"]


def test_task_family_plan_hides_unrelated_rare_tools_without_hiding_core() -> None:
    """Removing task-family slicing must re-expose unrelated workflow/memory schemas."""
    specs = [
        _spec("read_file"),
        _spec("apply_patch"),
        _spec("tool_search"),
        _spec("workflow_run"),
        _spec("workflow_runs"),
        _spec("recall_memory"),
        _spec("save_memory"),
    ]

    plan = ToolExposurePlanner(
        schema_budget_tokens=1,
        minimum_deferred_tools=1,
    ).plan(
        ToolCatalog.from_specs(specs),
        task_text="Fix the cron parser and run its tests.",
    )

    assert set(plan.visible_names) == {"apply_patch", "read_file", "tool_search"}
    assert set(plan.hidden_names) == {
        "recall_memory", "save_memory", "workflow_run", "workflow_runs",
    }
    assert plan.estimated_tokens_after < plan.estimated_tokens_before


def test_task_family_plan_keeps_matching_family_as_callable_hints() -> None:
    """Classifying the wrong family would make an explicitly requested workflow undiscoverable."""
    specs = [
        _spec("read_file"),
        _spec("tool_search"),
        _spec("workflow_run"),
        _spec("workflow_runs"),
        _spec("recall_memory"),
    ]

    plan = ToolExposurePlanner(
        schema_budget_tokens=1,
        minimum_deferred_tools=1,
    ).plan(
        ToolCatalog.from_specs(specs),
        task_text="Run the saved workflow and inspect workflow history.",
    )

    assert "workflow_run" in plan.visible_names
    assert "workflow_runs" in plan.visible_names
    assert "workflow_run" in plan.deferred_names
    assert "workflow_runs" in plan.deferred_names
    assert "recall_memory" in plan.hidden_names


def test_unknown_task_keeps_behavior_compatible_all_hint_fallback() -> None:
    """Missing task intent must not silently remove tools from legacy callers."""
    specs = [
        _spec("read_file"),
        _spec("tool_search"),
        _spec("workflow_run"),
        _spec("recall_memory"),
    ]

    plan = ToolExposurePlanner(
        schema_budget_tokens=1,
        minimum_deferred_tools=1,
    ).plan(ToolCatalog.from_specs(specs), task_text="")

    assert set(plan.visible_names) == {
        "read_file", "recall_memory", "tool_search", "workflow_run",
    }
    assert plan.hidden_names == ()


def test_hidden_tool_search_unlocks_full_schema_on_the_next_turn() -> None:
    """Dropping hidden targets from the run catalog would break two-hop reachability."""
    specs = [
        _spec("read_file"),
        _spec("tool_search"),
        _spec("workflow_run"),
        _spec("recall_memory"),
    ]
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1),
    ),))
    context = _context("task-family-unlock")
    context.request.messages = (
        {"role": "user", "content": "Fix the cron parser and run tests."},
    )

    async def execute():
        initial = {
            item["function"]["name"]: item["function"]["description"]
            for item in expose_specs(specs)
        }
        output = search_and_unlock("select:workflow_run")
        unlocked = {
            item["function"]["name"]: item["function"]["description"]
            for item in expose_specs(specs)
        }
        return initial, output, unlocked

    initial, output, unlocked = asyncio.run(pipeline.run("run", context, execute))

    assert "workflow_run" not in initial
    assert '"name": "workflow_run"' in output
    assert unlocked["workflow_run"] == specs[2]["function"]["description"]
    recorded = context.metadata["tool_exposure"]["last_plan"]
    assert recorded["visible_names"] == sorted(unlocked)
    assert recorded["hidden_names"] == ["recall_memory"]
    assert recorded["estimated_tokens_after"] < recorded["estimated_tokens_before"]


def test_pure_continuation_keeps_original_task_family_visible() -> None:
    """A `go on` activation must retain task-aware deferred tool routing."""
    task = "Run the saved workflow and inspect workflow history."
    specs = [
        _spec("read_file"),
        _spec("tool_search"),
        _spec("workflow_run"),
        _spec("recall_memory"),
    ]
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1),
    ),))
    context = _context("continuation-task-family")
    context.request.messages = (
        {
            "role": "assistant",
            "content": "Stopped at the work limit.",
            "_nz_continuation": {
                "version": 1,
                "status": "max_turns",
                "summary": f"## Latest User Instruction\n{task}",
            },
        },
        {"role": "user", "content": "go on"},
    )

    async def execute():
        return {item["function"]["name"] for item in expose_specs(specs)}

    visible = asyncio.run(pipeline.run("run", context, execute))

    assert "workflow_run" in visible
    assert "recall_memory" not in visible


def test_real_coding_catalog_fits_schema_budget_after_task_family_slicing() -> None:
    """Re-exposing a rare family must make the real coding surface exceed its 6K gate."""
    import nz_coder.runtime.execution.loop  # noqa: F401 - production side-effect registrations
    from nz_coder.tools import get_catalog_specs

    catalog = ToolCatalog.from_specs(get_catalog_specs())
    plan = ToolExposurePlanner().plan(
        catalog,
        task_text="Fix the cron parser, update tests, and run pytest.",
    )

    assert catalog.schema_tokens > 9_000
    assert plan.estimated_tokens_after <= 6_000
    assert {"read_file", "apply_patch", "bash", "tool_search"}.issubset(
        plan.visible_names
    )
    assert len(plan.hidden_names) >= 20


def test_plan_mode_small_repo_analysis_does_not_eagerly_send_every_schema() -> None:
    import nz_coder.runtime.execution.loop  # noqa: F401 - production registrations
    from nz_coder.tools import get_catalog_specs

    specs = filter_specs_for_permission_mode(get_catalog_specs(), "plan")
    catalog = ToolCatalog.from_specs(specs)
    plan = ToolExposurePlanner().plan(
        catalog,
        task_text=(
            "Inspect the cron_engine package and explain the parser-to-scheduler "
            "control flow. Do not modify files."
        ),
    )

    assert len(catalog.names()) >= 35
    assert "repo_map" in plan.visible_names
    assert "repo_map" in plan.deferred_names
    assert "workflow_runs" in plan.hidden_names
    assert plan.estimated_tokens_after < plan.estimated_tokens_before
