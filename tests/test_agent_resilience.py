"""Agent runtime resilience parity tests."""
from __future__ import annotations

import pytest


def test_tool_alias_repairs_only_unique_case_separator_match():
    from nz_coder.runtime.agent.agent_resilience import resolve_tool_name_alias

    assert resolve_tool_name_alias("Todo-Create", {"todo_create", "read_file"}) == "todo_create"
    assert resolve_tool_name_alias("read_file", {"read_file"}) is None
    assert resolve_tool_name_alias("red", {"read_file"}) is None
    assert resolve_tool_name_alias("a-b", {"a_b", "ab"}) is None


def test_tool_call_repair_is_immutable_and_reports_identity():
    from nz_coder.runtime.agent.agent_resilience import repair_tool_call_names

    original = [{
        "id": "call-1",
        "function": {"name": "Read-File", "arguments": "{}"},
    }]
    calls, repairs = repair_tool_call_names(original, {"read_file"})

    assert original[0]["function"]["name"] == "Read-File"
    assert calls[0]["function"]["name"] == "read_file"
    assert repairs == [{"from": "Read-File", "to": "read_file", "tool_call_id": "call-1"}]


def test_tool_call_id_repair_fills_missing_and_duplicate_ids_immutably():
    from nz_coder.runtime.agent.agent_resilience import repair_tool_call_ids

    original = [
        {"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}},
        {"id": "call-1", "function": {"name": "grep_search", "arguments": "{}"}},
        {"function": {"name": "repo_map", "arguments": "{}"}},
    ]
    generated = iter(("generated-2", "generated-3"))

    calls, repairs = repair_tool_call_ids(
        original,
        id_factory=lambda: next(generated),
    )

    assert [call.get("id") for call in original] == ["call-1", "call-1", None]
    assert [call["id"] for call in calls] == [
        "call-1", "generated-2", "generated-3",
    ]
    assert [repair["reason"] for repair in repairs] == ["duplicate", "missing"]


def test_tool_call_envelope_repair_preserves_protocol_and_error_identity():
    from nz_coder.runtime.agent.agent_resilience import (
        repair_tool_call_envelopes,
        repair_tool_call_ids,
    )

    original = [None, {"function": {"name": "read_file", "arguments": "{}"}}]
    calls, repairs = repair_tool_call_envelopes(original)
    calls, _id_repairs = repair_tool_call_ids(
        calls,
        id_factory=iter(("malformed-id", "read-id")).__next__,
    )

    assert original[0] is None
    assert calls[0]["id"] == "malformed-id"
    assert calls[0]["function"]["name"] == "_nz_malformed_tool_call"
    assert calls[0]["provider_extra"]["nz_malformed_tool_call"] is True
    assert calls[1]["id"] == "read-id"
    assert repairs == [{"index": 0, "original_type": "NoneType"}]


def test_repaired_malformed_tool_call_remains_model_visible_error(monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.agent.agent_resilience import repair_tool_call_envelopes
    from nz_coder.runtime.execution import tool_executor

    dispatched = []
    monkeypatch.setattr(
        tool_executor,
        "dispatch",
        lambda _name, _arguments: dispatched.append(True) or "unexpected",
    )
    calls, _repairs = repair_tool_call_envelopes([None])

    result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one(
        calls[0],
        0,
    )

    assert result.name == "unknown"
    assert result.dispatch_failed is True
    assert "malformed tool call" in result.output.lower()
    assert dispatched == []


def test_tool_result_error_cancel_and_code_classification():
    from nz_coder.runtime.agent.agent_resilience import (
        extract_structured_tool_error_code,
        is_cancelled_tool_result_content,
        is_tool_result_error_content,
    )

    assert is_tool_result_error_content("[Tool Error] edit: OLD_TEXT_NOT_FOUND: x")
    assert is_tool_result_error_content("Error: edit: OLD_TEXT_NOT_FOUND: x")
    assert is_tool_result_error_content("Denied by user")
    assert is_cancelled_tool_result_content("[Cancelled] user stopped")
    assert extract_structured_tool_error_code(
        "[Tool Error] edit: OLD_TEXT_NOT_FOUND: x"
    ) == "OLD_TEXT_NOT_FOUND"
    assert not is_tool_result_error_content("Command exited with code 1")


def test_tool_executor_projects_structured_error_metadata(monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor

    monkeypatch.setattr(
        tool_executor,
        "dispatch",
        lambda _name, _args: "Error: edit_file: OLD_TEXT_NOT_FOUND: missing",
    )
    result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "call-1",
        "function": {"name": "edit_file", "arguments": "{}"},
    }, 0)

    assert result.dispatch_failed is True
    assert result.metadata["error_code"] == "OLD_TEXT_NOT_FOUND"


def test_tool_executor_returns_implementation_exception_as_repair_evidence(monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor

    def explode(_name, _arguments):
        raise RuntimeError("fixture tool failed")

    monkeypatch.setattr(tool_executor, "dispatch", explode)
    monkeypatch.setattr(tool_executor, "is_transactional_write_tool", lambda _name: True)
    result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "call-1",
        "function": {"name": "write_file", "arguments": '{"path":"app.py"}'},
    }, 0)

    assert result.executed is True
    assert result.dispatch_failed is True
    assert result.is_write is True
    assert result.metadata["error_type"] == "RuntimeError"
    assert "fixture tool failed" in result.output


