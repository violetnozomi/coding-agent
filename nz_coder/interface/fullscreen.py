"""One persistent full-screen terminal application for the interactive CLI."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import StringIO
import re
import shutil
import time

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.filters import Condition, to_filter
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, to_formatted_text
from prompt_toolkit.formatted_text.utils import split_lines
from prompt_toolkit.history import History
from prompt_toolkit.key_binding import DynamicKeyBindings, KeyBindings
from prompt_toolkit.layout import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, ConditionalProcessor, PasswordProcessor
from prompt_toolkit.mouse_events import MouseButton, MouseEventType
from prompt_toolkit.styles import BaseStyle
from prompt_toolkit.data_structures import Point
from prompt_toolkit.widgets import Frame
from rich.console import Console
from rich.markdown import Markdown

from nz_coder.interface.selector import FuzzySelector, SelectorActionResult


_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_OUTPUT_BLOCK_CHARS = 40_000
_MAX_OUTPUT_TOTAL_CHARS = 120_000
_SIDEBAR_CACHE_SECONDS = 0.5
_STATUS_CACHE_SECONDS = 0.2
_MESSAGE_NAV_DEFAULTS = {
    "messages_first": "home",
    "messages_last": "end",
    "messages_next": "c-x j",
    "messages_previous": "c-x k",
    "messages_last_user": "c-x h",
}


@dataclass(frozen=True)
class MessageAnchor:
    """Rendered line range and source identity for one visible message."""

    message_id: str
    role: str
    start: int
    end: int
    turn_number: int | None
    markdown: str
    part_id: str | None = None


class _TranscriptControl(UIControl):
    """Virtualized line view over cached durable and transient projections."""

    def __init__(self, owner: "FullscreenComposer") -> None:
        self.owner = owner
        self._mouse_down: Point | None = None

    def create_content(self, width: int, height: int) -> UIContent:
        groups = self.owner._content_line_groups(width)
        lengths = tuple(len(group) for group in groups)
        total = max(1, sum(lengths))
        self.owner._rendered_line_count = total

        def get_line(index: int) -> StyleAndTextTuples:
            cursor = index
            for group, length in zip(groups, lengths):
                if cursor < length:
                    return list(group[cursor])
                cursor -= length
            return []

        cursor_y = total - 1 if self.owner._follow_tail else min(
            max(0, self.owner._transcript_window.vertical_scroll), total - 1
        )
        return UIContent(
            get_line=get_line,
            line_count=total,
            show_cursor=False,
            cursor_position=Point(x=0, y=cursor_y),
        )

    def mouse_handler(self, mouse_event):  # noqa: ANN001, ANN202
        """Use rendered logical rows for message details and ToolPart toggles."""
        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            self.owner.hover_transcript_line(mouse_event.position.y)
            return None
        if mouse_event.button != MouseButton.LEFT:
            return NotImplemented
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            self._mouse_down = mouse_event.position
            return None
        if mouse_event.event_type != MouseEventType.MOUSE_UP:
            return NotImplemented
        origin = self._mouse_down
        self._mouse_down = None
        # A drag belongs to terminal text selection and must never activate UI.
        if origin is None or origin != mouse_event.position:
            return None
        return None if self.owner.activate_transcript_line(mouse_event.position.y) else NotImplemented


class _DetailControl(UIControl):
    """Virtualized, independently scrollable Markdown detail viewport."""

    def __init__(self, owner: "FullscreenComposer") -> None:
        self.owner = owner

    def create_content(self, width: int, height: int) -> UIContent:
        lines = self.owner._detail_content_lines(width)
        total = max(1, len(lines))

        def get_line(index: int) -> StyleAndTextTuples:
            return list(lines[index]) if index < len(lines) else []

        cursor_y = min(max(0, self.owner._detail_window.vertical_scroll), total - 1)
        return UIContent(
            get_line=get_line,
            line_count=total,
            show_cursor=False,
            cursor_position=Point(x=0, y=cursor_y),
        )


class TerminalSurfaceError(RuntimeError):
    """The root terminal Application stopped before producing a submission."""


class FullscreenComposer:
    """Own the terminal screen for the complete lifetime of one CLI session."""

    def __init__(
        self,
        *,
        transcript_provider: Callable[[], object],
        status_provider: Callable[[], str],
        product_state_provider: Callable[[], dict] | None = None,
        attachments_provider: Callable[[], Iterable[object]] | None = None,
        sidebar_provider: Callable[[], str] | None,
        sidebar_mode: str,
        completer: Completer,
        history: History | None,
        style: BaseStyle,
        mouse_support: bool,
        message_keybindings: dict[str, str] | None = None,
        empty_ctrl_c_requests_exit: Callable[[], bool],
        clear_exit_request: Callable[[], None],
        paste_image: Callable[[], bool] | None = None,
        input=None,  # noqa: ANN001
        output=None,  # noqa: ANN001
    ) -> None:
        self.transcript_provider = transcript_provider
        self.status_provider = status_provider
        self.product_state_provider = product_state_provider or (lambda: {})
        self.attachments_provider = attachments_provider or (lambda: ())
        self.sidebar_provider = sidebar_provider
        self.sidebar_mode = sidebar_mode
        self.completer = completer
        self.history = history
        self.style = style
        self.mouse_support = mouse_support
        self.message_keybindings = {
            **_MESSAGE_NAV_DEFAULTS,
            **(message_keybindings or {}),
        }
        self.empty_ctrl_c_requests_exit = empty_ctrl_c_requests_exit
        self.clear_exit_request = clear_exit_request
        self.paste_image = paste_image
        self._input = input
        self._output = output
        self._submissions: asyncio.Queue[object] = asyncio.Queue()
        self._application_task: asyncio.Task | None = None
        self._closed = False
        self._recovery_count = 0
        self._run_active = False
        self._cancel_run: Callable[[], None] | None = None
        self._stream_text = ""
        self._run_status: tuple[str, ...] = ()
        self._run_output: list[tuple[tuple[str, str], ...]] = []
        self._notices: list[tuple[tuple[str, str], ...]] = []
        self._transcript_dirty = True
        self._transcript_width = 0
        self._transcript_cache: tuple[tuple[str, str], ...] = ()
        self._transcript_lines: tuple[tuple[tuple[str, str], ...], ...] = ((('', ''),),)
        self._message_anchors: tuple[MessageAnchor, ...] = ()
        self._projection_anchors: tuple[MessageAnchor, ...] = ()
        self._expanded_tool_parts: set[str] = set()
        self._hovered_tool_part = ""
        self._output_lines: tuple[tuple[tuple[str, str], ...], ...] = ()
        self._output_dirty = True
        self._stream_safe = ""
        self._stream_lines: tuple[tuple[tuple[str, str], ...], ...] = ()
        self._sidebar_cache = ""
        self._sidebar_cached_at = 0.0
        self._status_cache = ""
        self._status_cached_at = 0.0
        self._follow_tail = True
        self._rendered_line_count = 1
        self._selector: FuzzySelector | None = None
        self._dialog_future: asyncio.Future | None = None
        self._dialog_kind = ""
        self._dialog_message = ""
        self._dialog_password = False
        self._dialog_detail = ""
        self._dialog_detail_title = "Message"
        self._detail_cache_text = ""
        self._detail_cache_width = 0
        self._detail_lines: tuple[tuple[tuple[str, str], ...], ...] = ((('', ''),),)
        self._build_application()

    def _build_application(self) -> None:
        columns = shutil.get_terminal_size(fallback=(100, 30)).columns
        self._transcript_control = _TranscriptControl(self)
        self._transcript_window = Window(
            self._transcript_control,
            wrap_lines=True,
            always_hide_cursor=True,
            right_margins=[],
        )
        self.buffer = Buffer(
            completer=self.completer,
            complete_while_typing=True,
            history=self.history,
            multiline=True,
        )
        self._input_window = Window(
            BufferControl(
                buffer=self.buffer,
                input_processors=[BeforeInput([("class:prompt", "❯ ")])],
            ),
            height=Dimension(min=3, max=8),
            wrap_lines=True,
        )
        header = Window(
            FormattedTextControl(self._header), height=1, style="class:composer.border"
        )
        divider = Window(height=1, char="─", style="class:composer.border")
        attachments = ConditionalContainer(
            Window(
                FormattedTextControl(self._attachment_text),
                height=1,
                style="class:status",
            ),
            filter=Condition(lambda: bool(tuple(self.attachments_provider()))),
        )
        footer = Window(
            FormattedTextControl(self._footer), height=1, style="class:bottom-toolbar"
        )
        main = HSplit([
            header,
            self._transcript_window,
            divider,
            attachments,
            self._input_window,
            footer,
        ])
        sidebar = ConditionalContainer(
            VSplit([
                Window(width=1, char="│", style="class:composer.border"),
                Window(
                    FormattedTextControl(self._sidebar_text),
                    width=Dimension(min=30, preferred=42, max=48),
                    wrap_lines=True,
                    style="class:sidebar",
                ),
            ]),
            filter=Condition(
                lambda: self._show_sidebar(
                    shutil.get_terminal_size(fallback=(columns, 30)).columns
                )
            ),
        )
        content = VSplit([main, sidebar])

        self._dialog_buffer = Buffer(multiline=False)
        dialog_control = BufferControl(
            buffer=self._dialog_buffer,
            input_processors=[
                BeforeInput([("class:selector.search", "Search: ")]),
                ConditionalProcessor(
                    PasswordProcessor(),
                    filter=Condition(lambda: self._dialog_password),
                ),
            ],
        )
        self._dialog_input = Window(dialog_control, height=1)
        self._detail_control = _DetailControl(self)
        self._detail_window = Window(
            self._detail_control,
            height=Dimension(min=5, preferred=18, max=22),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        dialog_input = ConditionalContainer(
            HSplit([
                Window(height=1, char="─", style="class:selector.rule"),
                self._dialog_input,
            ]),
            filter=Condition(lambda: self._dialog_kind != "detail"),
        )
        dialog = Frame(
            HSplit([
                Window(
                    FormattedTextControl(self._dialog_hint),
                    height=Dimension(min=1, max=4),
                    wrap_lines=True,
                ),
                dialog_input,
                ConditionalContainer(
                    self._detail_window,
                    filter=Condition(lambda: self._dialog_kind == "detail"),
                ),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._dialog_results),
                        height=Dimension(min=3, preferred=15, max=15),
                    ),
                    filter=Condition(lambda: self._dialog_kind != "detail"),
                ),
            ]),
            title=lambda: self._dialog_title(),
        )
        visible_dialog = ConditionalContainer(dialog, filter=Condition(self._dialog_visible))
        root = FloatContainer(
            content=content,
            floats=[
                Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12)),
                Float(left=4, right=4, top=2, content=visible_dialog),
            ],
        )
        self._bindings = self._build_bindings()
        self._dynamic_bindings = DynamicKeyBindings(lambda: self._bindings)
        self.application = Application(
            layout=Layout(root, focused_element=self._input_window),
            key_bindings=self._dynamic_bindings,
            full_screen=True,
            mouse_support=self.mouse_support,
            style=self.style,
            input=self._input,
            output=self._output,
        )
        self._dialog_buffer.on_text_changed += self._dialog_query_changed

    def _build_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        main = Condition(lambda: not self._dialog_visible())
        dialog = Condition(self._dialog_visible)

        @bindings.add("/", filter=main)
        def _slash(_event) -> None:  # noqa: ANN001
            before = self.buffer.document.text_before_cursor
            self.buffer.insert_text("/")
            if not before.strip():
                self.buffer.start_completion(select_first=True)

        @bindings.add("enter", filter=main)
        def _submit(_event) -> None:  # noqa: ANN001
            completion_state = self.buffer.complete_state
            if completion_state and completion_state.completions:
                completion = completion_state.current_completion or completion_state.completions[0]
                slash_command = self.buffer.document.text.lstrip().startswith("/")
                self.buffer.apply_completion(completion)
                if not slash_command:
                    return
            if self.buffer.text.strip():
                self.clear_exit_request()
                value = self.buffer.text
                self.buffer.reset(append_to_history=True)
                self._submissions.put_nowait(value)

        @bindings.add("escape", "enter", filter=main)
        def _newline(_event) -> None:  # noqa: ANN001
            self.buffer.insert_text("\n")

        @bindings.add("c-v", filter=main)
        def _paste(_event) -> None:  # noqa: ANN001
            from nz_coder.interface.terminal_input import _system_clipboard_text

            data = self.application.clipboard.get_data().text
            if not data:
                data = _system_clipboard_text()
            if data:
                self.buffer.insert_text(data[:200_000])
            elif self.paste_image is not None and self.paste_image():
                self.invalidate()

        @bindings.add("c-c", eager=True, filter=main)
        def _ctrl_c(_event) -> None:  # noqa: ANN001
            from nz_coder.interface.terminal_input import TerminalInputAction

            if self.buffer.text:
                self.buffer.reset()
                self.clear_exit_request()
            elif self._run_active:
                if self._cancel_run is not None:
                    self._cancel_run()
            elif self.empty_ctrl_c_requests_exit():
                self._submissions.put_nowait(TerminalInputAction("exit_confirmed"))
            self.invalidate()

        @bindings.add("c-d", eager=True, filter=main)
        def _eof(_event) -> None:  # noqa: ANN001
            from nz_coder.interface.terminal_input import TerminalInputAction

            if not self.buffer.text and not self._run_active:
                self._submissions.put_nowait(TerminalInputAction("exit_confirmed"))

        @bindings.add("c-p", eager=True, filter=main)
        @bindings.add("c-k", eager=True, filter=main)
        def _palette(_event) -> None:  # noqa: ANN001
            from nz_coder.interface.terminal_input import TerminalInputAction

            self._submissions.put_nowait(TerminalInputAction("command_palette", self.buffer.text))

        @bindings.add("f2", eager=True, filter=main)
        def _cycle(_event) -> None:  # noqa: ANN001
            from nz_coder.interface.terminal_input import TerminalInputAction

            self._submissions.put_nowait(TerminalInputAction("model_cycle", "/model-cycle next"))

        @bindings.add("c-x", "e", eager=True, filter=main)
        def _editor(_event) -> None:  # noqa: ANN001
            self.buffer.open_in_editor()

        leader_commands = {
            "t": "/theme",
            "m": "/model-picker",
            "n": "/new-session",
            "l": "/session",
            "g": "/timeline",
            "c": "/compact",
            "s": "/status",
            "u": "/undo",
            "r": "/redo",
            "y": "/copy-last",
        }
        for key, command in leader_commands.items():
            @bindings.add("c-x", key, eager=True, filter=main)
            def _leader(_event, selected=command) -> None:  # noqa: ANN001
                from nz_coder.interface.terminal_input import TerminalInputAction

                self._submissions.put_nowait(TerminalInputAction("leader", selected))

        @bindings.add("pageup", filter=main)
        def _page_up(_event) -> None:  # noqa: ANN001
            self._follow_tail = False
            self._transcript_window.vertical_scroll = max(
                0, self._transcript_window.vertical_scroll - 10
            )

        @bindings.add("pagedown", filter=main)
        def _page_down(_event) -> None:  # noqa: ANN001
            self._follow_tail = False
            self._transcript_window.vertical_scroll += 10

        navigation = {
            "messages_first": "first",
            "messages_last": "last",
            "messages_next": "next",
            "messages_previous": "previous",
            "messages_last_user": "last-user",
        }
        for name, action in navigation.items():
            sequence = self.message_keybindings.get(name, "none")
            if sequence == "none":
                continue

            @bindings.add(*sequence.split(), eager=True, filter=main)
            def _navigate(_event, selected=action) -> None:  # noqa: ANN001
                self.navigate_message(selected)

        @bindings.add("up", eager=True, filter=dialog)
        @bindings.add("c-p", eager=True, filter=dialog)
        def _dialog_up(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._scroll_detail(-1)
                return
            if self._selector is not None:
                self._selector.move(-1)
                self.invalidate()

        @bindings.add("down", eager=True, filter=dialog)
        @bindings.add("c-n", eager=True, filter=dialog)
        def _dialog_down(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._scroll_detail(1)
                return
            if self._selector is not None:
                self._selector.move(1)
                self.invalidate()

        @bindings.add("pageup", eager=True, filter=dialog)
        def _dialog_page_up(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._scroll_detail(-10)

        @bindings.add("pagedown", eager=True, filter=dialog)
        def _dialog_page_down(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._scroll_detail(10)

        @bindings.add("home", eager=True, filter=dialog)
        def _dialog_home(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._detail_window.vertical_scroll = 0
                self.invalidate()

        @bindings.add("end", eager=True, filter=dialog)
        def _dialog_end(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._detail_window.vertical_scroll = max(0, len(self._detail_lines) - 1)
                self.invalidate()

        @bindings.add("enter", eager=True, filter=dialog)
        def _dialog_accept(_event) -> None:  # noqa: ANN001
            if self._dialog_kind == "detail":
                self._resolve_dialog(None)
                return
            if self._dialog_kind == "text":
                self._resolve_dialog(self._dialog_buffer.text)
                return
            selector = self._selector
            if selector is None:
                return
            value = selector.current_value()
            if selector.multiple:
                if not selector.selected_values and value is not None:
                    selector.selected_values.append(value)
                if selector.selected_values:
                    self._resolve_dialog(tuple(selector.selected_values))
            elif value is not None:
                self._resolve_dialog(value)

        @bindings.add(" ", eager=True, filter=dialog)
        def _dialog_toggle(event) -> None:  # noqa: ANN001
            selector = self._selector
            if selector is None or not selector.multiple:
                event.current_buffer.insert_text(" ")
                return
            selector.toggle_current()
            self._dialog_buffer.reset()
            selector.query = ""
            selector.selected = 0

        @bindings.add("escape", eager=True, filter=dialog)
        @bindings.add("c-c", eager=True, filter=dialog)
        def _dialog_cancel(_event) -> None:  # noqa: ANN001
            self._resolve_dialog(None)

        for key in ("c-a", "c-d", "c-f"):
            @bindings.add(key, eager=True, filter=dialog)
            def _dialog_action(_event, selected_key=key) -> None:  # noqa: ANN001
                selector = self._selector
                if selector is None:
                    return
                for action_key, action, _title in selector.actions:
                    if action_key == selected_key:
                        self._resolve_dialog(SelectorActionResult(action, selector.current_value()))
                        return

        return bindings

    async def read_async(self, default: str = "", *, open_editor: bool = False):
        """Wait for the next submission while keeping the Application alive."""
        await self.start_async()
        if default and not self.buffer.text:
            self.buffer.text = default
        if open_editor:
            try:
                self.buffer.open_in_editor()
            except Exception as exc:
                raise TerminalSurfaceError(
                    f"External editor failed: {exc}"
                ) from exc
        self.application.layout.focus(self._input_window)
        self.invalidate()
        submission = asyncio.create_task(self._submissions.get())
        application_task = self._application_task
        assert application_task is not None
        done, _pending = await asyncio.wait(
            (submission, application_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if submission in done:
            return submission.result()
        submission.cancel()
        try:
            await submission
        except asyncio.CancelledError:
            pass
        try:
            await application_task
        except asyncio.CancelledError as exc:
            if self._closed:
                raise EOFError from exc
            raise TerminalSurfaceError("Terminal application was cancelled") from exc
        except (EOFError, KeyboardInterrupt) as exc:
            raise EOFError from exc
        except Exception as exc:
            raise TerminalSurfaceError(
                f"Terminal application failed: {type(exc).__name__}: {exc}"
            ) from exc
        raise EOFError

    async def start_async(self) -> None:
        if self._application_task is not None:
            if not self._application_task.done():
                return
            try:
                self._application_task.result()
            except asyncio.CancelledError as exc:
                raise TerminalSurfaceError("Terminal application was cancelled") from exc
            except (EOFError, KeyboardInterrupt) as exc:
                raise EOFError from exc
            except Exception as exc:
                raise TerminalSurfaceError(
                    f"Terminal application failed: {type(exc).__name__}: {exc}"
                ) from exc
            raise EOFError
        if self._closed:
            raise EOFError
        self._application_task = asyncio.create_task(self.application.run_async())
        await asyncio.sleep(0)

    async def recover_async(self) -> bool:
        """Rebuild the root once after a fatal renderer failure."""
        if self._closed or self._recovery_count >= 1:
            return False
        self._recovery_count += 1
        draft = self.buffer.text
        self._application_task = None
        self._build_application()
        self.buffer.text = draft
        await self.start_async()
        self.append_output("Terminal UI recovered after an internal rendering error.\n")
        return True

    async def close_async(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._dialog_future is not None and not self._dialog_future.done():
            self._dialog_future.set_result(None)
        if self.application.is_running:
            self.application.exit()
        task = self._application_task
        if task is not None:
            try:
                await task
            except (KeyboardInterrupt, asyncio.CancelledError, Exception):
                pass

    async def select_async(self, **kwargs):  # noqa: ANN003, ANN202
        """Open a fuzzy selector as an overlay in this Application."""
        await self.start_async()
        selector = FuzzySelector(style=None, mouse_support=self.mouse_support, **kwargs)
        self._selector = selector
        self._dialog_kind = "selector"
        self._dialog_message = selector.text
        self._dialog_password = False
        self._dialog_buffer.reset()
        self._dialog_future = asyncio.get_running_loop().create_future()
        self.application.layout.focus(self._dialog_input)
        selector._notify_move()
        self.invalidate()
        return await self._dialog_future

    async def prompt_text_async(
        self, message: str, *, password: bool = False, default: str = ""
    ) -> str | None:
        """Open a one-line text/password overlay in this Application."""
        await self.start_async()
        self._selector = None
        self._dialog_kind = "text"
        self._dialog_message = str(message)
        self._dialog_password = bool(password)
        self._dialog_buffer.text = default
        self._dialog_future = asyncio.get_running_loop().create_future()
        self.application.layout.focus(self._dialog_input)
        self.invalidate()
        return await self._dialog_future

    def begin_run(self) -> None:
        self._run_active = True
        self._stream_text = ""
        self._stream_safe = ""
        self._stream_lines = ()
        self._run_status = ()
        self._run_output.clear()
        self._output_dirty = True
        self.refresh_transcript()
        self.invalidate()

    def set_cancel_run(self, callback: Callable[[], None] | None) -> None:
        """Bind cancellation for only the currently running Agent task."""
        self._cancel_run = callback

    def has_pending_submission(self) -> bool:
        """Return whether a newer prompt or command is waiting behind this run."""
        return not self._submissions.empty()

    def apply_preferences(
        self,
        *,
        style: BaseStyle,
        mouse_support: bool,
        sidebar_mode: str,
        message_keybindings: dict[str, str] | None = None,
    ) -> None:
        """Apply persisted UI preferences without rebuilding the root app."""
        self.style = style
        self.mouse_support = bool(mouse_support)
        self.sidebar_mode = str(sidebar_mode)
        if message_keybindings is not None:
            self.message_keybindings = {
                **_MESSAGE_NAV_DEFAULTS,
                **message_keybindings,
            }
            self._bindings = self._build_bindings()
        self.application.style = style
        self.application.mouse_support = to_filter(self.mouse_support)
        self.invalidate()

    def set_stream(self, text: str) -> None:
        self._stream_text = str(text)
        safe = _safe_transient_text(self._stream_text)
        if safe != self._stream_safe:
            self._stream_safe = safe
            self._stream_lines = _plain_text_lines(safe, "class:transcript")
        self._scroll_to_tail()
        self.invalidate()

    def set_run_status(self, lines: Iterable[str] | None) -> None:
        self._run_status = tuple(str(line) for line in (lines or ()) if str(line).strip())[:4]
        self._scroll_to_tail()
        self.invalidate()

    def append_output(self, value: str) -> None:
        clean = str(value or "")[:_MAX_OUTPUT_BLOCK_CHARS]
        fragments = tuple(
            (str(style), str(text))
            for style, text, *_handler in to_formatted_text(ANSI(clean))
        )
        target = self._run_output if self._run_active else self._notices
        target.append(fragments)
        del target[:-100]
        _bound_output_fragments(target, _MAX_OUTPUT_TOTAL_CHARS)
        self._output_dirty = True
        if not self._run_active:
            self.refresh_transcript()
        self._scroll_to_tail()
        self.invalidate()

    def end_run(self) -> None:
        self._run_active = False
        self._stream_text = ""
        self._stream_safe = ""
        self._stream_lines = ()
        self._run_status = ()
        self._run_output.clear()
        self._output_dirty = True
        self._cancel_run = None
        self.refresh_transcript()
        self.invalidate()

    def refresh_transcript(self) -> None:
        """Invalidate only the durable transcript projection and sidebar snapshot."""
        self._transcript_dirty = True
        self._sidebar_cached_at = 0.0
        self._status_cached_at = 0.0
        self._scroll_to_tail()

    def _scroll_to_tail(self) -> None:
        # FormattedTextControl's logical cursor owns sticky scrolling. The
        # next render places it on the newest line when follow mode is active.
        return None

    def invalidate(self) -> None:
        if self.application.is_running:
            self.application.invalidate()

    @property
    def run_active(self) -> bool:
        return self._run_active

    def _resolve_dialog(self, value) -> None:  # noqa: ANN001
        future = self._dialog_future
        self._dialog_future = None
        self._dialog_kind = ""
        self._selector = None
        self._dialog_detail = ""
        self._detail_cache_text = ""
        self._detail_window.vertical_scroll = 0
        self._dialog_buffer.reset()
        self.application.layout.focus(self._input_window)
        self.invalidate()
        if future is not None and not future.done():
            future.set_result(value)

    def _dialog_query_changed(self, _buffer) -> None:  # noqa: ANN001
        if self._selector is not None:
            self._selector.query = self._dialog_buffer.text
            self._selector.selected = 0
            self._selector._notify_move()
        self.invalidate()

    def _dialog_visible(self) -> bool:
        return bool(self._dialog_kind)

    def _dialog_title(self) -> str:
        if self._selector is not None:
            return self._selector.title
        if self._dialog_kind == "detail":
            return self._dialog_detail_title
        return "Input"

    def _dialog_hint(self):  # noqa: ANN202
        return [("class:selector.hint", self._dialog_message)]

    def _dialog_results(self):  # noqa: ANN202
        if self._selector is not None:
            return self._selector._render_results()
        return [("class:selector.hint", "Enter confirm · Esc cancel")]

    def _detail_content_lines(
        self, width: int
    ) -> tuple[tuple[tuple[str, str], ...], ...]:
        width = max(20, int(width))
        if self._detail_cache_text != self._dialog_detail or self._detail_cache_width != width:
            self._detail_lines = _markdown_lines(self._dialog_detail, width)
            self._detail_cache_text = self._dialog_detail
            self._detail_cache_width = width
        return self._detail_lines

    def _scroll_detail(self, amount: int) -> None:
        self._detail_window.vertical_scroll = max(
            0,
            min(
                max(0, len(self._detail_lines) - 1),
                self._detail_window.vertical_scroll + int(amount),
            ),
        )
        self.invalidate()

    def _transcript_fragments(self):  # noqa: ANN202
        columns = shutil.get_terminal_size(fallback=(100, 30)).columns
        sidebar_width = 43 if self._show_sidebar(columns) else 0
        width = max(40, columns - sidebar_width - 4)
        groups = self._content_line_groups(width)
        fragments: list[tuple[str, str]] = []
        for group in groups:
            for index, line in enumerate(group):
                if fragments or index:
                    fragments.append(("", "\n"))
                fragments.extend(line)
        self._rendered_line_count = max(1, sum(len(group) for group in groups))
        return fragments

    def _content_line_groups(
        self, width: int
    ) -> tuple[tuple[tuple[tuple[str, str], ...], ...], ...]:
        self._ensure_transcript(width)
        self._ensure_output_lines()
        status = _plain_text_lines("\n".join(self._run_status), "class:status")
        return tuple(
            group
            for group in (
                self._transcript_lines,
                self._output_lines,
                self._stream_lines,
                status,
            )
            if group
        )

    def _ensure_transcript(self, width: int) -> None:
        if self._transcript_dirty or width != self._transcript_width:
            transcript = self.transcript_provider()
            if _is_transcript_document(transcript):
                if not transcript.blocks:
                    from nz_coder.interface.presentation_tokens import build_empty_state

                    self._transcript_lines = _markdown_lines(
                        build_empty_state(self.product_state_provider()), width
                    )
                    self._message_anchors = ()
                    self._projection_anchors = ()
                    self._transcript_width = width
                    self._transcript_dirty = False
                    return
                self._render_transcript_document(transcript, width)
                self._transcript_width = width
                self._transcript_dirty = False
                return
            transcript = _bounded_transcript(str(transcript or ""))
            self._transcript_cache = tuple(
                (str(style), str(text))
                for style, text, *_handler in to_formatted_text(
                    _render_markdown(transcript, width)
                )
            )
            self._transcript_lines = _fragment_lines(self._transcript_cache)
            self._message_anchors = ()
            self._projection_anchors = ()
            self._transcript_width = width
            self._transcript_dirty = False

    def _render_transcript_document(self, document, width: int) -> None:  # noqa: ANN001
        """Render bounded message blocks independently and record line anchors."""
        header, blocks = _bounded_transcript_document(document)
        lines = list(_markdown_lines(header, width))
        anchors: list[MessageAnchor] = []
        projections: list[MessageAnchor] = []
        for block in blocks:
            markdown = (
                block.markdown
                if not block.part_id or block.part_id in self._expanded_tool_parts
                else block.compact_markdown or block.markdown
            )
            if block.part_id and block.part_id == self._hovered_tool_part:
                markdown = markdown.replace("**▸", "**▶", 1)
            block_lines = _markdown_lines(markdown, width)
            if lines and block_lines:
                lines.append((('', ''),))
            start = len(lines)
            lines.extend(block_lines)
            end = max(start, len(lines) - 1)
            anchor = MessageAnchor(
                message_id=block.message_id,
                role=block.role,
                start=start,
                end=end,
                turn_number=block.turn_number,
                markdown=block.markdown,
                part_id=block.part_id,
            )
            projections.append(anchor)
            if block.navigable:
                anchors.append(anchor)
        self._transcript_lines = tuple(lines) or ((('', ''),),)
        self._message_anchors = tuple(anchors)
        self._projection_anchors = tuple(projections)
        self._transcript_cache = tuple(
            fragment
            for line_number, line in enumerate(self._transcript_lines)
            for fragment in ((('', '\n'),) if line_number else ()) + tuple(line)
        )

    def navigate_message(self, action: str) -> bool:
        """Apply InfCode-compatible message-boundary navigation."""
        columns = shutil.get_terminal_size(fallback=(100, 30)).columns
        sidebar_width = 43 if self._show_sidebar(columns) else 0
        self._ensure_transcript(max(40, columns - sidebar_width - 4))
        anchors = self._message_anchors
        if action == "last":
            self._follow_tail = True
            self._scroll_to_tail()
            self.invalidate()
            return True
        if not anchors:
            return False
        current = max(0, self._transcript_window.vertical_scroll)
        target = None
        if action == "first":
            target = anchors[0]
        elif action == "last-user":
            target = next((item for item in reversed(anchors) if item.role == "user"), None)
        elif action == "next":
            target = next((item for item in anchors if item.start > current + 1), None)
            if target is None:
                self._transcript_window.vertical_scroll += 10
        elif action == "previous":
            target = next((item for item in reversed(anchors) if item.start < current - 1), None)
            if target is None:
                self._transcript_window.vertical_scroll = max(0, current - 10)
        else:
            return False
        self._follow_tail = False
        if target is not None:
            self._transcript_window.vertical_scroll = target.start
        self.invalidate()
        return target is not None

    def activate_transcript_line(self, line: int) -> bool:
        """Open a message detail overlay or toggle one ToolPart in place."""
        anchor = next(
            (item for item in self._projection_anchors if item.start <= line <= item.end),
            None,
        )
        if anchor is None:
            return False
        if anchor.part_id:
            if anchor.part_id in self._expanded_tool_parts:
                self._expanded_tool_parts.remove(anchor.part_id)
            else:
                self._expanded_tool_parts.add(anchor.part_id)
            self._transcript_dirty = True
            self._follow_tail = False
            self.invalidate()
            return True
        if anchor.role not in {"user", "assistant"}:
            return False
        label = f"Turn {anchor.turn_number}" if anchor.turn_number else "Assistant message"
        self._selector = None
        self._dialog_kind = "detail"
        self._dialog_detail_title = label
        self._dialog_message = "Enter or Esc returns"
        self._dialog_detail = anchor.markdown
        self._detail_cache_text = ""
        self._detail_window.vertical_scroll = 0
        columns = shutil.get_terminal_size(fallback=(100, 30)).columns
        self._detail_content_lines(max(20, columns - 12))
        self._dialog_future = None
        self.invalidate()
        return True

    def hover_transcript_line(self, line: int) -> None:
        """Highlight only actionable ToolParts without rebuilding root state."""
        anchor = next(
            (item for item in self._projection_anchors if item.start <= line <= item.end),
            None,
        )
        part_id = anchor.part_id if anchor is not None and anchor.part_id else ""
        if part_id == self._hovered_tool_part:
            return
        self._hovered_tool_part = part_id
        self._transcript_dirty = True
        self.invalidate()

    def _ensure_output_lines(self) -> None:
        if not self._output_dirty:
            return
        fragments = tuple(
            fragment
            for block in (*self._notices, *self._run_output)
            for fragment in block
        )
        self._output_lines = _fragment_lines(fragments) if fragments else ()
        self._output_dirty = False

    def _sidebar_text(self):  # noqa: ANN202
        return [("class:sidebar", self._sidebar_value())]

    def _sidebar_value(self) -> str:
        now = time.monotonic()
        if now - self._sidebar_cached_at >= _SIDEBAR_CACHE_SECONDS:
            value = self.sidebar_provider() if self.sidebar_provider is not None else ""
            self._sidebar_cache = _bounded_sidebar(value)
            self._sidebar_cached_at = now
        return self._sidebar_cache

    def _show_sidebar(self, columns: int) -> bool:
        text = self._sidebar_value()
        return bool(text) and (
            self.sidebar_mode == "show" or (self.sidebar_mode == "auto" and columns > 120)
        )

    def _header(self) -> StyleAndTextTuples:
        from nz_coder.interface.presentation_tokens import build_header

        state = dict(self.product_state_provider())
        state["run_state"] = "running" if self._run_active else state.get("run_state", "idle")
        columns = shutil.get_terminal_size(fallback=(100, 30)).columns
        return [
            ("class:status", build_header(state, width=columns)),
        ]

    def _attachment_text(self) -> StyleAndTextTuples:
        from nz_coder.interface.presentation_tokens import attachment_chips

        columns = shutil.get_terminal_size(fallback=(100, 30)).columns
        return [("class:status", " Attached  " + attachment_chips(
            self.attachments_provider(), width=max(20, columns - 12)
        ))]

    def _footer(self) -> StyleAndTextTuples:
        if self._run_active:
            value = " RUNNING · Ctrl+C cancel Agent · new requests queue "
        else:
            value = " Enter send · Alt+Enter newline · Ctrl+K commands · Ctrl+C clear/exit "
        return [("class:bottom-toolbar", value)]


def render_rich_output(*objects, width: int = 100, **kwargs) -> str:  # noqa: ANN003
    """Render Rich values to ANSI for projection inside prompt_toolkit."""
    output = StringIO()
    console = Console(
        file=output,
        force_terminal=True,
        color_system="truecolor",
        width=max(40, min(int(width), 240)),
    )
    console.print(*objects, **kwargs)
    return output.getvalue()


def _bounded_transcript(value: str, limit: int = 200_000) -> str:
    """Keep the newest transcript suffix so one screen cannot exhaust memory."""
    text = str(value or "").replace("\x00", "")
    if len(text) <= limit:
        return text
    return "… earlier transcript omitted …\n\n" + text[-limit:]


def _is_transcript_document(value: object) -> bool:
    """Recognize the timeline projection without coupling the UI to its module."""
    return (
        isinstance(getattr(value, "header", None), str)
        and isinstance(getattr(value, "blocks", None), tuple)
    )


def _bounded_transcript_document(document, limit: int = 200_000):  # noqa: ANN001, ANN202
    """Retain complete newest message blocks instead of slicing Markdown."""
    header = str(document.header or "")
    kept = []
    used = min(len(header), limit)
    for block in reversed(document.blocks):
        size = len(str(block.markdown or ""))
        if kept and used + size > limit:
            break
        if not kept and size > limit:
            # One pathological message remains renderable, but stays bounded.
            from dataclasses import replace

            block = replace(
                block,
                markdown="… earlier message content omitted …\n\n" + block.markdown[-limit:],
            )
            size = len(block.markdown)
        kept.append(block)
        used += size
    kept.reverse()
    if len(kept) < len(document.blocks):
        header = header.rstrip() + "\n\n… earlier transcript omitted …\n"
    return header, tuple(kept)


def _markdown_lines(value: str, width: int):  # noqa: ANN202
    fragments = tuple(
        (str(style), str(text))
        for style, text, *_handler in to_formatted_text(_render_markdown(value, width))
    )
    return _fragment_lines(fragments)


def _render_markdown(value: str, width: int) -> ANSI:
    """Render trusted Markdown structure while neutralizing embedded escapes."""
    clean = _CONTROL.sub("", _ANSI_ESCAPE.sub("", str(value or "")))
    if not clean.strip():
        clean = "No messages yet."
    return ANSI(render_rich_output(Markdown(clean), width=width, soft_wrap=False))


def _bounded_sidebar(value: str, limit: int = 8_000) -> str:
    text = str(value or "").replace("\x00", "")
    return text[:limit] + ("\n…" if len(text) > limit else "")


def _safe_transient_text(value: str, limit: int = 120_000) -> str:
    """Keep untrusted streamed text inert until durable Markdown rendering."""
    return _CONTROL.sub("", _ANSI_ESCAPE.sub("", str(value or "")))[:limit]


def _bound_output_fragments(
    blocks: list[tuple[tuple[str, str], ...]], limit: int
) -> None:
    """Bound retained command/tool output without splitting ANSI sequences."""
    total = sum(len(text) for block in blocks for _style, text in block)
    while len(blocks) > 1 and total > limit:
        removed = blocks.pop(0)
        total -= sum(len(text) for _style, text in removed)


def _fragment_lines(
    fragments: tuple[tuple[str, str], ...]
) -> tuple[tuple[tuple[str, str], ...], ...]:
    return tuple(
        tuple((str(style), str(text)) for style, text, *_handler in line)
        for line in split_lines(fragments)
    )


def _plain_text_lines(
    value: str, style: str
) -> tuple[tuple[tuple[str, str], ...], ...]:
    text = str(value or "")
    if not text:
        return ()
    return tuple(((style, line),) for line in text.split("\n"))
