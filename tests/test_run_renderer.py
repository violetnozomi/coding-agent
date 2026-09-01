"""Tests for structured terminal rendering from Session lifecycle events."""
from __future__ import annotations

import asyncio
import json
import time
from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from nz_coder.permissions import PermissionManager
from nz_coder.protocol.public_error import PublicError
from nz_coder.protocol.session_events import SessionEventBus, scoped_session_event_bus
from nz_coder.interface.run_renderer import (
    TerminalRunRenderer,
    _sanitize,
    render_permission_request,
    render_question_request,
)
from nz_coder.runtime.execution import tool_executor


class FakeStreamingRenderer:
    def __init__(self) -> None:
        self.paused = 0
        self.resumed = 0
        self.statuses = []

    def pause(self) -> None:
        self.paused += 1

    def resume(self) -> None:
        self.resumed += 1

    def set_status(self, lines) -> None:  # noqa: ANN001
        self.statuses.append(lines)


def _view():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    streaming = FakeStreamingRenderer()
    return TerminalRunRenderer(console, streaming), output, streaming


def test_run_renderer_projects_tool_and_run_events_without_duplicate_callback():
    view, output, streaming = _view()
    bus = SessionEventBus(session_id="session-1")
    tracker = SimpleNamespace(current_changed_paths=lambda: ["src/app.py"])
    agent = SimpleNamespace(event_bus=bus, model_id="model-test", change_tracker=tracker)
    view.begin(agent)
    bus.publish("session.run.started", {"model": "model-test"})
    bus.publish(
        "session.tool.started",
        {
            "tool_call_id": "call-1",
            "index": 0,
            "name": "edit_file",
            "category": "edit",
            "summary": "edit_file: src/app.py",
        },
    )
    bus.publish(
        "session.tool.completed",
        {
            "tool_call_id": "call-1",
            "index": 0,
            "name": "edit_file",
            "category": "edit",
            "status": "ok",
            "duration_ms": 12.5,
            "output": "Updated src/app.py",
        },
    )

    view.on_tool("edit_file", "Updated src/app.py")
    bus.publish("session.run.completed", {"status": "completed"})
    view.finish({"status": "completed"})

    rendered = output.getvalue()
    assert rendered.count("Edit · edit_file") == 1
    assert "edit_file: src/app.py" in rendered
    assert "Updated src/app.py" not in rendered
    assert "Run completed · 1 tool(s)" in rendered
    assert "1 changed file(s): src/app.py" in rendered
    assert streaming.paused == streaming.resumed


def test_run_renderer_single_line_sanitize_uses_terminal_columns():
    from nz_coder.interface.presentation_tokens import terminal_text_width

    value = _sanitize("读取🚀-src/authentication.py", 10)

    assert terminal_text_width(value) <= 10
    assert not value.endswith("\u200d")


def test_run_renderer_projects_running_tool_metadata_without_scrollback_spam():
    view, output, streaming = _view()
    bus = SessionEventBus(session_id="session-1")
    agent = SimpleNamespace(event_bus=bus, model_id="model-test")
    view.begin(agent)
    bus.publish(
        "session.tool.started",
        {
            "tool_call_id": "call-1",
            "index": 0,
            "name": "bash",
            "summary": "bash: pytest -q",
        },
    )
    bus.publish(
        "message.part.updated",
        {
            "message_id": "msg-1",
            "part": {
                "type": "tool",
                "tool": "bash",
                "call_id": "call-1",
                "state": {
                    "status": "running",
                    "title": "pytest -q",
                    "time": {"start": 1.0},
                    "metadata": {"output": "first\n47 passed"},
                },
            },
        },
    )

    stop = asyncio.Event()
    stop.set()
    asyncio.run(view.watch(stop))

    status = streaming.statuses[-1]
    assert any("bash · pytest -q" in row for row in status)
    assert any("47 passed" in row for row in status)
    assert "bash" not in output.getvalue()

    bus.publish(
        "session.tool.completed",
        {
            "tool_call_id": "call-1",
            "name": "bash",
            "status": "ok",
            "output": "47 passed",
        },
    )
    view.drain(render_completed=False)
    assert "47 passed" not in output.getvalue()
    view.on_tool("bash", "47 passed")
    assert output.getvalue().count("Bash · bash") == 1


