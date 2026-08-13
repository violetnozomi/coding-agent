"""Stable tool-result projection through focused operations."""
from __future__ import annotations

from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.runtime.tool_executor import ToolExecutionResult
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector


def test_projection_appends_contiguous_result_and_runs_post_hook() -> None:
    """Bypassing focused callbacks would lose accounting or post-result hooks."""
    traced: list[tuple[str, str]] = []
    hooked: list[tuple[str, str]] = []
    context = ToolProjectionContext(
        signal_from_metadata=lambda metadata: metadata.get("handoff") if metadata else None,
        record_result=lambda _result: False,
        trace_result=lambda result, output, **_kwargs: traced.append((result.name, output)),
        stall_orchestrator=None,
        after_result=lambda _messages, result, output: hooked.append((result.name, output)),
    )
    result = ToolExecutionResult(
        name="read_file",
        tool_input={"path": "a.py"},
        output="contents",
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    )
    call = {"id": "call-read", "function": {"name": "read_file", "arguments": {}}}
    messages: list[dict] = []

    state = ProductionToolResultProjector().consume(
        context,
        [(0, call, result)],
        messages,
    )

    assert state["all_succeeded"] is True
    assert messages == [{
        "role": "tool",
        "tool_call_id": "call-read",
        "content": "contents",
    }]
    assert traced == [("read_file", "contents")]
    assert hooked == [("read_file", "contents")]


def test_projection_applies_one_budget_before_every_model_visible_consumer() -> None:
    observed: list[str] = []
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda _result, output, **_kwargs: observed.append(output),
        stall_orchestrator=None,
        after_result=lambda _messages, _result, output: observed.append(output),
    )
    result = ToolExecutionResult(
        name="bash",
        tool_input={"command": "generate"},
        output="HEAD\n" + ("x" * 4000) + "\nTAIL",
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    )
    call = {"id": "call-large", "function": {"name": "bash", "arguments": {}}}
    messages: list[dict] = []
    policy = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=100),
        artifact_writer=lambda _call_id, _output: ".nz-coder/full.txt",
    )

    ProductionToolResultProjector(projector=policy).consume(
        context,
        [(0, call, result)],
        messages,
        on_tool=lambda _name, output: observed.append(output),
    )

    assert messages[0]["content"] == observed[0] == observed[1] == observed[2]
    assert "HEAD" in messages[0]["content"]
    assert "TAIL" in messages[0]["content"]
    assert result.metadata["projection"]["truncated"] is True
    assert result.metadata["projection"]["artifact_path"] == ".nz-coder/full.txt"


def test_projection_uses_real_next_request_capacity() -> None:
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
        available_result_tokens=lambda _messages: 60,
    )
    result = ToolExecutionResult(
        name="bash", tool_input={"command": "large"}, output="x" * 4000,
        executed=True, dispatch_failed=False, command_failed=False, is_write=False,
    )
    policy = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=200),
        artifact_writer=lambda _call_id, _output: "artifact.txt",
    )

    ProductionToolResultProjector(projector=policy).consume(
        context,
        [(0, {"id": "capacity", "function": {"name": "bash", "arguments": {}}}, result)],
        [],
    )

    projection = result.metadata["projection"]
    assert projection["batch_budget_tokens"] == 60
    assert projection["projected_tokens"] <= 60
