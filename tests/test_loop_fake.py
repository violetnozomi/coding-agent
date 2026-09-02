"""Agent loop tests with a fake OpenAI-compatible client."""

import asyncio

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest


_trust_env_stack: dict[str, str | None] = {}


class FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, name: str, arguments, call_id: str = "call_1"):
        self.id = call_id
        self.type = "function"
        raw_args = arguments if isinstance(arguments, str) else json.dumps(arguments)
        self.function = FakeFunction(name, raw_args)

    def model_dump(self):
        return {
            "id": self.id,
            "type": self.type,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


class FakeMessage:
    def __init__(self, content: str = "", tool_calls=None, reasoning_content: str = None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.reasoning_content = reasoning_content


class FakeChoice:
    def __init__(self, message, finish_reason=None):
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    def __init__(self, message, *, finish_reason=None, usage=None):
        self.choices = [FakeChoice(message, finish_reason)]
        self.usage = usage


class FakeCompletions:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0
        self.requests = []

    def create(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if not self.items:
            return FakeResponse(FakeMessage("done"))
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, items):
        self.chat = FakeChat(FakeCompletions(items))


def test_parallel_tool_diagnostic_is_appended_after_every_tool_result():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.execution.tool_executor import ToolExecutionResult

    tool_calls = [
        {
            "id": "call-error",
            "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        },
        {
            "id": "call-read",
            "type": "function",
            "function": {"name": "grep_search", "arguments": "{}"},
        },
    ]
    messages = [{"role": "assistant", "content": "", "tool_calls": tool_calls}]
    dispatched = [
        (
            0,
            tool_calls[0],
            ToolExecutionResult(
                name="bash",
                tool_input={"command": "cd repo"},
                output="Error: command is not allowed",
                executed=True,
                dispatch_failed=True,
                command_failed=False,
                is_write=False,
            ),
        ),
        (
            1,
            tool_calls[1],
            ToolExecutionResult(
                name="grep_search",
                tool_input={"pattern": "symbol"},
                output="Found one match",
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
            ),
        ),
    ]
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=FakeClient([]),
        trace_enabled=False,
    )

    agent._consume_dispatched_tools(dispatched, messages)

    assert [message["role"] for message in messages] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert [message.get("tool_call_id") for message in messages[1:3]] == [
        "call-error",
        "call-read",
    ]


def _run_agent(agent, *args, **kwargs):
    return asyncio.run(agent.run(*args, **kwargs))


def _tmp_workdir():
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    old = config.WORKDIR
    tmp = Path(tempfile.mkdtemp())
    trust_path = tmp.parent / f".{tmp.name}-nz-trust.json"
    _trust_env_stack[str(tmp)] = os.environ.get("NZ_CODER_WORKSPACE_TRUST_STORE")
    os.environ["NZ_CODER_WORKSPACE_TRUST_STORE"] = str(trust_path)
    snapshot = load_config_snapshot(tmp)
    WorkspaceTrustStore(trust_path).trust(
        tmp,
        "workspace-config",
        snapshot.workspace_fingerprint,
    )
    config.WORKDIR = tmp
    return old, tmp


def _restore_workdir(old, tmp):
    from nz_coder.foundation import config

    config.WORKDIR = old
    shutil.rmtree(str(tmp), ignore_errors=True)
    trust_path = tmp.parent / f".{tmp.name}-nz-trust.json"
    trust_path.unlink(missing_ok=True)
    previous = _trust_env_stack.pop(str(tmp), None)
    if previous is None:
        os.environ.pop("NZ_CODER_WORKSPACE_TRUST_STORE", None)
    else:
        os.environ["NZ_CODER_WORKSPACE_TRUST_STORE"] = previous


def test_loop_executes_tool_then_final_response():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"}),
                FakeToolCall("bash", {"command": "test -f hello.txt"}, call_id="call_2"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write a file"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        assert (tmp / "hello.txt").read_text(encoding="utf-8") == "hello"
        assert messages[0]["_nz_user_agent"] == "build"
        assert messages[0]["_nz_user_model"]["provider_id"] == agent.provider_id
        assert messages[0]["_nz_user_model"]["model_id"] == agent.model_id
        assert messages[0]["_nz_time"]["created"] >= 0
        assert any(m.get("role") == "tool" and "Created hello.txt" in m.get("content", "") for m in messages)
        first_assistant = next(m for m in messages if m.get("role") == "assistant")
        assert first_assistant["_nz_mode"] == "build"
        assert first_assistant["_nz_agent"] == "build"
        assert first_assistant["_nz_path"] == {
            "cwd": str(tmp),
            "root": str(tmp),
        }
        parts = first_assistant["_nz_parts"]
        assert parts[0]["type"] == "step-start"
        tool_parts = [part for part in parts if part["type"] == "tool"]
        assert [part["state"]["status"] for part in tool_parts] == ["completed", "completed"]
        assert {part["call_id"] for part in tool_parts} == {"call_1", "call_2"}
        finish = next(part for part in parts if part["type"] == "step-finish")
        assert finish["reason"] == "tool-calls"
        patch = next(part for part in parts if part["type"] == "patch")
        assert patch["hash"] == parts[0]["snapshot"]
        assert patch["files"] == ["hello.txt"]
        turn_diffs = messages[0]["_nz_summary"]["diffs"]
        assert turn_diffs == [{
            "file": "hello.txt",
            "additions": 1,
            "deletions": 0,
            "status": "added",
        }]
        assert first_assistant["_nz_session_summary"]["additions"] == 1
        assert first_assistant["_nz_session_summary"]["files"] == 1
        assistants = [item for item in messages if item.get("role") == "assistant"]
        assert "_nz_end_state" not in assistants[0]
        assert assistants[-1]["_nz_end_state"] == {"reason": "completed"}
        assert fake.chat.completions.calls == 2
    finally:
        _restore_workdir(old, tmp)


def test_strict_progress_gate_does_not_block_investigation_calls_at_limit():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        agent.runtime_state.investigation_calls_since_edit = 20
        calls = [
            FakeToolCall("grep_search", {"pattern": "token"}, call_id="read").model_dump(),
            FakeToolCall(
                "edit_file",
                {"path": "app.py", "old_string": "a", "new_string": "b"},
                call_id="write",
            ).model_dump(),
        ]

        with scoped_runtime_overrides(strict_local_tools=True):
            blocked = agent._strict_progress_rejections(calls)
        with scoped_runtime_overrides(strict_local_tools=False):
            ordinary = agent._strict_progress_rejections(calls)

        assert blocked == {}
        assert ordinary == {}
    finally:
        _restore_workdir(old, tmp)


def test_strict_progress_gate_allows_reads_crossing_limit_in_same_batch():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        agent.runtime_state.investigation_calls_since_edit = 18
        calls = [
            FakeToolCall(
                "grep_search", {"pattern": f"token-{index}"}, call_id=f"read-{index}"
            ).model_dump()
            for index in range(3)
        ]

        with scoped_runtime_overrides(strict_local_tools=True):
            blocked = agent._strict_progress_rejections(calls)

        assert blocked == {}
    finally:
        _restore_workdir(old, tmp)


def test_strict_progress_gate_never_turns_repeated_reads_into_terminal_blocker():
    """Read pressure remains advisory even when the same call is reconsidered."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        agent.runtime_state.investigation_calls_since_edit = 20
        call = [FakeToolCall(
            "grep_search",
            {"pattern": "same-budget-exhausted-search"},
            call_id="read",
        ).model_dump()]

        with scoped_runtime_overrides(strict_local_tools=True):
            first = agent._strict_progress_rejections(call)
            second = agent._strict_progress_rejections(call)

        assert first == {}
        assert second == {}
    finally:
        _restore_workdir(old, tmp)


def test_strict_progress_soft_boundary_allows_reads_beyond_retired_hard_limit():
    """The real strict loop keeps executing reads after the advisory nudge."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        (tmp / "tests").mkdir()
        tokens = " ".join(f"token_{index}" for index in range(10))
        (tmp / "app.py").write_text(f"value = '{tokens}'\n", encoding="utf-8")
        (tmp / "tests" / "test_app.py").write_text(
            "def test_value():\n    assert True\n",
            encoding="utf-8",
        )
        localized_calls = [
            FakeToolCall("read_file", {"path": "app.py"}, call_id="source"),
            FakeToolCall(
                "read_file",
                {"path": "tests/test_app.py"},
                call_id="test",
            ),
            *[
                FakeToolCall(
                    "grep_search",
                    {"pattern": f"token_{index}", "path": "app.py"},
                    call_id=f"grep-{index}",
                )
                for index in range(10)
            ],
        ]
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=localized_calls)),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "grep_search", {"pattern": f"late-clue-{index}", "path": "app.py"},
                    call_id=f"late-{index}",
                )
                for index in range(8)
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "grep_search",
                    {"pattern": "beyond-old-limit-one", "path": "app.py"},
                    call_id="beyond-one",
                ),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "grep_search",
                    {"pattern": "beyond-old-limit-two", "path": "app.py"},
                    call_id="beyond-two",
                ),
            ])),
            FakeResponse(FakeMessage("continued after soft convergence")),
        ])
        messages = [{"role": "user", "content": "Fix the bug in app.py"}]
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            trace_enabled=False,
        )

        with scoped_runtime_overrides(strict_local_tools=True):
            status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 5
        assert agent.runtime_state.investigation_calls_since_edit == 22
        assert "Final blocker" not in next(
            message["content"]
            for message in messages
            if message.get("tool_call_id") == "beyond-two"
        )
    finally:
        _restore_workdir(old, tmp)


def test_strict_successful_changed_file_verification_ends_without_another_model_turn():
    import subprocess

    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        subprocess.run(["git", "init", "--quiet"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@localhost"], cwd=tmp, check=True
        )
        (tmp / "app.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=tmp, check=True)

        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "edit_file",
                    {"path": "app.py", "old_text": "value = 1\n", "new_text": "value = 2\n"},
                    call_id="edit",
                ),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("diff_status", {}, call_id="diff"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("verify_changed_files", {}, call_id="verify"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("read_file", {"path": "app.py"}, call_id="extra-read"),
            ])),
        ])
        messages = [{"role": "user", "content": "Fix app.py"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        with scoped_runtime_overrides(strict_local_tools=True):
            status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 3
        assert not any(message.get("tool_call_id") == "extra-read" for message in messages)
    finally:
        _restore_workdir(old, tmp)


def test_strict_verify_before_diff_ends_without_another_model_turn():
    """The terminal consumer uses generation facts, not tool-call order."""
    import subprocess

    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        subprocess.run(["git", "init", "--quiet"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp, check=True)
        subprocess.run(["git", "config", "user.email", "test@localhost"], cwd=tmp, check=True)
        (tmp / "app.py").write_text("value = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=tmp, check=True)
        subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=tmp, check=True)

        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall(
                "edit_file",
                {"path": "app.py", "old_text": "value = 1\n", "new_text": "value = 2\n"},
                call_id="edit",
            )])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("verify_changed_files", {}, call_id="verify"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("diff_status", {}, call_id="diff"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("read_file", {"path": "app.py"}, call_id="extra-read"),
            ])),
        ])
        messages = [{"role": "user", "content": "Fix app.py"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        with scoped_runtime_overrides(strict_local_tools=True):
            status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 3
        assert not any(message.get("tool_call_id") == "extra-read" for message in messages)
    finally:
        _restore_workdir(old, tmp)


def test_verification_terminal_requires_strict_successful_source_diff():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides
    from nz_coder.runtime.execution.tool_executor import ToolExecutionResult

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )

        def result(output):
            return ToolExecutionResult(
                name="verify_changed_files",
                tool_input={},
                output=output,
                executed=True,
                dispatch_failed=False,
                command_failed=False,
                is_write=False,
            )

        agent.runtime_state.has_diff = True
        agent.runtime_state.source_only = False
        with scoped_runtime_overrides(strict_local_tools=True):
            assert agent._strict_verification_completed([(0, {}, result("OK: no source"))]) is False

        agent.runtime_state.source_only = True
        with scoped_runtime_overrides(strict_local_tools=True):
            assert agent._strict_verification_completed([(0, {}, result("FAIL: broken"))]) is False
        with scoped_runtime_overrides(strict_local_tools=False):
            assert agent._strict_verification_completed([(0, {}, result("OK: passed"))]) is False

        agent.runtime_state.mutation_generation = 1
        agent.runtime_state.diff_generation = 1
        agent.runtime_state.verification_generation = 1
        agent.vm._needed = True
        agent.vm._state = __import__(
            "nz_coder.runtime.verification.evidence", fromlist=["VerificationState"]
        ).VerificationState.BLOCKED_ENVIRONMENT
        with scoped_runtime_overrides(strict_local_tools=True):
            assert agent._strict_verification_completed([(0, {}, result("OK: passed"))]) is False
    finally:
        _restore_workdir(old, tmp)


def test_loop_forwards_model_acceptance_contract_to_verification_manager():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.execution.tool_executor import ToolExecutionResult

    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=FakeClient([]),
        trace_enabled=False,
    )
    agent.runtime_state.set_acceptance_criteria_from_text(
        "完成后运行 python -m pytest -q cron_engine/tests。"
    )
    agent.runtime_state.observe_tool(
        "edit_file", {"path": "cron_engine/parser.py"}, "Done"
    )
    agent.runtime_state.has_diff = True
    observed = []
    agent.vm.observe_acceptance_contract = (
        lambda command, output, *, passed: observed.append(
            (command, output, passed)
        )
    )

    agent._observe_runtime_tool(ToolExecutionResult(
        name="bash",
        tool_input={"command": "python -m pytest -q cron_engine/tests"},
        output="59 passed",
        executed=True,
        dispatch_failed=False,
        command_failed=False,
        is_write=False,
    ))

    assert observed == [
        ("python -m pytest -q cron_engine/tests", "59 passed", True)
    ]


