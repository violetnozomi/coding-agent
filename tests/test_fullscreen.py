"""Tests for the bounded full-screen terminal projection."""
from __future__ import annotations

import asyncio

import pytest

from prompt_toolkit.completion import DummyCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.styles import Style

from nz_coder.interface.fullscreen import (
    FullscreenComposer,
    TerminalSurfaceError,
    _MAX_OUTPUT_TOTAL_CHARS,
    _bounded_sidebar,
    _bounded_transcript,
    _render_markdown,
)
from nz_coder.interface.terminal_input import TerminalInputAction
from nz_coder.interface.timeline import build_transcript_document


def _plain(value) -> str:  # noqa: ANN001
    return "".join(text for _style, text in to_formatted_text(value))


def test_fullscreen_markdown_is_rendered_and_terminal_controls_are_removed():
    rendered = _plain(_render_markdown("# Title\n\n**bold**\x1b[31m\x07", 80))

    assert "Title" in rendered
    assert "bold" in rendered
    assert "**" not in rendered
    assert "\x1b" not in rendered
    assert "\x07" not in rendered


def test_fullscreen_transcript_and_sidebar_are_bounded():
    transcript = _bounded_transcript("x" * 250_000)
    sidebar = _bounded_sidebar("y" * 10_000)

    assert transcript.startswith("… earlier transcript omitted …")
    assert len(transcript) < 201_000
    assert sidebar.endswith("\n…")
    assert len(sidebar) < 8_100