def test_run_renderer_projects_nested_child_tool_from_task_metadata():
    view, output, streaming = _view()
    bus = SessionEventBus(session_id="session-1")
    view.begin(SimpleNamespace(event_bus=bus, model_id="model-test"))
    bus.publish("message.part.updated", {
        "message_id": "msg-1",
        "part": {
            "type": "tool",
            "tool": "task",
            "call_id": "task-1",
            "state": {
                "status": "running",
                "title": "Explore Task — inspect parser",
                "time": {"start": time.time()},
                "metadata": {
                    "child_session_id": "child-1",
                    "child_status": "running",
                    "child_current_tool": "grep_search",
                    "child_current_title": "find parser references",
                    "child_tool_count": 2,
                },
            },
        },
    })

    view.drain(render_completed=False)

    status = streaming.statuses[-1]
    assert any("task · Explore Task — inspect parser" in row for row in status)
    assert any("↳ grep_search find parser references" in row for row in status)
    assert output.getvalue() == ""


def test_run_renderer_projects_durable_retry_until_new_assistant_progress():
    view, output, streaming = _view()
    bus = SessionEventBus(session_id="session-1")
    agent = SimpleNamespace(event_bus=bus, model_id="model-test")
    view.begin(agent)
    bus.publish(
        "message.part.updated",
        {
            "message_id": "msg-1",
            "part": {
                "id": "retry-1",
                "type": "retry",
                "attempt": 2,
                "message": "rate limited",
                "next": time.time() + 5,
            },
        },
    )

    view.drain(render_completed=False)

    assert "Retry 2 in" in streaming.statuses[-1][0]
    assert "rate limited" in streaming.statuses[-1][0]
    assert output.getvalue() == ""

    bus.publish(
        "message.part.updated",
        {
            "message_id": "msg-1",
            "part": {"id": "text-1", "type": "text", "text": "connected"},
        },
    )
    view.drain(render_completed=False)

    assert "Waiting for model-test" in streaming.statuses[-1][0]


def test_run_renderer_projects_typed_assistant_error_and_terminal_footer_once():
    view, output, _streaming = _view()
    bus = SessionEventBus(session_id="session-1")
    agent = SimpleNamespace(event_bus=bus, model_id="model-test")
    view.begin(agent)
    info = {
        "id": "msg-1",
        "role": "assistant",
        "agent": "build",
        "model_id": "provider/model-test",
        "time": {"created": 10.0, "completed": 12.5},
        "error": {
            "name": "ProviderAuthError",
            "data": {
                "providerID": "provider",
                "message": "credential rejected",
                    "public_error": PublicError(
                        "provider_auth_error",
                        "credential rejected",
                        metadata={"provider_id": "provider"},
                    ).to_dict(),
            },
        },
        "end_state": {"reason": "errored"},
    }
    bus.publish("message.updated", {"message_id": "msg-1", "info": info})
    # Reconciliation updates for the same durable message must not duplicate it.
    bus.publish("message.updated", {"message_id": "msg-1", "info": info})
    bus.publish("session.run.completed", {"status": "error"})

    view.finish({"status": "error"})

    rendered = output.getvalue()
    assert rendered.count("credential rejected") == 1
    assert "Run /connect" in rendered
    assert "Build · provider/model-test · 2.5s · errored" in rendered


def test_run_renderer_does_not_render_aborted_message_as_error_card():
    view, output, _streaming = _view()
    bus = SessionEventBus(session_id="session-1")
    view.begin(SimpleNamespace(event_bus=bus, model_id="model-test"))
    bus.publish("message.updated", {
        "message_id": "msg-1",
        "info": {
            "id": "msg-1",
            "role": "assistant",
            "error": {
                "name": "MessageAbortedError",
                "data": {"message": "Request interrupted by user"},
            },
            "end_state": {"reason": "canceled"},
        },
    })
    bus.publish("session.run.cancelled", {})

    view.cancel()

    assert "Request interrupted by user" not in output.getvalue()


def test_run_renderer_falls_back_when_callback_has_no_event_bus():
    view, output, _streaming = _view()
    view.begin(SimpleNamespace(model_id="model-test"))

    view.on_tool("grep_search", "one\ntwo")
    view.finish({"status": "completed_unverified"})

    rendered = output.getvalue()
    assert "Tool · grep_search" in rendered
    assert "one" not in rendered
    assert "Run completed unverified" in rendered