def test_loop_forwards_trusted_bash_exit_code_to_verification_manager():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.execution.tool_executor import ToolExecutionResult

    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=FakeClient([]),
        trace_enabled=False,
    )
    observed = []
    agent.vm.observe_bash = (
        lambda tool_input, output, dispatch_failed, command_failed, *, exit_code:
        observed.append((tool_input, output, dispatch_failed, command_failed, exit_code))
    )
    result = ToolExecutionResult(
        name="bash",
        tool_input={"command": "pytest tests/test_api.py::test_retry"},
        output="Command exited with code 1\n1 failed",
        executed=True,
        dispatch_failed=False,
        command_failed=True,
        is_write=False,
        metadata={"exit": 1},
    )

    try:
        agent._observe_verification_tool(result)
    finally:
        agent.close()

    assert observed == [(
        {"command": "pytest tests/test_api.py::test_retry"},
        "Command exited with code 1\n1 failed",
        False,
        True,
        1,
    )]


def test_queued_followup_stops_before_next_provider_step():
    """A newer terminal prompt takes over only after the current tool step settles."""
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "settled.txt", "content": "done"}),
            ])),
            FakeResponse(FakeMessage("obsolete second step")),
        ])
        messages = [{"role": "user", "content": "start the first task"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.set_followup_pending(lambda: fake.chat.completions.calls >= 1)

        result = _run_agent(agent, messages, stream=False)

        assert result["status"] == "interrupted"
        assert fake.chat.completions.calls == 1
        assert (tmp / "settled.txt").read_text(encoding="utf-8") == "done"
        assistants = [
            item for item in messages if item.get("role") == "assistant"
        ]
        assert assistants[0].get("_nz_end_state") is None
        assert assistants[-1]["_nz_end_state"] == {"reason": "interrupted"}
        assert not assistants[-1].get("tool_calls")
        tool_part = next(
            part for part in assistants[0]["_nz_parts"] if part["type"] == "tool"
        )
        assert tool_part["state"]["status"] == "completed"
    finally:
        _restore_workdir(old, tmp)


def test_identical_step_snapshots_skip_empty_session_patch_rebuild(monkeypatch):
    """Read-only steps must not rescan the full Session diff for an empty patch."""
    from nz_coder.loop import AgentLoop
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.session.session_processor import SessionProcessor

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
        message = {"role": "assistant", "content": ""}
        attach_message_identity(message, session_id=agent.session_id)
        processor = SessionProcessor(message)
        processor.start_step(snapshot="same-snapshot")
        monkeypatch.setattr(
            agent.workspace_snapshots,
            "changed_files",
            lambda *_args: (_ for _ in ()).throw(AssertionError("empty patch was rebuilt")),
        )
        monkeypatch.setattr(
            agent,
            "_refresh_snapshot_summaries",
            lambda *_args: (_ for _ in ()).throw(AssertionError("summaries were rebuilt")),
        )

        agent._record_step_patch([], processor, "same-snapshot")

        assert not any(part.get("type") == "patch" for part in message.get("_nz_parts", []))
    finally:
        _restore_workdir(old, tmp)


def test_output_limit_reasoning_only_persists_warning_and_usage():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.session.session_processor import REASONING_LENGTH_WARNING

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(
                FakeMessage(reasoning_content="unfinished reasoning"),
                finish_reason="length",
                usage={
                    "prompt_tokens": 40,
                    "completion_tokens": 10,
                    "total_tokens": 50,
                    "prompt_tokens_details": {"cached_tokens": 15},
                    "completion_tokens_details": {"reasoning_tokens": 7},
                    "cache_write_input_tokens": 2,
                },
            ),
            FakeResponse(FakeMessage("final visible answer")),
        ])
        messages = [{"role": "user", "content": "solve"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = _run_agent(agent, messages, stream=False)

        assistant = next(item for item in messages if item.get("role") == "assistant")
        warning = next(
            part
            for part in assistant["_nz_parts"]
            if part.get("type") == "text" and part.get("ignored") is True
        )
        finish = next(part for part in assistant["_nz_parts"] if part["type"] == "step-finish")
        assert result["status"] == "completed"
        assert result["content"] == "final visible answer"
        assert warning["text"] == REASONING_LENGTH_WARNING
        assert finish["reason"] == "length"
        assert finish["tokens"] == {
            "input": 23,
            "output": 3,
            "total": 50,
            "reasoning": 7,
            "cache": {"read": 15, "write": 2},
        }
        assert assistant["_nz_usage"] == {
            "input": 23,
            "output": 3,
            "total": 50,
            "reasoning": 7,
            "cache_read": 15,
            "cache_write": 2,
        }
        assert fake.chat.completions.calls == 2
        assert any(
            message.get("_nz_output_limit_continuation") is True
            for message in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_authoritative_registry_pricing_persists_assistant_and_step_cost():
    from nz_coder.loop import AgentLoop
    from nz_coder.protocol.message_schema import message_records
    from nz_coder.providers.registry import ModelPricing

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([FakeResponse(
            FakeMessage("done"),
            finish_reason="stop",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        )])
        messages = [{"role": "user", "content": "solve"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.model_pricing = ModelPricing(
            input=1.0,
            output=4.0,
            cache_read=0.1,
        )

        result = _run_agent(agent, messages, stream=False)

        assistant = next(item for item in messages if item.get("role") == "assistant")
        finish = next(
            part for part in assistant["_nz_parts"] if part["type"] == "step-finish"
        )
        record = message_records(messages, agent.session_id)[1]
        assert result["status"] == "completed"
        assert assistant["_nz_usage"] == {
            "input": 80,
            "output": 30,
            "total": 140,
            "reasoning": 10,
            "cache_read": 20,
        }
        assert finish["cost"] == pytest.approx(0.000242)
        assert record["info"]["cost"] == pytest.approx(0.000242)
        assert record["info"]["provider_id"] == agent.provider.name
        assert record["info"]["model_id"] == agent.model_id
        assert record["info"]["parent_id"] == message_records(
            messages, agent.session_id
        )[0]["info"]["id"]
        assert record["info"]["time"]["completed"] >= record["info"]["time"]["created"]
        assert record["info"]["tokens"] == {
            "input": 80,
            "output": 30,
            "total": 140,
            "reasoning": 10,
            "cache": {"read": 20, "write": 0},
        }
    finally:
        _restore_workdir(old, tmp)


def test_provider_reported_cost_takes_priority_over_registry_estimate():
    from nz_coder.loop import AgentLoop
    from nz_coder.protocol.message_schema import message_records
    from nz_coder.providers.registry import ModelPricing

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([FakeResponse(
            FakeMessage("done"),
            finish_reason="stop",
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "total_tokens": 140,
                "cost": 0.75,
            },
        )])
        messages = [{"role": "user", "content": "solve"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.model_pricing = ModelPricing(input=1.0, output=4.0)

        _run_agent(agent, messages, stream=False)

        record = message_records(messages, agent.session_id)[1]
        finish = next(part for part in record["parts"] if part["type"] == "step-finish")
        assert record["info"]["cost"] == 0.75
        assert finish["cost"] == 0.75
    finally:
        _restore_workdir(old, tmp)


def test_foreground_task_cost_is_merged_into_parent_assistant(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent
    from nz_coder.loop import AgentLoop
    from nz_coder.protocol.message_schema import message_records

    class ChildMessage:
        content = "child done"
        tool_calls = []

        def model_dump(self):
            return {"role": "assistant", "content": self.content, "tool_calls": []}

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        child = FakeClient([FakeResponse(
            ChildMessage(),
            usage={
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "total_tokens": 25,
                "cost": 0.25,
            },
        )])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: child)
        parent = FakeClient([
            FakeResponse(
                FakeMessage(tool_calls=[FakeToolCall(
                    "task",
                    {"prompt": "inspect", "agent_type": "explore"},
                )]),
                finish_reason="tool_calls",
                usage={
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "cost": 0.10,
                },
            ),
            FakeResponse(
                FakeMessage("done"),
                finish_reason="stop",
                usage={
                    "prompt_tokens": 50,
                    "completion_tokens": 5,
                    "total_tokens": 55,
                    "cost": 0.20,
                },
            ),
        ])
        messages = [{"role": "user", "content": "solve"}]
        agent = AgentLoop("test", permission_mode="auto", client=parent, trace_enabled=False)

        result = _run_agent(agent, messages, stream=False)

        assistants = [
            record
            for record in message_records(messages, agent.session_id)
            if record["info"].get("role") == "assistant"
        ]
        first_finish = next(
            part for part in assistants[0]["parts"] if part["type"] == "step-finish"
        )
        assert result["status"] == "completed"
        assert assistants[0]["info"]["cost"] == pytest.approx(0.35)
        assert first_finish["cost"] == pytest.approx(0.10)
        assert assistants[1]["info"]["cost"] == pytest.approx(0.20)
    finally:
        subagent.set_parent_session(None)
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_output_limit_does_not_execute_incomplete_tool_call():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.session.session_processor import OUTPUT_LENGTH_WARNING

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(
                FakeMessage(tool_calls=[
                    FakeToolCall(
                        "write_file",
                        {"path": "partial.txt", "content": "bad"},
                    ),
                ]),
                finish_reason="length",
            ),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "write_file",
                    {"path": "partial.txt", "content": "good"},
                ),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = _run_agent(agent, messages, stream=False)

        assistant = next(item for item in messages if item.get("role") == "assistant")
        warning = next(
            part for part in assistant["_nz_parts"]
            if part.get("ignored") is True
        )
        tool = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
        assert result["status"] == "completed"
        assert warning["text"] == OUTPUT_LENGTH_WARNING
        assert tool["state"]["status"] == "error"
        assert "was not executed" in tool["state"]["error"]
        repair_result = next(
            message
            for message in messages
            if message.get("role") == "tool"
            and message.get("_nz_output_limit_repair") is True
        )
        assert repair_result["tool_call_id"] == "call_1"
        assert "not executed" in repair_result["content"]
        assert (tmp / "partial.txt").read_text(encoding="utf-8") == "good"
        assert fake.chat.completions.calls == 3
    finally:
        _restore_workdir(old, tmp)


def test_provider_finish_error_is_not_reported_as_success():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("partial"), finish_reason="error"),
        ])
        messages = [{"role": "user", "content": "answer"}]
        notices = []
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = _run_agent(agent, messages, stream=False, on_text=notices.append)

        assistant = next(item for item in messages if item.get("role") == "assistant")
        finish = next(part for part in assistant["_nz_parts"] if part["type"] == "step-finish")
        assert result["status"] == "error"
        assert finish["reason"] == "error"
        assert "provider ended the response with an error" in assistant["_nz_error"]
        assert notices == [f"partial\n\n{assistant['_nz_error']}"]
        assert fake.chat.completions.calls == 1
    finally:
        _restore_workdir(old, tmp)


def test_exhausted_provider_error_keeps_safe_status_and_identity(monkeypatch):
    from nz_coder.loop import AgentLoop
    from nz_coder.protocol.message_schema import message_records

    class RateLimitFailure(Exception):
        status_code = 429
        code = "rate_limit"
        headers = {"retry-after": "4"}
        body = {"error": "quota"}

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([RateLimitFailure("slow down")])
        messages = [{"role": "user", "content": "answer"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        monkeypatch.setattr(agent, "_handle_api_error", lambda _error: False)

        result = _run_agent(agent, messages, stream=False)

        assistant = next(item for item in messages if item.get("role") == "assistant")
        info = message_records([assistant], agent.session_id)[0]["info"]
        assert result["status"] == "aborted"
        assert info["finish"] == "error"
        assert info["error"] == {
            "name": "APIError",
            "data": {
                "message": "The provider request failed.",
                "isRetryable": False,
                "statusCode": 429,
                "public_error": {
                    "schema": "nz.public_error.v1",
                    "code": "provider_error",
                    "message": "The provider request failed.",
                    "retryable": False,
                    "metadata": {
                        "error_type": "RateLimitFailure",
                        "status_code": 429,
                    },
                },
            },
        }
        assert "slow down" not in str(info)
        assert "quota" not in str(info)
        assert assistant["_nz_error"] == "The provider request failed."
    finally:
        _restore_workdir(old, tmp)


def test_empty_tool_calls_finish_cannot_leave_false_tool_terminal_state():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("done"), finish_reason="tool_calls"),
        ])
        messages = [{"role": "user", "content": "answer"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = _run_agent(agent, messages, stream=False)

        assistant = next(item for item in messages if item.get("role") == "assistant")
        finish = next(part for part in assistant["_nz_parts"] if part["type"] == "step-finish")
        assert result["status"] == "completed"
        assert finish["reason"] == "stop"
        assert not any(part["type"] == "tool" for part in assistant["_nz_parts"])
        assert fake.chat.completions.calls == 1
    finally:
        _restore_workdir(old, tmp)


def test_last_step_keeps_narrow_repair_available_at_emergency_hard_cap():
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", {"path": ".", "depth": 1}),
            ])),
            FakeResponse(FakeMessage(
                "The maximum step count was reached. I inspected the workspace; "
                "no implementation tasks remain. Continue with targeted review next."
            )),
        ])
        messages = [{"role": "user", "content": "Inspect and summarize this workspace."}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        with scoped_runtime_overrides(max_agent_turns=2):
            result = _run_agent(agent, messages, stream=False)

        final_request = fake.chat.completions.requests[-1]
        assert final_request["messages"][-1]["role"] == "assistant"
        assert "CRITICAL - EMERGENCY HARD-CAP CALL" in final_request["messages"][-1]["content"]
        assert "one already-identified local repair remains" in final_request["messages"][-1]["content"]
        assert final_request.get("tools")
        assert result["status"] == "max_turns"
        assert not any(
            part.get("type") == "patch"
            for message in messages
            for part in message.get("_nz_parts", [])
        )
    finally:
        _restore_workdir(old, tmp)


def test_loop_binds_question_asker_to_tool_call():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        questions = [{
            "header": "Scope",
            "question": "Which scope should be changed?",
            "options": [
                {"label": "Current module", "description": "Keep the change narrow."},
                {"label": "Whole project", "description": "Apply the change everywhere."},
            ],
        }]
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("question", {"questions": questions}),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "make the requested change"}]
        seen = []
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            trace_enabled=False,
            question_asker=lambda payload: seen.append(payload) or [["Current module"]],
        )

        result = _run_agent(agent, messages, stream=False)

        tool_result = next(message for message in messages if message.get("role") == "tool")
        tool_part = next(
            part
            for message in messages
            for part in message.get("_nz_parts", [])
            if part.get("type") == "tool" and part.get("tool") == "question"
        )
        question_part = next(
            part
            for message in messages
            for part in message.get("_nz_parts", [])
            if part.get("type") == "question"
        )
        summary_part = next(
            part
            for message in messages
            for part in message.get("_nz_parts", [])
            if part.get("type") == "question-summary"
        )
        assert result["status"] == "completed"
        assert seen[0][0]["header"] == "Scope"
        assert '"Current module"' in tool_result["content"]
        assert tool_part["state"]["title"] == "Asked 1 question"
        assert tool_part["state"]["metadata"] == {"answers": [["Current module"]]}
        assert question_part["status"] == "completed"
        assert question_part["request_id"].startswith("question-")
        assert question_part["questions"][0]["custom"] is True
        assert question_part["response"] == {"answers": [["Current module"]]}
        assert summary_part["tool_call_id"] == question_part["tool_call_id"]
        assert summary_part["answers"] == [["Current module"]]
    finally:
        _restore_workdir(old, tmp)


