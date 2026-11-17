"""Behavior regressions for tool counts in terminal run summaries."""
from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

import pytest
from rich.console import Console

from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.protocol.session_events import SessionEventBus
from tests.test_run_renderer import FakeStreamingRenderer


@pytest.fixture
def terminal():
    output = StringIO()
    view = TerminalRunRenderer(
        Console(file=output, force_terminal=False, width=100),
        FakeStreamingRenderer(),
    )
    bus = SessionEventBus(session_id="count-session")
    agent = SimpleNamespace(event_bus=bus, model_id="offline-count")
    view.begin(agent)
    yield SimpleNamespace(view=view, output=output, bus=bus, agent=agent)
    view.close()
    bus.close()


def _tool(call_id="call-1", *, status="error", owner="", part_id="part-1"):
    part = {
        "id": part_id,
        "message_id": "msg-1",
        "type": "tool",
        "tool": "bash",
        "call_id": call_id,
        "state": {
            "status": status,
            "input": {"command": "controlled-command"},
            "time": {"start": 1.0},
        },
    }
    if status == "error":
        part["state"].update(error="Tool execution aborted", interrupted=True)
    if owner:
        part["interaction_run_id"] = owner
    return part


def _record(message_id, parts, *, owner=""):
    info = {"id": message_id, "role": "assistant"}
    if owner:
        info["interaction_run_id"] = owner
    return {
        "info": info,
        "parts": [{**part, "message_id": message_id} for part in parts],
    }


def test_started_canonical_interrupted_call_is_counted_once(terminal):
    terminal.bus.publish("session.tool.started", {"tool_call_id": "call-1", "name": "bash"})
    terminal.bus.publish("message.part.created", {"part": _tool(status="running")})
    terminal.bus.publish("message.part.updated", {"part": _tool()})
    terminal.bus.publish("session.run.cancelled", {})

    terminal.view.cancel()

    assert "Run cancelled · 1 tool(s)" in terminal.output.getvalue()
    assert "Bash · bash" not in terminal.output.getvalue()


@pytest.mark.parametrize("status", ["pending", "running", "error", "completed"])
def test_canonical_tool_without_legacy_events_is_counted(terminal, status):
    terminal.bus.publish("message.part.updated", {"part": _tool(status=status)})

    terminal.view.cancel()

    assert "Run cancelled · 1 tool(s)" in terminal.output.getvalue()


def test_same_name_distinct_calls_are_counted_separately(terminal):
    terminal.bus.publish("message.part.updated", {"part": _tool("call-1")})
    terminal.bus.publish("message.part.updated", {"part": _tool("call-2", part_id="part-2")})

    terminal.view.cancel()

    assert "Run cancelled · 2 tool(s)" in terminal.output.getvalue()


def test_fallback_and_interrupted_call_both_contribute_to_summary(terminal):
    terminal.view.on_tool("read_file", "existing fallback result")
    terminal.bus.publish("message.part.updated", {"part": _tool()})

    terminal.view.cancel()

    assert "Run cancelled · 2 tool(s)" in terminal.output.getvalue()


@pytest.mark.parametrize("detail", ["compact", "hidden"])
def test_callback_only_same_name_calls_remain_separate(terminal, detail):
    terminal.view.close()
    terminal.view.begin(SimpleNamespace(model_id="callback-only"))
    terminal.view.detail_provider = lambda: detail
    terminal.view.on_tool("read_file", "first")
    terminal.view.on_tool("read_file", "second")

    terminal.view.finish()

    rendered = terminal.output.getvalue()
    assert "Run completed · 2 tool(s)" in rendered
    assert rendered.count("Tool · read_file") == (0 if detail == "hidden" else 2)


def test_hidden_completed_card_and_interrupted_call_are_both_counted(terminal):
    terminal.view.detail_provider = lambda: "hidden"
    terminal.bus.publish("session.tool.started", {"tool_call_id": "call-done", "name": "bash"})
    terminal.bus.publish("session.tool.completed", {
        "tool_call_id": "call-done", "name": "bash", "status": "ok", "output": "done",
    })
    terminal.view.on_tool("bash", "done")
    terminal.bus.publish("message.part.updated", {"part": _tool()})

    terminal.view.cancel()

    assert "Run cancelled · 2 tool(s)" in terminal.output.getvalue()
    assert "Bash · bash" not in terminal.output.getvalue()


@pytest.mark.parametrize("identity", [{}, {"index": 0}, {"index": None}])
def test_completed_without_call_id_uses_one_reducer_identity(terminal, identity):
    terminal.bus.publish("session.tool.started", {"name": "bash", **identity})
    terminal.bus.publish("session.tool.completed", {
        "name": "bash", "status": "ok", "output": "done", **identity,
    })
    terminal.view.on_tool("bash", "done")

    terminal.view.finish()

    rendered = terminal.output.getvalue()
    assert rendered.count("Bash · bash") == 1
    assert "Run completed · 1 tool(s)" in rendered


