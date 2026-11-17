"""Behavioral tests for model-facing prompt request accounting."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
import pytest

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
        task_assessment_state={}, plan_revision=0,
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
        task_assessment_state={}, plan_revision=0,
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
            metadata={},
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


@pytest.mark.parametrize("preamble", ["", "Repository instructions and evaluation wrapper. " * 25])
def test_prompt_builder_uses_runtime_task_after_compaction_for_all_queries(
    monkeypatch, preamble,
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
    task = preamble + "Repair `editions_match` in matcher.py for unspecified editions."
    queries = []
    runtime_state = SimpleNamespace(
        task_assessment_state={}, plan_revision=0,
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

    assert len(queries) == 3
    assert all("editions_match" in query and "matcher.py" in query for query in queries)


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
        task_assessment_state={}, plan_revision=0,
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


@pytest.fixture
def prompt_agent(tmp_path):
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from tests.test_loop_fake import FakeClient

    with scoped_workdir(tmp_path), scoped_session("dynamic-context-tail"):
        agent = AgentLoop(
            "Keep project rules stable.",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        agent.runtime_state.initial_task_text = "Fix parser.py and verify its tests."
        try:
            yield agent
        finally:
            agent.close()


def test_pending_handoff_keeps_investigation_notes(prompt_agent, tmp_path):
    from test_execution_handoff import _case, _submit
    state, messages, call, proposal = _case(tmp_path)
    assert not _submit(tmp_path, state, messages, call, proposal).dispatch_failed
    prompt_agent.runtime_state = state
    prompt_agent._sp.update_section('focus', [{"summary": "PENDING_ACTION_FINDING: cancellation must follow the commit boundary.", "details": ""}])
    request = prompt_agent._build_api_messages(messages)
    assert "PENDING_ACTION_FINDING" in str(request)


def test_automatic_plan_is_visible_separately_from_notes(prompt_agent):
    prompt_agent.runtime_state.plan_text = "AUTOMATIC_PLAN: patch the decoder then run the regression."
    prompt_agent._sp.update_section('focus', [{"summary": "INVESTIGATION_NOTE: header parsing belongs to the transport layer.", "details": ""}])
    request = prompt_agent._build_api_messages([{"role": "user", "content": "Fix the decoder."}])
    assert "AUTOMATIC_PLAN" in str(request)
    assert "INVESTIGATION_NOTE" in str(request)


def test_long_plan_keeps_working_notes_and_readback_hint(prompt_agent, monkeypatch):
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "SYSTEM_CONTEXT_BUDGET_TOKENS", 6000)
    prompt_agent.runtime_state.plan_text = "调查计划说明。" * 1000
    prompt_agent._sp.update_section('focus', [{"summary": "Long evidence. " * 500, "details": ""}])
    prompt_agent._sp.update_section('focus', [{"summary": "SHORT_FINDING: preserve the cancellation boundary.", "details": ""}])
    request = prompt_agent._build_api_messages([{"role": "user", "content": "Fix the decoder."}])
    assert "SHORT_FINDING" in str(request)
    assert "read_scratchpad" in str(request[-1]["content"])


@pytest.mark.parametrize("history_kind", ["tool_results", "compacted", "continuation"])
def test_dynamic_updates_preserve_history_prefix_and_do_not_accumulate(
    prompt_agent, history_kind,
):
    """Changing working memory must leave the replayed task/history cacheable."""
    if history_kind == "compacted":
        messages = [{
            "role": "user",
            "content": "<session-summary>\n## Goal\nFix parser.py\n</session-summary>",
            "_nz_compaction": {"auto": True},
        }]
    elif history_kind == "continuation":
        messages = [
            {"role": "user", "content": "Fix parser.py and verify its tests."},
            {
                "role": "assistant",
                "content": "Stopped at the work limit.",
                "_nz_continuation": {
                    "version": 1,
                    "status": "max_turns",
                    "summary": "## Latest User Instruction\nFix parser.py and verify its tests.",
                },
            },
            {"role": "user", "content": "go on"},
        ]
    else:
        messages = [
            {"role": "user", "content": "Fix parser.py and verify its tests."},
            {
                "role": "assistant", "content": "Inspect both files.",
                "tool_calls": [
                    {"id": "read-source", "type": "function", "function": {
                        "name": "read_file", "arguments": '{"path":"parser.py"}',
                    }},
                    {"id": "read-test", "type": "function", "function": {
                        "name": "read_file", "arguments": '{"path":"test_parser.py"}',
                    }},
                ],
            },
            {"role": "tool", "tool_call_id": "read-source", "content": "def parse(): pass"},
            {"role": "tool", "tool_call_id": "read-test", "content": "def test_parse(): pass"},
        ]
    original = copy.deepcopy(messages)
    prompt_agent._sp.update_section('focus', [{"summary": "Investigate parser branch first.", "details": ""}], mode="replace")
    first = prompt_agent._build_api_messages(messages)
    prompt_agent._sp.update_section('focus', [{"summary": "Patch parser branch and rerun tests.", "details": ""}], mode="replace")
    second = prompt_agent._build_api_messages(messages)

    assert first[:-1] == second[:-1]
    assert first[-1]["role"] == second[-1]["role"] == "user"
    assert second[-1]["content"].startswith("<context-injection>")
    assert second[-1]["content"].endswith("</context-injection>")
    assert any(m["role"] == "user" for m in second[1:-1])
    assert "Investigate parser branch first." in first[-1]["content"]
    assert "Patch parser branch and rerun tests." in second[-1]["content"]
    assert "Investigate parser branch first." not in str(second)
    assert sum("<context-injection>" in str(m.get("content", "")) for m in second) == 1
    assert messages == original
    if history_kind == "tool_results":
        assert [m["tool_call_id"] for m in second if m["role"] == "tool"] == ["read-source", "read-test"]
        assert [m["role"] for m in second] == ["system", "user", "assistant", "tool", "tool", "user"]


def test_growing_history_retains_previous_request_prefix_before_dynamic_state(prompt_agent):
    """Appending a tool step must extend the stable prefix rather than rewrite it."""
    messages = [{"role": "user", "content": "Fix parser.py and verify its tests."}]
    prompt_agent._sp.update_section('focus', [{"summary": "Inspect parser.py.", "details": ""}], mode="replace")
    first = prompt_agent._build_api_messages(messages)
    assert "<context-injection>" not in first[1]["content"]
    messages.extend([
        {"role": "assistant", "content": "Read parser.py.", "tool_calls": [
            {"id": "read-source", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"parser.py"}',
            }},
        ]},
        {"role": "tool", "tool_call_id": "read-source", "content": "def parse(): pass"},
    ])
    prompt_agent._sp.update_section('focus', [{"summary": "Implement the parser correction.", "details": ""}], mode="replace")
    second = prompt_agent._build_api_messages(messages)

    assert second[:len(first) - 1] == first[:-1]
    assert second[-2]["role"] == "tool"
    assert "Implement the parser correction." in second[-1]["content"]
    assert all("<context-injection>" not in str(m) for m in messages)


@pytest.mark.parametrize("profile", ["read_child", "write_child"])
def test_child_profiles_keep_their_own_prompt_without_parent_dynamic_state(prompt_agent, profile):
    """The parent tail injection must not leak working memory into child profiles."""
    prompt_agent.runtime_profile = profile
    prompt_agent._sp.update_section('focus', [{"summary": "Parent-only implementation notes.", "details": ""}], mode="replace")
    messages = [{"role": "user", "content": "Inspect the requested file."}]

    request = prompt_agent._build_api_messages(messages)

    assert [m["role"] for m in request] == ["system", "user", "user"]
    assert request[1]["content"] == "Inspect the requested file."
    assert "<task-assessment>" in request[-1]["content"]
    assert "expected_revision=0" in request[-1]["content"]
    assert "Parent-only implementation notes." not in str(request)