def test_loop_question_dismissal_is_completed_metadata_and_continues():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        questions = [{
            "header": "Scope",
            "question": "Which scope should be changed?",
            "options": [
                {"label": "Current module", "description": "Keep it narrow."},
                {"label": "Whole project", "description": "Apply everywhere."},
            ],
        }]
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("question", {"questions": questions}),
            ])),
            FakeResponse(FakeMessage("continued with best judgment")),
        ])
        messages = [{"role": "user", "content": "make the requested change"}]
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            trace_enabled=False,
            question_asker=lambda _payload: None,
        )

        result = _run_agent(agent, messages, stream=False)

        tool_part = next(
            part
            for message in messages
            for part in message.get("_nz_parts", [])
            if part.get("type") == "tool" and part.get("tool") == "question"
        )
        question_part = next(
            part
            for message in messages
            for part in message.get("_nz_parts", [])
            if part.get("type") == "question"
        )
        assert result["status"] == "completed"
        assert fake.chat.completions.calls == 2
        assert tool_part["state"]["status"] == "completed"
        assert tool_part["state"]["title"] == "Question dismissed"
        assert tool_part["state"]["metadata"] == {
            "answers": [],
            "dismissed": True,
        }
        assert question_part["status"] == "terminated"
        assert not any(
            part.get("type") == "question-summary"
            for message in messages
            for part in message.get("_nz_parts", [])
        )
    finally:
        _restore_workdir(old, tmp)


def test_loop_can_rebind_http_interaction_askers_after_construction():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="default",
            client=FakeClient([]),
            trace_enabled=False,
        )
        def question_asker(_questions):
            return [["Current module"]]

        def permission_asker(_name, _input):
            return "once"

        agent.set_interaction_askers(
            question_asker=question_asker,
            permission_asker=permission_asker,
        )

        assert agent.question_asker is question_asker
        assert agent.plan_mode.question_asker is question_asker
        assert agent.permissions.ask_user("edit_file", {"path": "demo.py"}) is True
    finally:
        _restore_workdir(old, tmp)


def test_loop_reports_failed_verification_without_reopening_completion():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_gate_prompts = config.MAX_VERIFICATION_GATE_PROMPTS
    config.MAX_VERIFICATION_GATE_PROMPTS = 1
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "app.py", "content": "print('bad')\n"}),
                FakeToolCall("bash", {"command": "python -m pytest -q"}, call_id="call_2"),
            ])),
            FakeResponse(FakeMessage("final too early")),
            FakeResponse(FakeMessage("still final")),
        ])
        messages = [{"role": "user", "content": "fix the tests"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert status["verification_needed"] is True
        assert fake.chat.completions.calls == 2
        assert not any(
            m.get("role") == "user" and "<verification-required>" in m.get("content", "")
            for m in messages
        )
        assert any(
            m.get("role") == "user" and "<test-failure-diagnostic>" in m.get("content", "")
            for m in messages
        )
    finally:
        config.MAX_VERIFICATION_GATE_PROMPTS = old_gate_prompts
        _restore_workdir(old, tmp)


def test_loop_runs_required_static_and_targeted_stages_before_completion():
    """模型静态检查后提前结束会被 gate 拉回，目标测试通过后才能完成。"""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_reflection = config.REFLECTION_ENABLED
    config.REFLECTION_ENABLED = False
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "write_file",
                    {"path": "app.py", "content": "def run():\n    return 'ok'\n"},
                ),
                FakeToolCall(
                    "write_file",
                    {
                        "path": "tests/test_app.py",
                        "content": "from app import run\n\n\ndef test_run():\n    assert run() == 'ok'\n",
                    },
                    call_id="call_2",
                ),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "bash",
                    {"command": "python -m py_compile app.py"},
                    call_id="call_3",
                ),
            ])),
            FakeResponse(FakeMessage("final before target")),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "bash",
                    {"command": "python -m pytest -q tests/test_app.py::test_run"},
                    call_id="call_4",
                ),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "implement run and verify the exact test"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        from nz_coder.runtime.verification.hooks import verification_gate_hook
        agent.hooks.register_before_no_tool_response(verification_gate_hook)
        agent.vm._plan_builder = lambda _changed_files: {
            "recommended": [],
            "fallback": [],
            "notes": [],
            "stages": [
                {
                    "name": "static",
                    "required": True,
                    "commands": [{
                        "command": "python -m py_compile app.py",
                        "reason": "changed source",
                        "required": True,
                    }],
                },
                {
                    "name": "targeted",
                    "required": True,
                    "commands": [{
                        "command": "pytest tests/test_app.py::test_run",
                        "reason": "exact target",
                        "required": True,
                    }],
                },
                {"name": "regression", "required": False, "commands": []},
            ],
        }

        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert status["verification_needed"] is False
        assert fake.chat.completions.calls == 5
        assert any(
            message.get("role") == "user"
            and "<verification-pipeline>" in message.get("content", "")
            and "tests/test_app.py::test_run" in message.get("content", "")
            for message in messages
        )
        stages = status["verification_pipeline"]["stages"]
        assert [stage["status"] for stage in stages[:2]] == ["passed", "passed"]
        evidence = agent.run_evidence.verification_results
        assert [item["stage"] for item in evidence] == ["static", "targeted"]
    finally:
        config.REFLECTION_ENABLED = old_reflection
        _restore_workdir(old, tmp)