def test_tool_detail_modes_hide_or_expand_output():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    streaming = FakeStreamingRenderer()
    mode = {"value": "hidden"}
    view = TerminalRunRenderer(console, streaming, detail_provider=lambda: mode["value"])
    view.begin(SimpleNamespace(model_id="model-test"))
    view.on_tool("read_file", "\n".join(f"line-{index}" for index in range(20)))
    assert "Tool · read_file" not in output.getvalue()

    mode["value"] = "full"
    view.on_tool("read_file", "\n".join(f"full-{index}" for index in range(20)))
    assert "full-19" in output.getvalue()


def test_error_card_width_is_bounded_on_wide_terminals():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=160)
    view = TerminalRunRenderer(console, FakeStreamingRenderer())
    view.begin(SimpleNamespace(model_id="model-test"))

    view.on_tool(
        "bash",
        "Command exited with code 1\n" + "very-long-output " * 30,
    )

    assert max(len(line) for line in output.getvalue().splitlines()) <= 100


def test_compact_bash_with_output_keeps_infcode_style_output_block():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)
    view = TerminalRunRenderer(console, FakeStreamingRenderer())
    view.begin(SimpleNamespace(model_id="model-test"))

    view.on_tool("bash", "package import failed")

    rendered = output.getvalue()
    assert "Bash · bash" in rendered
    assert "package import failed" in rendered


def test_compact_coding_tools_use_domain_specific_cards():
    view, output, _streaming = _view()
    view.begin(SimpleNamespace(model_id="model-test"))

    for name, category, summary in (
        ("edit_file", "edit", "Updated src/app.py"),
        ("read_file", "read", "source"),
        ("grep_search", "read", "14 matches"),
        ("web_search", "web", "3 results"),
        ("task", "agent", "Child analyzer completed"),
    ):
        view._render_tool({
            "name": name,
            "category": category,
            "summary": summary,
            "status": "ok",
            "duration_ms": 0,
        })

    rendered = output.getvalue()
    assert "Edit · edit_file" in rendered
    assert "Read · read_file" in rendered
    assert "Search · grep_search" in rendered
    assert "Web Search · web_search" in rendered
    assert "Child · task" in rendered


def test_all_high_frequency_tools_have_shared_product_labels():
    view, output, _streaming = _view()
    view.begin(SimpleNamespace(model_id="model-test"))

    for name, category, label in (
        ("apply_patch", "edit", "Edit"),
        ("bash", "command", "Bash"),
        ("process", "process", "Process"),
        ("webfetch", "web", "Web Search"),
        ("repo_context", "read", "Repo Lookup"),
        ("verification", "verification", "Verification"),
        ("mcp__docs__search", "mcp", "MCP"),
    ):
        view._render_tool({
            "name": name,
            "category": category,
            "summary": f"{label} summary",
            "status": "ok",
            "duration_ms": 1,
        })

    rendered = output.getvalue()
    for label in ("Edit", "Bash", "Process", "Web Search", "Repo Lookup", "Verification", "MCP"):
        assert f"{label} ·" in rendered


def test_run_renderer_strips_terminal_control_sequences():
    assert _sanitize("safe\x1b[31mred\x1b[0m\x07text", 100) == "saferedtext"


def test_remote_renderer_deduplicates_event_id_across_snapshot_replay():
    view, output, streaming = _view()
    streaming.on_token = lambda value: output.write(value or "")
    view.begin_remote(SimpleNamespace(model_id="remote"))
    event = {
        "type": "message.part.delta",
        "properties": {"delta": "once"},
        "meta": {
            "schema_version": 1,
            "event_id": "event-once",
            "sequence": 1,
            "timestamp": time.time(),
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_id": "agent-1",
        },
    }

    view.feed(event)
    view.feed(event)

    assert output.getvalue() == "once"


def test_remote_renderer_rebase_clears_stale_running_tool_without_double_apply():
    view, output, streaming = _view()
    streaming.on_token = lambda value: output.write(value or "")
    view.begin_remote(SimpleNamespace(model_id="remote"))
    delta = {
        "type": "message.part.delta",
        "properties": {"delta": "once"},
        "meta": {
            "schema_version": 1,
            "event_id": "delta-before-gap",
            "sequence": 1,
            "timestamp": time.time(),
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_id": "agent-1",
        },
    }
    started = {
        "type": "session.tool.started",
        "properties": {"tool_call_id": "call-1", "name": "bash"},
        "meta": {
            "schema_version": 1,
            "event_id": "tool-before-gap",
            "sequence": 2,
            "timestamp": time.time(),
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_id": "agent-1",
        },
    }

    view.feed(delta)
    view.feed(started)
    assert "call-1" in view._running_parts
    view.rebase_remote()
    view.feed(delta)
    view.feed(started)

    assert view._running_parts == {}
    assert output.getvalue() == "once"


