"""Tests for persisted InfCode-style Session usage statistics."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from nz_coder.interface.commands import build_default_registry
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.protocol.message_schema import attach_message_identity
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.session_stats import aggregate_session_stats, render_session_stats
from nz_coder.state.sessions import save_session


def _assistant(session_id: str, message_id: str, *, priced: bool) -> dict:
    message = {
        "role": "assistant",
        "content": "done",
        "_nz_provider_id": "openrouter",
        "_nz_model_id": "vendor/code-model",
        "_nz_usage": {
            "input": 80 if priced else 5,
            "output": 30 if priced else 0,
            "total": 140 if priced else 5,
            "reasoning": 10 if priced else 0,
            "cache_read": 20 if priced else 0,
            "cache_write": 2 if priced else 0,
        },
    }
    attach_message_identity(message, message_id, session_id=session_id)
    if priced:
        message["_nz_cost"] = 0.35
        message["_nz_child_cost"] = 0.25
        message["_nz_parts"] = [
            {
                "id": "part-finish",
                "message_id": message_id,
                "type": "step-finish",
                "reason": "tool-calls",
                "cost": 0.10,
                "tokens": {"input": 80, "output": 30, "total": 140},
                "time": {"start": 1.0, "end": 2.0},
            },
            {
                "id": "part-task",
                "message_id": message_id,
                "type": "tool",
                "tool": "task",
                "call_id": "call-task",
                "state": {
                    "status": "completed",
                    "input": {"prompt": "inspect"},
                    "output": "child done",
                    "time": {"start": 1.0, "end": 2.0},
                },
            },
        ]
    return message


def test_stats_aggregate_parent_cost_once_and_keep_child_model_usage(tmp_path):
    from nz_coder.runtime.agent import subagent

    with scoped_workdir(tmp_path):
        session_id = "session-stats"
        messages = [
            {"role": "user", "content": "solve"},
            _assistant(session_id, "msg-priced", priced=True),
            _assistant(session_id, "msg-unpriced", priced=False),
        ]
        save_session(messages, session_id=session_id)
        child = subagent._new_subagent_state(session_id, "explore", None)
        child.update({
            "provider_id": "openrouter",
            "model_id": "vendor/child-model",
            "status": "completed",
            "messages": [{"role": "assistant", "content": "child done"}],
            "tokens": {
                "input": 20,
                "output": 5,
                "total": 25,
                "reasoning": 0,
                "cache_read": 0,
                "cache_write": 0,
            },
            "cost": 0.25,
            "cost_known": True,
        })
        subagent._save_subagent_state(session_id, child, tmp_path)

        stats = aggregate_session_stats()

    assert stats["total_sessions"] == 2
    assert stats["top_level_sessions"] == 1
    assert stats["child_sessions"] == 1
    assert stats["total_messages"] == 4
    assert stats["total_cost"] == pytest.approx(0.35)
    assert stats["unattributed_background_cost"] == 0
    assert stats["complete_cost"] is False
    assert stats["unpriced_assistant_messages"] == 1
    assert stats["total_tokens"] == {
        "input": 105,
        "output": 35,
        "reasoning": 10,
        "cache_read": 20,
        "cache_write": 2,
    }
    assert stats["model_usage"]["openrouter/vendor/code-model"]["cost"] == pytest.approx(0.10)
    assert stats["model_usage"]["openrouter/vendor/child-model"]["cost"] == pytest.approx(0.25)
    assert stats["tool_usage"] == {"task": 1}
    assert "known requests only" in render_session_stats(stats)


def test_stats_command_is_registered_and_rejects_invalid_days(tmp_path):
    class Console:
        def __init__(self):
            self.values = []

        def print(self, value="", *_args, **_kwargs):
            self.values.append(str(value))

    console = Console()
    agent = SimpleNamespace(session_id="session-a")
    context = CommandContext(
        history=[],
        session_state={"id": "session-a", "agent": agent},
        system_prompt="system",
        renderer=object(),
        console=console,
        build_agent=lambda *_args: agent,
    )
    registry = build_default_registry()

    with scoped_workdir(tmp_path):
        assert registry.dispatch("/stats invalid", context) is True

    assert any("Stats error" in value for value in console.values)
    assert "stats" in {command.name for command in registry.visible_commands()}


def test_stats_date_range_prefers_message_times_over_file_mtime(tmp_path):
    messages = [
        {
            "role": "user",
            "content": "inspect",
            "_nz_time": {"created": 100.0},
        },
        {
            "role": "assistant",
            "content": "done",
            "_nz_time": {"created": 110.0, "completed": 120.0},
        },
    ]

    with scoped_workdir(tmp_path):
        path = save_session(messages, session_id="session-message-time")
        path.touch()
        stats = aggregate_session_stats()

    assert stats["date_range"] == {"earliest": 100.0, "latest": 120.0}