def test_loop_reflection_reopens_incomplete_completion(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    config.REFLECTION_ENABLED = True
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "app.py", "content": "def run():\n    return 'ok'\n"}),
            ])),
            FakeResponse(FakeMessage("done without tests")),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "test_app.py", "content": "from app import run\n\n\ndef test_run():\n    assert run() == 'ok'\n"}),
            ])),
            FakeResponse(FakeMessage("done with tests")),
        ])
        reviews = iter([
            {
                "review_status": "needs_fix",
                "summary": "Requested tests are still missing.",
                "missing_evidence": ["test_app.py"],
                "quality_notes": ["Task asked for tests but no test file change was recorded."],
                "required_next_steps": ["Add the missing tests and rerun verification."],
            },
            {
                "review_status": "approved",
                "summary": "Coverage and verification now match the request.",
                "missing_evidence": [],
                "quality_notes": [],
                "required_next_steps": [],
            },
        ])
        messages = [{"role": "user", "content": "Implement app.py and add tests."}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        from nz_coder.runtime.verification.hooks import reflection_gate_hook
        agent.hooks.register_before_no_tool_response(reflection_gate_hook)
        monkeypatch.setattr(agent.vm, "should_gate", lambda: False)
        monkeypatch.setattr(agent, "_run_reflection_review", lambda content_text: next(reviews))
        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 4
        assert (tmp / "app.py").exists()
        assert (tmp / "test_app.py").exists()
        assert any(
            m.get("role") == "user" and "<reflection-review>" in m.get("content", "")
            for m in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_loop_streaming_emits_only_accepted_final_answer(monkeypatch):
    from nz_coder.loop import AgentLoop, LLMResult

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
        from nz_coder.runtime.verification.hooks import reflection_gate_hook
        agent.hooks.register_before_no_tool_response(reflection_gate_hook)
        results = iter([
            LLMResult(content="draft answer", tool_calls=[]),
            LLMResult(content="accepted answer", tool_calls=[]),
        ])
        statuses = iter(["continue", "completed"])
        tokens = []
        messages = [{"role": "user", "content": "fix the bug"}]

        monkeypatch.setattr(agent, "_call_llm", lambda *args, **kwargs: next(results))
        monkeypatch.setattr(agent.vm, "should_gate", lambda: False)
        monkeypatch.setattr(agent, "_check_reflection_gate", lambda messages, status, content_text: next(statuses))

        status = _run_agent(agent, messages, stream=True, on_token=tokens.append)

        assert status["status"] == "completed"
        assert tokens == ["accepted answer", None]
    finally:
        _restore_workdir(old, tmp)


def test_loop_reflection_parses_subagent_output(monkeypatch):
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.agent import subagent

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
        agent.runtime_state.initial_task_text = "Implement pkg/app.py and add tests."
        agent.runtime_state.task_mode = "feature"
        agent.runtime_state.acceptance_criteria = ["Implement pkg/app.py", "Add tests"]
        agent.runtime_state.requested_paths = ["pkg/app.py", "tests/test_app.py"]
        agent.runtime_state.edits_this_run = 1
        agent.runtime_state.has_diff = True
        agent.run_evidence.modified_files.append("pkg/app.py")
        agent.run_evidence.verification_results.append({"status": "passed", "summary": "OK: py_compile changed files"})

        monkeypatch.setattr(
            subagent,
            "run_subagent",
            lambda *args, **kwargs: (
                "VERDICT: needs_fix\n"
                "SUMMARY: Tests are missing.\n"
                "MISSING:\n"
                "- tests/test_app.py\n"
                "QUALITY:\n"
                "- No test-file change was recorded.\n"
                "NEXT:\n"
                "- Add the requested tests.\n\n"
                "[Subagent status: completed]"
            ),
        )

        review = agent._run_reflection_review("done")

        assert review["review_status"] == "needs_fix"
        assert review["summary"] == "Tests are missing."
        assert review["missing_evidence"] == ["tests/test_app.py"]
        assert review["quality_notes"] == ["No test-file change was recorded."]
        assert review["required_next_steps"] == ["Add the requested tests."]
    finally:
        _restore_workdir(old, tmp)


def test_loop_injects_prompt_hook_guidance_from_settings():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        settings_dir = tmp / ".nz-coder"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "turn-guidance",
                            "event": "turn_start",
                            "action": {"type": "prompt", "message": "Keep edits minimal and focused."},
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        messages = [{"role": "user", "content": "fix the bug"}]

        _run_agent(agent, messages, stream=False)

        request_messages = fake.chat.completions.requests[0]["messages"]
        assert any(
            msg.get("role") == "user"
            and "<hook-guidance>" in msg.get("content", "")
            and "Keep edits minimal and focused." in msg.get("content", "")
            for msg in request_messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_loop_pre_tool_hook_rejects_write_file_from_settings():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        settings_dir = tmp / ".nz-coder"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "block-write",
                            "event": "pre_tool_use",
                            "if": 'tool == "write_file"',
                            "action": {"type": "prompt", "message": "Creating new files is blocked by policy."},
                            "reject": True,
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"}),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write a file"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        _run_agent(agent, messages, stream=False)

        assert not (tmp / "hello.txt").exists()
        tool_messages = [m for m in messages if m.get("role") == "tool"]
        assert tool_messages
        assert "Denied: Creating new files is blocked by policy." in tool_messages[0]["content"]
    finally:
        _restore_workdir(old, tmp)


def test_loop_no_tool_hook_reopens_for_missing_requested_tests():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        settings_dir = tmp / ".nz-coder"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "reopen-missing-tests",
                            "event": "no_tool_response",
                            "if": 'missing_requested_test_paths_count != "0"',
                            "action": {
                                "type": "prompt",
                                "message": "Missing requested tests: $MISSING_REQUESTED_TEST_PATHS. Add them before finishing.",
                            },
                            "continue": True,
                            "once": True,
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fake = FakeClient([
            FakeResponse(FakeMessage("done too early")),
            FakeResponse(FakeMessage("done after hook reminder")),
        ])
        messages = [{"role": "user", "content": "Update src/app.py and add tests/test_app.py"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 2
        second_request_messages = fake.chat.completions.requests[1]["messages"]
        assert any(
            msg.get("role") == "user"
            and "<hook-guidance>" in msg.get("content", "")
            and "Missing requested tests: tests/test_app.py" in msg.get("content", "")
            for msg in second_request_messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_loop_reports_invalid_tool_json_and_continues():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("read_file", "{", call_id="bad_json")
            ])),
            FakeResponse(FakeMessage("fixed")),
        ])
        messages = [{"role": "user", "content": "read something"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        tool_messages = [m for m in messages if m.get("role") == "tool"]
        assert "Invalid JSON arguments" in tool_messages[0]["content"]
        assert fake.chat.completions.calls == 2
    finally:
        _restore_workdir(old, tmp)


def test_loop_preserves_reasoning_content_between_tool_turns():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(
                tool_calls=[FakeToolCall("read_file", {"path": "missing.txt"})],
                reasoning_content="private provider reasoning token",
            )),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "read a file"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        assistant_messages = [m for m in messages if m.get("role") == "assistant"]
        private_state = assistant_messages[0]["_nz_provider_reasoning_content"]
        assert private_state["schema"] == "nz.provider_private_state.v2"
        assert private_state["provider_instance_id"].startswith(
            "provider-instance-"
        )
        assert private_state["provider_id"] == "openai-compatible"
        assert private_state["payload"] == "private provider reasoning token"
        second_request_messages = fake.chat.completions.requests[1]["messages"]
        assert any(
            m.get("role") == "assistant"
            and m.get("reasoning_content") == "private provider reasoning token"
            for m in second_request_messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_loop_retries_transient_api_errors():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            RuntimeError("temporary outage"),
            RuntimeError("temporary outage"),
            FakeResponse(FakeMessage("recovered")),
        ])
        messages = [{"role": "user", "content": "hello"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.recovery.backoff_base = 0
        _run_agent(agent, messages, stream=False)

        assert fake.chat.completions.calls == 3
        assert messages[-1]["content"] == "recovered"
        retries = [
            part for part in messages[-1]["_nz_parts"]
            if part["type"] == "retry"
        ]
        assert [part["attempt"] for part in retries] == [1, 2]
        assert all(part["message"] == "An internal error occurred." for part in retries)
        assert all("temporary outage" not in str(part) for part in retries)
        assert all(part["error"]["name"] == "APIError" for part in retries)
        assert all(part["error"]["data"]["isRetryable"] is True for part in retries)
        assert all(part["time"]["created"] >= 0 for part in retries)
    finally:
        _restore_workdir(old, tmp)


def test_loop_writes_trace_events():
    from nz_coder.loop import AgentLoop
    from nz_coder.state.trace import TraceRecorder

    old, tmp = _tmp_workdir()
    try:
        settings_dir = tmp / ".nz-coder"
        settings_dir.mkdir()
        (settings_dir / "settings.json").write_text(
            json.dumps(
                {
                    "hooks": [
                        {
                            "id": "trace-turn-hook",
                            "event": "turn_start",
                            "action": {"type": "prompt", "message": "Trace this turn."},
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        fake = FakeClient([
            FakeResponse(FakeMessage("done")),
        ])
        tracer = TraceRecorder(trace_dir=tmp / "traces", enabled=True)
        messages = [{"role": "user", "content": "hello"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, tracer=tracer)
        _run_agent(agent, messages, stream=False)

        text = tracer.path.read_text(encoding="utf-8")
        assert '"event": "run_start"' in text
        assert '"event": "hook_triggered"' in text
        assert '"hook_event": "turn_start"' in text
        assert '"event": "llm_response"' in text
        assert '"event": "run_end"' in text
        run_end = next(
            json.loads(line)
            for line in text.splitlines()
            if json.loads(line).get("event") == "run_end"
        )
        assert run_end["runtime"]["mutation_generation"] == 0
    finally:
        _restore_workdir(old, tmp)


def test_runtime_summary_exposes_generation_terminal_evidence():
    """Catches final-risk filtering silently falling back to all generations."""
    from nz_coder.loop import AgentLoop

    agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
    agent.runtime_state.mutation_generation = 3
    agent.runtime_state.diff_generation = 3
    agent.runtime_state.verification_generation = 3

    runtime = agent._runtime_summary()

    assert runtime["package_install_attempts"] == 0
    assert runtime["emergency_broad_exploration"] == 0
    assert runtime["work_phase"] == "normal"

    assert runtime["mutation_generation"] == 3
    assert runtime["diff_generation"] == 3
    assert runtime["verification_generation"] == 3



def test_loop_tracks_session_scoped_runtime_state_and_trace():
    from nz_coder.loop import AgentLoop
    from nz_coder.state.sessions import session_runtime_state_path

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        messages = [{"role": "user", "content": "hello"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=True, session_id="session-a")
        _run_agent(agent, messages, stream=False)

        assert agent.session_id == "session-a"
        assert agent.tracer.session_id == "session-a"
        assert "session-a__" in agent.tracer.path.name
        row = json.loads(agent.tracer.path.read_text(encoding="utf-8").splitlines()[0])
        assert row["session_id"] == "session-a"
        assert session_runtime_state_path("session-a").exists()
    finally:
        _restore_workdir(old, tmp)


def test_loop_injects_denial_guidance_and_allows_a_safe_final_response(monkeypatch):
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"}),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write a file"}]
        agent = AgentLoop("test", permission_mode="default", client=fake, trace_enabled=False)
        monkeypatch.setattr(agent.permissions, "ask_user", lambda *args, **kwargs: False)
        status = _run_agent(agent, messages, stream=False)

        assert any(
            m.get("role") == "user" and "所有写操作均被用户拒绝" in m.get("content", "")
            for m in messages
        )
        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 2
    finally:
        _restore_workdir(old, tmp)


def test_loop_can_explicitly_continue_after_permission_denial(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        monkeypatch.setattr(config, "CONTINUE_LOOP_ON_DENY", True)
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"}),
            ])),
            FakeResponse(FakeMessage("understood")),
        ])
        messages = [{"role": "user", "content": "write a file"}]
        agent = AgentLoop("test", permission_mode="default", client=fake, trace_enabled=False)
        monkeypatch.setattr(agent.permissions, "ask_user", lambda *args, **kwargs: False)

        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 2
        assert not (tmp / "hello.txt").exists()
    finally:
        _restore_workdir(old, tmp)


def test_loop_can_complete_hard_refactor_with_structural_edit():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        (tmp / "user_manager.py").write_text('''class UserManager:
    def __init__(self):
        self.users = {}

    def create_user(self, name, email):
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            raise ValueError(f"Invalid email: {email}")
        self.users[email] = {"name": name, "email": email}
        return self.users[email]
''', encoding="utf-8")
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("load_optional_tools", {"packs": ["python_ast"]}, call_id="call_0"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("python_structural_edit", {
                    "path": "user_manager.py",
                    "insertions": [{
                        "before_symbol": "UserManager",
                        "code": '''def validate_email(email):
    return bool(email and "@" in email and "." in email.split("@")[-1])
''',
                    }],
                    "replacements": [{
                        "target": "UserManager.create_user",
                        "code": '''def create_user(self, name, email):
    if not validate_email(email):
        raise ValueError(f"Invalid email: {email}")
    if email in self.users:
        raise ValueError(f"User already exists: {email}")
    self.users[email] = {"name": name, "email": email}
    return self.users[email]
''',
                    }],
                }),
                FakeToolCall("python_symbol_check", {
                    "path": "user_manager.py",
                    "symbols": ["validate_email", "UserManager", "UserManager.create_user"],
                    "calls": [{"caller": "UserManager.create_user", "callee": "validate_email"}],
                }, call_id="call_2"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "refactor user_manager.py"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        status = _run_agent(agent, messages, stream=False)

        namespace = {}
        exec((tmp / "user_manager.py").read_text(encoding="utf-8"), namespace)
        validate_email = namespace["validate_email"]
        UserManager = namespace["UserManager"]

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 3
        assert validate_email("a@b.c") is True
        assert validate_email("") is False
        assert validate_email("noat") is False
        assert validate_email("no@dot") is False
        assert UserManager().create_user("Test", "test@example.com")["email"] == "test@example.com"
        assert any(
            m.get("role") == "tool" and "UserManager.create_user calls validate_email" in m.get("content", "")
            for m in messages
        )
    finally:
        _restore_workdir(old, tmp)


# ── 新增测试 ──────────────────────────────────────────────────────────────────

def test_tool_call_limit_read_only_dispatches_only_prefix():
    """超过 MAX_TOOL_CALLS_PER_RESPONSE 的只读工具只执行前缀，不再绕过 loop 限额。"""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_limit = config.MAX_TOOL_CALLS_PER_RESPONSE
    config.MAX_TOOL_CALLS_PER_RESPONSE = 2
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("bash", {"command": "echo a"}, call_id="c1"),
                FakeToolCall("bash", {"command": "echo b"}, call_id="c2"),
                FakeToolCall("bash", {"command": "echo c"}, call_id="c3"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "run stuff"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
        assert all("Too many tool calls" not in m["content"] for m in tool_msgs)
        assert agent.tool_calls_this_run == 2
    finally:
        config.MAX_TOOL_CALLS_PER_RESPONSE = old_limit
        _restore_workdir(old, tmp)


def test_tool_call_limit_write_dispatches_only_prefix():
    """写工具批次也只能执行前缀，不能在串行分支绕过上限。"""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_limit = config.MAX_TOOL_CALLS_PER_RESPONSE
    config.MAX_TOOL_CALLS_PER_RESPONSE = 2
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "a.py", "content": "a = 1\n"}, call_id="c1"),
                FakeToolCall("write_file", {"path": "b.py", "content": "b = 1\n"}, call_id="c2"),
                FakeToolCall("write_file", {"path": "c.py", "content": "c = 1\n"}, call_id="c3"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write files"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        assert (tmp / "a.py").exists()
        assert (tmp / "b.py").exists()
        assert not (tmp / "c.py").exists()
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
        assert agent.tool_calls_this_run == 2
    finally:
        config.MAX_TOOL_CALLS_PER_RESPONSE = old_limit
        _restore_workdir(old, tmp)


def test_write_failure_triggers_rollback():
    """write_file 后工具出错应触发 transaction rollback，并注入 transaction-rollback 消息。"""
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "app.py", "content": "x=1"}, call_id="c1"),
                # bash 工具用非法命令让 dispatch 失败（不影响非写工具的 all_succeeded），
                # 但 write_file 本身就是写工具，事务已 begin。
                # 用一个不存在工具来让 dispatch_failed=True 触发 rollback。
                FakeToolCall("no_such_tool_xyz", {}, call_id="c2"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write and fail"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        assert any(
            "<transaction-rollback>" in m.get("content", "")
            for m in messages if m.get("role") == "user"
        )
    finally:
        _restore_workdir(old, tmp)


def test_bash_test_failure_no_rollback():
    """bash 非零退出（测试失败）不应触发 transaction rollback。"""
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "app.py", "content": "x=1"}, call_id="c1"),
                # bash exit code 1 = 测试失败，不是 dispatch 失败
                FakeToolCall("bash", {"command": "exit 1"}, call_id="c2"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write and test"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        # 不应出现 transaction-rollback
        assert not any(
            "<transaction-rollback>" in m.get("content", "")
            for m in messages if m.get("role") == "user"
        )
    finally:
        _restore_workdir(old, tmp)


def test_400_client_error_does_not_require_http_transport_library():
    """400 客户端错误应注入诊断消息到对话，而不是无限重试。"""
    from nz_coder.loop import AgentLoop

    class FakeClientError(Exception):
        status_code = 400
        code = "invalid_request_error"
        body = {
            "error": {
                "message": "invalid json in tool",
                "type": "invalid_request_error",
            }
        }

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeClientError("bad request"),
            FakeResponse(FakeMessage("corrected")),
        ])
        messages = [{"role": "user", "content": "test"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        # 对话中应含有 api-error-diagnostic 消息
        assert any(
            "<api-error-diagnostic>" in m.get("content", "")
            for m in messages if m.get("role") == "user"
        )
        # 只调用了 2 次（不重试 400）
        assert fake.chat.completions.calls == 2
    finally:
        _restore_workdir(old, tmp)


def test_402_balance_error_aborts_after_one_provider_request():
    """Account failures are terminal and must not consume Agent turns."""
    from nz_coder.loop import AgentLoop

    class BalanceError(RuntimeError):
        status_code = 402
        code = "invalid_request_error"
        body = {
            "error": {
                "message": "Insufficient Balance",
                "type": "unknown_error",
                "code": "invalid_request_error",
            }
        }

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            BalanceError("Insufficient Balance: invalid_request_error"),
        ])
        messages = [{"role": "user", "content": "fix the issue"}]
        agent = AgentLoop(
            "test", permission_mode="auto", client=fake, trace_enabled=False,
        )

        result = _run_agent(agent, messages, stream=False)

        assert result["status"] == "aborted"
        assert fake.chat.completions.calls == 1
        assert not any(
            "<api-error-diagnostic>" in str(message.get("content") or "")
            for message in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_context_overflow_recovery_does_not_require_http_transport_library():
    """Provider context rejection follows processor compact instead of 400 repair."""
    from nz_coder.loop import AgentLoop

    class FakeContextOverflow(Exception):
        status_code = 400
        code = "context_length_exceeded"
        body = {"error": {"type": "context_length_exceeded"}}

    old, tmp = _tmp_workdir()
    try:
        overflow = FakeContextOverflow(
            "context_length_exceeded: maximum context length is 8192 tokens",
        )
        fake = FakeClient([
            overflow,
            FakeResponse(FakeMessage("## Goal\n- finish the task")),
            FakeResponse(FakeMessage("resumed answer")),
        ])
        messages = [{"role": "user", "content": "test overflow recovery"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 3
        assert messages[-1]["content"] == "resumed answer"
        assert any(
            isinstance(message.get("_nz_compaction"), dict)
            and message["_nz_compaction"].get("overflow") is True
            for message in messages
        )
        assert not any(
            "<api-error-diagnostic>" in str(message.get("content") or "")
            for message in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_openai_client_error_classification_is_transport_neutral():
    from nz_coder.runtime.execution.loop import _is_client_error

    class FakeClientError(Exception):
        status_code = 400

    assert _is_client_error(FakeClientError("bad request")) is True


def test_context_overflow_stops_after_three_compaction_attempts(monkeypatch):
    from nz_coder.loop import AgentLoop, LLMResult
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
        compact_calls = []

        monkeypatch.setattr(
            agent,
            "_call_llm",
            lambda *_args, **_kwargs: LLMResult(
                needs_compaction=True,
                compaction_error="context_length_exceeded",
            ),
        )

        def fake_compact(_messages, *, overflow=False):
            compact_calls.append(overflow)
            return [{
                "role": "user",
                "content": "<session-summary>bounded</session-summary>",
                "_nz_compaction": {"overflow": overflow},
            }]

        monkeypatch.setattr(agent, "_compact_messages", fake_compact)
        messages = [{"role": "user", "content": "large request"}]

        with scoped_runtime_overrides(max_agent_turns=5):
            status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "error"
        assert compact_calls == [True, True, True]
        assert any(
            "Compaction exhausted" in str(message.get("_nz_error") or "")
            for message in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_pre_send_and_reactive_compactions_share_one_three_attempt_owner(monkeypatch):
    from nz_coder.state.context import prompt_budget
    from nz_coder.loop import AgentLoop, LLMResult
    from nz_coder.runtime.core.execution_context import scoped_runtime_overrides

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        model_calls = []
        compact_calls = []
        monkeypatch.setattr(agent, "_prompt_budget", lambda: prompt_budget(10_000, 2_000))
        monkeypatch.setattr(agent, "_projected_request_tokens", lambda _messages: 9_000)
        monkeypatch.setattr(
            agent,
            "_call_llm",
            lambda *_args, **_kwargs: model_calls.append(True) or LLMResult(
                needs_compaction=True,
                compaction_error="context_length_exceeded",
            ),
        )

        def fake_compact(_messages, *, overflow=False):
            compact_calls.append(overflow)
            return [{
                "role": "user",
                "content": "<session-summary>bounded</session-summary>",
                "_nz_compaction": {"auto": True, "created_at": time.time()},
            }]

        monkeypatch.setattr(agent, "_compact_messages", fake_compact)
        messages = [{"role": "user", "content": "large request"}]

        with scoped_runtime_overrides(max_agent_turns=5):
            status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "error"
        assert compact_calls == [False, True, False]
        assert len(model_calls) == 2
        assert any(
            "Compaction exhausted" in str(message.get("_nz_error") or "")
            for message in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_pre_send_compaction_failure_is_checkpointed_without_provider_call(monkeypatch):
    from nz_coder.state.context import prompt_budget
    from nz_coder.loop import AgentLoop
    from nz_coder.state.sessions import load_session

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        monkeypatch.setattr(agent, "_prompt_budget", lambda: prompt_budget(10_000, 2_000))
        monkeypatch.setattr(agent, "_projected_request_tokens", lambda _messages: 9_000)
        monkeypatch.setattr(
            agent,
            "_compact_messages",
            lambda _messages: (_ for _ in ()).throw(RuntimeError("summary unavailable")),
        )
        messages = [{"role": "user", "content": "large request"}]

        status = _run_agent(agent, messages, stream=False)
        persisted = load_session(agent.session_id)

        assert status["status"] == "error"
        assert agent.client.chat.completions.calls == 0
        assert persisted["run_status"] == "error"
        errors = [
            message.get("_nz_error")
            for message in persisted["messages"]
            if message.get("_nz_error")
        ]
        assert "An internal error occurred." in errors
        assert "summary unavailable" not in str(persisted)
    finally:
        _restore_workdir(old, tmp)


def test_context_layers_keep_dynamic_state_out_of_system_prompt():
    from nz_coder.loop import _build_context_layers, _inject_dynamic_context

    stable, dynamic, stats = _build_context_layers(
        "SYSTEM\n",
        "MEMORY\n",
        "<system-reminder>state</system-reminder>",
        "## Working Memory\n- note",
        max_tokens=6000,
    )
    messages = [{"role": "user", "content": "fix the bug"}]
    injected = _inject_dynamic_context(messages, dynamic)

    assert stable == "SYSTEM\n"
    assert "MEMORY" in injected[0]["content"]
    assert "system-reminder" not in stable
    assert "Working Memory" not in stable
    assert injected[0]["role"] == "user"
    assert "<context-injection>" in injected[0]["content"]
    assert injected[0]["content"].endswith("fix the bug")
    assert stats["before_total_tokens"] == stats["after_total_tokens"]


def test_instruction_reminder_precedes_dynamic_context_on_first_user():
    from nz_coder.loop import _inject_dynamic_context, _inject_instruction_reminder

    messages = [{"role": "user", "content": "fix the bug"}]
    dynamic = _inject_dynamic_context(messages, "<context-injection>memory</context-injection>")
    injected = _inject_instruction_reminder(
        dynamic,
        "<system-reminder>rules</system-reminder>",
    )

    content = injected[0]["content"]
    assert content.startswith("<system-reminder>rules</system-reminder>")
    assert content.index("<context-injection>") < content.index("fix the bug")


def test_instruction_reminder_falls_back_to_system_without_user_message():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        (tmp / "AGENTS.md").write_text("SYSTEM-FALLBACK-RULE", encoding="utf-8")
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )

        request = agent._build_api_messages([
            {"role": "assistant", "content": "continuation"},
        ])

        assert "SYSTEM-FALLBACK-RULE" in request[0]["content"]
        assert all(
            "SYSTEM-FALLBACK-RULE" not in str(message.get("content", ""))
            for message in request[1:]
        )
    finally:
        _restore_workdir(old, tmp)


def test_context_layer_budget_truncates_memory_and_scratch():
    from nz_coder.loop import _build_context_layers

    stable, dynamic, stats = _build_context_layers(
        "SYSTEM\n",
        "M" * 20000,
        "STATE\n",
        "S" * 20000,
        max_tokens=1000,
    )

    assert stats["after_total_tokens"] <= stats["budget_tokens"]
    assert stats["after_total_tokens"] < stats["before_total_tokens"]
    assert "SYSTEM" in stable
    assert "truncated by context budget" in stable or "truncated by context budget" in dynamic


def _set_planning_config(config, enabled=True, max_replans=2, idle_turns=5):
    old = (
        config.PLANNING_ENABLED,
        config.REPLAN_MAX_ATTEMPTS,
        config.REPLAN_IDLE_TURNS,
        config.PLANNING_MAX_TOKENS,
    )
    config.PLANNING_ENABLED = enabled
    config.REPLAN_MAX_ATTEMPTS = max_replans
    config.REPLAN_IDLE_TURNS = idle_turns
    config.PLANNING_MAX_TOKENS = 200
    return old


def _restore_planning_config(config, old):
    (
        config.PLANNING_ENABLED,
        config.REPLAN_MAX_ATTEMPTS,
        config.REPLAN_IDLE_TURNS,
        config.PLANNING_MAX_TOKENS,
    ) = old


def test_planning_disabled_keeps_llm_call_count_unchanged():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=False)
    scratchpad.clear()
    try:
        fake = FakeClient([])
        messages = [{
            "role": "user",
            "content": (
                "Add a REST endpoint for users, add tests, and run "
                "pytest -q tests/test_users.py."
            ),
        }]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.runtime_state.initial_task_text = messages[0]["content"]
        agent.runtime_state.task_mode = "test"
        agent.runtime_state.verification_contract = {
            "command": "pytest -q tests/test_users.py",
        }
        asyncio.run(agent._maybe_generate_plan(messages))

        assert fake.chat.completions.calls == 0
        assert not any(e["category"] == "plan" for e in scratchpad.entries)
        assert agent.runtime_state.task_contract["requirements"]
        assert agent.runtime_state.requirement_ledger["items"]
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_runtime_bootstrap_does_not_bind_traceback_path_as_artifact():
    """The loop must pass Runtime path authority into deterministic bootstrap."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=False)
    try:
        source = tmp / "src" / "_pytest" / "runner.py"
        source.parent.mkdir(parents=True)
        source.write_text("# traceback frame\n", encoding="utf-8")
        task = (
            "Solve the initialization bug. Traceback: File "
            "src/_pytest/runner.py, line 42. Run python -m pytest -q "
            "tests/test_commands.py."
        )
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        agent.runtime_state.initial_task_text = task
        agent.runtime_state.set_acceptance_criteria_from_text(task)

        asyncio.run(agent._maybe_generate_plan([{
            "role": "user",
            "content": task,
        }]))

        behavior = next(
            item
            for item in agent.runtime_state.task_contract["requirements"]
            if item["kind"] == "behavior"
        )
        assert agent.runtime_state.requested_paths == []
        assert behavior["expected_artifacts"] == []
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_invalid_planner_json_retains_runtime_bootstrap_contract():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    try:
        fake = FakeClient([FakeResponse(FakeMessage('{"objective":"truncated'))])
        messages = [{
            "role": "user",
            "content": (
                "Implement named cron fields, add tests, update README, preserve API "
                "compatibility, then run pytest -q cron_engine/tests."
            ),
        }]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.runtime_state.initial_task_text = messages[0]["content"]
        agent.runtime_state.task_mode = "test"
        agent.runtime_state.verification_contract = {
            "command": "pytest -q cron_engine/tests",
        }
        asyncio.run(agent._maybe_generate_plan(messages))

        assert fake.chat.completions.calls == 1
        assert agent.runtime_state.task_contract["requirements"]
        assert agent.runtime_state.requirement_ledger["items"]
        assert fake.chat.completions.requests[0]["response_format"] == {
            "type": "json_object",
        }
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_gateway_observer_adds_control_usage_once_but_not_coding_usage():
    from nz_coder.loop import AgentLoop

    class UsageContext:
        finalized = False

        def __init__(self):
            self.values = []

        def add_usage(self, value):
            self.values.append(value)

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        context = UsageContext()
        agent.active_run_context = context
        usage = {
            "input": 100,
            "output": 20,
            "total": 120,
            "reasoning": 5,
            "cache_read": 10,
            "cache_write": 0,
        }

        agent._model_gateway_observer("model_call_finish", {
            "purpose": "planning",
            "provider_id": "openai-responses",
            "model_id": "gpt-planner",
            "status": "completed",
            "attempts": 1,
            "duration_ms": 10.0,
            "usage": usage,
            "cost": 0.001,
            "cost_source": "registry",
        })
        agent._model_gateway_observer("model_call_finish", {
            "purpose": "coding",
            "provider_id": "anthropic",
            "model_id": "claude-coder",
            "status": "completed",
            "attempts": 1,
            "duration_ms": 15.0,
            "usage": usage,
            "cost": 0.002,
            "cost_source": "provider",
        })

        assert len(context.values) == 1
        assert context.values[0].input_tokens == 100
        assert context.values[0].output_tokens == 20
        assert agent.runtime_state.provider_calls_by_purpose == {
            "planning": 1,
            "coding": 1,
        }
        assert agent.runtime_state.provider_calls_by_model == {
            "openai-responses/gpt-planner": 1,
            "anthropic/claude-coder": 1,
        }
        assert agent.runtime_state.provider_cost_usd == 0.003
        assert agent.runtime_state.provider_cost_sources == {
            "registry": 1,
            "provider": 1,
        }
    finally:
        _restore_workdir(old, tmp)


def test_gateway_observer_accounts_auto_mode_usage_exactly_once():
    """Classifier cost is distinct from coding and added once to run totals."""
    from nz_coder.loop import AgentLoop

    class UsageContext:
        finalized = False

        def __init__(self):
            self.values = []

        def add_usage(self, value):
            self.values.append(value)

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        context = UsageContext()
        agent.active_run_context = context
        agent._model_gateway_observer("model_call_finish", {
            "purpose": "auto_mode",
            "provider_id": "fake",
            "model_id": "judge",
            "status": "completed",
            "attempts": 1,
            "duration_ms": 7.5,
            "usage": {
                "input": 12,
                "output": 3,
                "total": 15,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
            "cost": 0.001,
            "cost_source": "estimated",
        })

        assert agent.runtime_state.provider_calls_by_purpose["auto_mode"] == 1
        assert agent.runtime_state.provider_usage_by_purpose["auto_mode"]["input"] == 12
        assert agent.runtime_state.provider_cost_usd_by_purpose["auto_mode"] == 0.001
        assert len(context.values) == 1
        assert context.values[0].input_tokens == 12
        assert context.values[0].output_tokens == 3
    finally:
        _restore_workdir(old, tmp)


def test_gateway_observer_sanitizes_auto_provider_errors_before_trace():
    """Provider error echoes cannot copy classifier payloads into trace."""
    from nz_coder.loop import AgentLoop

    events = []
    agent = AgentLoop.__new__(AgentLoop)
    agent.tracer = type(
        "TracerSpy",
        (),
        {"log": lambda _self, name, **payload: events.append((name, payload))},
    )()
    payload = {
        "purpose": "auto_mode",
        "attempt": 1,
        "provider_id": "fake",
        "model_id": "judge",
        "request_model_id": "judge-wire",
        "variant": "default",
        "error": "validation echoed AUTO_TRACE_SECRET",
        "body": {"prompt": "AUTO_TRACE_SECRET"},
        "headers": {"Authorization": "Bearer AUTO_TRACE_SECRET"},
        "request": {"messages": ["AUTO_TRACE_SECRET"]},
    }

    agent._model_gateway_observer(
        "model_call_response_format_fallback",
        payload,
    )

    serialized = json.dumps(events, ensure_ascii=False)
    assert "AUTO_TRACE_SECRET" not in serialized
    assert events == [(
        "model_call_response_format_fallback",
        {
            "purpose": "auto_mode",
            "attempt": 1,
            "provider_id": "fake",
            "model_id": "judge",
            "request_model_id": "judge-wire",
            "variant": "default",
        },
    )]
    assert payload["error"].endswith("AUTO_TRACE_SECRET")


def test_terminal_auto_bash_classifier_accounts_once_end_to_end():
    """Residual Bash uses one Auto Gateway call and one accounting record."""
    from nz_coder.loop import AgentLoop
    from nz_coder.providers.registry import ModelPricing
    from nz_coder.state.trace import TraceRecorder

    approvals = []

    async def approve(*_args):
        approvals.append(True)
        return "once"

    old, tmp = _tmp_workdir()
    agent = None
    try:
        fake = FakeClient([
            FakeResponse(
                FakeMessage(tool_calls=[
                    FakeToolCall("bash", {"command": "echo auto-ok"}),
                ]),
                finish_reason="tool_calls",
                usage={
                    "prompt_tokens": 20,
                    "completion_tokens": 4,
                    "total_tokens": 24,
                },
            ),
            FakeResponse(
                FakeMessage(json.dumps({
                    "decision": "allow",
                    "reason_code": "safe_local_command",
                    "reason": "bounded local command",
                })),
                finish_reason="stop",
                usage={
                    "prompt_tokens": 12,
                    "completion_tokens": 3,
                    "total_tokens": 15,
                },
            ),
            FakeResponse(
                FakeMessage("done"),
                finish_reason="stop",
                usage={
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            ),
        ])
        tracer = TraceRecorder(trace_dir=tmp / "traces", enabled=True)
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            tracer=tracer,
            auto_mode_classifier_enabled=True,
        )
        agent.model_pricing = ModelPricing(input=1.0, output=2.0)
        agent.set_interaction_askers(auto_permission_asker=approve)
        messages = [{"role": "user", "content": "Print a local marker"}]

        result = _run_agent(agent, messages, stream=False)

        requests = fake.chat.completions.requests
        classifier_request = requests[1]
        rows = [
            json.loads(line)
            for line in tracer.path.read_text(encoding="utf-8").splitlines()
        ]
        auto_finishes = [
            row for row in rows
            if row.get("event") == "model_call_finish"
            and row.get("purpose") == "auto_mode"
        ]
        auto_decisions = [
            row for row in rows if row.get("event") == "auto_mode_decision"
        ]

        assert result["status"] == "completed"
        assert result["content"] == "done"
        assert approvals == []
        assert len(requests) == 3
        assert classifier_request.get("tools", []) == []
        assert classifier_request.get("stream", False) is False
        assert classifier_request["response_format"] == {"type": "json_object"}
        assert classifier_request["max_tokens"] == 256
        assert agent.runtime_state.provider_calls_by_purpose["auto_mode"] == 1
        assert agent.runtime_state.provider_usage_by_purpose["auto_mode"] == {
            "input": 12,
            "output": 3,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "total": 15,
        }
        assert agent.runtime_state.provider_cost_usd_by_purpose["auto_mode"] == (
            0.000018
        )
        assert len(auto_finishes) == 1
        assert auto_finishes[0]["usage"]["input"] == 12
        assert auto_finishes[0]["cost"] == 0.000018
        assert len(auto_decisions) == 1
        assert auto_decisions[0]["source"] == "classifier"
    finally:
        if agent is not None:
            agent.close()
        _restore_workdir(old, tmp)


def test_runtime_summary_exposes_complete_provider_accounting():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
        )
        agent.runtime_state.observe_provider_call(
            "planning",
            usage={"input": 10, "output": 2, "total": 12},
            attempts=1,
            duration_ms=5.0,
            cost=0.004,
            cost_source="registry",
        )
        agent.runtime_state.observe_provider_turn({
            "turn": 1,
            "reason": "initial_investigation",
            "outcome": "investigation_batch",
            "tool_names": ["read_file"],
        })

        runtime = agent._runtime_summary()

        assert runtime["provider_calls"] == 1
        agent.runtime_state.forbids_test_changes = True
        runtime = agent._runtime_summary()
        assert runtime["forbids_test_changes"] is True
        assert runtime["provider_calls_by_purpose"] == {"planning": 1}
        assert runtime["provider_usage"]["total"] == 12
        assert runtime["provider_usage_by_purpose"]["planning"]["input"] == 10
        assert runtime["provider_cost_usd"] == 0.004
        assert runtime["provider_cost_usd_by_purpose"] == {"planning": 0.004}
        assert runtime["provider_turns_by_reason"] == {"initial_investigation": 1}
        assert runtime["provider_turns_by_outcome"] == {"investigation_batch": 1}
        assert runtime["provider_turn_records"][0]["tool_names"] == ["read_file"]
    finally:
        _restore_workdir(old, tmp)


def test_planning_enabled_generates_plan_for_feature_task():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    scratchpad.clear()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("## Plan\n1. Implement endpoint - app.py - verify_changed_files")),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "Add a REST endpoint for users"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 2
        assert "tools" not in fake.chat.completions.requests[0]
        assert fake.chat.completions.requests[1].get("tools")
        assert any(e["category"] == "plan" for e in scratchpad.entries)
        assert agent.runtime_state.plan_generated is True
        assert "## Plan" in agent.runtime_state.plan_text
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_planning_call_returns_task_contract_without_extra_model_call():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    scratchpad.clear()
    try:
        planner = json.dumps({
            "objective": "Create the requested artifact",
            "plan": [{
                "title": "Create artifact",
                "target": "hello.txt",
                "verification": "file diff",
            }],
            "requirements": [{
                "id": "R1",
                "description": "Create hello.txt",
                "kind": "artifact",
                "expected_artifacts": ["hello.txt"],
                "satisfaction_mode": "deterministic",
            }],
            "constraints": [],
        })
        fake = FakeClient([
            FakeResponse(FakeMessage(planner)),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("write_file", {"path": "hello.txt", "content": "hello\n"}),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{
            "role": "user",
            "content": "Add a new hello artifact file for this feature and verify it.",
        }]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 3
        assert agent.runtime_state.task_contract["objective"] == (
            "Create the requested artifact"
        )
        ledger = agent.runtime_state.requirement_ledger_snapshot()
        assert ledger.status("R1") == "satisfied"
        assert "## Plan" in agent.runtime_state.plan_text
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_planning_enabled_skips_simple_bugfix_task():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        messages = [{"role": "user", "content": "fix bug"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        assert fake.chat.completions.calls == 1
        assert agent.runtime_state.plan_generated is False
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_planning_restore_hydrates_plan_without_new_planning_call():
    import json

    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    scratchpad.clear()
    try:
        state_dir = tmp / ".nz-coder"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "runtime_state.json").write_text(
            json.dumps({
                "active": True,
                "turn_count": 0,
                "max_turns": 50,
                "plan_generated": True,
                "plan_text": "## Plan\n1. Existing plan",
                "replan_count": 1,
                "task_mode": "feature",
                "initial_task_text": "Add a REST endpoint",
            }),
            encoding="utf-8",
        )
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        messages = [{"role": "user", "content": "Add a REST endpoint"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        _run_agent(agent, messages, stream=False)

        assert fake.chat.completions.calls == 1
        assert any(e["category"] == "plan" and "Existing plan" in e["content"] for e in scratchpad.entries)
        assert agent._replan_count == 1
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_go_on_resumes_inactive_max_turns_task_state_with_fresh_budget():
    """A resumable activation preserves task facts without reusing spent turns."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=False)
    old_max_turns = config.MAX_AGENT_TURNS
    config.MAX_AGENT_TURNS = 20
    try:
        original_task = (
            "Fix parser.py and run python -m pytest -q tests/test_parser.py"
        )
        state = RuntimeState()
        state.reset(max_turns=20)
        state.initial_task_text = original_task
        state.set_acceptance_criteria_from_text(original_task)
        state.turn_count = 200
        state.read_files = ["parser.py", "tests/test_parser.py"]
        state_dir = tmp / ".nz-coder"
        state_dir.mkdir(exist_ok=True)
        state.save(state_dir / "runtime_state.json", active=False)

        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        messages = [
            {
                "role": "assistant",
                "content": "Stopped before verification.",
                "_nz_continuation": {
                    "version": 1,
                    "status": "max_turns",
                    "summary": f"## Latest User Instruction\n{original_task}",
                },
            },
            {"role": "user", "content": "go on"},
        ]
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            trace_enabled=False,
            session_id="resume-max-turns-go-on",
        )

        _run_agent(agent, messages, stream=False)

        assert fake.chat.completions.calls > 0
        assert agent.runtime_state.turn_count > 0
        assert agent.runtime_state.initial_task_text == original_task
        assert agent.runtime_state.verification_contract["command"] == (
            "python -m pytest -q tests/test_parser.py"
        )
        assert agent.runtime_state.read_files == [
            "parser.py", "tests/test_parser.py",
        ]
    finally:
        config.MAX_AGENT_TURNS = old_max_turns
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_substantive_continuation_overlays_current_round_constraints(monkeypatch):
    """A resumed task keeps its origin while honoring the newest User constraints."""
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    old, tmp = _tmp_workdir()
    monkeypatch.setattr(config, "RUNTIME_STATE_PERSIST", True)
    agent = None
    try:
        original_task = (
            "Fix parser.py. The parser must preserve numeric fields. "
            "Run python -m pytest -q tests/test_parser.py."
        )
        state = RuntimeState()
        state.reset(max_turns=20)
        state.initial_task_text = original_task
        state.set_acceptance_criteria_from_text(original_task)
        state.read_files = ["parser.py", "tests/test_parser.py"]
        state_dir = tmp / ".nz-coder"
        state_dir.mkdir(exist_ok=True)
        state.save(state_dir / "runtime_state.json", active=False)

        current_instruction = (
            "Continue the parser fix in parser_v2.py. The fallback must reject "
            "empty aliases. Do not modify tests. Run python -m pytest -q "
            "tests/test_new_parser.py."
        )
        messages = [
            {
                "role": "assistant",
                "content": "Stopped before verification.",
                "_nz_continuation": {
                    "version": 1,
                    "status": "max_turns",
                    "summary": f"## Latest User Instruction\n{original_task}",
                },
            },
            {"role": "user", "content": current_instruction},
        ]
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=FakeClient([]),
            trace_enabled=False,
            session_id="resume-with-current-round-constraints",
        )

        agent._init_run(messages, stream=False)

        assert agent.runtime_state.initial_task_text == original_task
        assert agent.runtime_state.verification_contract["command"] == (
            "python -m pytest -q tests/test_new_parser.py"
        )
        assert agent.runtime_state.forbids_test_changes is True
        assert agent.runtime_state.wants_tests is False
        assert agent.runtime_state.requested_paths == [
            "parser_v2.py",
            "parser.py",
        ]
        assert any(
            "fallback must reject empty aliases" in criterion
            for criterion in agent.runtime_state.acceptance_criteria
        )
        assert any(
            "parser must preserve numeric fields" in criterion
            for criterion in agent.runtime_state.acceptance_criteria
        )
        assert agent.runtime_state.read_files == [
            "parser.py", "tests/test_parser.py",
        ]
    finally:
        if agent is not None:
            agent.close()
        _restore_workdir(old, tmp)


def test_replan_respects_max_attempts():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True, max_replans=1, idle_turns=5)
    try:
        fake = FakeClient([FakeResponse(FakeMessage("## Plan\n1. Revised"))])
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=True)
        rs = agent.runtime_state
        rs.plan_generated = True
        rs.plan_text = "## Plan\n1. Original"
        rs.task_mode = "feature"
        rs.turn_count = 5
        rs.max_turns = 10
        rs.last_edit_turn = 0
        rs.initial_task_text = "Add a REST endpoint"
        rs.initial_plan_complexity = "moderate"

        assert agent._should_replan() is True
        asyncio.run(agent._maybe_replan())
        assert agent.runtime_state.replan_count == 1
        assert agent._should_replan() is False
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_replan_verification_guard_requires_diff():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    try:
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
        rs = agent.runtime_state
        rs.plan_generated = True
        rs.plan_text = "## Plan\n1. Original"
        rs.task_mode = "feature"
        rs.turn_count = 1
        rs.last_edit_turn = 1
        rs.has_diff = False
        rs.changed_files_verified = False
        rs.verification_attempts = 2

        assert agent._should_replan() is False
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_patch_risk_triggers_one_replan_and_enters_replan_prompt():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    try:
        fake = FakeClient([FakeResponse(FakeMessage("## Plan\n1. Preserve API"))])
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        rs = agent.runtime_state
        rs.plan_generated = True
        rs.plan_text = "## Plan\n1. Original"
        rs.task_mode = "bugfix"
        rs.turn_count = 1
        rs.last_edit_turn = 1
        rs.patch_risk = {
            "risk": "high",
            "fingerprint": "risk-123",
            "requires_replan": True,
            "risk_signals": [{
                "category": "deleted_public_symbols",
                "severity": "replan",
                "detail": "pkg/api.py: public_api",
            }],
            "reasons": ["public-looking symbols deleted"],
            "affected_files": ["pkg/api.py"],
            "likely_tests": [],
            "suggested_verification": [],
            "review_notes": [],
        }

        assert agent._should_replan() is True
        asyncio.run(agent._maybe_replan())

        request_text = fake.chat.completions.requests[0]["messages"][1]["content"]
        assert "Current patch risk summary" in request_text
        assert "deleted_public_symbols" in request_text
        assert rs.risk_replan_fingerprint == "risk-123"
        assert agent._should_replan() is False
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_patch_risk_refresh_uses_change_tracker_without_git(monkeypatch):
    import nz_coder.intelligence.verification_planner as planner
    from nz_coder.loop import AgentLoop
    from nz_coder.tools import dispatch
    from nz_coder.tools.files import bind_tool_state

    old, tmp = _tmp_workdir()
    monkeypatch.setattr(
        planner,
        "_git_deleted_files",
        lambda: (_ for _ in ()).throw(AssertionError("automatic risk refresh queried Git")),
    )
    try:
        target = tmp / "api.py"
        target.write_text("def public_api(value):\n    return value\n", encoding="utf-8")
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)
        agent.runtime_state.task_mode = "bugfix"
        agent.runtime_state.requested_paths = ["api.py"]
        with bind_tool_state(txn=agent.txn, change_tracker=agent.change_tracker):
            result = dispatch("write_file", {"path": "api.py", "content": "VALUE = 1\n"})
        assert not result.startswith("Error:")
        messages = []

        agent._refresh_patch_risk(messages)

        report = agent.runtime_state.patch_risk
        assert report["requires_replan"] is True
        assert any(
            item["category"] == "deleted_public_symbols"
            for item in report["risk_signals"]
        )
        assert agent.run_evidence.impact_review["fingerprint"] == report["fingerprint"]
        assert any("<patch-risk-review>" in item.get("content", "") for item in messages)
        assert not (tmp / ".git").exists()

        agent._refresh_patch_risk(messages)

        assert sum(
            "<patch-risk-review>" in item.get("content", "")
            for item in messages
        ) == 1

        target.write_text(
            "def public_api(value):\n    return value\n",
            encoding="utf-8",
        )
        agent._refresh_patch_risk(messages)

        assert agent.runtime_state.patch_risk["affected_files"] == []
        assert agent.runtime_state.patch_risk["requires_replan"] is False
        assert agent.runtime_state.has_diff is False
    finally:
        _restore_workdir(old, tmp)


