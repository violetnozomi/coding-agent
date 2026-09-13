"""Interactive prompt editing, completion, history, and status for the CLI."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.shortcuts import CompleteStyle
from prompt_toolkit.styles import merge_styles

from nz_coder.interface.commands.registry import (
    CommandRegistry,
    product_command_category,
)
from nz_coder.interface.presentation_tokens import (
    clip_terminal_text,
    terminal_text_width,
)
from nz_coder.interface.preferences import (
    TerminalPreferences,
    command_keybinding,
    configurable_keybinding_actions,
    load_terminal_preferences,
    message_keybindings,
    prompt_style,
    selector_style,
    theme_names,
)
from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.foundation.user_paths import resolve_private_attachment
from nz_coder.interface.selector import FuzzySelector
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.state.sessions import list_session_ids


_IGNORED_DIRECTORIES = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".nz-coder",
    ".pytest_cache",
    ".ruff_cache",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
_FILE_REFERENCE = re.compile(r"(?:^|\s)@([^\s@]*)$")
_INLINE_FILE_REFERENCE = re.compile(r"(?:^|\s)@([^\s@]+)")
_MAX_COMPLETION_FILES = 10_000
_MAX_ATTACHMENTS = 20
_MAX_CLIPBOARD_CHARS = 200_000
_DOUBLE_CTRL_C_SECONDS = 1.0


def _split_dropped_paths(text: str, *, os_name: str | None = None) -> tuple[str, ...]:
    """Split a path-only submission using the host shell's quoting rules."""
    selected_os = os.name if os_name is None else os_name
    values = shlex.split(str(text), posix=selected_os != "nt")
    if selected_os != "nt":
        return tuple(values)
    return tuple(
        value[1:-1]
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}
        else value
        for value in values
    )


@dataclass(frozen=True)
class TerminalInputAction:
    """An input-surface action emitted without submitting user text."""

    name: str
    text: str = ""


@dataclass(frozen=True)
class AttachedFile:
    """One validated workspace file queued for the next user turn."""

    path: str
    size: int
    host_path: str = ""


