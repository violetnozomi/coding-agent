"""Async fuzzy keyboard selector for interactive terminal commands."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import FormattedTextControl, HSplit, Layout, Window
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea


_VISIBLE_ROWS = 14


@dataclass(frozen=True)
class SelectorOption:
    """One value and its searchable terminal label."""

    value: object
    label: str
    order: int


@dataclass(frozen=True)
class SelectorActionResult:
    """One dialog-local key action applied to the focused option."""

    action: str
    value: object | None


class FuzzySelector:
    """Filter options while typing and return one value with a single Enter."""

    def __init__(
        self,
        *,
        title: str,
        values: Iterable[tuple[object, str]],
        text: str = "Type to filter · Up/Down move · Enter select · Esc cancel",
        multiple: bool = False,
        allow_custom: bool = False,
        actions: Iterable[tuple[str, str, str]] = (),
        style: Style | None = None,
        mouse_support: bool = False,
        on_move: Callable[[object | None], None] | None = None,
    ) -> None:
        self.title = str(title)
        self.text = str(text)
        self.multiple = bool(multiple)
        self.allow_custom = bool(allow_custom)
        self.actions = tuple(actions)
        self.style = style
        self.mouse_support = bool(mouse_support)
        self.on_move = on_move
        self.options = tuple(
            SelectorOption(value=value, label=str(label), order=index)
            for index, (value, label) in enumerate(values)
        )
        self.query = ""
        self.selected = 0
        self.selected_values: list[object] = []
        self._application: Application | None = None

    def filtered_options(self) -> tuple[SelectorOption, ...]:
        """Return stable fuzzy matches, favoring exact/prefix/compact hits."""
        query = " ".join(self.query.lower().split())
        if not query:
            return self.options
        ranked = []
        for option in self.options:
            score = _fuzzy_score(query, option.label.lower())
            if score is not None:
                ranked.append((score, option.order, option))
        ranked.sort(key=lambda item: (item[0], item[1]))
        return tuple(item[2] for item in ranked)

    def move(self, offset: int) -> None:
        matches = self.filtered_options()
        if not matches:
            self.selected = 0
            return
        self.selected = (self.selected + offset) % len(matches)
        self._notify_move()

    def current_value(self) -> object | None:
        matches = self.filtered_options()
        if not matches:
            custom = self.query.strip()
            return custom if self.allow_custom and custom else None
        self.selected = min(self.selected, len(matches) - 1)
        return matches[self.selected].value

    def toggle_current(self) -> None:
        """Toggle one multiple-choice value while preserving selection order."""
        value = self.current_value()
        if value is None:
            return
        for index, selected in enumerate(self.selected_values):
            if selected == value:
                self.selected_values.pop(index)
                return
        self.selected_values.append(value)

    def application(self, *, input=None, output=None) -> Application:  # noqa: ANN001
        """Build a full-screen Application; injectable I/O keeps it testable."""
        search = TextArea(
            height=1,
            prompt="Search: ",
            multiline=False,
            wrap_lines=False,
            style="class:selector.search",
        )
        results = Window(
            FormattedTextControl(self._render_results),
            height=Dimension(min=3, preferred=_VISIBLE_ROWS + 1, max=_VISIBLE_ROWS + 1),
            always_hide_cursor=True,
        )
        body = HSplit([
            Window(
                FormattedTextControl([("class:selector.hint", self.text)]),
                height=min(4, self.text.count("\n") + 2),
                wrap_lines=True,
            ),
            Window(height=1, char="─", style="class:selector.rule"),
            search,
            Window(height=1),
            results,
        ])
        bindings = KeyBindings()

        @bindings.add("up", eager=True)
        @bindings.add("c-p", eager=True)
        def _up(event) -> None:  # noqa: ANN001
            self.move(-1)
            event.app.invalidate()

        @bindings.add("down", eager=True)
        @bindings.add("c-n", eager=True)
        def _down(event) -> None:  # noqa: ANN001
            self.move(1)
            event.app.invalidate()

        @bindings.add("pageup", eager=True)
        def _page_up(event) -> None:  # noqa: ANN001
            self.move(-_VISIBLE_ROWS)
            event.app.invalidate()

        @bindings.add("pagedown", eager=True)
        def _page_down(event) -> None:  # noqa: ANN001
            self.move(_VISIBLE_ROWS)
            event.app.invalidate()

        @bindings.add("enter", eager=True)
        def _accept(event) -> None:  # noqa: ANN001
            value = self.current_value()
            if self.multiple:
                if not self.selected_values and value is not None:
                    self.selected_values.append(value)
                if self.selected_values:
                    event.app.exit(result=tuple(self.selected_values))
            elif value is not None:
                event.app.exit(result=value)

        @bindings.add(" ", eager=True)
        def _toggle(event) -> None:  # noqa: ANN001
            if not self.multiple:
                event.current_buffer.insert_text(" ")
                return
            if self.query.strip() and not self.filtered_options():
                event.current_buffer.insert_text(" ")
                return
            self._toggle_and_reset(search, event.app)

        @bindings.add("c-space", eager=True)
        def _force_toggle(event) -> None:  # noqa: ANN001
            if not self.multiple:
                return
            self._toggle_and_reset(search, event.app)

        @bindings.add("escape", eager=True)
        @bindings.add("c-c", eager=True)
        def _cancel(event) -> None:  # noqa: ANN001
            event.app.exit(result=None)

        for key, action, _title in self.actions:
            def _trigger(event, selected_action=action) -> None:  # noqa: ANN001
                event.app.exit(
                    result=SelectorActionResult(selected_action, self.current_value())
                )

            bindings.add(key, eager=True)(_trigger)

        def _query_changed(_buffer) -> None:  # noqa: ANN001
            self.query = search.text
            self.selected = 0
            self._notify_move()
            if self._application is not None:
                self._application.invalidate()

        search.buffer.on_text_changed += _query_changed
        application = Application(
            layout=Layout(Frame(body, title=self.title), focused_element=search),
            key_bindings=bindings,
            style=self.style or _selector_style(),
            full_screen=True,
            mouse_support=self.mouse_support,
            input=input,
            output=output,
        )
        self._application = application
        return application

    def _toggle_and_reset(self, search: TextArea, application: Application) -> None:
        """Toggle the focused value and reset filtering for another choice."""
        self.toggle_current()
        search.text = ""
        self.query = ""
        self.selected = 0
        application.invalidate()

    async def run_async(self) -> object | None:
        """Run inside the caller's event loop."""
        return await self.application().run_async()

    def _render_results(self):  # noqa: ANN202
        matches = self.filtered_options()
        if not matches:
            custom = self.query.strip()
            if self.allow_custom and custom:
                marker = "[x]" if any(value == custom for value in self.selected_values) else "[ ]"
                prefix = marker if self.multiple else "›"
                return [("class:selector.selected", f" {prefix} Use custom answer: {custom}")]
            return [("class:selector.empty", "  No matches")]
        self.selected = min(self.selected, len(matches) - 1)
        start = max(0, self.selected - (_VISIBLE_ROWS // 2))
        start = min(start, max(0, len(matches) - _VISIBLE_ROWS))
        visible = matches[start:start + _VISIBLE_ROWS]
        fragments: StyleAndTextTuples = []
        for index, option in enumerate(visible, start=start):
            selected = index == self.selected
            style = "class:selector.selected" if selected else "class:selector.option"
            if self.multiple:
                checked = any(value == option.value for value in self.selected_values)
                marker = "[x]" if checked else "[ ]"
            else:
                marker = "›" if selected else " "
            fragment = (style, f" {marker} {option.label}")
            if self.mouse_support:
                fragment = (*fragment, self._mouse_handler(index))
            fragments.append(fragment)
            if index < start + len(visible) - 1:
                fragments.append(("", "\n"))
        return fragments

    def _mouse_handler(self, index: int):
        def handle(mouse_event):  # noqa: ANN001
            if mouse_event.event_type != MouseEventType.MOUSE_UP:
                return None
            self.selected = index
            value = self.current_value()
            if value is not None and self._application is not None:
                self._application.exit(result=value)
            return None

        return handle

    def _notify_move(self) -> None:
        if self.on_move is not None:
            self.on_move(self.current_value())


def _fuzzy_score(query: str, candidate: str) -> tuple[int, int, int, int] | None:
    """Score exact, prefix, substring, then compact subsequence matches."""
    if query == candidate:
        return (0, 0, 0, len(candidate))
    if candidate.startswith(query):
        return (1, 0, 0, len(candidate))
    position = candidate.find(query)
    if position >= 0:
        return (2, 0, position, len(candidate))
    positions: list[int] = []
    cursor = 0
    for character in query:
        position = candidate.find(character, cursor)
        if position < 0:
            return None
        positions.append(position)
        cursor = position + 1
    span = positions[-1] - positions[0] + 1
    gaps = span - len(query)
    return (3, gaps, positions[0], len(candidate))


def _selector_style() -> Style:
    return Style.from_dict({
        "frame.label": "#5fd7ff bold",
        "frame.border": "#536273",
        "selector.hint": "#8c98a8",
        "selector.rule": "#394451",
        "selector.search": "#ffffff bg:#202a34",
        "selector.option": "#d8dee9",
        "selector.selected": "#ffffff bg:#007f9e bold",
        "selector.empty": "#8c98a8 italic",
    })