def test_patch_risk_reuses_project_profile_from_prompt(monkeypatch):
    import nz_coder.intelligence.project_profile as profile_module
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    calls = []
    profile = {
        "languages": ["python"],
        "package_managers": ["pip"],
        "source_roots": ["src"],
        "test_roots": ["tests"],
        "test_commands": ["pytest"],
        "typecheck_commands": [],
        "lint_commands": [],
        "build_commands": [],
        "generated_dirs": [],
        "known_env_noise": [],
    }
    monkeypatch.setattr(
        profile_module,
        "build_project_profile",
        lambda save=False: calls.append(save) or profile,
    )
    try:
        agent = AgentLoop("test", permission_mode="auto", client=FakeClient([]), trace_enabled=False)

        assert "languages=python" in agent._project_profile_block()
        agent._refresh_patch_risk([])

        assert calls == [False]
    finally:
        _restore_workdir(old, tmp)


def test_planning_failure_does_not_block_react_loop():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    try:
        fake = FakeClient([
            RuntimeError("planner down"),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "Add a REST endpoint for users"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=True)
        status = _run_agent(agent, messages, stream=False)
        trace_text = agent.tracer.path.read_text(encoding="utf-8")

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 2
        assert agent.runtime_state.plan_generated is False
        assert messages[-1]["content"] == "done"
        assert '"event": "planning_failed"' in trace_text
        assert '"error_type": "RuntimeError"' in trace_text
        assert '"client_error": false' in trace_text
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_replan_failure_does_not_raise_or_increment_count():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True, max_replans=2, idle_turns=5)
    try:
        fake = FakeClient([RuntimeError("replan down")])
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=True)
        rs = agent.runtime_state
        rs.plan_generated = True
        rs.plan_text = "## Plan\n1. Original"
        rs.task_mode = "feature"
        rs.turn_count = 5
        rs.max_turns = 10
        rs.last_edit_turn = 0
        rs.initial_task_text = "Add a REST endpoint"
        rs.initial_plan_complexity = "moderate"

        asyncio.run(agent._maybe_replan())
        trace_text = agent.tracer.path.read_text(encoding="utf-8")

        assert fake.chat.completions.calls == 1
        assert '"event": "replan_failed"' in trace_text
        assert '"error_type": "RuntimeError"' in trace_text
        assert '"client_error": false' in trace_text
        assert agent._replan_count == 0
        assert agent.runtime_state.replan_count == 0
        assert agent.runtime_state.plan_text == "## Plan\n1. Original"
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)