def test_fullscreen_application_survives_multiple_submissions():
    """Submitting a turn must not leave and recreate the alternate screen."""
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "# Session",
                status_provider=lambda: "provider/model · default",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            first = asyncio.create_task(composer.read_async())
            await asyncio.sleep(0.05)
            application_task = composer._application_task
            pipe.send_text("first request\r")
            assert await asyncio.wait_for(first, 1) == "first request"
            assert application_task is not None and not application_task.done()

            second = asyncio.create_task(composer.read_async())
            await asyncio.sleep(0.05)
            pipe.send_text("second request\r")
            assert await asyncio.wait_for(second, 1) == "second request"
            assert composer._application_task is application_task
            assert not application_task.done()

            leader = asyncio.create_task(composer.read_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x18y")
            action = await asyncio.wait_for(leader, 1)
            assert action == TerminalInputAction("leader", "/copy-last")
            assert composer._application_task is application_task
            await composer.close_async()
            assert application_task.done()

    asyncio.run(scenario())


def test_fullscreen_selector_is_an_overlay_not_a_second_application():
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "Session",
                status_provider=lambda: "provider/model · default",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            await composer.start_async()
            application_task = composer._application_task
            selected = asyncio.create_task(composer.select_async(
                title="Permission required",
                values=[("once", "Allow once"), ("reject", "Reject")],
            ))
            await asyncio.sleep(0.05)
            pipe.send_text("\r")
            assert await asyncio.wait_for(selected, 1) == "once"
            assert composer._application_task is application_task
            assert application_task is not None and not application_task.done()
            await composer.close_async()

    asyncio.run(scenario())


def test_fullscreen_selector_detail_is_complete_and_independently_scrollable():
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "Session",
                status_provider=lambda: "provider/model · plan",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            detail = "# Approved plan\n\n" + "\n".join(
                f"- Step {index}: detailed action" for index in range(30)
            ) + "\nFINAL-MARKER"
            selected = asyncio.create_task(composer.select_async(
                title="Plan ready",
                text="Read the full plan · PgUp/PgDn scroll · Enter select",
                detail=detail,
                values=[("approve", "Approve plan"), ("revise", "Keep planning")],
            ))
            await asyncio.sleep(0.05)

            assert composer._dialog_kind == "selector-detail"
            rendered = "\n".join(
                "".join(fragment[1] for fragment in line)
                for line in composer._detail_content_lines(68)
            )
            assert "FINAL-MARKER" in rendered
            composer._scroll_detail(10)
            assert composer._selector_detail_window.vertical_scroll == 10

            pipe.send_text("\x1b")
            assert await asyncio.wait_for(selected, 1) is None
            await composer.close_async()

    asyncio.run(scenario())


def test_fullscreen_first_idle_ctrl_c_shows_confirmation_hint():
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "Session",
                status_provider=lambda: "provider/model",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            read = asyncio.create_task(composer.read_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x03")
            await asyncio.sleep(0.05)
            assert "Ctrl+C again" in _plain(composer._footer())
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
            await composer.close_async()

    asyncio.run(scenario())


def test_fullscreen_run_cancel_is_immediately_visible():
    async def scenario() -> None:
        cancelled = []
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "Session",
                status_provider=lambda: "provider/model",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            await composer.start_async()
            composer.begin_run()
            composer.set_cancel_run(lambda: cancelled.append(True))
            pipe.send_text("\x03")
            await asyncio.sleep(0.05)
            assert cancelled == [True]
            assert any("Cancellation requested" in line for line in composer._run_status)
            await composer.close_async()

    asyncio.run(scenario())


def test_cancelled_terminal_notice_survives_fullscreen_end_run():
    """The durable idle screen must retain the cancellation result."""
    composer = FullscreenComposer(
        transcript_provider=lambda: "Session",
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )

    composer.begin_run()
    composer.append_output("temporary tool output")
    composer.append_notice("■ Run cancelled · 0 tool(s) · 0.2s")
    composer.end_run()

    rendered = "".join(text for _style, text in composer._transcript_fragments())
    assert "Run cancelled" in rendered
    assert "temporary tool output" not in rendered


def test_begin_run_folds_old_command_notices_out_of_live_region():
    composer = FullscreenComposer(
        transcript_provider=lambda: "Session",
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )
    composer.append_output("old /help output")

    composer.begin_run()

    assert composer._notices == []


def test_fullscreen_preferences_update_without_rebuilding_application():
    style = Style.from_dict({"status": "#ffffff"})
    composer = FullscreenComposer(
        transcript_provider=lambda: "Session",
        status_provider=lambda: "provider/model",
        sidebar_provider=lambda: "Sidebar",
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )
    application = composer.application

    composer.apply_preferences(style=style, mouse_support=True, sidebar_mode="show")

    assert composer.application is application
    assert composer.application.style is style
    assert composer.mouse_support is True
    assert composer.sidebar_mode == "show"


def test_fullscreen_stream_updates_reuse_durable_markdown_projection():
    calls = 0

    def transcript() -> str:
        nonlocal calls
        calls += 1
        return "# Durable transcript\n\n" + ("history " * 5_000)

    composer = FullscreenComposer(
        transcript_provider=transcript,
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )

    for index in range(20):
        composer.set_stream(f"token {index}")
        composer.set_run_status((f"working {index}",))
        composer._transcript_fragments()

    assert calls == 1
    composer.refresh_transcript()
    composer._transcript_fragments()
    assert calls == 2


def test_fullscreen_transient_stream_is_inert_and_outputs_are_bounded():
    composer = FullscreenComposer(
        transcript_provider=lambda: "Session",
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )
    composer.set_stream("safe\x1b[2J text\x07")
    for _ in range(200):
        composer.append_output("x" * 2_000)

    rendered = "".join(text for _style, text in composer._transcript_fragments())
    retained = sum(len(text) for block in composer._notices for _style, text in block)
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "safe text" in rendered
    assert retained <= _MAX_OUTPUT_TOTAL_CHARS
    assert len(composer._notices) <= 100


def test_fullscreen_sticky_tail_can_be_suspended_and_restored():
    composer = FullscreenComposer(
        transcript_provider=lambda: "Session",
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=False,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )
    composer._transcript_fragments()
    content = composer._transcript_control.create_content(80, 24)
    assert content.cursor_position.y == content.line_count - 1
    composer._follow_tail = False
    composer._transcript_window.vertical_scroll = 12
    composer.set_stream("new token")
    assert composer._transcript_window.vertical_scroll == 12
    composer._follow_tail = True
    composer._scroll_to_tail()
    composer._transcript_fragments()
    content = composer._transcript_control.create_content(80, 24)
    assert content.cursor_position.y == content.line_count - 1


def test_fullscreen_message_navigation_and_toolpart_activation_share_anchors():
    document = build_transcript_document("session-1", [
        {"role": "user", "content": "first", "_nz_message_id": "user-1"},
        {
            "role": "assistant",
            "content": "answer",
            "_nz_message_id": "assistant-1",
            "_nz_parts": [{
                "id": "part-1",
                "type": "tool",
                "tool": "read_file",
                "call_id": "call-1",
                "state": {
                    "status": "completed",
                    "input": {"path": "app.py"},
                    "output": "line one\nline two\nline three\nline four",
                },
            }],
        },
        {"role": "user", "content": "second", "_nz_message_id": "user-2"},
    ], compact_tools=True)
    composer = FullscreenComposer(
        transcript_provider=lambda: document,
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=True,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )
    composer._transcript_fragments()
    assert [item.message_id for item in composer._message_anchors] == [
        "user-1", "assistant-1", "user-2",
    ]

    composer._transcript_window.vertical_scroll = composer._message_anchors[0].start
    assert composer.navigate_message("next") is True
    assert composer._transcript_window.vertical_scroll == composer._message_anchors[1].start
    assert composer.navigate_message("last-user") is True
    assert composer._transcript_window.vertical_scroll == composer._message_anchors[-1].start

    tool_anchor = next(item for item in composer._projection_anchors if item.part_id)
    collapsed_count = len(composer._transcript_lines)
    assert composer.activate_transcript_line(tool_anchor.start) is True
    composer._transcript_fragments()
    assert "part-1" in composer._expanded_tool_parts
    assert len(composer._transcript_lines) > collapsed_count

    user_anchor = composer._message_anchors[0]
    assert composer.activate_transcript_line(user_anchor.start) is True
    assert composer._dialog_kind == "detail"
    assert "first" in composer._dialog_detail


def test_fullscreen_detail_scroll_hover_and_drag_activation_are_independent():
    document = build_transcript_document("session-1", [
        {"role": "user", "content": "\n\n".join(f"line {i}" for i in range(60))},
        {
            "role": "assistant",
            "content": "done",
            "_nz_parts": [{
                "id": "part-hover",
                "type": "tool",
                "tool": "bash",
                "call_id": "call-hover",
                "state": {"status": "completed", "input": {}, "output": "ok"},
            }],
        },
    ], compact_tools=True)
    composer = FullscreenComposer(
        transcript_provider=lambda: document,
        status_provider=lambda: "provider/model",
        sidebar_provider=None,
        sidebar_mode="hide",
        completer=DummyCompleter(),
        history=None,
        style=Style.from_dict({}),
        mouse_support=True,
        empty_ctrl_c_requests_exit=lambda: False,
        clear_exit_request=lambda: None,
        output=DummyOutput(),
    )
    composer._transcript_fragments()
    tool = next(item for item in composer._projection_anchors if item.part_id)
    composer.hover_transcript_line(tool.start)
    composer._transcript_fragments()
    assert composer._hovered_tool_part == "part-hover"
    assert "▶" in "".join(text for _style, text in composer._transcript_cache)

    user = composer._message_anchors[0]
    down = MouseEvent(
        Point(x=0, y=user.start), MouseEventType.MOUSE_DOWN,
        MouseButton.LEFT, frozenset(),
    )
    dragged = MouseEvent(
        Point(x=2, y=user.start + 1), MouseEventType.MOUSE_UP,
        MouseButton.LEFT, frozenset(),
    )
    composer._transcript_control.mouse_handler(down)
    composer._transcript_control.mouse_handler(dragged)
    assert composer._dialog_kind == ""

    up = MouseEvent(
        Point(x=0, y=user.start), MouseEventType.MOUSE_UP,
        MouseButton.LEFT, frozenset(),
    )
    composer._transcript_control.mouse_handler(down)
    composer._transcript_control.mouse_handler(up)
    lines = composer._detail_content_lines(40)
    assert composer._dialog_kind == "detail"
    assert len(lines) > 20
    composer._scroll_detail(10)
    assert composer._detail_window.vertical_scroll == 10
    assert composer._transcript_window.vertical_scroll != 10


def test_fullscreen_custom_message_keybinding_updates_without_root_rebuild():
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            document = build_transcript_document("session-1", [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "last"},
            ])
            composer = FullscreenComposer(
                transcript_provider=lambda: document,
                status_provider=lambda: "provider/model",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                message_keybindings={"messages_last_user": "c-n"},
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            await composer.start_async()
            application = composer.application
            actions = []
            composer.navigate_message = lambda action: actions.append(action) or True
            pipe.send_text("\x0e")
            await asyncio.sleep(0.05)
            assert actions == ["last-user"]

            composer.apply_preferences(
                style=Style.from_dict({}),
                mouse_support=False,
                sidebar_mode="hide",
                message_keybindings={"messages_last_user": "c-o"},
            )
            assert composer.application is application
            pipe.send_text("\x0f")
            await asyncio.sleep(0.05)
            assert actions == ["last-user", "last-user"]
            await composer.close_async()

    asyncio.run(scenario())


def test_fullscreen_root_failure_is_reported_without_hanging_and_recovers_once():
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "Session",
                status_provider=lambda: "provider/model",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )

            async def fail() -> None:
                await asyncio.sleep(0)
                raise RuntimeError("render boom")

            composer._application_task = asyncio.create_task(fail())
            with pytest.raises(TerminalSurfaceError, match="render boom"):
                await asyncio.wait_for(composer.read_async(), 1)

            assert await composer.recover_async() is True
            submission = asyncio.create_task(composer.read_async())
            await asyncio.sleep(0.05)
            pipe.send_text("recovered request\r")
            assert await asyncio.wait_for(submission, 1) == "recovered request"
            assert await composer.recover_async() is False
            await composer.close_async()

    asyncio.run(scenario())


def test_fullscreen_reports_a_submission_waiting_behind_active_run():
    composer = object.__new__(FullscreenComposer)
    composer._submissions = asyncio.Queue()

    assert composer.has_pending_submission() is False
    composer._submissions.put_nowait("follow up")
    assert composer.has_pending_submission() is True


def test_fullscreen_external_editor_failure_is_a_product_surface_error():
    async def scenario() -> None:
        with create_pipe_input() as pipe:
            composer = FullscreenComposer(
                transcript_provider=lambda: "Session",
                status_provider=lambda: "provider/model · default",
                sidebar_provider=None,
                sidebar_mode="hide",
                completer=DummyCompleter(),
                history=None,
                style=Style.from_dict({}),
                mouse_support=False,
                empty_ctrl_c_requests_exit=lambda: False,
                clear_exit_request=lambda: None,
                input=pipe,
                output=DummyOutput(),
            )
            await composer.start_async()

            def fail_editor():
                raise OSError("editor unavailable")

            composer.buffer.open_in_editor = fail_editor
            with pytest.raises(TerminalSurfaceError, match="editor unavailable"):
                await composer.read_async(open_editor=True)
            await composer.close_async()

    asyncio.run(scenario())
