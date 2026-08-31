"""Behavioral tests for model-facing prompt request accounting."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
from nz_coder.runtime.core.middleware import MiddlewarePipeline
from nz_coder.tool_platform.exposure import (
    ToolExposureMiddleware,
    ToolExposurePlanner,
)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"Use {name}.",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_llm_request_trace_accounts_for_model_visible_tool_schemas(
    monkeypatch,
) -> None:
    """Trace totals must explain Provider input beyond chat messages."""
    events = []
    instructions = SimpleNamespace(
        reminder="",
        source_count=0,
        included_count=0,
        truncated_count=0,
        per_file_truncated_count=0,
        total_truncated_count=0,
        omitted_count=0,
        included_bytes=0,
        paths=[],
        disabled_count=0,
        warnings=[],
    )
    monkeypatch.setattr(
        "nz_coder.runtime.conversation.prompt_builder.load_instruction_context",
        lambda _path: instructions,
    )
    runtime_state = SimpleNamespace(
        strict_progress_nudges=0,
        investigation_calls_since_edit=0,
        mutation_generation=0,
        build_prompt_block=lambda strict=False: "",
    )
    host = SimpleNamespace(
        _structured_output_active_repair="",
        runtime_profile="main",
        _sp=SimpleNamespace(build_prompt_block=lambda: ""),
        _memory_block=lambda _query: "",
        runtime_state=runtime_state,
        _project_profile_block=lambda: "",
        _hook_prompt_block=lambda: "",
        _lineage_recovery_block=lambda _messages: "",
        _implementation_bundle_block=lambda _query: "",
        _repo_retrieval_block=lambda _query: "",
        system_prompt="system",
        _plan_mode_prompt_block=lambda: "",
        _sanitize_messages=lambda messages: list(messages),
        _active_tool_specs=lambda: [_tool("read_file"), _tool("tool_search")],
        tracer=SimpleNamespace(
            log=lambda event, **data: events.append((event, data)),
        ),
    )

    ProductionPromptBuilder().build(
        host,
        [{"role": "user", "content": "read README.md"}],
    )

    request = next(data for event, data in events if event == "llm_request")
    assert request["message_token_estimate"] > 0
    assert request["tool_count"] == 2
    assert request["tool_schema_token_estimate"] > 0
    assert request["token_estimate"] == (
        request["message_token_estimate"]
        + request["tool_schema_token_estimate"]
    )


def test_prompt_builder_traces_task_aware_tool_exposure_decision(monkeypatch) -> None:
    """Removing exposure telemetry must make schema-cost regressions unexplained."""
    events = []
    instructions = SimpleNamespace(
        reminder="",
        source_count=0,
        included_count=0,
        truncated_count=0,
        per_file_truncated_count=0,
        total_truncated_count=0,
        omitted_count=0,
        included_bytes=0,
        paths=[],
        disabled_count=0,
        warnings=[],
    )
    monkeypatch.setattr(
        "nz_coder.runtime.conversation.prompt_builder.load_instruction_context",
        lambda _path: instructions,
    )
    runtime_state = SimpleNamespace(
        strict_progress_nudges=0,
        investigation_calls_since_edit=0,
        mutation_generation=0,
        build_prompt_block=lambda strict=False: "",
    )
    specs = [
        _tool("read_file"),
        _tool("tool_search"),
        _tool("workflow_run"),
        _tool("recall_memory"),
    ]
    host = SimpleNamespace(
        _structured_output_active_repair="",
        runtime_profile="main",
        _sp=SimpleNamespace(build_prompt_block=lambda: ""),
        _memory_block=lambda _query: "",
        runtime_state=runtime_state,
        _project_profile_block=lambda: "",
        _hook_prompt_block=lambda: "",
        _lineage_recovery_block=lambda _messages: "",
        _implementation_bundle_block=lambda _query: "",
        _repo_retrieval_block=lambda _query: "",
        system_prompt="system",
        _plan_mode_prompt_block=lambda: "",
        _sanitize_messages=lambda messages: list(messages),
        _active_tool_specs=lambda: specs,
        tracer=SimpleNamespace(log=lambda event, **data: events.append((event, data))),
    )
    context = SimpleNamespace(
        request=SimpleNamespace(
            session_id="exposure-trace",
            messages=({"role": "user", "content": "Fix parser tests."},),
        ),
        metadata={},
    )
    pipeline = MiddlewarePipeline((ToolExposureMiddleware(
        ToolExposurePlanner(schema_budget_tokens=1, minimum_deferred_tools=1),
    ),))

    async def execute():
        return ProductionPromptBuilder().build(
            host,
            [{"role": "user", "content": "Fix parser tests."}],
        )

    asyncio.run(pipeline.run("run", context, execute))

    exposure = next(data for event, data in events if event == "tool_exposure_planned")
    request = next(data for event, data in events if event == "llm_request")
    assert exposure["visible_names"] == ["read_file", "tool_search"]
    assert exposure["hidden_names"] == ["recall_memory", "workflow_run"]
    assert exposure["estimated_tokens_after"] < exposure["estimated_tokens_before"]
    assert request["tool_count"] == 2


def test_prompt_builder_uses_runtime_task_after_compaction_for_all_queries(
    monkeypatch,
) -> None:
    """A compaction summary must not replace the canonical task query."""
    instructions = SimpleNamespace(
        reminder="",
        source_count=0,
        included_count=0,
        truncated_count=0,
        per_file_truncated_count=0,
        total_truncated_count=0,
        omitted_count=0,
        included_bytes=0,
        paths=[],
        disabled_count=0,
        warnings=[],
    )
    monkeypatch.setattr(
        "nz_coder.runtime.conversation.prompt_builder.load_instruction_context",
        lambda _path: instructions,
    )
    task = "Repair the single-dimension xarray roundtrip regression."
    queries = []
    runtime_state = SimpleNamespace(
        strict_progress_nudges=0,
        investigation_calls_since_edit=0,
        mutation_generation=0,
        initial_task_text=task,
        build_prompt_block=lambda strict=False: "",
    )

    def capture_query(query: str) -> str:
        queries.append(query)
        return ""

    host = SimpleNamespace(
        _structured_output_active_repair="",
        runtime_profile="main",
        _sp=SimpleNamespace(build_prompt_block=lambda: ""),
        _memory_block=capture_query,
        runtime_state=runtime_state,
        _project_profile_block=lambda: "",
        _hook_prompt_block=lambda: "",
        _lineage_recovery_block=lambda _messages: "",
        _implementation_bundle_block=capture_query,
        _repo_retrieval_block=capture_query,
        system_prompt="system",
        _plan_mode_prompt_block=lambda: "",
        _sanitize_messages=lambda messages: list(messages),
        _active_tool_specs=lambda: [],
        tracer=SimpleNamespace(log=lambda _event, **_data: None),
    )
    messages = [{
        "role": "user",
        "content": "<session-summary>\n## Goal\n- stale wrapper\n</session-summary>",
        "_nz_compaction": {"auto": True},
    }]

    ProductionPromptBuilder().build(host, messages)

    assert queries == [task, task, task]


def test_prompt_builder_uses_original_task_for_pure_continuation_queries(
    monkeypatch,
) -> None:
    """A pure resume signal must not replace task-aware retrieval authority."""
    instructions = SimpleNamespace(
        reminder="",
        source_count=0,
        included_count=0,
        truncated_count=0,
        per_file_truncated_count=0,
        total_truncated_count=0,
        omitted_count=0,
        included_bytes=0,
        paths=[],
        disabled_count=0,
        warnings=[],
    )
    monkeypatch.setattr(
        "nz_coder.runtime.conversation.prompt_builder.load_instruction_context",
        lambda _path: instructions,
    )
    task = "Run the saved workflow and inspect its failed verification."
    queries = []
    runtime_state = SimpleNamespace(
        strict_progress_nudges=0,
        investigation_calls_since_edit=0,
        mutation_generation=0,
        initial_task_text=task,
        build_prompt_block=lambda strict=False: "",
    )

    def capture_query(query: str) -> str:
        queries.append(query)
        return ""

    host = SimpleNamespace(
        _structured_output_active_repair="",
        runtime_profile="main",
        _sp=SimpleNamespace(build_prompt_block=lambda: ""),
        _memory_block=capture_query,
        runtime_state=runtime_state,
        _project_profile_block=lambda: "",
        _hook_prompt_block=lambda: "",
        _lineage_recovery_block=lambda _messages: "",
        _implementation_bundle_block=capture_query,
        _repo_retrieval_block=capture_query,
        system_prompt="system",
        _plan_mode_prompt_block=lambda: "",
        _sanitize_messages=lambda messages: list(messages),
        _active_tool_specs=lambda: [],
        tracer=SimpleNamespace(log=lambda _event, **_data: None),
    )
    messages = [
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
    ]

    ProductionPromptBuilder().build(host, messages)

    assert queries == [task, task, task]