class FakeDeltaFunction:
    def __init__(self, name: str = "", arguments: str = ""):
        self.name = name
        self.arguments = arguments


class FakeDeltaToolCall:
    def __init__(self, index: int, call_id: str = "call_1", name: str = "", arguments: str = ""):
        self.index = index
        self.id = call_id
        self.function = FakeDeltaFunction(name, arguments)


class FakeDelta:
    def __init__(self, content: str | None = None, tool_calls=None, reasoning_content: str | None = None):
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = reasoning_content


class FakeStreamChoice:
    def __init__(self, delta):
        self.delta = delta


class FakeStreamChunk:
    def __init__(self, delta):
        self.choices = [FakeStreamChoice(delta)]


class FakeStreamingCompletions:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        assert kwargs.get("stream") is True
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch


class FakeStreamingChat:
    def __init__(self, batches):
        self.completions = FakeStreamingCompletions(batches)


class FakeStreamingClient:
    def __init__(self, batches):
        self.chat = FakeStreamingChat(batches)


def test_streaming_context_overflow_returns_typed_compaction_outcome():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeStreamingClient([
            RuntimeError("context_length_exceeded: maximum context length is 8192 tokens")
        ])
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = agent._call_streaming([{"role": "user", "content": "large request"}])

        assert result.needs_compaction is True
        assert result.diagnostic is None
        assert "context_length_exceeded" in result.compaction_error
        assert fake.chat.completions.calls == 1
    finally:
        _restore_workdir(old, tmp)