def test_embedded_and_remote_feeds_share_the_same_logical_renderer_state():
    events = [
        {
            "type": "session.tool.started",
            "properties": {
                "tool_call_id": "call-parity",
                "name": "read_file",
                "category": "read",
                "summary": "read_file: app.py",
            },
        },
        {
            "type": "session.tool.completed",
            "properties": {
                "tool_call_id": "call-parity",
                "name": "read_file",
                "category": "read",
                "summary": "read_file: app.py",
                "status": "ok",
                "duration_ms": 2.0,
                "output": "source",
            },
        },
        {"type": "session.run.completed", "properties": {"status": "completed"}},
    ]

    rendered = []
    for mode in ("embedded", "remote"):
        view, output, _streaming = _view()
        view.begin_remote(SimpleNamespace(model_id=mode))
        for sequence, event in enumerate(events, 1):
            view.feed({
                **event,
                "meta": {
                    "schema_version": 1,
                    "event_id": f"{mode}-{sequence}",
                    "sequence": sequence,
                    "timestamp": time.time(),
                    "session_id": "session-1",
                    "run_id": "run-1",
                    "agent_id": "agent-1",
                },
            })
        view.finish({"status": "completed"})
        rendered.append(output.getvalue())

    assert rendered[0].replace("embedded", "remote") == rendered[1]
    assert "Read · read_file" in rendered[0]
    assert "Run completed · 1 tool(s)" in rendered[0]


def test_process_tool_renders_compact_product_card():
    view, output, _streaming = _view()
    view.begin(SimpleNamespace(model_id="model-test"))
    view.on_tool("process", json.dumps({
        "operation": "start",
        "process": {
            "process_id": "proc_ab12",
            "command": "npm run dev",
            "status": "running",
            "exit_code": None,
        },
    }))

    rendered = output.getvalue()
    assert "Process · proc_ab12" in rendered
    assert "npm run dev" in rendered
    assert "START · RUNNING" in rendered
    assert '"process_id"' not in rendered


def test_process_lifecycle_event_uses_same_compact_card():
    view, output, _streaming = _view()
    view.begin_remote(SimpleNamespace(model_id="remote"))
    view.feed({
        "type": "process.started",
        "properties": {"process": {
            "process_id": "proc_live",
            "command": "python server.py",
            "status": "running",
        }},
        "meta": {
            "schema_version": 1,
            "event_id": "process-started",
            "sequence": 1,
            "timestamp": time.time(),
            "session_id": "session-1",
            "run_id": "run-1",
            "agent_id": "agent-1",
        },
    })

    assert "Process · proc_live" in output.getvalue()
    assert "python server.py" in output.getvalue()
    assert "RUNNING" in output.getvalue()


def test_rich_permission_and_question_render_as_cards():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)

    render_permission_request(console, "bash: pytest -q")
    render_question_request(console, {
        "header": "Database",
        "question": "Choose storage",
        "options": [{"label": "SQLite", "description": "Local file"}],
    })

    rendered = output.getvalue()
    assert "Permission required" in rendered
    assert "bash: pytest -q" in rendered
    assert "Database" in rendered
    assert "1. SQLite — Local file" in rendered


def test_tool_executor_publishes_started_event_from_active_session_context(monkeypatch):
    import nz_coder.tools.bash  # noqa: F401

    bus = SessionEventBus(session_id="session-1")
    subscription = bus.subscribe({"session.tool.started"})
    monkeypatch.setattr(tool_executor, "dispatch", lambda _name, _input: "ok")
    executor = tool_executor.ToolExecutor(PermissionManager("auto"))
    call = {
        "id": "call-1",
        "function": {
            "name": "bash",
            "arguments": '{"command": "pwd"}',
        },
    }

    with scoped_session_event_bus(bus):
        result = executor.execute_one(call, 0)

    event = subscription.get(timeout=0)
    assert result.output == "ok"
    assert event.properties == {
        "tool_call_id": "call-1",
        "index": 0,
        "name": "bash",
        "category": "command",
        "summary": "bash: pwd",
        "is_write": False,
    }
