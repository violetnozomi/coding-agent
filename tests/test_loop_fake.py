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
    def __init__(self, content: str = "", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


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

    def create(self, **kwargs):
        self.calls += 1
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
                FakeToolCall("write_file", {"path": "hello.txt", "content": "hello"})
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