class TerminalCompleter(Completer):
    """Complete slash commands, command arguments, and workspace files."""

    def __init__(
        self,
        registry: CommandRegistry,
        workspace: Path,
        *,
        file_provider: Callable[[], Iterable[str]] | None = None,
        session_provider: Callable[[], Iterable[str]] | None = None,
        model_provider: Callable[[], Iterable[str]] | None = None,
    ) -> None:
        self._registry = registry
        self._workspace = workspace.resolve()
        self._file_provider = file_provider or self._scan_files
        self._session_provider = session_provider or list_session_ids
        self._model_provider = model_provider or _known_model_ids
        self._files: tuple[str, ...] | None = None

    def invalidate_files(self) -> None:
        """Refresh file references after an Agent may have changed the tree."""
        self._files = None

    def get_completions(self, document: Document, complete_event):  # noqa: ANN001
        text = document.text_before_cursor
        file_match = _FILE_REFERENCE.search(text)
        if file_match:
            fragment = file_match.group(1)
            for path in self._matching_files(fragment):
                yield Completion(
                    path,
                    start_position=-len(fragment),
                    display=f"@{path}",
                    display_meta="workspace file",
                )
            return

        stripped = text.lstrip()
        if not stripped.startswith("/"):
            return
        if " " not in stripped:
            fragment = stripped[1:].lower()
            for command in self._registry.visible_commands():
                for name in (command.name, *command.aliases):
                    if not name.lower().startswith(fragment):
                        continue
                    meta = command.description
                    if name != command.name:
                        meta = f"{meta} (alias for /{command.name})"
                    yield Completion(
                        f"/{name}",
                        start_position=-len(stripped),
                        display=f"/{name}",
                        display_meta=meta,
                    )
            return

        command, argument = stripped.split(maxsplit=1)
        normalized = command.lower().lstrip("/")
        if normalized == "resume":
            yield from self._value_completions(argument, self._session_provider(), "saved session")
        elif normalized in {"model", "models"}:
            values = ("list", "reset", *self._model_provider())
            yield from self._value_completions(argument, values, "workspace model")
        elif normalized == "mode":
            yield from self._value_completions(
                argument,
                ("default", "auto", "plan", "acceptEdits"),
                "permission mode",
            )
        elif normalized == "theme":
            yield from self._value_completions(argument, theme_names(), "terminal theme")
        elif normalized == "tool-details":
            yield from self._value_completions(
                argument, ("compact", "normal", "detailed"), "tool card details",
            )
        elif normalized == "mouse":
            yield from self._value_completions(argument, ("on", "off"), "mouse support")
        elif normalized == "sidebar":
            yield from self._value_completions(
                argument, ("auto", "show", "hide"), "sidebar visibility",
            )
        elif normalized == "model-cycle":
            yield from self._value_completions(
                argument,
                ("next", "previous", "favorite-next", "favorite-previous"),
                "model cycle",
            )
        elif normalized == "keybind":
            yield from self._value_completions(
                argument,
                ("list", "reset", *configurable_keybinding_actions()),
                "terminal keybinding action",
            )
        elif normalized == "permission":
            prefix = "mode "
            if argument.lower().startswith(prefix):
                mode = argument[len(prefix):]
                yield from self._value_completions(
                    mode,
                    ("default", "auto", "plan", "acceptEdits"),
                    "permission mode",
                )
            else:
                yield from self._value_completions(
                    argument,
                    ("mode", "rules"),
                    "permission action",
                )

    def _matching_files(self, fragment: str) -> tuple[str, ...]:
        if self._files is None:
            self._files = tuple(sorted(self._file_provider(), key=str.lower))
        lowered = fragment.lower()
        prefix = [path for path in self._files if path.lower().startswith(lowered)]
        contains = [
            path for path in self._files
            if lowered in path.lower() and path not in prefix
        ]
        return tuple((prefix + contains)[:100])

    def _scan_files(self) -> tuple[str, ...]:
        return scan_workspace_files(self._workspace)

    @staticmethod
    def _value_completions(fragment: str, values: Iterable[str], meta: str):
        lowered = fragment.lower()
        for value in values:
            candidate = str(value)
            if candidate.lower().startswith(lowered):
                yield Completion(
                    candidate,
                    start_position=-len(fragment),
                    display_meta=meta,
                )