def test_streaming_tool_turn_suppresses_preamble_output():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeStreamingClient([[
            FakeStreamChunk(FakeDelta(content="### 最终修改方案")),
            FakeStreamChunk(FakeDelta(tool_calls=[FakeDeltaToolCall(0, name="write_file", arguments="{\"path\": \"app.py\", \"content\": \"x\"}")])),
        ]])
        seen = []
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = agent._call_streaming([{"role": "user", "content": "update file"}], on_token=seen.append)
        assistant = agent._make_assistant_message(result)

        assert seen == []
        assert result.content == "### 最终修改方案"
        assert result.tool_calls[0]["function"]["name"] == "write_file"
        assert result.tool_calls[0]["function"]["arguments"] == '{"path": "app.py", "content": "x"}'
        assert assistant["content"] == ""
    finally:
        _restore_workdir(old, tmp)


def test_streaming_final_turn_emits_content_once_after_stream_end():
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        fake = FakeStreamingClient([[
            FakeStreamChunk(FakeDelta(content="hello")),
            FakeStreamChunk(FakeDelta(content=" world")),
        ]])
        seen = []
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

        result = agent._call_streaming([{"role": "user", "content": "say hi"}], on_token=seen.append)
        assistant = agent._make_assistant_message(result)

        assert seen == []
        assert result.tool_calls == []
        assert assistant["content"] == "hello world"
    finally:
        _restore_workdir(old, tmp)


def test_committed_write_batch_attaches_lsp_diagnostics_after_commit(monkeypatch):
    from nz_coder.foundation import config
    import nz_coder.loop as loop_mod
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    config.LSP_WRITE_DIAGNOSTICS_ENABLED = True
    (tmp / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp / "other.py").write_text("y = 1\n", encoding="utf-8")
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "write_file",
                    {"path": "new.py", "content": "z = 1\n"},
                    call_id="c1",
                ),
                FakeToolCall(
                    "edit_file",
                    {"path": "app.py", "old_text": "x = 1", "new_text": "x = 2"},
                    call_id="c2",
                ),
                FakeToolCall(
                    "apply_patch",
                    {
                        "changes": [{
                            "path": "other.py",
                            "old_text": "y = 1",
                            "new_text": "y = 2",
                        }],
                    },
                    call_id="c3",
                ),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "update three files"}]
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            trace_enabled=False,
        )
        observed = {}

        def fake_collect(paths, workspace):
            assert agent.txn.active is False
            observed["paths"] = list(paths)
            observed["workspace"] = workspace
            return (
                "<lsp-diagnostics>\n"
                "- other.py:1:1 [error] broken\n"
                "</lsp-diagnostics>"
            )

        monkeypatch.setattr(loop_mod, "collect_write_diagnostics", fake_collect)
        _run_agent(agent, messages, stream=False)

        assert observed["paths"] == ["new.py", "app.py", "other.py"]
        assert observed["workspace"] == tmp.resolve()
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in messages
            if message.get("role") == "tool"
        }
        assert "<lsp-diagnostics>" not in tool_messages["c1"]
        assert "<lsp-diagnostics>" not in tool_messages["c2"]
        assert "<lsp-diagnostics>" in tool_messages["c3"]
    finally:
        _restore_workdir(old, tmp)


def test_rolled_back_write_batch_never_publishes_lsp_state(monkeypatch):
    from nz_coder.foundation import config
    import nz_coder.loop as loop_mod
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    config.LSP_WRITE_DIAGNOSTICS_ENABLED = True
    calls = []
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall(
                    "write_file",
                    {"path": "app.py", "content": "x = 1\n"},
                    call_id="c1",
                ),
                FakeToolCall("missing_tool_for_rollback", {}, call_id="c2"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "write then fail"}]
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=fake,
            trace_enabled=False,
        )
        monkeypatch.setattr(
            loop_mod,
            "collect_write_diagnostics",
            lambda paths, workspace: calls.append((paths, workspace)) or "",
        )

        _run_agent(agent, messages, stream=False)

        assert calls == []
        assert not (tmp / "app.py").exists()
        assert not any(
            "<lsp-diagnostics>" in message.get("content", "")
            for message in messages
        )
    finally:
        _restore_workdir(old, tmp)


