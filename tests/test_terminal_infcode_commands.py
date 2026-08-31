"""Source-aligned InfCode terminal command parity tests."""
from __future__ import annotations

import asyncio
from io import StringIO
from types import SimpleNamespace

from nz_coder.interface.clipboard import copy_text
from nz_coder.interface.commands import build_default_registry
from nz_coder.interface.commands.handlers import core
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.interface.commands.registry import product_command_category
from nz_coder.interface.selector import SelectorActionResult
from nz_coder.interface.timeline import format_transcript
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import load_session, rename_session, save_session


class _Console:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def print(self, message="", *_args, **_kwargs) -> None:  # noqa: ANN001
        self.messages.append(message)


def _context(tmp_path, *, history=None, terminal=None):
    permissions = SimpleNamespace(mode="default")
    agent = SimpleNamespace(
        session_id="session-1",
        permissions=permissions,
        model_capabilities=SimpleNamespace(available_variants=("low", "high")),
    )
    state = {"id": "session-1", "agent": agent}
    return CommandContext(
        history=list(history or []),
        session_state=state,
        system_prompt="system",
        renderer=object(),
        console=_Console(),
        build_agent=lambda *_args, **_kwargs: agent,
        terminal_input=terminal,
    )


def test_infcode_terminal_command_names_are_registered():
    registry = build_default_registry()
    names = {command.name for command in registry.visible_commands()}

    assert {
        "rename", "delete-session", "copy", "export", "skills", "mcps",
        "variants", "editor", "exit",
    } <= names


def test_palette_prioritizes_suggested_recent_and_common_commands():
    registry = build_default_registry()

    commands = registry.palette_commands(recent=("diff", "status"))
    names = [command.name for command in commands]
    suggested_count = sum(command.suggested for command in commands)

    assert all(command.suggested for command in commands[:suggested_count])
    assert names.index("diff") < names.index("attach")
    assert product_command_category(registry.get("help")) == "Essentials"
    assert product_command_category(registry.get("attach")) == "Files"
    assert product_command_category(registry.get("processes")) == "Processes"
    assert product_command_category(registry.get("memory")) == "Memory"
    assert product_command_category(registry.get("skills")) == "Extensions"
    assert product_command_category(registry.get("theme")) == "Settings"


def test_help_defaults_to_essentials_and_all_is_explicit(tmp_path):
    registry = build_default_registry()
    ctx = _context(tmp_path)
    ctx.registry = registry

    core.handle_help(ctx)
    default_body = str(ctx.console.messages[-1].renderable)
    assert "/status" in default_body
    assert "/compact" not in default_body
    assert "/help all" in default_body

    ctx.args = "all"
    core.handle_help(ctx)
    all_body = str(ctx.console.messages[-1].renderable)
    assert "/compact" in all_body
    assert "Files" in all_body and "Extensions" in all_body


def test_session_picker_requires_double_delete_action(tmp_path):
    class _Terminal:
        interactive = True

        def __init__(self):
            self.values = iter((
                SelectorActionResult("delete", "session-2"),
                SelectorActionResult("delete", "session-2"),
                None,
            ))

        async def select_async(self, **_kwargs):
            return next(self.values)

    terminal = _Terminal()
    ctx = _context(tmp_path, terminal=terminal)
    with scoped_workdir(tmp_path):
        save_session([], session_id="session-1")
        save_session([], session_id="session-2", activate=False)

        asyncio.run(core.handle_session_picker(ctx))

        assert load_session("session-2") == {}
        assert load_session("session-1")["session_id"] == "session-1"


def test_transcript_formats_roles_tools_and_safe_fences():
    history = [
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": "done",
            "reasoning_content": "considered",
            "tool_calls": [{
                "function": {
                    "name": "bash",
                    "arguments": '{"command":"printf ```"}',
                },
            }],
        },
        {"role": "tool", "content": "output ``` value"},
        {"role": "user", "content": "hidden", "_nz_synthetic": True},
    ]

    transcript = format_transcript("session-1", history, title="Review", tool_details=True)

    assert transcript.startswith("# Review")
    assert "## User\n\ninspect" in transcript
    assert "## Assistant" in transcript
    assert "_Thinking:_" in transcript
    assert "**Tool: bash**" in transcript
    assert "output ``` value" in transcript
    assert "hidden" not in transcript
    assert "````\noutput ``` value\n````" in transcript


def test_session_rename_persists_and_survives_save(tmp_path):
    with scoped_workdir(tmp_path):
        save_session([], session_id="session-1")
        assert rename_session("session-1", "Parser review") == "Parser review"
        save_session([{"role": "user", "content": "next"}], session_id="session-1")

        payload = load_session("session-1")

    assert payload["title"] == "Parser review"
    assert payload["messages"][-1]["content"] == "next"


def test_export_is_workspace_bounded_and_atomic(tmp_path):
    ctx = _context(
        tmp_path,
        history=[{"role": "user", "content": "hello"}],
    )
    with scoped_workdir(tmp_path):
        ctx.args = "exports/review.md"
        core.handle_export(ctx)
        exported = tmp_path / "exports" / "review.md"
        assert exported.exists()
        assert "## User\n\nhello" in exported.read_text(encoding="utf-8")

        ctx.args = "../outside.md"
        core.handle_export(ctx)

    assert "must stay inside the workspace" in str(ctx.console.messages[-1])
    assert not (tmp_path.parent / "outside.md").exists()


def test_copy_uses_osc52_when_terminal_is_available(monkeypatch):
    class _TTY(StringIO):
        def isatty(self) -> bool:
            return True

    stream = _TTY()
    monkeypatch.setattr("nz_coder.interface.clipboard.sys.stdout", stream)
    monkeypatch.setattr("nz_coder.interface.clipboard._native_copy_commands", lambda: ())

    assert copy_text("hello") is True
    assert "\x1b]52;c;aGVsbG8=\x07" in stream.getvalue()


def test_variants_editor_and_exit_commands_use_existing_session_owner(monkeypatch, tmp_path):
    selected: list[str] = []
    terminal = SimpleNamespace(interactive=True)
    ctx = _context(tmp_path, terminal=terminal)
    monkeypatch.setattr(
        core,
        "active_model_selection",
        lambda: SimpleNamespace(provider="openai", model_id="gpt-test", variant=None),
    )
    monkeypatch.setattr(core, "handle_model", lambda command_ctx: selected.append(command_ctx.args))

    ctx.args = "high"
    asyncio.run(core.handle_variants(ctx))
    core.handle_editor(ctx)
    core.handle_exit(ctx)

    assert selected == ["openai/gpt-test high"]
    assert ctx.session_state["open_editor"] is True
    assert ctx.session_state["exit_requested"] is True
