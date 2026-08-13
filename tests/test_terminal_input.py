"""Tests for the prompt-toolkit terminal input surface."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from nz_coder.interface.commands import build_default_registry
from nz_coder.interface.terminal_input import (
    TerminalInputAction,
    TerminalCompleter,
    TerminalInput,
    _has_submission,
    _prepare_history_path,
    scan_workspace_files,
)


def _complete(completer: TerminalCompleter, text: str):
    return list(completer.get_completions(Document(text, len(text)), CompleteEvent()))


def test_slash_completion_uses_registered_command_metadata(tmp_path):
    completer = TerminalCompleter(
        build_default_registry(),
        tmp_path,
        file_provider=lambda: (),
        session_provider=lambda: (),
    )

    matches = _complete(completer, "/res")

    assert [item.text for item in matches] == ["/resume"]
    assert matches[0].start_position == -4
    assert "Resume a saved session" in str(matches[0].display_meta)

    model_aliases = _complete(completer, "/models")
    assert [item.text for item in model_aliases] == ["/models"]
    assert "alias for /model" in str(model_aliases[0].display_meta)


def test_command_argument_completion_covers_sessions_and_modes(tmp_path):
    completer = TerminalCompleter(
        build_default_registry(),
        tmp_path,
        file_provider=lambda: (),
        session_provider=lambda: ("session-alpha", "session-beta"),
        model_provider=lambda: ("openai/gpt-test", "anthropic/claude-test"),
    )

    assert [item.text for item in _complete(completer, "/resume session-a")] == [
        "session-alpha"
    ]
    assert [item.text for item in _complete(completer, "/mode ac")] == ["acceptEdits"]
    assert [item.text for item in _complete(completer, "/permission ru")] == ["rules"]
    assert [item.text for item in _complete(completer, "/model openai/g")] == [
        "openai/gpt-test"
    ]


def test_file_reference_completion_is_bounded_to_workspace_snapshot(tmp_path):
    completer = TerminalCompleter(
        build_default_registry(),
        tmp_path,
        file_provider=lambda: ("README.md", "src/app.py", "src/api.py"),
        session_provider=lambda: (),
    )

    matches = _complete(completer, "please inspect @src/a")

    assert [item.text for item in matches] == ["src/api.py", "src/app.py"]
    assert all(item.start_position == -5 for item in matches)


def test_file_completion_snapshot_can_be_invalidated(tmp_path):
    snapshots = iter((("old.py",), ("new.py",)))
    completer = TerminalCompleter(
        build_default_registry(),
        tmp_path,
        file_provider=lambda: next(snapshots),
        session_provider=lambda: (),
    )

    assert [item.text for item in _complete(completer, "@o")] == ["old.py"]
    completer.invalidate_files()
    assert [item.text for item in _complete(completer, "@n")] == ["new.py"]


def test_workspace_scan_ignores_runtime_bulk_and_symlink_files(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("", encoding="utf-8")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "outside-link").symlink_to(outside)

    try:
        assert scan_workspace_files(tmp_path) == ("src/app.py",)
    finally:
        outside.unlink(missing_ok=True)


def test_history_file_is_workspace_owned_and_private(tmp_path, monkeypatch):
    import nz_coder.interface.terminal_input as terminal_input

    hardened = []
    monkeypatch.setattr(
        terminal_input,
        "harden_private_path",
        lambda path: hardened.append(Path(path)),
    )
    history = _prepare_history_path(tmp_path)

    assert history == tmp_path / ".nz-coder" / "prompt-history"
    assert history.exists()
    assert os.stat(history).st_mode & 0o777 == 0o600
    assert history.parent in hardened
    assert history in hardened


def test_terminal_input_uses_injected_session_and_inline_composer(tmp_path):
    class FakeSession:
        def prompt(self):
            return "multi\nline"

    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        state_provider=lambda: {
            "provider": "openai",
            "model": "gpt-test",
            "mode": "plan",
            "session": "session-123",
            "context": "2k/128k",
        },
        interactive=True,
        prompt_session=FakeSession(),
    )

    assert terminal.read() == "multi\nline"
    prompt = "".join(text for _style, text in terminal._composer_prompt())
    assert "New request" in prompt
    assert "openai/gpt-test" in prompt
    assert "plan" in prompt
    assert "2k/128k" in prompt
    assert "│ ❯ " in prompt


def test_composer_width_is_terminal_column_safe_for_cjk_and_emoji(tmp_path):
    from nz_coder.interface.presentation_tokens import terminal_text_width

    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        state_provider=lambda: {
            "provider": "模型提供商🚀",
            "model": "编程模型👨\u200d💻",
            "mode": "default",
            "context": "2k/128k",
        },
        interactive=True,
        prompt_session=object(),
    )
    terminal._composer_width = lambda: 42

    prompt = "".join(text for _style, text in terminal._composer_prompt())
    header = prompt.splitlines()[0]

    assert terminal_text_width(header) == 42
    assert not header.endswith("\u200d")


def test_empty_enter_is_not_a_submission():
    assert _has_submission("") is False
    assert _has_submission("  \n") is False
    assert _has_submission("explain this code") is True


def test_slash_key_binding_opens_command_completion():
    bindings = __import__(
        "nz_coder.interface.terminal_input",
        fromlist=["_build_key_bindings"],
    )._build_key_bindings()
    handlers = [
        binding.handler
        for binding in bindings.bindings
        if binding.keys == ("/",)
    ]

    class Buffer:
        document = Document("", 0)
        inserted = ""
        completion_started = False

        def insert_text(self, text):
            self.inserted += text

        def start_completion(self, *, select_first):
            assert select_first is True
            self.completion_started = True

    class Event:
        current_buffer = Buffer()

    assert len(handlers) == 1
    handlers[0](Event())
    assert Event.current_buffer.inserted == "/"
    assert Event.current_buffer.completion_started is True


def test_terminal_input_async_read_uses_prompt_async(tmp_path):
    class FakeSession:
        async def prompt_async(self):
            return "async input"

    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=True,
        prompt_session=FakeSession(),
    )

    assert asyncio.run(terminal.read_async()) == "async input"


def test_terminal_input_async_selector_awaits_fuzzy_selector(monkeypatch, tmp_path):
    captured = {}

    class FakeSelector:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def run_async(self):
            return "session-two"

    monkeypatch.setattr(
        "nz_coder.interface.terminal_input.FuzzySelector",
        FakeSelector,
    )
    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=True,
        prompt_session=object(),
    )

    selected = asyncio.run(terminal.select_async(
        title="Resume session",
        values=[("session-one", "One"), ("session-two", "Two")],
    ))

    assert selected == "session-two"
    assert captured["title"] == "Resume session"
    assert captured["values"][1] == ("session-two", "Two")


def test_terminal_input_selector_is_disabled_without_tty(tmp_path):
    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=False,
    )

    assert asyncio.run(terminal.select_async(
        title="Choose",
        values=[("one", "One")],
    )) is None


def test_terminal_input_falls_back_without_a_tty(tmp_path):
    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        fallback_reader=lambda: "piped input",
        interactive=False,
    )

    assert terminal.read() == "piped input"
    assert not (tmp_path / ".nz-coder" / "prompt-history").exists()


def test_ctrl_p_binding_exits_to_command_palette_with_current_text():
    bindings = __import__(
        "nz_coder.interface.terminal_input", fromlist=["_build_key_bindings"],
    )._build_key_bindings()
    handler = next(
        binding.handler for binding in bindings.bindings if binding.keys == ("c-p",)
    )
    captured = {}

    class Event:
        current_buffer = type("Buffer", (), {"text": "draft"})()
        app = type("App", (), {"exit": lambda _self, **kwargs: captured.update(kwargs)})()

    handler(Event())
    assert captured["result"] == TerminalInputAction("command_palette", "draft")


def test_ctrl_c_binding_clears_populated_input_before_exit():
    bindings = __import__(
        "nz_coder.interface.terminal_input", fromlist=["_build_key_bindings"],
    )._build_key_bindings()
    handler = next(
        binding.handler for binding in bindings.bindings if binding.keys == ("c-c",)
    )

    class Buffer:
        text = "draft request"
        reset_called = False

        def reset(self):
            self.text = ""
            self.reset_called = True

    class App:
        result = None

        def exit(self, *, result):
            self.result = result

    class Event:
        current_buffer = Buffer()
        app = App()

    handler(Event())
    assert Event.current_buffer.reset_called is True
    assert Event.app.result is None


def test_ctrl_c_binding_emits_exit_press_for_empty_input():
    bindings = __import__(
        "nz_coder.interface.terminal_input", fromlist=["_build_key_bindings"],
    )._build_key_bindings()
    handler = next(
        binding.handler for binding in bindings.bindings if binding.keys == ("c-c",)
    )

    class Buffer:
        text = ""

    class App:
        result = None

        def exit(self, *, result):
            self.result = result

    class Event:
        current_buffer = Buffer()
        app = App()

    handler(Event())
    assert Event.app.result == TerminalInputAction("exit_press")


def test_owned_ctrl_c_binding_keeps_first_press_in_active_composer():
    decisions = iter((False, True))
    bindings = __import__(
        "nz_coder.interface.terminal_input", fromlist=["_build_key_bindings"],
    )._build_key_bindings(empty_ctrl_c_requests_exit=lambda: next(decisions))
    handler = next(
        binding.handler for binding in bindings.bindings if binding.keys == ("c-c",)
    )

    class Buffer:
        text = ""

    class App:
        result = None
        invalidated = 0

        def exit(self, *, result):
            self.result = result

        def invalidate(self):
            self.invalidated += 1

    class Event:
        current_buffer = Buffer()
        app = App()

    handler(Event())
    assert Event.app.result is None
    assert Event.app.invalidated == 1

    handler(Event())
    assert Event.app.result == TerminalInputAction("exit_confirmed")


def test_two_empty_ctrl_c_presses_exit_within_one_second(monkeypatch, tmp_path):
    class FakeConsole:
        def __init__(self):
            self.messages = []

        def print(self, message):
            self.messages.append(message)

    class FakeSession:
        async def prompt_async(self, **_kwargs):
            return TerminalInputAction("exit_press")

    fake_console = FakeConsole()
    terminal = TerminalInput(
        console=fake_console,
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=True,
        prompt_session=FakeSession(),
    )
    decisions = iter((False, True))
    monkeypatch.setattr(
        terminal, "_empty_ctrl_c_requests_exit", lambda: next(decisions),
    )

    with pytest.raises(EOFError):
        asyncio.run(terminal.read_async())

    assert any("again to exit" in str(message) for message in fake_console.messages)


def test_empty_ctrl_c_exit_window_expires(monkeypatch, tmp_path):
    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=True,
        prompt_session=object(),
    )
    clock = iter((10.0, 11.5, 12.0))
    monkeypatch.setattr(
        "nz_coder.interface.terminal_input.time.monotonic", lambda: next(clock),
    )

    assert terminal._empty_ctrl_c_requests_exit() is False
    assert terminal._empty_ctrl_c_requests_exit() is False
    assert terminal._empty_ctrl_c_requests_exit() is True


def test_ctrl_v_binding_inserts_application_clipboard_text(monkeypatch):
    bindings = __import__(
        "nz_coder.interface.terminal_input", fromlist=["_build_key_bindings"],
    )._build_key_bindings()
    handler = next(
        binding.handler for binding in bindings.bindings if binding.keys == ("c-v",)
    )
    inserted = []

    class Event:
        current_buffer = type("Buffer", (), {
            "insert_text": lambda _self, value: inserted.append(value),
        })()
        app = type("App", (), {
            "clipboard": type("Clipboard", (), {
                "get_data": lambda _self: type("Data", (), {"text": "clip\ntext"})(),
            })(),
            "output": object(),
        })()

    monkeypatch.setattr(
        "nz_coder.interface.terminal_input._system_clipboard_text", lambda: "fallback",
    )
    handler(Event())
    assert inserted == ["clip\ntext"]


def test_command_palette_returns_selected_registered_command(monkeypatch, tmp_path):
    class FakeSession:
        async def prompt_async(self, **_kwargs):
            return TerminalInputAction("command_palette", "draft")

    terminal = TerminalInput(
        console=object(), registry=build_default_registry(), workspace=tmp_path,
        interactive=True, prompt_session=FakeSession(),
    )
    monkeypatch.setattr(terminal, "_command_palette", lambda: _async_value("status"))

    assert asyncio.run(terminal.read_async()) == "/status"


def test_attachment_is_workspace_bounded_and_consumed_once(tmp_path):
    source = tmp_path / "src.py"
    source.write_text("print('ok')\n", encoding="utf-8")
    terminal = TerminalInput(
        console=object(), registry=build_default_registry(), workspace=tmp_path,
        interactive=False,
    )

    attachment = terminal.queue_attachment("@src.py")
    content, consumed = terminal.prepare_submission("review it")

    assert attachment.path == "src.py"
    assert consumed == (attachment,)
    assert "<attached-files>" in content
    assert "src.py" in content
    assert terminal.attachments() == ()
    with pytest.raises(ValueError, match="inside the workspace"):
        terminal.queue_attachment("../outside.py")


def test_remote_terminal_can_disable_client_path_attachments(tmp_path):
    source = tmp_path / "local-only.txt"
    source.write_text("client data", encoding="utf-8")
    terminal = TerminalInput(
        console=object(),
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=False,
        attachments_enabled=False,
    )

    with pytest.raises(ValueError, match="remote URL"):
        terminal.queue_attachment("local-only.txt")
    assert _complete(terminal.completer, "@local") == []
    submission, attachments = terminal.prepare_submission("review @local-only.txt")
    assert submission == "review @local-only.txt"
    assert attachments == ()


def test_inline_file_reference_becomes_one_shot_attachment(tmp_path):
    source = tmp_path / "src.py"
    source.write_text("print('inline')\n", encoding="utf-8")
    terminal = TerminalInput(
        console=object(), registry=build_default_registry(), workspace=tmp_path,
        interactive=False,
    )

    content, attachments = terminal.prepare_submission("review @src.py please")

    assert [item.path for item in attachments] == ["src.py"]
    assert "src.py" in content
    assert terminal.attachments() == ()


async def _async_value(value):
    return value


def test_windows_clipboard_text_uses_powershell_and_decodes_unicode():
    import subprocess
    from nz_coder.interface.terminal_input import _system_clipboard_text

    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "中文 🚀".encode("utf-8"), b"")

    result = _system_clipboard_text(
        os_name="nt",
        platform="win32",
        which=lambda name: name if name == "powershell.exe" else None,
        runner=runner,
    )

    assert result == "中文 🚀"
    assert calls[0][0][0] == "powershell.exe"