class TerminalInput:
    """Own one PromptSession and fall back safely outside an interactive TTY."""

    def __init__(
        self,
        *,
        console,
        registry: CommandRegistry,
        workspace: Path | None = None,
        state_provider: Callable[[], dict[str, str]] | None = None,
        fallback_reader: Callable[[], str] | None = None,
        interactive: bool | None = None,
        prompt_session: PromptSession | None = None,
        transcript_provider: Callable[[], str] | None = None,
        sidebar_provider: Callable[[], str] | None = None,
        attachments_enabled: bool = True,
    ) -> None:
        self.console = console
        self.workspace = (workspace or current_workdir()).resolve()
        self.registry = registry
        self.state_provider = state_provider or (lambda: {})
        self.transcript_provider = transcript_provider
        self.sidebar_provider = sidebar_provider
        self.attachments_enabled = bool(attachments_enabled)
        self.fallback_reader = fallback_reader or self._fallback_read
        if interactive is None:
            interactive = bool(
                getattr(console, "is_terminal", False)
                and getattr(sys.stdin, "isatty", lambda: False)()
            )
        self.interactive = interactive
        self.completer = TerminalCompleter(
            registry,
            self.workspace,
            file_provider=None if self.attachments_enabled else (lambda: ()),
        )
        self.preferences = load_terminal_preferences(self.workspace)
        self._attachments: list[AttachedFile] = []
        self._recent_commands: list[str] = []
        self._last_empty_ctrl_c: float | None = None
        self._history_path: Path | None = None
        self._owns_session = prompt_session is None
        self._used_fullscreen = False
        self.fullscreen = None
        self.session = prompt_session
        if self.interactive and self.session is None:
            self._history_path = _prepare_history_path(self.workspace)
            self.session = self._new_session()
            if self.transcript_provider is not None:
                from nz_coder.interface.fullscreen import FullscreenComposer

                history = FileHistory(str(self._history_path)) if self._history_path else None
                self.fullscreen = FullscreenComposer(
                    transcript_provider=self.transcript_provider,
                    status_provider=self._status_text,
                    product_state_provider=self.state_provider,
                    attachments_provider=self.attachments,
                    sidebar_provider=self.sidebar_provider,
                    sidebar_mode=self.preferences.sidebar,
                    completer=self.completer,
                    history=history,
                    style=merge_styles([
                        prompt_style(self.preferences.theme),
                        selector_style(self.preferences.theme),
                    ]),
                    mouse_support=self.preferences.mouse,
                    message_keybindings=message_keybindings(self.preferences),
                    empty_ctrl_c_requests_exit=self._empty_ctrl_c_requests_exit,
                    clear_exit_request=self._clear_exit_request,
                    paste_image=self.paste_clipboard_image,
                )

    def read(self) -> str:
        """Read one submission; Alt+Enter inserts a newline and Enter submits."""
        if not self.interactive or self.session is None:
            return self.fallback_reader()
        while True:
            try:
                result = self.session.prompt()
            finally:
                self._finish_composer()
                self._secure_history()
            if isinstance(result, TerminalInputAction) and result.name == "exit_press":
                if self._empty_ctrl_c_requests_exit():
                    raise EOFError
                self.console.print("[info]Press Ctrl+C again to exit.[/info]")
                continue
            if isinstance(result, TerminalInputAction) and result.name == "exit_confirmed":
                raise EOFError
            self._last_empty_ctrl_c = None
            value = str(result)
            self._remember_command(value)
            return value

    async def read_async(self, *, open_editor: bool = False) -> str:
        """Read one submission without nesting an event loop in the Agent CLI."""
        if not self.interactive or self.session is None:
            return self.fallback_reader()
        default = ""
        while True:
            try:
                result = await self._prompt_async(default, open_editor=open_editor)
                open_editor = False
            finally:
                self._finish_composer()
                self._secure_history()
            if not isinstance(result, TerminalInputAction):
                self._last_empty_ctrl_c = None
                value = str(result)
                self._remember_command(value)
                return value
            if result.name == "exit_press":
                if self._empty_ctrl_c_requests_exit():
                    raise EOFError
                self.console.print("[info]Press Ctrl+C again to exit.[/info]")
                default = ""
                continue
            if result.name == "exit_confirmed":
                raise EOFError
            if result.name == "command_palette":
                selected = await self._command_palette()
                if selected is not None:
                    value = f"/{selected}"
                    self._remember_command(value)
                    return value
                default = result.text
                continue
            return result.text

    def has_pending_submission(self) -> bool:
        """Return whether the persistent terminal surface queued a follow-up."""
        surface = self.fullscreen
        return bool(surface is not None and surface.has_pending_submission())

    def _empty_ctrl_c_requests_exit(self) -> bool:
        """Match InfCode's one-second, double-Ctrl+C exit gesture."""
        now = time.monotonic()
        previous = self._last_empty_ctrl_c
        self._last_empty_ctrl_c = now
        if previous is None or now - previous > _DOUBLE_CTRL_C_SECONDS:
            return False
        self._last_empty_ctrl_c = None
        return True

    async def select_async(
        self,
        *,
        title: str,
        values: Iterable[tuple[object, str]],
        text: str = "Type to filter · Up/Down move · Enter select · Esc cancel",
        detail: str = "",
        multiple: bool = False,
        allow_custom: bool = False,
        actions: Iterable[tuple[str, str, str]] = (),
        on_move: Callable[[object | None], None] | None = None,
    ) -> object | None:
        """Show a keyboard-operated choice dialog without nesting event loops."""
        choices = list(values)
        if not self.interactive or not choices:
            return None
        selector = FuzzySelector(
            title=title,
            text=(f"{detail}\n{text}" if detail and self.fullscreen is None else text),
            values=choices,
            multiple=multiple,
            allow_custom=allow_custom,
            actions=actions,
            style=selector_style(self.preferences.theme),
            mouse_support=self.preferences.mouse,
            on_move=on_move,
        )
        try:
            if self.fullscreen is not None:
                return await self.fullscreen.select_async(
                    title=title,
                    text=text,
                    detail=detail,
                    values=choices,
                    multiple=multiple,
                    allow_custom=allow_custom,
                    actions=actions,
                    on_move=on_move,
                )
            return await selector.run_async()
        except (EOFError, KeyboardInterrupt):
            return None

    def refresh_files(self) -> None:
        self.completer.invalidate_files()

    def refresh_view(self) -> None:
        """Refresh durable full-screen projections after an external state change."""
        if self.fullscreen is not None:
            self.fullscreen.refresh_transcript()
            self.fullscreen.invalidate()

    def navigate_message(self, action: str) -> bool:
        """Navigate the persistent transcript when that surface is active."""
        if self.fullscreen is None:
            return False
        return bool(self.fullscreen.navigate_message(action))

    def reload_preferences(self) -> TerminalPreferences:
        """Reload persisted UI state and apply it to subsequent prompts."""
        self.preferences = load_terminal_preferences(self.workspace)
        if self.interactive and self._owns_session:
            self.session = self._new_session()
        if self.fullscreen is not None:
            self.fullscreen.apply_preferences(
                style=merge_styles([
                    prompt_style(self.preferences.theme),
                    selector_style(self.preferences.theme),
                ]),
                mouse_support=self.preferences.mouse,
                sidebar_mode=self.preferences.sidebar,
                message_keybindings=message_keybindings(self.preferences),
            )
        return self.preferences

    def queue_attachment(self, value: str) -> AttachedFile:
        """Queue one non-symlink workspace file for the next Agent turn."""
        if not self.attachments_enabled:
            raise ValueError(
                "Client-local path attachments are disabled for a remote URL; "
                "put the file in the server workspace or use a LOCAL DAEMON."
            )
        attachment = self._resolve_attachment(value, strict=True)
        assert attachment is not None
        self._attachments = [item for item in self._attachments if item.path != attachment.path]
        self._attachments.append(attachment)
        if len(self._attachments) > _MAX_ATTACHMENTS:
            self._attachments.pop(0)
        if self.fullscreen is not None:
            self.fullscreen.invalidate()
        return attachment

    def _resolve_attachment(self, value: str, *, strict: bool) -> AttachedFile | None:
        """Resolve one workspace-bounded file reference without following symlinks."""
        raw = str(value).strip().lstrip("@")
        if not raw:
            if strict:
                raise ValueError("Use /attach PATH")
            return None
        if raw.startswith("user-state://attachments/"):
            try:
                private = resolve_private_attachment(self.workspace, raw)
                if private.is_symlink() or not private.is_file():
                    raise ValueError("private attachment is missing or unsafe")
                return AttachedFile(raw, private.stat().st_size, str(private))
            except (OSError, ValueError) as exc:
                if strict:
                    raise ValueError("Private attachment is missing or unsafe") from exc
                return None
        candidate = Path(raw)
        path = candidate if candidate.is_absolute() else self.workspace / candidate
        if path.is_symlink():
            if strict:
                raise ValueError("Attachment symlinks are not allowed")
            return None
        try:
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(self.workspace).as_posix()
        except (OSError, ValueError) as exc:
            if strict:
                raise ValueError("Attachment must be a file inside the workspace") from exc
            return None
        if not resolved.is_file():
            if strict:
                raise ValueError("Attachment must be a regular file")
            return None
        return AttachedFile(relative, resolved.stat().st_size)

    def remove_attachment(self, value: str = "") -> int:
        """Remove one queued attachment, or all attachments when requested."""
        target = str(value).strip().lstrip("@")
        if not target or target.lower() == "all":
            count = len(self._attachments)
            self._attachments.clear()
            if self.fullscreen is not None:
                self.fullscreen.invalidate()
            return count
        before = len(self._attachments)
        self._attachments = [item for item in self._attachments if item.path != target]
        if self.fullscreen is not None:
            self.fullscreen.invalidate()
        return before - len(self._attachments)

    def attachments(self) -> tuple[AttachedFile, ...]:
        return tuple(self._attachments)

    def paste_clipboard_image(self) -> bool:
        """Persist and queue a system clipboard image through normal attachments."""
        if not self.attachments_enabled:
            return False
        from nz_coder.interface.clipboard import persist_image, read_image

        image = read_image()
        if image is None:
            return False
        reference = persist_image(self.workspace, image)
        attachment = self._resolve_attachment(reference, strict=False)
        if attachment is None:
            return False
        self._attachments.append(attachment)
        if len(self._attachments) > _MAX_ATTACHMENTS:
            self._attachments.pop(0)
        if self.fullscreen is not None:
            self.fullscreen.invalidate()
        return True

    def _dropped_file_attachments(self, text: str) -> tuple[AttachedFile, ...]:
        """Recognize a prompt consisting entirely of shell-quoted workspace paths."""
        if not self.attachments_enabled:
            return ()
        try:
            values = _split_dropped_paths(text)
        except ValueError:
            return ()
        if not values or len(values) > _MAX_ATTACHMENTS:
            return ()
        resolved = [self._resolve_attachment(value, strict=False) for value in values]
        if any(item is None for item in resolved):
            return ()
        return tuple(item for item in resolved if item is not None)

    def prepare_submission(self, text: str) -> tuple[str, tuple[AttachedFile, ...]]:
        """Attach file references to one turn, then clear the one-shot queue."""
        if not self.attachments_enabled:
            self._attachments.clear()
            return text, ()
        attachments = list(self.attachments())
        self._attachments.clear()
        dropped = self._dropped_file_attachments(text)
        if dropped:
            text = "Please inspect the attached file(s)."
            attachments.extend(dropped)
        seen = {item.path for item in attachments}
        for match in _INLINE_FILE_REFERENCE.finditer(str(text or "")):
            attachment = self._resolve_attachment(match.group(1), strict=False)
            if attachment is None or attachment.path in seen:
                continue
            attachments.append(attachment)
            seen.add(attachment.path)
            if len(attachments) >= _MAX_ATTACHMENTS:
                break
        if not attachments:
            return text, ()
        lines = [
            "<attached-files>",
            "The user explicitly attached these workspace files. Read them with the file tools when relevant:",
        ]
        lines.extend(f"- {item.path} ({item.size} bytes)" for item in attachments)
        lines.extend(["</attached-files>", "", text])
        return "\n".join(lines), tuple(attachments)

    async def prompt_text_async(
        self,
        message: str,
        *,
        password: bool = False,
        default: str = "",
    ) -> str | None:
        """Read a small dialog value; passwords are masked and never persisted."""
        if not self.interactive:
            return None
        if self.fullscreen is not None:
            return await self.fullscreen.prompt_text_async(
                message, password=password, default=default
            )
        session = PromptSession(
            message=message,
            is_password=password,
            style=prompt_style(self.preferences.theme),
        )
        try:
            return await session.prompt_async(default=default)
        except (EOFError, KeyboardInterrupt):
            return None

    def _new_session(self) -> PromptSession:
        history = FileHistory(str(self._history_path)) if self._history_path else None
        return PromptSession(
            message=self._composer_prompt,
            multiline=True,
            history=history,
            auto_suggest=AutoSuggestFromHistory(),
            completer=self.completer,
            complete_while_typing=True,
            complete_style=CompleteStyle.COLUMN,
            reserve_space_for_menu=10,
            enable_history_search=True,
            key_bindings=_build_key_bindings(
                empty_ctrl_c_requests_exit=self._empty_ctrl_c_requests_exit,
                clear_exit_request=self._clear_exit_request,
                paste_image=self.paste_clipboard_image,
            ),
            bottom_toolbar=self._bottom_toolbar,
            rprompt=[("class:composer.border", "│")],
            prompt_continuation=self._continuation_prompt,
            style=prompt_style(self.preferences.theme),
            mouse_support=self.preferences.mouse,
            enable_open_in_editor=True,
        )

    async def _prompt_async(self, default: str, *, open_editor: bool = False):
        if self.fullscreen is not None:
            self._used_fullscreen = True
            from nz_coder.interface.fullscreen import TerminalSurfaceError

            for _attempt in range(2):
                try:
                    return await self.fullscreen.read_async(
                        default, open_editor=open_editor
                    )
                except TerminalSurfaceError as exc:
                    if await self.fullscreen.recover_async():
                        open_editor = False
                        continue
                    failed_surface = self.fullscreen
                    self.fullscreen = None
                    await failed_surface.close_async()
                    disable_surface = getattr(self.console, "disable_surface", None)
                    if callable(disable_surface):
                        disable_surface()
                    else:
                        self.console = getattr(self.console, "_base", self.console)
                    printer = getattr(self.console, "print", None)
                    if callable(printer):
                        printer(
                            f"Terminal UI failed twice; continuing in safe inline mode: {exc}",
                            markup=False,
                        )
                    break
        pre_run = None
        if open_editor:
            def pre_run() -> None:
                from prompt_toolkit.application.current import get_app

                get_app().current_buffer.open_in_editor()
        try:
            return await self.session.prompt_async(default=default, pre_run=pre_run)
        except TypeError:
            # Small injected fakes in embedders may implement only prompt_async().
            return await self.session.prompt_async()

    async def _command_palette(self) -> str | None:
        commands = self.registry.palette_commands(recent=tuple(self._recent_commands))
        values = []
        for command in commands:
            binding = command_keybinding(
                command.name, command.keybind, self.preferences,
            )
            suffix = f"  · {binding}" if binding else ""
            values.append((
                command.name,
                f"[{product_command_category(command)}] /{command.name:<18} "
                f"{command.description}{suffix}",
            ))
        selected = await self.select_async(
            title="Commands",
            values=values,
            text="Ctrl+K command palette · type to filter · Enter run · Esc return",
        )
        return str(selected) if selected is not None else None

    def _remember_command(self, value: str) -> None:
        """Keep a bounded in-memory MRU for palette ordering."""
        if not str(value).lstrip().startswith("/"):
            return
        name = str(value).lstrip()[1:].split(maxsplit=1)[0].lower()
        command = self.registry.get(name)
        if command is None:
            return
        canonical = command.name
        self._recent_commands = [
            item for item in self._recent_commands if item != canonical
        ]
        self._recent_commands.insert(0, canonical)
        del self._recent_commands[8:]

    def _secure_history(self) -> None:
        if self._history_path is not None:
            try:
                self._history_path.chmod(0o600)
            except OSError:
                pass

    def _fallback_read(self) -> str:
        first_line = self.console.input("[bold cyan]nz-coder >> [/bold cyan]")
        from nz_coder.interface.cli import _drain_pasted_lines

        extra_lines = _drain_pasted_lines()
        return "\n".join([first_line, *extra_lines]) if extra_lines else first_line

    def _composer_prompt(self) -> StyleAndTextTuples:
        """Render an inline composer header and first-line input prompt."""
        width = self._composer_width()
        status = self._status_text()
        inner_width = max(1, width - 2)
        title = f"─ New request · {status} " if status else "─ New request "
        # Reserve one rule cell where possible so the clipped label still
        # reads as a framed composer.  Width is terminal-column based: Python
        # string length is wrong for CJK, emoji, and combining sequences.
        title = clip_terminal_text(title, max(1, inner_width - 1))
        available = max(0, inner_width - terminal_text_width(title))
        header = f"╭{title}{'─' * available}╮\n"
        return [
            ("class:composer.border", header),
            ("class:composer.border", "│ "),
            ("class:prompt", "❯ "),
        ]

    def _status_text(self) -> str:
        try:
            state = self.state_provider()
        except Exception:
            state = {}
        provider = state.get("provider", "-")
        model = state.get("model", "-")
        mode = state.get("mode", "-")
        context = state.get("context", "")
        values = [f"{provider}/{model}", mode]
        location = str(state.get("location") or "")
        if location and location != "LOCAL":
            values.insert(0, location)
        if context:
            values.append(context)
        if self._attachments:
            values.append(f"{len(self._attachments)} attached")
        return " · ".join(values)

    def _bottom_toolbar(self) -> StyleAndTextTuples:
        """Keep exit guidance inside the active composer without rebuilding it."""
        previous = self._last_empty_ctrl_c
        if previous is not None and time.monotonic() - previous <= _DOUBLE_CTRL_C_SECONDS:
            return [("class:bottom-toolbar", " Ctrl+C again to exit ")]
        return [("class:bottom-toolbar", " Enter send · Alt+Enter newline · Ctrl+K commands ")]

    def _clear_exit_request(self) -> None:
        self._last_empty_ctrl_c = None

    def _continuation_prompt(
        self,
        _prompt_width: int,
        _line_number: int,
        _is_soft_wrap: bool,
    ) -> StyleAndTextTuples:
        return [
            ("class:composer.border", "│ "),
            ("class:prompt.continuation", "  "),
        ]

    def _composer_width(self) -> int:
        terminal_columns = shutil.get_terminal_size(fallback=(100, 24)).columns
        console_columns = getattr(self.console, "width", terminal_columns)
        try:
            columns = min(terminal_columns, int(console_columns))
        except (TypeError, ValueError):
            columns = terminal_columns
        return max(50, min(columns, 120))

    def _finish_composer(self) -> None:
        """Close the inline composer after prompt_toolkit restores the terminal."""
        if self._used_fullscreen:
            self._used_fullscreen = False
            return
        printer = getattr(self.console, "print", None)
        if callable(printer):
            width = self._composer_width()
            printer(f"[dim cyan]╰{'─' * (width - 2)}╯[/dim cyan]")

    async def close_async(self) -> None:
        """Restore the terminal after the persistent full-screen application."""
        if self.fullscreen is not None:
            await self.fullscreen.close_async()


