"""Agent loop tests with a fake OpenAI-compatible client."""

import json
import shutil
import tempfile
from pathlib import Path


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
    def __init__(self, message):
        self.message = message


class FakeResponse:
    def __init__(self, message):
        self.choices = [FakeChoice(message)]


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
        agent.run(messages, stream=False)

        assert (tmp / "hello.txt").read_text(encoding="utf-8") == "hello"
        assert any(m.get("role") == "tool" and "Created hello.txt" in m.get("content", "") for m in messages)
        assert fake.chat.completions.calls == 2
    finally:
        _restore_workdir(old, tmp)


def test_loop_requires_verification_after_writes():
    from nz_coder import config
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
        status = agent.run(messages, stream=False)

        assert status["status"] == "completed_unverified"
        assert status["verification_needed"] is True
        assert any(
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
        agent.run(messages, stream=False)

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
        agent.run(messages, stream=False)

        assistant_messages = [m for m in messages if m.get("role") == "assistant"]
        assert assistant_messages[0]["reasoning_content"] == "private provider reasoning token"
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
        agent.run(messages, stream=False)

        assert fake.chat.completions.calls == 3
        assert messages[-1]["content"] == "recovered"
    finally:
        _restore_workdir(old, tmp)


def test_loop_writes_trace_events():
    from nz_coder.loop import AgentLoop
    from nz_coder.trace import TraceRecorder

    old, tmp = _tmp_workdir()
    try:
        fake = FakeClient([
            FakeResponse(FakeMessage("done")),
        ])
        tracer = TraceRecorder(trace_dir=tmp / "traces", enabled=True)
        messages = [{"role": "user", "content": "hello"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, tracer=tracer)
        agent.run(messages, stream=False)

        text = tracer.path.read_text(encoding="utf-8")
        assert '"event": "run_start"' in text
        assert '"event": "llm_response"' in text
        assert '"event": "run_end"' in text
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
        status = agent.run(messages, stream=False)

        namespace = {}
        exec((tmp / "user_manager.py").read_text(encoding="utf-8"), namespace)
        validate_email = namespace["validate_email"]
        UserManager = namespace["UserManager"]

        assert status["status"] == "completed"
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
    from nz_coder import config
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
        agent.run(messages, stream=False)

        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["c1", "c2"]
        assert all("Too many tool calls" not in m["content"] for m in tool_msgs)
        assert agent.tool_calls_this_run == 2
    finally:
        config.MAX_TOOL_CALLS_PER_RESPONSE = old_limit
        _restore_workdir(old, tmp)


def test_tool_call_limit_write_dispatches_only_prefix():
    """写工具批次也只能执行前缀，不能在串行分支绕过上限。"""
    from nz_coder import config
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
        agent.run(messages, stream=False)

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
        agent.run(messages, stream=False)

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
        agent.run(messages, stream=False)

        # 不应出现 transaction-rollback
        assert not any(
            "<transaction-rollback>" in m.get("content", "")
            for m in messages if m.get("role") == "user"
        )
    finally:
        _restore_workdir(old, tmp)


def test_400_api_error_injects_diagnostic():
    """400 客户端错误应注入诊断消息到对话，而不是无限重试。"""
    from openai import BadRequestError
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    try:
        # 构造一个 BadRequestError（400）
        import httpx

        http_response = httpx.Response(
            400,
            json={"error": {"message": "invalid json in tool", "type": "invalid_request_error"}},
            request=httpx.Request("POST", "https://api.example.com/v1/chat/completions"),
        )
        bad_err = BadRequestError(
            "invalid json in tool",
            response=http_response,
            body={"error": {"message": "invalid json in tool", "type": "invalid_request_error"}},
        )

        fake = FakeClient([
            bad_err,
            FakeResponse(FakeMessage("corrected")),
        ])
        messages = [{"role": "user", "content": "test"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        status = agent.run(messages, stream=False)

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

    assert stable == "SYSTEM\nMEMORY\n"
    assert "system-reminder" not in stable
    assert "Working Memory" not in stable
    assert injected[0]["role"] == "user"
    assert "<context-injection>" in injected[0]["content"]
    assert injected[0]["content"].endswith("fix the bug")
    assert stats["before_total_tokens"] == stats["after_total_tokens"]


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
    from nz_coder import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=False)
    scratchpad.clear()
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        messages = [{"role": "user", "content": "Add a REST endpoint for users"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        status = agent.run(messages, stream=False)

        assert status["status"] == "completed"
        assert fake.chat.completions.calls == 1
        assert not any(e["category"] == "plan" for e in scratchpad.entries)
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_planning_enabled_generates_plan_for_feature_task():
    from nz_coder import config
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
        status = agent.run(messages, stream=False)

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


def test_planning_enabled_skips_simple_bugfix_task():
    from nz_coder import config
    from nz_coder.loop import AgentLoop

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    try:
        fake = FakeClient([FakeResponse(FakeMessage("done"))])
        messages = [{"role": "user", "content": "fix bug"}]
        agent = AgentLoop("test", permission_mode="auto", client=fake, trace_enabled=False)
        agent.run(messages, stream=False)

        assert fake.chat.completions.calls == 1
        assert agent.runtime_state.plan_generated is False
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_planning_restore_hydrates_plan_without_new_planning_call():
    import json

    from nz_coder import config
    from nz_coder.loop import AgentLoop
    from nz_coder.tools.scratchpad import scratchpad

    old, tmp = _tmp_workdir()
    old_planning = _set_planning_config(config, enabled=True)
    scratchpad.clear()
    try:
        state_dir = tmp / ".nz-coder"
        state_dir.mkdir()
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
        agent.run(messages, stream=False)

        assert fake.chat.completions.calls == 1
        assert any(e["category"] == "plan" and "Existing plan" in e["content"] for e in scratchpad.entries)
        assert agent._replan_count == 1
    finally:
        scratchpad.clear()
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_replan_respects_max_attempts():
    from nz_coder import config
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
        agent._maybe_replan([])
        assert agent.runtime_state.replan_count == 1
        assert agent._should_replan() is False
    finally:
        _restore_planning_config(config, old_planning)
        _restore_workdir(old, tmp)


def test_replan_verification_guard_requires_diff():
    from nz_coder import config
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


def test_planning_failure_does_not_block_react_loop():
    from nz_coder import config
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
        status = agent.run(messages, stream=False)
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
    from nz_coder import config
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

        agent._maybe_replan([])
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
