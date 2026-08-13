"""Tests for the blocking-Agent to async-terminal interaction bridge."""
from __future__ import annotations

import asyncio

from nz_coder.interface.interactions import (
    TerminalInteractionBridge,
    bind_terminal_interactions,
)
from nz_coder.permissions import PermissionManager
from nz_coder.tools import dispatch
from nz_coder.tools.question import scoped_question_asker


class FakeRenderer:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1


class FakeTerminalInput:
    interactive = True

    def __init__(self, results) -> None:
        self.results = list(results)
        self.requests = []

    async def select_async(self, **kwargs):
        self.requests.append(kwargs)
        return self.results.pop(0)


def _question(*, multiple: bool = False) -> dict:
    return {
        "header": "Storage",
        "question": "Which storage backend should be used?",
        "multiple": multiple,
        "options": [
            {"label": "SQLite", "description": "Local database."},
            {"label": "PostgreSQL", "description": "External service."},
        ],
    }


def test_permission_bridge_returns_infcode_reply_from_worker_thread():
    async def scenario():
        terminal = FakeTerminalInput(["always"])
        renderer = FakeRenderer()
        bridge = TerminalInteractionBridge(terminal, renderer, asyncio.get_running_loop())
        result = await asyncio.to_thread(
            bridge.ask_permission,
            "edit_file",
            {"path": "app.py"},
        )
        return result, terminal, renderer

    result, terminal, renderer = asyncio.run(scenario())

    assert result == "always"
    assert terminal.requests[0]["values"][0] == ("once", "Allow once")
    assert "edit_file: app.py" in terminal.requests[0]["text"]
    assert renderer.paused == renderer.resumed == 1


def test_question_bridge_supports_single_multiple_and_custom_answers():
    async def scenario():
        terminal = FakeTerminalInput([
            "Use the existing database",
            ("SQLite", "PostgreSQL"),
        ])
        renderer = FakeRenderer()
        bridge = TerminalInteractionBridge(terminal, renderer, asyncio.get_running_loop())
        answers = await asyncio.to_thread(
            bridge.ask_question,
            [_question(), _question(multiple=True)],
        )
        return answers, terminal, renderer

    answers, terminal, renderer = asyncio.run(scenario())

    assert answers == [
        ["Use the existing database"],
        ["SQLite", "PostgreSQL"],
    ]
    assert terminal.requests[0]["allow_custom"] is True
    assert terminal.requests[0]["multiple"] is False
    assert terminal.requests[1]["multiple"] is True
    assert renderer.paused == renderer.resumed == 2


def test_question_bridge_cancel_dismisses_remaining_questions():
    async def scenario():
        terminal = FakeTerminalInput([None])
        renderer = FakeRenderer()
        bridge = TerminalInteractionBridge(terminal, renderer, asyncio.get_running_loop())
        result = await asyncio.to_thread(bridge.ask_question, [_question(), _question()])
        return result, terminal, renderer

    result, terminal, renderer = asyncio.run(scenario())

    assert result is None
    assert len(terminal.requests) == 1
    assert renderer.paused == renderer.resumed == 1


def test_bridge_conservatively_rejects_on_event_loop_thread():
    async def scenario():
        terminal = FakeTerminalInput(["once"])
        renderer = FakeRenderer()
        bridge = TerminalInteractionBridge(terminal, renderer, asyncio.get_running_loop())
        return bridge.ask_permission("write_file", {"path": "x.py"}), terminal

    result, terminal = asyncio.run(scenario())

    assert result == "reject"
    assert terminal.requests == []


def test_bind_terminal_interactions_updates_both_agent_askers():
    class Agent:
        def set_interaction_askers(self, **kwargs):
            self.askers = kwargs

    async def scenario():
        agent = Agent()
        bridge = bind_terminal_interactions(
            agent,
            FakeTerminalInput([]),
            FakeRenderer(),
        )
        return agent, bridge

    agent, bridge = asyncio.run(scenario())

    assert agent.askers["permission_asker"].__self__ is bridge
    assert agent.askers["question_asker"].__self__ is bridge


def test_permission_manager_always_reply_installs_scoped_rule_through_bridge():
    async def scenario():
        terminal = FakeTerminalInput(["always"])
        bridge = TerminalInteractionBridge(
            terminal,
            FakeRenderer(),
            asyncio.get_running_loop(),
        )
        manager = PermissionManager("default", asker=bridge.ask_permission)
        allowed = await asyncio.to_thread(
            manager.ask_user,
            "bash",
            {"command": "git status"},
        )
        return allowed, manager

    allowed, manager = asyncio.run(scenario())

    assert allowed is True
    assert manager.check("bash", {"command": "git diff"})["behavior"] == "allow"
    assert manager.check("bash", {"command": "python build.py"})["behavior"] == "ask"


def test_question_tool_dispatch_uses_bridge_through_copied_thread_context():
    async def scenario():
        terminal = FakeTerminalInput(["SQLite"])
        bridge = TerminalInteractionBridge(
            terminal,
            FakeRenderer(),
            asyncio.get_running_loop(),
        )
        with scoped_question_asker(bridge.ask_question):
            return await asyncio.to_thread(
                dispatch,
                "question",
                {"questions": [_question()]},
            )

    result = asyncio.run(scenario())

    assert result == (
        'User has answered your questions: "Which storage backend should be used?"='
        '"SQLite". You can now continue with the user\'s answers in mind.'
    )