def scan_workspace_files(workspace: Path, limit: int = _MAX_COMPLETION_FILES) -> tuple[str, ...]:
    """Return bounded, workspace-relative files without following directory links."""
    root = workspace.resolve()
    result: list[str] = []
    try:
        for directory, directories, files in os.walk(root, followlinks=False):
            directories[:] = sorted(
                name for name in directories
                if name not in _IGNORED_DIRECTORIES
                and not (Path(directory) / name).is_symlink()
            )
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink():
                    continue
                try:
                    relative = path.resolve().relative_to(root).as_posix()
                except (OSError, ValueError):
                    continue
                result.append(relative)
                if len(result) >= max(1, int(limit)):
                    return tuple(result)
    except OSError:
        return tuple(result)
    return tuple(result)


def _prepare_history_path(workspace: Path) -> Path:
    root = workspace.resolve()
    directory = prepare_user_storage(root).workspace_state / "terminal"
    path = directory / "prompt-history"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    harden_private_path(directory)
    descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(descriptor)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    harden_private_path(path)
    return path


def _known_model_ids() -> tuple[str, ...]:
    """Return offline model choices without triggering discovery or registry sync."""
    from nz_coder.providers.models import (
        active_model_selection,
        cached_models,
        configured_catalog_models,
    )
    from nz_coder.providers.registry import registry_models

    active = active_model_selection()
    values = {f"{active.provider}/{active.model_id}"}
    try:
        values.update(f"{item.provider}/{item.model_id}" for item in cached_models())
        values.update(f"{item.provider}/{item.model_id}" for item in configured_catalog_models())
        values.update(f"{item.provider}/{item.model_id}" for item in registry_models())
    except (OSError, RuntimeError, ValueError):
        # Completion must remain available when one optional local catalog is bad.
        pass
    return tuple(sorted(values, key=str.lower))