def test_agent_run_preserves_session_scratchpad():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=False)
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        agent = AgentLoop("test", permission_mode="default", client=fake, trace_enabled=False)
        scratchpad.clear()
        assert not scratchpad.update("plan", "keep this session fact").startswith("Error:")
        messages = [{"role": "user", "content": "continue"}]
        _run_agent(agent, messages, stream=False)
        assert any(entry["content"] == "keep this session fact" for entry in scratchpad.entries)
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_auto_compaction_resets_stall_sidecar_history():
    """Catches carrying pre-compaction repeats into the summarized context."""
    from nz_coder.loop import AgentLoop

    class Sidecar:
        reset_count = 0

        def reset(self):
            self.reset_count += 1

    agent = AgentLoop.__new__(AgentLoop)
    agent.session_id = "session-test"
    agent.stall_orchestrator = Sidecar()
    messages = [{"role": "user", "content": "summary", "_nz_compaction": {}}]

    agent._stamp_auto_compaction(messages)

    assert agent.stall_orchestrator.reset_count == 1


def test_approved_plan_exit_is_terminal_without_another_model_call(tmp_path):
    """Plan approval is a product boundary, not a prompt for paid narration."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall(
                "plan_exit",
                {"title": "Parser update", "summary": "- Change parser\n- Run tests"},
                call_id="plan-exit",
            ),
        ])),
    ])
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="plan",
            client=fake,
            question_asker=lambda _questions: [["Approve Plan (Recommended)"]],
            trace_enabled=False,
        )
        agent.plan_mode.write("# Parser update\n\n1. Change parser\n2. Run tests")
        messages = [{"role": "user", "content": "Create an implementation plan only"}]

        status = _run_agent(agent, messages, stream=False)
        agent.close()

    assert status["status"] == "completed"
    assert "Parser update" in status["content"]
    assert "Change parser" in status["content"]
    assert fake.chat.completions.calls == 1


def test_plan_exit_can_resume_build_in_same_run(tmp_path):
    """Explicit implement choice keeps the model loop alive after approval."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    fake = FakeClient([
        FakeResponse(FakeMessage(tool_calls=[
            FakeToolCall(
                "plan_exit",
                {"title": "Parser update", "summary": "- Implement\n- Verify"},
                call_id="plan-exit",
            ),
        ])),
        FakeResponse(FakeMessage("Implementation phase complete.")),
    ])
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="plan",
            client=fake,
            question_asker=lambda _questions: [["Implement in This Session"]],
            trace_enabled=False,
        )
        agent.plan_mode.write("# Parser update\n\n1. Implement\n2. Verify")

        status = _run_agent(
            agent,
            [{"role": "user", "content": "Plan and then implement the parser update"}],
            stream=False,
        )
        agent.close()

    assert status["status"] == "completed"
    assert status["content"] == "Implementation phase complete."
    assert fake.chat.completions.calls == 2


def test_agent_close_cancels_and_settles_stall_sidecar():
    """Explicit disposal must retire Provider work owned by the old Session."""
    from nz_coder.loop import AgentLoop

    class Sidecar:
        calls: list[float] = []

        def cancel_and_settle(self, timeout=0):
            self.calls.append(timeout)
            return True

    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=FakeClient([]),
        trace_enabled=False,
    )
    sidecar = Sidecar()
    agent.stall_orchestrator = sidecar

    agent.close()

    assert sidecar.calls == [0.5]


def test_identical_third_tool_call_is_blocked_before_dispatch():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_threshold = config.DOOM_LOOP_THRESHOLD
    config.DOOM_LOOP_THRESHOLD = 3
    try:
        repeated = {"path": ".", "depth": 1}
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", repeated, call_id="c1"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", repeated, call_id="c2"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", repeated, call_id="c3"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "inspect the workspace"}]
        agent = AgentLoop(
            "test", permission_mode="auto", client=fake, trace_enabled=False,
            stall_sidecar=lambda _prompt: {"is_stuck": False},
        )

        status = _run_agent(agent, messages, stream=False)

        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in messages
            if message.get("role") == "tool"
        }
        assert status["status"] == "blocked"
        assert fake.chat.completions.calls == 3
        assert agent.tool_calls_this_run == 2
        assert "Doom loop detected" not in tool_messages["c1"]
        assert "Doom loop detected" not in tool_messages["c2"]
        assert "Doom loop detected" in tool_messages["c3"]
        assert any(
            message.get("role") == "user"
            and "<doom-loop-diagnostic>" in message.get("content", "")
            for message in messages
        )
    finally:
        config.DOOM_LOOP_THRESHOLD = old_threshold
        _restore_workdir(old, tmp)


def test_interleaved_stall_sidecar_nudge_blocks_only_the_following_call():
    from nz_coder.foundation import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_threshold = config.DOOM_LOOP_THRESHOLD
    config.DOOM_LOOP_THRESHOLD = 3
    try:
        repeated = {"path": ".", "depth": 1}
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", repeated, call_id="c1"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", repeated, call_id="c2"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", {"path": ".", "depth": 2}, call_id="c3"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", repeated, call_id="c4"),
            ])),
            FakeResponse(FakeMessage(tool_calls=[
                FakeToolCall("list_directory", {"path": ".", "depth": 9}, call_id="c5"),
            ])),
            FakeResponse(FakeMessage("done")),
        ])
        messages = [{"role": "user", "content": "inspect with a refined query"}]
        agent = AgentLoop(
            "test", permission_mode="auto", client=fake, trace_enabled=False,
            stall_sidecar=lambda _signal: {"is_stuck": True, "nudge": "Change approach."},
        )

        status = _run_agent(agent, messages, stream=False)

        assert status["status"] == "completed"
        assert agent.tool_calls_this_run == 4
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in messages
            if message.get("role") == "tool"
        }
        assert "Doom loop detected" not in tool_messages["c4"]
        assert "Change approach." in tool_messages["c5"]
        assert any(
            "Change approach." in message.get("content", "")
            for message in messages
            if message.get("role") == "tool"
        )
    finally:
        config.DOOM_LOOP_THRESHOLD = old_threshold
        _restore_workdir(old, tmp)


def test_provider_stall_sidecar_coerces_infcodex_camel_case_string_boolean():
    """Catches dropping a semantically valid L2 verdict due to JSON quirks."""
    from nz_coder.loop import AgentLoop

    fake = FakeClient([
        FakeResponse(FakeMessage(
            '{"isStuck":"true","reason":"same result","nudge":"Use grep_search."}'
        )),
    ])
    agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

    verdict = agent._provider_stall_sidecar("signal plus transcript")

    assert verdict == {
        "is_stuck": True,
        "reason": "same result",
        "nudge": "Use grep_search.",
        "trace": "coerced_string_bool",
    }


def test_provider_stall_sidecar_forces_structured_report_tool():
    """DeepSeek may put reasoning outside content; the report call is authoritative."""
    from nz_coder.loop import AgentLoop

    fake = FakeClient([
        FakeResponse(FakeMessage(
            reasoning_content="The repeated verification is legitimate.",
            tool_calls=[FakeToolCall(
                "report_stall_judgment",
                {
                    "isStuck": False,
                    "reason": "workspace evidence changed",
                    "suggestedTool": "",
                    "nudge": "",
                },
            )],
        )),
    ])
    agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)

    verdict = agent._provider_stall_sidecar("signal plus transcript")

    assert verdict == {
        "is_stuck": False,
        "reason": "workspace evidence changed",
        "nudge": "",
        "trace": "sidecar_ok",
    }
    request = fake.chat.completions.requests[0]
    assert request["tool_choice"] == {
        "type": "function",
        "function": {"name": "report_stall_judgment"},
    }
    assert request["tools"][0]["function"]["name"] == "report_stall_judgment"


def test_provider_stall_sidecar_cancel_retires_gateway_poll():
    """Terminal settlement must not leave L2 usage for the next run."""
    import threading

    from nz_coder.loop import AgentLoop

    started = threading.Event()
    release = threading.Event()
    fake = FakeClient([])

    def blocking_create(**_kwargs):
        started.set()
        release.wait(timeout=2)
        return FakeResponse(FakeMessage("late"))

    fake.chat.completions.create = blocking_create
    agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
    cancel_event = threading.Event()
    outcome = {}
    worker = threading.Thread(
        target=lambda: outcome.setdefault(
            "verdict",
            agent._provider_stall_sidecar("signal", cancel_event),
        )
    )
    worker.start()
    assert started.wait(timeout=1)
    cancel_event.set()
    worker.join(timeout=1)
    release.set()

    assert not worker.is_alive()
    assert outcome["verdict"]["trace"] == "cancelled"
    assert agent.runtime_state.provider_calls_by_purpose == {
        "stall_sidecar": 1,
    }


def test_real_agent_awaits_async_stop_hook_revise_then_accept():
    """Catches the production natural-stop path using only synchronous hooks."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision

    decisions = iter((
        StopHookDecision("reanimate", "Correct the missing import.", "sidecar-verifier"),
        StopHookDecision(),
    ))
    seen = []

    async def verifier(context):
        seen.append(context)
        return next(decisions)

    fake = FakeClient([
        FakeResponse(FakeMessage("First answer")),
        FakeResponse(FakeMessage("Corrected answer")),
    ])
    messages = [{"role": "user", "content": "Fix the import"}]
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=fake,
        hooks=AgentHooks(stop_hooks=[verifier]),
        trace_enabled=False,
    )

    status = _run_agent(agent, messages, stream=False)

    assert status["status"] == "completed"
    assert fake.chat.completions.calls == 2
    assert len(seen) == 2
    assert seen[0].transcript[-1]["content"] == "First answer"
    assert any(
        message.get("_nz_stop_hook") is True
        and "Correct the missing import." in message.get("content", "")
        for message in messages
    )


def test_real_agent_async_stop_hook_blocked_surfaces_reason_without_extra_turn():
    """Catches a blocked verdict being finalized as an ordinary completion."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision

    async def verifier(_context):
        return StopHookDecision(
            "abort",
            "Grant repository access.",
            "sidecar-verifier",
        )

    fake = FakeClient([FakeResponse(FakeMessage("Cannot proceed"))])
    messages = [{"role": "user", "content": "Modify private repository"}]
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=fake,
        hooks=AgentHooks(stop_hooks=[verifier]),
        trace_enabled=False,
    )

    status = _run_agent(agent, messages, stream=False)

    assert status["status"] == "stopped_by_hook"
    assert fake.chat.completions.calls == 1
    assert agent.hooks.stop_hook_reason == "Grant repository access."


def test_agent_injected_sidecar_is_first_and_injected_clients_stay_opt_in():
    """Catches test seams gaining hidden calls or fallback hooks running first."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision

    async def fallback(_context):
        return StopHookDecision()

    async def sidecar(_context):
        return StopHookDecision()

    hooks = AgentHooks(stop_hooks=[fallback])
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=FakeClient([]),
        hooks=hooks,
        sidecar_verifier=sidecar,
        trace_enabled=False,
    )

    assert agent.hooks.stop_hooks == [sidecar, fallback]
    agent.close()

    inert = AgentLoop(
        "test",
        permission_mode="auto",
        client=FakeClient([]),
        trace_enabled=False,
    )
    assert not hasattr(inert, "_sidecar_verifier_handle")
    inert.close()


def test_agent_owned_client_installs_production_sidecar_by_default():
    """Catches the verifier existing as library code but missing from products."""
    from nz_coder.loop import AgentLoop

    class Provider:
        name = "openai-compatible"

        def __init__(self):
            self.client = FakeClient([])

        def create_client(self):
            return self.client

    provider = Provider()
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        provider=provider,
        trace_enabled=False,
    )

    assert agent.hooks.stop_hooks[0] is agent._sidecar_verifier_handle
    assert agent._sidecar_verifier_handle._resolved.client is provider.client
    assert agent._sidecar_verifier_handle._resolved.source == "inherit-main"
    agent.close()


def test_production_sidecar_runs_isolated_request_after_real_agent_stop(tmp_path):
    """Catches production assembly that installs a hook but never invokes it."""
    from nz_coder.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.trace import TraceRecorder

    class Provider:
        name = "openai-compatible"

        def __init__(self):
            self.client = object()
            self.requests = []

        def create_client(self):
            return self.client

        def create_completion(self, _client, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                return FakeResponse(FakeMessage("Implemented the requested change."))
            return FakeResponse(FakeMessage(
                "",
                [FakeToolCall(
                    "emit_sidecar_verdict",
                    {"verdict": "accept", "reason": "The request is satisfied."},
                )],
            ))

    provider = Provider()
    tracer = TraceRecorder(trace_dir=tmp_path / "traces", enabled=True)
    messages = [{"role": "user", "content": "Explain the parser behavior in detail"}]
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            provider=provider,
            tracer=tracer,
        )
        status = _run_agent(agent, messages, stream=False)
        agent.close()

    assert status["status"] == "completed"
    assert len(provider.requests) == 2
    judge_request = provider.requests[1]
    assert [message["role"] for message in judge_request["messages"]] == [
        "system",
        "user",
    ]
    assert len(judge_request["tools"]) == 1
    assert judge_request["tool_choice"]["function"]["name"] == "emit_sidecar_verdict"
    events = [json.loads(line)["event"] for line in tracer.path.read_text().splitlines()]
    assert "sidecar_gate_decision" in events
    assert "sidecar_started" in events
    assert "sidecar_finished" in events