def test_tool_executor_returns_unrenderable_result_as_repair_evidence(monkeypatch):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor

    class Unrenderable:
        def __str__(self):
            raise ValueError("broken result")

    monkeypatch.setattr(tool_executor, "dispatch", lambda _name, _arguments: Unrenderable())
    result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "call-1",
        "function": {"name": "read_file", "arguments": "{}"},
    }, 0)

    assert result.dispatch_failed is True
    assert result.metadata["malformed_result"] is True
    assert "broken result" in result.output


@pytest.mark.parametrize("arguments", ["[]", '"scalar"', "null", [], 7])
def test_tool_executor_rejects_non_object_arguments_as_repair_evidence(
    arguments,
    monkeypatch,
):
    """Provider argument shape drift must not crash permission or dispatch."""
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor

    dispatched = []
    monkeypatch.setattr(
        tool_executor,
        "dispatch",
        lambda _name, _arguments: dispatched.append(True) or "unexpected",
    )

    result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "call-shape",
        "function": {"name": "read_file", "arguments": arguments},
    }, 0)

    assert result.executed is False
    assert result.dispatch_failed is True
    assert "arguments must be a JSON object" in result.output
    assert result.tool_input == {}
    assert dispatched == []


@pytest.mark.parametrize(
    "tool_call",
    [None, {}, {"function": "read_file"}, {"function": {}}, {
        "function": {"name": 7, "arguments": "{}"},
    }],
)
def test_tool_executor_rejects_malformed_call_envelope(tool_call, monkeypatch):
    """Incomplete provider call envelopes become repair evidence, not KeyError."""
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor

    dispatched = []
    monkeypatch.setattr(
        tool_executor,
        "dispatch",
        lambda _name, _arguments: dispatched.append(True) or "unexpected",
    )

    result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one(
        tool_call,
        0,
    )

    assert result.name == "unknown"
    assert result.executed is False
    assert result.dispatch_failed is True
    assert "malformed tool call" in result.output.lower()
    assert dispatched == []


def test_tool_executor_deduplicates_unchanged_text_reads(monkeypatch, tmp_path):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import ToolOutput

    target = tmp_path / "sample.py"
    target.write_text("first\n", encoding="utf-8")
    calls = []

    def fake_dispatch(name, arguments):
        calls.append((name, dict(arguments)))
        return ToolOutput(
            target.read_text(encoding="utf-8"),
            metadata={"encoding": "utf-8"},
        )

    monkeypatch.setattr(tool_executor, "dispatch", fake_dispatch)
    call = {
        "id": "call-read",
        "function": {"name": "read_file", "arguments": {"path": "sample.py"}},
    }
    with scoped_workdir(tmp_path):
        executor = tool_executor.ToolExecutor(PermissionManager("auto"))
        first = executor.execute_one(call, 0)
        second = executor.execute_one(call, 0)

    assert first.output == "first\n"
    assert len(calls) == 1
    assert second.executed is True
    assert second.dispatch_failed is False
    assert second.metadata["read_cache_hit"] is True
    assert "[Read Cache]" in second.output
    assert "refer to that instead of re-reading" in second.output


def test_tool_executor_read_cache_invalidates_on_file_change(monkeypatch, tmp_path):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import ToolOutput

    target = tmp_path / "sample.py"
    target.write_text("one\n", encoding="utf-8")
    calls = []

    def fake_dispatch(_name, _arguments):
        calls.append(target.read_text(encoding="utf-8"))
        return ToolOutput(calls[-1], metadata={"encoding": "utf-8"})

    monkeypatch.setattr(tool_executor, "dispatch", fake_dispatch)
    call = {
        "id": "call-read",
        "function": {"name": "read_file", "arguments": {"path": "sample.py"}},
    }
    with scoped_workdir(tmp_path):
        executor = tool_executor.ToolExecutor(PermissionManager("auto"))
        first = executor.execute_one(call, 0)
        target.write_text("two is longer\n", encoding="utf-8")
        second = executor.execute_one(call, 0)

    assert first.output == "one\n"
    assert second.output == "two is longer\n"
    assert len(calls) == 2
    assert "read_cache_hit" not in second.metadata


def test_tool_executor_read_cache_can_be_cleared_after_compaction(monkeypatch, tmp_path):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import ToolOutput

    target = tmp_path / "sample.py"
    target.write_text("content\n", encoding="utf-8")
    calls = []

    def fake_dispatch(_name, _arguments):
        calls.append(1)
        return ToolOutput("content\n", metadata={"encoding": "utf-8"})

    monkeypatch.setattr(tool_executor, "dispatch", fake_dispatch)
    call = {
        "id": "call-read",
        "function": {"name": "read_file", "arguments": {"path": "sample.py"}},
    }
    with scoped_workdir(tmp_path):
        executor = tool_executor.ToolExecutor(PermissionManager("auto"))
        executor.execute_one(call, 0)
        executor.clear_read_cache()
        executor.execute_one(call, 0)

    assert len(calls) == 2


