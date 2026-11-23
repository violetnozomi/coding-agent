"""远程会话切换恢复真实 HTTP 待交互，选择器使用独立管道终端。"""
from __future__ import annotations

import argparse
import asyncio
import io
import threading
import time
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from nz_coder.http_service import NZCoderClient, SessionHTTPService, SessionManager
from nz_coder.interface import remote
from nz_coder.interface.selector import FuzzySelector
from nz_coder.tools.question import question, scoped_question_asker
from tests.test_http_service import FakeAgent


class QuestionToolAgent(FakeAgent):
    async def run(self, messages, stream=False):
        self.event_bus.publish("session.run.started", {"stream": stream})
        if messages[-1]["content"] == "permission":
            reply = self.permission_asker("write_file", {"path": "demo.py", "content": "ok"})
            answer = f"permission:{reply}"
        else:
            with scoped_question_asker(self.question_asker):
                result = question([{
                    "header": "Scope", "question": "Which scopes?", "multiple": True,
                    "options": [
                        {"label": "Repository", "description": "All files"},
                        {"label": "Current file", "description": "One file"},
                    ],
                }])
            self.answers = result.metadata["answers"]
            answer = str(result)
        messages.append({"role": "assistant", "content": answer})
        self.event_bus.publish("session.message.completed", {"content": answer})
        return {"status": "completed_unverified", "answer": answer}


@pytest.mark.parametrize("command, interaction", [
    ("/resume", "permission"), ("/attach", "question"),
    ("/resume", None), ("/attach", None),
])
def test_switch_session_follows_pending_interaction_before_next_input(
    tmp_path, monkeypatch, command, interaction,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    manager = SessionManager(
        agent_factory=QuestionToolAgent, workspace_roots=[workspace], restore_saved=False,
        interaction_timeout_seconds=10,
    )
    service = SessionHTTPService(port=0, manager=manager, token="offline-switch-token-1234")
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    client = NZCoderClient(service.base_url, service.token, timeout=2)
    first = client.create_session()["id"]
    second = client.create_session()["id"]
    session = manager.get(second)
    reads = []
    prompts = []
    output = io.StringIO()
    original_init = remote.TerminalInput.__init__

    def terminal_init(self, **kwargs):
        kwargs.update(interactive=True, prompt_session=object())
        original_init(self, **kwargs)

    async def read(self):
        reads.append(self.state_provider())
        if len(reads) == 1:
            return f"{command} {second}"
        assert self.state_provider()["session"] == second
        assert not session.info()["running"], "切换后须先恢复交互并等待服务端结算"
        assert session.interactions.list() == []
        raise EOFError

    async def select(self):
        prompts.append(self.title)
        with create_pipe_input() as pipe:
            app = self.application(input=pipe, output=DummyOutput())

            def submit():
                self._mouse_handler(0)(SimpleNamespace(event_type=MouseEventType.MOUSE_UP))
                if self.multiple:
                    self._mouse_handler(1)(SimpleNamespace(event_type=MouseEventType.MOUSE_UP))
                    pipe.send_text("\r")

            return await asyncio.wait_for(app.run_async(pre_run=submit), timeout=2)

    monkeypatch.setattr(remote, "_connection", lambda args: (service.base_url, service.token, "", True))
    monkeypatch.setattr(remote.TerminalInput, "__init__", terminal_init)
    monkeypatch.setattr(remote.TerminalInput, "read_async", read)
    monkeypatch.setattr(FuzzySelector, "run_async", select)
    try:
        if interaction:
            client.run(second, interaction)
            deadline = time.monotonic() + 3
            while not session.interactions.list() and time.monotonic() < deadline:
                time.sleep(.005)
            assert len(session.interactions.list()) == 1
        args = argparse.Namespace(new=False, session_id=first, workspace_id=None)
        assert asyncio.run(remote._attach(args, Console(file=output))) == 0
        assert len(reads) == 2
        assert len(prompts) == (1 if interaction else 0)
        if interaction:
            events = [event.to_dict() for event in session.event_bus.recent()]
            replies = [event for event in events if event["type"] == f"{interaction}.replied"]
            assert len(replies) == 1, "快照与事件中的同一交互只能回复一次"
            assert "session.run.settled" in [event["type"] for event in events]
            assert session.info()["status"] == "completed_unverified"
            assert reads[-1]["run_state"] == "completed_unverified"
            assert "Run finished (see checks and delivery review)" in output.getvalue()
            if interaction == "question":
                assert session.agent.answers == [["Repository", "Current file"]]
                assert "Repository" in client.messages(second)[-1]["content"]
            else:
                assert replies[0]["properties"]["reply"] == "once"
                assert client.messages(second)[-1]["content"] == "permission:once"
    finally:
        for item in session.interactions.list():
            if item["kind"] == "permission":
                client.reply_permission(second, item["id"], "reject")
            else:
                client.reject_question(second, item["id"])
        session.wait(timeout=3)
        service.shutdown()
        thread.join(timeout=5)
