"""Stable tool-result projection through focused operations."""
from __future__ import annotations

from nz_coder.runtime.core.tool_context import ToolProjectionContext
from nz_coder.runtime.execution.tool_executor import ToolExecutionResult
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
    assert projection["batch_budget_tokens"] == 52
    assert projection["projected_tokens"] <= 52


def test_projection_keeps_complete_batch_when_physical_capacity_can_hold_it() -> None:
    """A fallback cap must not discard evidence that fits the next request."""
    persisted: list[str] = []
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
        available_result_tokens=lambda _messages: 100_000,
    )
    output = "complete-evidence-line\n" * 5_000
    result = ToolExecutionResult(
        name="read_file",
        tool_input={"path": "large.py"},
        output=output,
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    )
    messages: list[dict] = []
    policy = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=100),
        artifact_writer=lambda _call_id, value: persisted.append(value) or "artifact.txt",
    )

    ProductionToolResultProjector(projector=policy).consume(
        context,
        [(0, {"id": "physical-capacity", "function": {
            "name": "read_file", "arguments": {},
        }}, result)],
        messages,
    )

    assert messages[0]["content"] == output
    assert result.metadata.get("projection") is None
    assert persisted == []


def test_projection_reserves_tool_result_envelope_capacity() -> None:
    """Result text must not consume capacity needed by its protocol envelope."""
    persisted: list[str] = []
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
        available_result_tokens=lambda _messages: 100,
    )
    output = "x" * 380
    result = ToolExecutionResult(
        name="read_file",
        tool_input={"path": "near-capacity.py"},
        output=output,
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    )
    messages: list[dict] = []
    policy = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=100),
        artifact_writer=lambda _call_id, value: persisted.append(value) or "artifact.txt",
    )

    ProductionToolResultProjector(projector=policy).consume(
        context,
        [(0, {"id": "envelope", "function": {
            "name": "read_file", "arguments": {},
        }}, result)],
        messages,
    )

    assert messages[0]["content"] != output
    assert result.metadata["projection"]["truncated"] is True
    assert persisted == [output]


def test_over_capacity_batch_spills_only_largest_required_result() -> None:
    """Smaller complete evidence must survive when one spill relieves pressure."""
    persisted: list[tuple[str, str]] = []
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
        available_result_tokens=lambda _messages: 246,
    )
    outputs = ("a" * 360, "b" * 360, "c" * 3_200)
    results = [
        ToolExecutionResult(
            name="read_file",
            tool_input={"path": f"result-{index}.txt"},
            output=output,
            executed=True,
            dispatch_failed=False,
            command_failed=False,
            is_write=False,
        )
        for index, output in enumerate(outputs)
    ]
    dispatched = [
        (index, {
            "id": f"call-{index}",
            "function": {"name": "read_file", "arguments": {}},
        }, result)
        for index, result in enumerate(results)
    ]
    messages: list[dict] = []
    policy = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=100),
        artifact_writer=lambda call_id, value: (
            persisted.append((call_id, value)) or f"{call_id}.txt"
        ),
    )

    ProductionToolResultProjector(projector=policy).consume(
        context,
        dispatched,
        messages,
    )

    assert [message["content"] for message in messages[:2]] == list(outputs[:2])
    assert messages[2]["content"] != outputs[2]
    assert [result.metadata.get("projection") for result in results[:2]] == [None, None]
    assert results[2].metadata["projection"]["truncated"] is True
    assert persisted == [("call-2", outputs[2])]


def test_permission_denial_is_recoverable_tool_feedback(monkeypatch) -> None:
    """Rejecting one unnecessary operation must not block the whole run."""
    from nz_coder.foundation import config

    monkeypatch.setattr(config, "CONTINUE_LOOP_ON_DENY", False)
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: True,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
    )
    result = ToolExecutionResult(
        name="bash",
        tool_input={"command": "diff -rq /outside ."},
        output="Denied: path outside workspace",
        executed=False,
        dispatch_failed=True,
        command_failed=False,
        is_write=False,
        permission_denied=True,
    )

    state = ProductionToolResultProjector().consume(
        context,
        [(0, {"id": "denied", "function": {"name": "bash", "arguments": {}}}, result)],
        [],
    )

    assert state["all_succeeded"] is False
    assert state["blocked"] is False


def test_consecutive_doom_loop_guard_remains_terminal() -> None:
    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: True,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
    )
    result = ToolExecutionResult(
        name="list_directory",
        tool_input={"path": "."},
        output="Denied: Doom loop detected",
        executed=False,
        dispatch_failed=True,
        command_failed=False,
        is_write=False,
        permission_denied=True,
        metadata={"stall_kind": "consecutive"},
    )

    state = ProductionToolResultProjector().consume(
        context,
        [(0, {"id": "doom", "function": {"name": "list_directory", "arguments": {}}}, result)],
        [],
    )

    assert state["blocked"] is True


def test_legacy_strict_progress_metadata_cannot_stop_processor() -> None:
    """Stale count-gate metadata must not reactivate a retired terminal denial."""
    settled: list[dict] = []

    class Processor:
        def settle_tool(self, *_args, **kwargs) -> None:
            settled.append(kwargs)

    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: True,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
    )
    result = ToolExecutionResult(
        name="grep_search",
        tool_input={"pattern": "another-clue", "path": "src"},
        output="Denied: Final blocker — strict investigation budget remains exhausted",
        executed=False,
        dispatch_failed=True,
        command_failed=False,
        is_write=False,
        permission_denied=True,
        metadata={"strict_terminal_blocker": True},
    )

    state = ProductionToolResultProjector().consume(
        context,
        [(0, {"id": "strict", "function": {
            "name": "grep_search", "arguments": {},
        }}, result)],
        [],
        processor=Processor(),
    )

    assert state["blocked"] is False
    assert settled[0]["continue_on_deny"] is True


def test_projection_stamps_resource_and_mutation_generation() -> None:
    class State:
        mutation_generation = 3

    context = ToolProjectionContext(
        signal_from_metadata=lambda _metadata: None,
        record_result=lambda _result: False,
        trace_result=lambda _result, _output, **_kwargs: None,
        stall_orchestrator=None,
        after_result=lambda _messages, _result, _output: None,
        runtime_state=State(),
    )
    result = ToolExecutionResult(
        name="read_file",
        tool_input={"path": "src/app.py"},
        output="VALUE = 1",
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    )
    messages: list[dict] = []

    ProductionToolResultProjector().consume(
        context,
        [(0, {"id": "read", "function": {"name": "read_file", "arguments": {}}}, result)],
        messages,
    )

    assert messages[0]["_nz_evidence_kind"] == "file_read"
    assert messages[0]["_nz_resource"] == "src/app.py"
    assert messages[0]["_nz_mutation_generation"] == 3
