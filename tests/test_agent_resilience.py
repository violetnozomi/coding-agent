"""Agent runtime resilience parity tests."""
from __future__ import annotations


def test_tool_alias_repairs_only_unique_case_separator_match():
    from nz_coder.runtime.agent_resilience import resolve_tool_name_alias

    assert resolve_tool_name_alias("Todo-Create", {"todo_create", "read_file"}) == "todo_create"
    assert resolve_tool_name_alias("read_file", {"read_file"}) is None
    assert resolve_tool_name_alias("red", {"read_file"}) is None
    assert resolve_tool_name_alias("a-b", {"a_b", "ab"}) is None


def test_tool_call_repair_is_immutable_and_reports_identity():
    from nz_coder.runtime.agent_resilience import repair_tool_call_names

    original = [{
        "id": "call-1",
        "function": {"name": "Read-File", "arguments": "{}"},
    }]
    calls, repairs = repair_tool_call_names(original, {"read_file"})

    assert original[0]["function"]["name"] == "Read-File"
    assert calls[0]["function"]["name"] == "read_file"
    assert repairs == [{"from": "Read-File", "to": "read_file", "tool_call_id": "call-1"}]


def test_tool_result_error_cancel_and_code_classification():
    from nz_coder.runtime.agent_resilience import (
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
    from nz_coder.runtime import tool_executor

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
    from nz_coder.runtime import tool_executor

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
    from nz_coder.runtime import tool_executor

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


def test_retry_description_classifies_common_provider_failures():
    from nz_coder.runtime.agent_resilience import describe_transient_provider_retry

    assert describe_transient_provider_retry(RuntimeError("stream incomplete")) == "Stream interrupted before completion"
    assert describe_transient_provider_retry(RuntimeError("socket hang up")) == "Provider connection error"
    assert describe_transient_provider_retry(RuntimeError("request timeout")) == "Provider request timed out"
    assert describe_transient_provider_retry(RuntimeError("aborted")) == "Provider stream aborted"


def test_terminal_promise_signal_extracts_machine_outcome_and_reason():
    from nz_coder.runtime.agent_resilience import extract_terminal_promise_signal

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
    from nz_coder import config, subagent
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
