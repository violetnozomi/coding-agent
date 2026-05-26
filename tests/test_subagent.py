"""Tests for subagent isolation and context handoff."""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from pathlib import Path


class FakeFunction:
    def __init__(self, name: str, arguments):
        self.name = name
        self.arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)


class FakeToolCall:
    def __init__(self, name: str, arguments, call_id: str = "call_1"):
        self.id = call_id
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []

    def model_dump(self):
        return {
            "role": "assistant",
            "content": self.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in self.tool_calls
            ],
        }


class FakeChoice:
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


class FakeCompletions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


class FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeClient:
    def __init__(self, responses):
        self.chat = FakeChat(FakeCompletions(responses))


def _tmp_workdir():
    from nz_coder import config

    old = config.WORKDIR
    tmp = Path(tempfile.mkdtemp())
    config.WORKDIR = tmp
    return old, tmp


def _restore_workdir(old, tmp):
    from nz_coder import config

    config.WORKDIR = old
    shutil.rmtree(str(tmp), ignore_errors=True)


def test_subagent_exposes_repo_intel_tools():
    from nz_coder.subagent import _subagent_tools

    explore_names = {spec["function"]["name"] for spec in _subagent_tools("explore")}
    general_names = {spec["function"]["name"] for spec in _subagent_tools("general")}

    assert {"smart_search", "read_symbol", "find_symbol_callers"} <= explore_names
    assert "write_file" not in explore_names
    assert {"write_file", "edit_file", "verify_changed_files"} <= general_names


def test_subagent_injects_parent_context(monkeypatch):
    from nz_coder import config, subagent
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    scratchpad.clear()
    try:
        state_dir = tmp / ".nz-coder"
        state_dir.mkdir()
        (state_dir / "runtime_state.json").write_text(
            json.dumps({
                "active": True,
                "turn_count": 3,
                "has_diff": True,
                "changed_files": ["django/utils/http.py"],
                "acceptance_criteria": ["tests/test_http.py::test_date"],
            }),
            encoding="utf-8",
        )
        scratchpad.update("finding", "timezone parsing points at django/utils/http.py")
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)

        result = subagent.run_subagent("explore timezone parsing")

        system = fake.chat.completions.requests[0]["messages"][0]["content"]
        assert "Parent agent context" in system
        assert "django/utils/http.py" in system
        assert "timezone parsing points" in system
        assert "[Subagent status: completed]" in result
    finally:
        scratchpad.clear()
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_subagent_persists_large_tool_output(monkeypatch):
    from nz_coder import config, subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("read_file", {"path": "huge.txt"})])),
            FakeResponse(FakeMessage("done")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent, "_run_allowed_tool", lambda *args, **kwargs: "X" * 35000)

        result = subagent.run_subagent("read huge output")

        second_messages = fake.chat.completions.requests[1]["messages"]
        tool_msg = next(msg for msg in second_messages if msg.get("role") == "tool")
        assert "<persisted-output>" in tool_msg["content"]
        assert (tmp / ".nz-coder" / "tool-results" / "subagent-call_1.txt").exists()
        assert "[Subagent status: completed]" in result
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_general_subagent_reports_verification_failure_and_rolls_back(monkeypatch):
    from nz_coder import config, subagent

    old, tmp = _tmp_workdir()
    old_turns = config.SUBAGENT_MAX_TURNS
    old_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 2
    config.SUBAGENT_TIMEOUT_SECONDS = 30
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage(tool_calls=[FakeToolCall("write_file", {"path": "app.py", "content": "bad"})])),
            FakeResponse(FakeMessage("patched")),
        ])
        monkeypatch.setattr(subagent, "OpenAI", lambda **kwargs: fake)
        monkeypatch.setattr(subagent, "_run_allowed_tool", lambda *args, **kwargs: "Created app.py")
        monkeypatch.setattr(subagent, "dispatch", lambda name, args: "FAIL: py_compile changed files\nSyntaxError")

        result = subagent.run_subagent("patch app.py", agent_type="general")

        assert "[Subagent status: verification_failed_rolled_back]" in result
        assert "SyntaxError" in result
    finally:
        config.SUBAGENT_MAX_TURNS = old_turns
        config.SUBAGENT_TIMEOUT_SECONDS = old_timeout
        _restore_workdir(old, tmp)


def test_completion_with_timeout_works_outside_main_thread():
    from nz_coder.subagent import SubagentTimeout, _completion_with_timeout

    class SlowCompletions:
        def create(self, **kwargs):
            time.sleep(0.2)
            return object()

    class SlowClient:
        class Chat:
            completions = SlowCompletions()
        chat = Chat()

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_completion_with_timeout, SlowClient(), timeout_seconds=0.01)
        try:
            future.result(timeout=1)
        except SubagentTimeout:
            pass
        else:
            raise AssertionError("expected SubagentTimeout")


def test_subagent_review_and_test_tiers_do_not_expose_write_tools():
    from nz_coder.subagent import _subagent_tools

    review_names = {spec["function"]["name"] for spec in _subagent_tools("review")}
    test_names = {spec["function"]["name"] for spec in _subagent_tools("test")}

    assert "bash" in review_names
    assert "bash" in test_names
    assert "write_file" not in review_names
    assert "edit_file" not in test_names
    assert "verify_changed_files" not in test_names