def test_tool_executor_read_cache_has_runtime_killswitch(monkeypatch, tmp_path):
    from nz_coder.foundation import config
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools import ToolOutput

    target = tmp_path / "sample.py"
    target.write_text("content\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(config, "READ_DEDUP_ENABLED", False)

    def fake_dispatch(_name, _arguments):
        calls.append(1)
        return ToolOutput("content\n", metadata={"encoding": "utf-8"})

    monkeypatch.setattr(tool_executor, "dispatch", fake_dispatch)
    call = {
        "id": "call-read",
        "function": {"name": "read_file", "arguments": {"path": "sample.py"}},
    }
    with scoped_workdir(tmp_path):
        executor = tool_executor.ToolExecutor(PermissionManager("auto"))
        executor.execute_one(call, 0)
        executor.execute_one(call, 0)

    assert len(calls) == 2


def test_retry_description_classifies_common_provider_failures():
    from nz_coder.runtime.agent.agent_resilience import describe_transient_provider_retry

    assert describe_transient_provider_retry(RuntimeError("stream incomplete")) == "Stream interrupted before completion"
    assert describe_transient_provider_retry(RuntimeError("socket hang up")) == "Provider connection error"
    assert describe_transient_provider_retry(RuntimeError("request timeout")) == "Provider request timed out"
    assert describe_transient_provider_retry(RuntimeError("aborted")) == "Provider stream aborted"
    assert describe_transient_provider_retry(
        ConnectionResetError("peer disconnected")
    ) == "Provider connection error"


def test_terminal_promise_signal_extracts_machine_outcome_and_reason():
    from nz_coder.runtime.agent.agent_resilience import extract_terminal_promise_signal

    assert extract_terminal_promise_signal(
        "done <promise>complete:tests passed</promise>"
    ) == ("COMPLETE", "tests passed")
    assert extract_terminal_promise_signal("plain text") == (None, None)


def test_main_agent_repairs_tool_name_before_history_and_dispatch():
    from nz_coder.loop import AgentLoop
    from tests.test_loop_fake import (
        FakeClient,
        FakeMessage,
        FakeResponse,
        FakeToolCall,
        _restore_workdir,
        _run_agent,
        _tmp_workdir,
    )

    old, tmp = _tmp_workdir()
    try:
        (tmp / "a.txt").write_text("hello", encoding="utf-8")
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("Read-File", {"path": "a.txt"}),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "read a.txt"}]
        agent = AgentLoop(
            "test", permission_mode="auto", client=fake, trace_enabled=False
        )
        _run_agent(agent, messages, stream=False)

        assistant = next(
            item for item in messages
            if item.get("role") == "assistant" and item.get("tool_calls")
        )
        assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
        assert any(
            item.get("role") == "tool" and "hello" in item.get("content", "")
            for item in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_subagent_repairs_tool_name_before_dispatch(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from tests.test_subagent import (
        FakeClient,
        FakeMessage,
        FakeResponse,
        FakeToolCall,
        _restore_workdir,
        _tmp_workdir,
    )

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 3
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        (tmp / "a.txt").write_text("hello", encoding="utf-8")
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("Read-File", {"path": "a.txt"}),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **_kwargs: fake)

        result = subagent.run_subagent("read a.txt", agent_type="explore")

        assert result.metadata["child_result"]["status"] == "completed"
        assert len(fake.chat.completions.requests) == 2
        second_messages = fake.chat.completions.requests[1]["messages"]
        assert any(
            item.get("role") == "tool" and "hello" in item.get("content", "")
            for item in second_messages
        )
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_tool_executor_uses_structured_bash_exit_metadata(monkeypatch):
    """G4: rendered text cannot hide or invent command failure."""
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.tools import ToolOutput

    monkeypatch.setattr(
        tool_executor,
        "dispatch",
        lambda _name, _args: ToolOutput("last output line", metadata={"exit": 3}),
    )
    failed = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "call-pipefail",
        "function": {"name": "bash", "arguments": {"command": "pytest | tail"}},
    }, 0)
    assert failed.command_failed is True

    monkeypatch.setattr(
        tool_executor,
        "dispatch",
        lambda _name, _args: ToolOutput(
            "Command exited with code 9", metadata={"exit": 0},
        ),
    )
    passed = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
        "id": "call-success",
        "function": {"name": "bash", "arguments": {"command": "echo ok"}},
    }, 0)
    assert passed.command_failed is False


def test_tool_executor_treats_invalid_structured_bash_exit_as_unknown():
    """Corrupt dynamic-tool metadata must not crash command classification."""
    from nz_coder.runtime.execution.tool_executor import command_failed_from_result

    assert command_failed_from_result(
        "bash",
        "output",
        {"exit": float("inf")},
        structured=True,
    ) is False