def _build_key_bindings(
    *,
    empty_ctrl_c_requests_exit: Callable[[], bool] | None = None,
    clear_exit_request: Callable[[], None] | None = None,
    paste_image: Callable[[], bool] | None = None,
) -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("/")
    def _open_command_menu(event) -> None:  # noqa: ANN001
        """Open slash completion deterministically at the start of a request."""
        buffer = event.current_buffer
        before = buffer.document.text_before_cursor
        buffer.insert_text("/")
        if not before.strip():
            buffer.start_completion(select_first=True)

    @bindings.add("enter")
    def _submit(event) -> None:  # noqa: ANN001
        buffer = event.current_buffer
        completion_state = buffer.complete_state
        if completion_state and completion_state.completions:
            completion = (
                completion_state.current_completion
                or completion_state.completions[0]
            )
            slash_command = buffer.document.text.lstrip().startswith("/")
            buffer.apply_completion(completion)
            if slash_command:
                buffer.validate_and_handle()
            return
        # An empty Enter should not leave another prompt in scrollback. This
        # also avoids accidental no-op turns while retaining whitespace inside
        # a real multiline request.
        if _has_submission(buffer.text):
            buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _newline(event) -> None:  # noqa: ANN001
        event.current_buffer.insert_text("\n")

    @bindings.add("c-p", eager=True)
    def _command_palette(event) -> None:  # noqa: ANN001
        event.app.exit(
            result=TerminalInputAction("command_palette", event.current_buffer.text)
        )

    @bindings.add("c-v")
    def _paste_clipboard(event) -> None:  # noqa: ANN001
        data = event.app.clipboard.get_data().text
        if not data:
            data = _system_clipboard_text()
        if data:
            event.current_buffer.insert_text(data[:_MAX_CLIPBOARD_CHARS])
            return
        if paste_image is not None and paste_image():
            event.app.invalidate()
            return
        bell = getattr(event.app.output, "bell", None)
        if callable(bell):
            bell()

    @bindings.add("c-c", eager=True)
    def _clear_or_request_exit(event) -> None:  # noqa: ANN001
        """Clear populated input; let two empty presses request app exit."""
        buffer = event.current_buffer
        if buffer.text:
            buffer.reset()
            if clear_exit_request is not None:
                clear_exit_request()
            return
        if empty_ctrl_c_requests_exit is not None:
            if empty_ctrl_c_requests_exit():
                event.app.exit(result=TerminalInputAction("exit_confirmed"))
            else:
                event.app.invalidate()
            return
        event.app.exit(result=TerminalInputAction("exit_press"))

    @bindings.add("f2", eager=True)
    def _cycle_model(event) -> None:  # noqa: ANN001
        event.app.exit(result=TerminalInputAction("model_cycle", "/model-cycle next"))

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
        def _leader(event, selected=command) -> None:  # noqa: ANN001
            event.app.exit(result=TerminalInputAction("leader", selected))

        bindings.add("c-x", key, eager=True)(_leader)

    @bindings.add("c-x", "e", eager=True)
    def _external_editor(event) -> None:  # noqa: ANN001
        event.current_buffer.open_in_editor()

    return bindings