def test_snapshot_filters_explicit_history_and_keeps_ownerless_parts(terminal):
    current = _tool("current", owner="current")
    old_part = _tool("old-part-owner", owner="old", part_id="part-old")
    old_message = _tool("old-message-owner", part_id="part-old-message")
    ownerless = _tool("ownerless", part_id="part-ownerless")
    inferred = _tool("current-message-owner", part_id="part-inferred")
    terminal.view.rebase_remote(interaction_run_id="current", messages=[
        _record("msg-current", [current, old_part, inferred], owner="current"),
        _record("msg-old", [{**old_message, "message_id": "msg-old"}], owner="old"),
        _record("msg-legacy", [ownerless]),
    ])

    terminal.view.cancel()

    assert "Run cancelled · 3 tool(s)" in terminal.output.getvalue()


def test_snapshot_part_without_call_id_uses_stable_part_id(terminal):
    part = _tool(owner="current")
    part.pop("call_id")
    terminal.view.rebase_remote(interaction_run_id="current", messages=[
        _record("msg-1", [part], owner="current"),
    ])

    terminal.view.cancel()

    assert "Run cancelled · 1 tool(s)" in terminal.output.getvalue()


def test_snapshot_and_replay_do_not_duplicate_completed_card(terminal):
    publisher = terminal.bus.for_interaction("current")
    event = publisher.publish("session.tool.completed", {
        "tool_call_id": "call-1", "name": "bash", "status": "ok", "output": "done",
    })
    terminal.view.drain()
    terminal.view.rebase_remote(interaction_run_id="current", messages=[
        _record("msg-1", [_tool(status="completed", owner="current")], owner="current"),
    ])
    terminal.view.feed(event)
    publisher.publish("session.tool.completed", {
        "tool_call_id": "call-1", "name": "bash", "status": "ok", "output": "done",
    })

    terminal.view.finish()

    rendered = terminal.output.getvalue()
    assert rendered.count("Bash · bash") == 1
    assert "Run completed · 1 tool(s)" in rendered


@pytest.mark.parametrize("rejected_owner", ["old", ""])
def test_bound_run_rejects_foreign_and_ownerless_completed_events(terminal, rejected_owner):
    terminal.view.rebase_remote(interaction_run_id="current")
    publisher = terminal.bus.for_interaction(rejected_owner) if rejected_owner else terminal.bus
    publisher.publish("session.tool.completed", {
        "tool_call_id": "foreign-call", "name": "bash", "status": "ok", "output": "foreign",
    })

    terminal.view.finish()

    rendered = terminal.output.getvalue()
    assert "Bash · bash" not in rendered
    assert "Run completed · 0 tool(s)" in rendered


def test_unbound_run_accepts_existing_ownerless_completed_event(terminal):
    terminal.bus.publish("session.tool.completed", {
        "tool_call_id": "legacy-call", "name": "bash", "status": "ok", "output": "legacy",
    })

    terminal.view.finish()

    assert "Run completed · 1 tool(s)" in terminal.output.getvalue()


def test_same_run_process_event_still_renders_without_reducer_change(terminal):
    terminal.view.rebase_remote(interaction_run_id="current")
    terminal.bus.for_interaction("current").publish("process.started", {
        "process_id": "process-live", "command": "controlled-command", "status": "running",
    })

    terminal.view.finish()

    rendered = terminal.output.getvalue()
    assert "Process · process-live" in rendered
    assert "Run completed · 0 tool(s)" in rendered


def test_next_begin_resets_interrupted_and_fallback_counts(terminal):
    terminal.view.on_tool("read_file", "fallback result")
    terminal.bus.publish("message.part.updated", {"part": _tool()})
    terminal.view.cancel()
    terminal.view.begin(terminal.agent)

    terminal.view.finish()

    rendered = terminal.output.getvalue()
    assert "Run cancelled · 2 tool(s)" in rendered
    assert "Run completed · 0 tool(s)" in rendered


def test_local_gap_snapshot_counts_current_interrupted_call_once(terminal):
    current = _tool(owner="current")
    historical = {**_tool("old-call", owner="old", part_id="part-old"), "message_id": "msg-old"}
    terminal.agent.event_publisher = terminal.bus.for_interaction("current")
    terminal.agent.active_run_context = SimpleNamespace(
        interaction_run_id="current",
        transcript=[
            {"role": "assistant", "content": "", "_nz_message_id": "msg-1",
             "_nz_session_id": "count-session",
             "_nz_interaction_run_id": "current", "_nz_parts": [current]},
            {"role": "assistant", "content": "", "_nz_message_id": "msg-old",
             "_nz_session_id": "count-session",
             "_nz_interaction_run_id": "old", "_nz_parts": [historical]},
        ],
    )
    terminal.agent.event_publisher.publish("session.tool.started", {
        "tool_call_id": "call-1", "name": "bash",
    })
    terminal.view.drain(render_completed=False)

    assert terminal.view._rebase_local_after_gap(terminal.view._subscription) is True
    terminal.view.cancel()

    assert "Run cancelled · 1 tool(s)" in terminal.output.getvalue()
