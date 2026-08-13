"""End-to-end handler tests for InfCode-aligned terminal product controls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder import config
from nz_coder.interface.commands import CommandContext, build_default_registry
from nz_coder.interface.commands.handlers import core
from nz_coder.interface.preferences import load_terminal_preferences
from nz_coder.interface.terminal_input import TerminalInput
from nz_coder.providers.models import DiscoveredModel, ModelSelection
from nz_coder.runtime.workdir import scoped_workdir


class Console:
    def __init__(self) -> None:
        self.messages = []
        self.themes = []

    def print(self, message="", *args, **kwargs):  # noqa: ANN001
        self.messages.append(message)

    def push_theme(self, theme):  # noqa: ANN001
        self.themes.append(theme)


class Agent:
    def __init__(self) -> None:
        self.permissions = SimpleNamespace(mode="default")
        self.session_id = "session-1"
        self.closed = False

    def close(self):
        self.closed = True


def _context(tmp_path, terminal, console=None):
    output = console or Console()
    agent = Agent()
    return CommandContext(
        history=[],
        session_state={"id": "session-1", "agent": agent},
        system_prompt="system",
        renderer=object(),
        console=output,
        build_agent=lambda *_args, **_kwargs: Agent(),
        terminal_input=terminal,
        registry=build_default_registry(),
    )


def test_terminal_commands_persist_and_attachment_reaches_next_submission(tmp_path):
    console = Console()
    terminal = TerminalInput(
        console=console,
        registry=build_default_registry(),
        workspace=tmp_path,
        interactive=False,
    )
    (tmp_path / "notes.txt").write_text("important\n", encoding="utf-8")
    context = _context(tmp_path, terminal, console)

    with scoped_workdir(tmp_path):
        asyncio.run(core.handle_theme(core._context_with_args(context, "nord")))
        core.handle_tool_details(core._context_with_args(context, "full"))
        core.handle_mouse(core._context_with_args(context, "off"))
        core.handle_attach(core._context_with_args(context, "notes.txt"))
        submission, attachments = terminal.prepare_submission("review")

    preferences = load_terminal_preferences(tmp_path)
    assert preferences.theme == "nord"
    assert preferences.tool_details == "full"
    assert preferences.mouse is False
    assert attachments[0].path == "notes.txt"
    assert "notes.txt" in submission
    assert console.themes


def test_connect_flow_masks_and_saves_credential_before_discovery(
    tmp_path, monkeypatch,
):
    console = Console()

    class ConnectTerminal:
        interactive = True

        def __init__(self) -> None:
            self.selections = iter(("anthropic", "anthropic/claude-test"))
            self.prompts = []

        async def select_async(self, **_kwargs):
            return next(self.selections)

        async def prompt_text_async(self, message, *, password=False, default=""):
            self.prompts.append((message, password))
            return "secret-value" if password else "https://api.anthropic.com"

    terminal = ConnectTerminal()
    context = _context(tmp_path, terminal, console)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "before")
    monkeypatch.setattr(config, "ANTHROPIC_API_BASE_URL", "https://before.example")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "before")
    monkeypatch.setenv("ANTHROPIC_API_BASE_URL", "https://before.example")
    monkeypatch.setattr(
        core,
        "discover_models",
        lambda *_args, **_kwargs: [DiscoveredModel("anthropic", "claude-test")],
    )
    monkeypatch.setattr(
        core,
        "active_model_selection",
        lambda: ModelSelection("anthropic", "claude-test", source="workspace"),
    )
    monkeypatch.setattr(core, "save_model_selection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "save_session", lambda *_args, **_kwargs: None)

    with scoped_workdir(tmp_path):
        asyncio.run(core.handle_connect(context))

    content = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "ANTHROPIC_API_KEY=secret-value" in content
    assert terminal.prompts[0][1] is True
    assert all("secret-value" not in str(message) for message in console.messages)
    assert context.agent is not None


def test_command_registry_exposes_categories_keybinds_and_new_controls():
    commands = {command.name: command for command in build_default_registry().visible_commands()}

    assert commands["model"].category == "Model"
    assert commands["model"].keybind == "Ctrl+X M"
    assert commands["theme"].category == "Terminal"
    assert commands["attach"].category == "Input"
    assert commands["connect"].suggested is True


def test_model_cycle_delegates_to_normal_transactional_model_switch(monkeypatch, tmp_path):
    terminal = TerminalInput(
        console=Console(), registry=build_default_registry(), workspace=tmp_path,
        interactive=False,
    )
    context = _context(tmp_path, terminal)
    monkeypatch.setattr(
        core, "active_model_selection",
        lambda: ModelSelection("openai", "current", source="workspace"),
    )
    monkeypatch.setattr(core, "cycle_model_id", lambda **_kwargs: "anthropic/next")
    switched = []
    monkeypatch.setattr(core, "handle_model", lambda ctx: switched.append(ctx.args))

    core.handle_model_cycle(core._context_with_args(context, "next"))

    assert switched == ["anthropic/next"]