def _has_submission(text: str) -> bool:
    """Return whether Enter should submit instead of keeping the editor open."""
    return bool(text.strip())


def _system_clipboard_text(
    *,
    platform: str | None = None,
    os_name: str | None = None,
    which=None,  # noqa: ANN001
    runner=None,  # noqa: ANN001
) -> str:
    """Read optional host text clipboard helpers without invoking a shell."""
    selected_os = os.name if os_name is None else os_name
    selected_platform = sys.platform if platform is None else platform
    executable_lookup = which or shutil.which
    process_runner = runner or subprocess.run
    if selected_os == "nt":
        commands = ((
            "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
            "-Command",
            "Add-Type -AssemblyName System.Windows.Forms;"
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new();"
            "[Console]::Write([Windows.Forms.Clipboard]::GetText())",
        ),)
    elif selected_platform == "darwin":
        commands = (("pbpaste",),)
    else:
        commands = (
            ("wl-paste", "--no-newline"),
            ("xclip", "-selection", "clipboard", "-out"),
            ("xsel", "--clipboard", "--output"),
        )
    for command in commands:
        if executable_lookup(command[0]) is None:
            continue
        try:
            result = process_runner(
                command,
                capture_output=True,
                check=False,
                timeout=1.0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0 or not result.stdout:
            continue
        return result.stdout[: _MAX_CLIPBOARD_CHARS * 4].decode(
            "utf-8", errors="replace",
        )[:_MAX_CLIPBOARD_CHARS]
    return ""
