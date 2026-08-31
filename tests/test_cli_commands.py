"""Tests for CLI slash-command registry and handlers."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nz_coder.interface import cli
from nz_coder.providers.models import ModelSelection
from nz_coder.tool_platform.permissions import PermissionRule


class FakeConsole:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def print(self, message="", *args, **kwargs) -> None:  # noqa: ANN001
        self.messages.append(message)


def test_narrow_process_view_keeps_complete_process_id():
    from io import StringIO
    from rich.console import Console
    from nz_coder.interface.commands.handlers.core import _render_processes

    output = StringIO()
    console = Console(file=output, width=80, force_terminal=False)
    console.print(_render_processes([{
        "process_id": "proc_0ebfa2a812ca",
        "status": "running",
        "command": "python -m http.server 8000 --directory a-very-long-directory",
        "cwd": "/workspace/project",
        "started_at": None,
        "exit_code": None,
        "owner_session_id": "session-owner",
        "pty_tier": "pipe",
    }], width=80))

    rendered = output.getvalue()
    assert "proc_0ebfa2a812ca" in rendered
    assert "RUNNING" in rendered
    assert "python -m http.server" in rendered


def test_narrow_session_view_keeps_complete_session_id():
    from io import StringIO
    from rich.console import Console
    from nz_coder.interface.commands.handlers.core import _render_session_options
    from nz_coder.interface.timeline import SessionOption

    output = StringIO()
    console = Console(file=output, width=80, force_terminal=False)
    console.print(_render_session_options([SessionOption(
        session_id="session-20260814_144950-ad401b3f",
        title="Parser implementation and regression tests",
        active=True,
        timestamp="2026-08-14 14:49",
        message_count=19,
        model="openai-compatible/deepseek-v4-flash",
        mode="acceptEdits",
    )]))

    rendered = output.getvalue()
    assert "session-20260814_144950-ad401b3f" in rendered
    assert "Parser implementation" in rendered
    assert "19 messages" in rendered


def test_top_level_help_contains_product_onboarding(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)

    assert cli.main(["--help"]) == 0

    rendered = "\n".join(str(item) for item in fake_console.messages)
    assert "Interactive:" in rendered
    assert "Headless:" in rendered
    assert "Configuration:" in rendered
    assert "nz-coder models" in rendered


class FakePermissions:
    def __init__(self, mode: str = "default") -> None:
        self.mode = mode
        self._allow_rules = [PermissionRule("bash", "allow", "prefix:git ")]
        self._deny_rules = [PermissionRule("bash", "deny", "prefix:rm ")]
        self._ask_rules = [PermissionRule("write_file", "ask")]


class FakeAgent:
    def __init__(self, session_id: str = "session-1", mode: str = "default") -> None:
        self.session_id = session_id
        self.permissions = FakePermissions(mode=mode)
        self.change_tracker = None
        self.cleared = False
        self.closed = False
        self.compact_focus = None

    def clear_scratchpad(self) -> None:
        self.cleared = True

    def close(self) -> None:
        self.closed = True

    def _compact_messages(self, messages, focus=None):
        self.compact_focus = focus
        return [{"role": "user", "content": "compacted"}]


class FakeTerminalInput:
    interactive = True

    def __init__(self, selected) -> None:
        self.selected = selected
        self.requests = []

    async def select_async(self, **kwargs):
        self.requests.append(kwargs)
        return self.selected


def test_copy_last_uses_only_latest_assistant_text(monkeypatch):
    fake_console = FakeConsole()
    copied = []
    monkeypatch.setattr(cli, "console", fake_console)
    monkeypatch.setattr(
        "nz_coder.interface.clipboard.copy_text",
        lambda text: copied.append(text) or True,
    )
    history = [
        {"role": "assistant", "content": "first"},
        {"role": "user", "content": "next"},
        {"role": "assistant", "content": "last"},
    ]

    assert cli.handle_command(
        "/copy-last",
        history,
        {"id": "session-1", "agent": FakeAgent()},
        "sys",
        object(),
    ) is True
    assert copied == ["last"]
    assert "copied" in str(fake_console.messages[-1]).lower()


def test_run_cli_renders_failure_and_closes_current_agent(monkeypatch):
    agent = FakeAgent()

    async def fail_run(*_args, **_kwargs):
        raise RuntimeError("loop failed")

    agent.run = fail_run

    class Renderer:
        on_token = None

        def start(self):
            return None

        def finish(self):
            return None

        def pause(self):
            return None

        def resume(self):
            return None

    monkeypatch.setattr(cli.config, "API_KEY", "test")
    monkeypatch.setattr(cli.memory_mgr, "load_all", lambda: None)
    monkeypatch.setattr(cli.memory_mgr, "build_prompt_block", lambda **_kwargs: "")
    monkeypatch.setattr(cli.skill_loader, "descriptions", lambda: "")
    monkeypatch.setattr(cli, "build", lambda **_kwargs: "system")
    monkeypatch.setattr(cli, "StreamingRenderer", Renderer)
    monkeypatch.setattr(cli, "create_session_id", lambda: "session-test")
    monkeypatch.setattr(cli, "activate_session", lambda value: value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "print_banner", lambda *_args: None)
    inputs = iter(("hello", "exit"))
    monkeypatch.setattr(cli, "read_user_query", lambda: next(inputs))

    asyncio.run(cli._run_cli())

    assert agent.closed is True


def test_run_cli_cancelled_turn_returns_to_same_repl(monkeypatch):
    agent = FakeAgent()
    calls = []

    async def run(messages, **_kwargs):
        calls.append([dict(item) for item in messages])
        if len(calls) == 1:
            raise asyncio.CancelledError
        return {"status": "completed"}

    agent.run = run

    class Renderer:
        on_token = None

        def start(self):
            return None

        def finish(self):
            return None

        def pause(self):
            return None

        def resume(self):
            return None

    monkeypatch.setattr(cli.config, "API_KEY", "test")
    monkeypatch.setattr(cli.memory_mgr, "load_all", lambda: None)
    monkeypatch.setattr(cli.memory_mgr, "build_prompt_block", lambda **_kwargs: "")
    monkeypatch.setattr(cli.skill_loader, "descriptions", lambda: "")
    monkeypatch.setattr(cli, "build", lambda **_kwargs: "system")
    monkeypatch.setattr(cli, "StreamingRenderer", Renderer)
    monkeypatch.setattr(cli, "create_session_id", lambda: "session-test")
    monkeypatch.setattr(cli, "activate_session", lambda value: value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "print_banner", lambda *_args: None)
    monkeypatch.setattr(cli, "save_session", lambda *_args, **_kwargs: None)
    inputs = iter(("first", "second", "exit"))
    monkeypatch.setattr(cli, "read_user_query", lambda: next(inputs))

    asyncio.run(cli._run_cli())

    assert len(calls) == 2
    assert calls[-1][-1]["role"] == "user"
    assert calls[-1][-1]["content"] == "second"
    assert calls[-1][-1]["_nz_user_agent"] == "build"
    assert calls[-1][-1]["_nz_time"]["created"] >= 0
    assert agent.closed is True


def test_typed_cancelled_turn_does_not_append_memory_reminder(monkeypatch):
    agent = FakeAgent()
    agent.tool_calls_this_run = 4
    agent.used_save_memory = False

    async def run(_messages, **_kwargs):
        return {"status": "cancelled"}

    agent.run = run

    class Renderer:
        on_token = None

        def start(self):
            return None

        def finish(self):
            return None

        def pause(self):
            return None

        def resume(self):
            return None

    saved = []
    monkeypatch.setattr(cli.config, "API_KEY", "test")
    monkeypatch.setattr(cli.memory_mgr, "load_all", lambda: None)
    monkeypatch.setattr(cli.memory_mgr, "build_prompt_block", lambda **_kwargs: "")
    monkeypatch.setattr(cli.skill_loader, "descriptions", lambda: "")
    monkeypatch.setattr(cli, "build", lambda **_kwargs: "system")
    monkeypatch.setattr(cli, "StreamingRenderer", Renderer)
    monkeypatch.setattr(cli, "create_session_id", lambda: "session-test")
    monkeypatch.setattr(cli, "activate_session", lambda value: value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "print_banner", lambda *_args: None)
    monkeypatch.setattr(
        cli,
        "save_session",
        lambda messages, **_kwargs: saved.append([dict(item) for item in messages]),
    )
    inputs = iter(("cancel me", "exit"))
    monkeypatch.setattr(cli, "read_user_query", lambda: next(inputs))

    asyncio.run(cli._run_cli())

    assert saved
    assert all(
        "substantial task" not in str(message.get("content", ""))
        for snapshot in saved
        for message in snapshot
    )


def test_handled_sigint_cancellation_is_fully_consumed():
    async def exercise():
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            cli._consume_current_task_cancellation()
        await asyncio.sleep(0)
        return task.cancelling() if hasattr(task, "cancelling") else 0

    assert asyncio.run(exercise()) == 0


def test_first_ctrl_c_while_editing_prompts_before_exit(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    agent = FakeAgent()

    class Renderer:
        on_token = None

        def start(self):
            return None

        def finish(self):
            return None

        def pause(self):
            return None

        def resume(self):
            return None

    monkeypatch.setattr(cli.config, "API_KEY", "test")
    monkeypatch.setattr(cli.memory_mgr, "load_all", lambda: None)
    monkeypatch.setattr(cli.memory_mgr, "build_prompt_block", lambda **_kwargs: "")
    monkeypatch.setattr(cli.skill_loader, "descriptions", lambda: "")
    monkeypatch.setattr(cli, "build", lambda **_kwargs: "system")
    monkeypatch.setattr(cli, "StreamingRenderer", Renderer)
    monkeypatch.setattr(cli, "create_session_id", lambda: "session-test")
    monkeypatch.setattr(cli, "activate_session", lambda value: value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "print_banner", lambda *_args: None)
    calls = 0

    def read():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise KeyboardInterrupt
        return "exit"

    monkeypatch.setattr(cli, "read_user_query", read)

    asyncio.run(cli._run_cli())

    assert calls == 2
    assert agent.closed is True
    assert any("again to exit" in str(message) for message in fake_console.messages)


def test_second_ctrl_c_while_editing_exits_fallback_repl(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    agent = FakeAgent()

    class Renderer:
        on_token = None

        def start(self):
            return None

        def finish(self):
            return None

        def pause(self):
            return None

        def resume(self):
            return None

    monkeypatch.setattr(cli.config, "API_KEY", "test")
    monkeypatch.setattr(cli.memory_mgr, "load_all", lambda: None)
    monkeypatch.setattr(cli.memory_mgr, "build_prompt_block", lambda **_kwargs: "")
    monkeypatch.setattr(cli.skill_loader, "descriptions", lambda: "")
    monkeypatch.setattr(cli, "build", lambda **_kwargs: "system")
    monkeypatch.setattr(cli, "StreamingRenderer", Renderer)
    monkeypatch.setattr(cli, "create_session_id", lambda: "session-test")
    monkeypatch.setattr(cli, "activate_session", lambda value: value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "print_banner", lambda *_args: None)
    monkeypatch.setattr(cli.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(
        cli,
        "read_user_query",
        lambda: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    asyncio.run(cli._run_cli())

    assert agent.closed is True
    assert any("Goodbye" in str(message) for message in fake_console.messages)


def test_cancelled_async_command_returns_to_same_repl(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    agent = FakeAgent()

    class Renderer:
        on_token = None

        def start(self):
            return None

        def finish(self):
            return None

        def pause(self):
            return None

        def resume(self):
            return None

    monkeypatch.setattr(cli.config, "API_KEY", "test")
    monkeypatch.setattr(cli.memory_mgr, "load_all", lambda: None)
    monkeypatch.setattr(cli.memory_mgr, "build_prompt_block", lambda **_kwargs: "")
    monkeypatch.setattr(cli.skill_loader, "descriptions", lambda: "")
    monkeypatch.setattr(cli, "build", lambda **_kwargs: "system")
    monkeypatch.setattr(cli, "StreamingRenderer", Renderer)
    monkeypatch.setattr(cli, "create_session_id", lambda: "session-test")
    monkeypatch.setattr(cli, "activate_session", lambda value: value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args: agent)
    monkeypatch.setattr(cli, "print_banner", lambda *_args: None)

    async def cancelled_command(*_args, **_kwargs):
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        await asyncio.sleep(0)

    monkeypatch.setattr(cli, "handle_command_async", cancelled_command)
    inputs = iter(("/models", "exit"))
    monkeypatch.setattr(cli, "read_user_query", lambda: next(inputs))

    asyncio.run(cli._run_cli())

    assert agent.closed is True
    assert any("Command cancelled" in str(message) for message in fake_console.messages)


def test_cli_version_is_available_without_configuration(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)

    assert cli.main(["--version"]) == 0
    assert str(fake_console.messages[-1]).startswith("nz-coder ")


def test_handle_command_permission_mode_updates_agent(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history: list[dict] = []
    session_state = {"id": "session-1", "agent": FakeAgent()}

    handled = cli.handle_command("/permission mode acceptEdits", history, session_state, "sys", object())

    assert handled is True
    assert session_state["agent"].permissions.mode == "acceptEdits"
    assert fake_console.messages[-1] == "[success]Permission mode: acceptEdits[/success]"


def test_subagents_picker_opens_read_only_child_transcript(monkeypatch, tmp_path):
    fake_console = FakeConsole()
    terminal = FakeTerminalInput("subagent-1")
    session_state = {"id": "session-1", "agent": FakeAgent()}
    monkeypatch.setattr(cli, "console", fake_console)
    monkeypatch.setattr(
        "nz_coder.runtime.agent.subagent.list_subagent_sessions",
        lambda *_args: [{
            "session_id": "subagent-1",
            "agent_type": "explore",
            "status": "completed",
            "model_id": "provider/model",
            "message_count": 2,
            "updated_at": 1.0,
        }],
    )
    monkeypatch.setattr(
        "nz_coder.runtime.agent.subagent.load_subagent_session",
        lambda *_args: {
            "session_id": "subagent-1",
            "agent_type": "explore",
            "status": "completed",
            "model_id": "provider/model",
            "messages": [
                {"role": "user", "content": "inspect parser"},
                {"role": "assistant", "content": "found it"},
            ],
        },
    )

    handled = asyncio.run(cli.handle_command_async(
        "/subagents", [], session_state, "sys", object(), terminal,
    ))

    assert handled is True
    assert terminal.requests[0]["title"] == "Child Agent sessions"
    assert len(fake_console.messages) == 2
    assert fake_console.messages[0].title == "subagent-1"
    assert "Markdown" in type(fake_console.messages[1]).__name__
    assert session_state["id"] == "session-1"


def test_message_picker_renders_only_selected_turn_with_full_details(monkeypatch):
    fake_console = FakeConsole()
    terminal = FakeTerminalInput(1)
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    monkeypatch.setattr(cli, "console", fake_console)

    handled = asyncio.run(cli.handle_command_async(
        "/message", history, {"id": "session-1", "agent": FakeAgent()},
        "sys", object(), terminal,
    ))

    assert handled is True
    assert terminal.requests[0]["title"] == "Inspect message"
    rendered = fake_console.messages[-1]
    assert "first question" in rendered.markup
    assert "first answer" in rendered.markup
    assert "second question" not in rendered.markup


def test_interactive_child_route_resumes_owned_state_without_replacing_parent(monkeypatch):
    fake_console = FakeConsole()
    terminal = FakeTerminalInput(None)
    parent = FakeAgent()
    session_state = {"id": "session-1", "agent": parent}
    captured = {}
    monkeypatch.setattr(cli, "console", fake_console)
    monkeypatch.setattr(
        "nz_coder.runtime.agent.subagent.load_subagent_session",
        lambda *_args: {
            "session_id": "subagent-1",
            "agent_type": "explore",
            "status": "completed",
            "allowed_tools": ["read_file"],
            "claimed_paths": ["src"],
        },
    )

    async def resume(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return "continued child result"

    monkeypatch.setattr("nz_coder.runtime.agent.subagent.run_subagent_async", resume)

    handled = asyncio.run(cli.handle_command_async(
        "/subagent subagent-1 inspect more", [], session_state,
        "sys", object(), terminal,
    ))

    assert handled is True
    assert captured == {
        "prompt": "inspect more",
        "agent_type": "explore",
        "session_id": "subagent-1",
        "allowed_tools": ["read_file"],
        "target_paths": ["src"],
    }
    assert session_state == {"id": "session-1", "agent": parent}
    assert "continued child result" in str(fake_console.messages[-1].renderable)


def test_handle_command_mode_alias_updates_agent(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history: list[dict] = []
    session_state = {"id": "session-1", "agent": FakeAgent()}

    handled = cli.handle_command("/mode auto", history, session_state, "sys", object())

    assert handled is True
    assert session_state["agent"].permissions.mode == "auto"


def test_handle_command_permission_rules_lists_rule_groups(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history: list[dict] = []
    session_state = {"id": "session-1", "agent": FakeAgent()}

    handled = cli.handle_command("/permission rules", history, session_state, "sys", object())

    assert handled is True
    output = fake_console.messages[-1]
    assert "Permission rules:" in output
    assert "Allow rules:" in output
    assert "bash(prefix:git ) -> allow" in output
    assert "write_file -> ask" in output


def test_handle_command_new_session_rebuilds_agent_and_clears_history(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history = [{"role": "user", "content": "old"}]
    old_agent = FakeAgent(mode="acceptEdits")
    session_state = {"id": "session-1", "agent": old_agent}
    created: dict[str, object] = {}

    monkeypatch.setattr(
        "nz_coder.interface.commands.handlers.core.create_session_id",
        lambda: "session-2",
    )
    monkeypatch.setattr(
        "nz_coder.interface.commands.handlers.core.activate_session",
        lambda session_id: session_id,
    )

    def fake_build_agent(system_prompt, renderer, session_id, permission_mode=None):
        created["system_prompt"] = system_prompt
        created["renderer"] = renderer
        created["session_id"] = session_id
        created["permission_mode"] = permission_mode
        return FakeAgent(session_id=session_id, mode=permission_mode or "default")

    monkeypatch.setattr(cli, "_build_agent", fake_build_agent)

    handled = cli.handle_command("/new-session", history, session_state, "sys", object())

    assert handled is True
    assert history == []
    assert session_state["id"] == "session-2"
    assert session_state["agent"].session_id == "session-2"
    assert created["permission_mode"] == "acceptEdits"
    assert old_agent.closed is True


def test_handle_command_resume_restores_accept_edits_mode(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history: list[dict] = []
    old_agent = FakeAgent(mode="default")
    session_state = {"id": "session-1", "agent": old_agent}
    created: dict[str, object] = {}

    monkeypatch.setattr(
        "nz_coder.interface.commands.handlers.core.load_session",
        lambda _session_id: {
            "session_id": "saved-1",
            "workspace": str(cli.config.WORKDIR),
            "mode": "acceptEdits",
            "messages": [{"role": "user", "content": "restored"}],
        },
    )
    monkeypatch.setattr(
        "nz_coder.interface.commands.handlers.core.activate_session",
        lambda session_id: session_id,
    )

    def fake_build_agent(system_prompt, renderer, session_id, permission_mode=None):
        created["session_id"] = session_id
        created["permission_mode"] = permission_mode
        return FakeAgent(session_id=session_id, mode=permission_mode or "default")

    monkeypatch.setattr(cli, "_build_agent", fake_build_agent)

    handled = cli.handle_command("/resume saved-1", history, session_state, "sys", object())

    assert handled is True
    assert history == [{"role": "user", "content": "restored"}]
    assert session_state["id"] == "saved-1"
    assert created["permission_mode"] == "acceptEdits"
    assert old_agent.closed is True


def test_session_picker_resumes_selected_session(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    monkeypatch.setattr(
        core,
        "session_options",
        lambda limit=100: [SimpleNamespace(
            session_id="saved-2",
            active=False,
            message_count=2,
            model="model-test",
            mode="plan",
        )],
    )
    monkeypatch.setattr(
        core,
        "load_session",
        lambda _session_id: {
            "session_id": "saved-2",
            "workspace": str(cli.config.WORKDIR),
            "mode": "plan",
            "messages": [{"role": "user", "content": "restored"}],
        },
    )
    monkeypatch.setattr(core, "activate_session", lambda value: value)
    old_agent = FakeAgent()
    new_agent = FakeAgent(session_id="saved-2", mode="plan")
    monkeypatch.setattr(cli, "_build_agent", lambda *_args, **_kwargs: new_agent)
    history = []
    state = {"id": "session-1", "agent": old_agent}
    terminal = FakeTerminalInput("saved-2")

    handled = asyncio.run(cli.handle_command_async(
        "/session", history, state, "sys", object(), terminal,
    ))

    assert handled is True
    assert state == {"id": "saved-2", "agent": new_agent}
    assert history == [{"role": "user", "content": "restored"}]
    assert terminal.requests[0]["title"] == "Resume session"
    assert old_agent.closed is True


def test_session_picker_keeps_unsaved_active_session(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    monkeypatch.setattr(
        core,
        "session_options",
        lambda limit=100: [SimpleNamespace(
            session_id="active-new",
            active=True,
            message_count=0,
            model="model-test",
            mode="default",
        )],
    )
    monkeypatch.setattr(
        core,
        "load_session",
        lambda _session_id: (_ for _ in ()).throw(AssertionError("must not load")),
    )
    agent = FakeAgent(session_id="active-new")
    state = {"id": "active-new", "agent": agent}

    handled = asyncio.run(cli.handle_command_async(
        "/session",
        [],
        state,
        "sys",
        object(),
        FakeTerminalInput("active-new"),
    ))

    assert handled is True
    assert state == {"id": "active-new", "agent": agent}
    assert agent.closed is False
    assert "already active" in fake_console.messages[-1]


def test_model_picker_switches_selected_model(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    old = ModelSelection("openai", "old", source="workspace")
    new = ModelSelection("anthropic", "claude-test", source="workspace")
    selections = iter((old, old, new))
    monkeypatch.setattr(core, "active_model_selection", lambda: next(selections))
    monkeypatch.setattr(core, "_known_model_ids", lambda: (
        "openai/old", "anthropic/claude-test",
    ))
    selected = []
    monkeypatch.setattr(
        core,
        "save_model_selection",
        lambda provider, model, variant=None: selected.append((provider, model, variant)),
    )
    monkeypatch.setattr(core, "save_session", lambda *_args, **_kwargs: None)
    old_agent = FakeAgent(mode="plan")
    new_agent = FakeAgent(mode="plan")
    monkeypatch.setattr(cli, "_build_agent", lambda *_args, **_kwargs: new_agent)
    state = {"id": "session-1", "agent": old_agent}

    handled = asyncio.run(cli.handle_command_async(
        "/models",
        [],
        state,
        "sys",
        object(),
        FakeTerminalInput("anthropic/claude-test"),
    ))

    assert handled is True
    assert selected == [("anthropic", "claude-test", None)]
    assert state["agent"] is new_agent
    assert old_agent.closed is True


def test_mode_without_argument_opens_permission_picker(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    agent = FakeAgent(mode="default")
    state = {"id": "session-1", "agent": agent}
    terminal = FakeTerminalInput("acceptEdits")

    handled = asyncio.run(cli.handle_command_async(
        "/mode",
        [],
        state,
        "sys",
        object(),
        terminal,
    ))

    assert handled is True
    assert agent.permissions.mode == "acceptEdits"
    assert terminal.requests[0]["title"] == "Permission mode"
    labels = [label for _value, label in terminal.requests[0]["values"]]
    assert any("Ask before risky" in label for label in labels)
    assert any("Allow all tool operations" in label for label in labels)


def test_model_picker_discovers_active_provider_when_cache_is_empty(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    active = ModelSelection("openai-compatible", "current", source="configuration")
    monkeypatch.setattr(core, "active_model_selection", lambda: active)
    listings = iter((
        ("openai-compatible/current",),
        ("openai-compatible/current", "openai-compatible/next"),
    ))
    monkeypatch.setattr(core, "_known_model_ids", lambda: next(listings))
    discovered = []
    monkeypatch.setattr(
        core,
        "discover_models",
        lambda provider: discovered.append(provider),
    )
    agent = FakeAgent()
    state = {"id": "session-1", "agent": agent}
    terminal = FakeTerminalInput(None)

    handled = asyncio.run(cli.handle_command_async(
        "/models",
        [],
        state,
        "sys",
        object(),
        terminal,
    ))

    assert handled is True
    assert discovered == ["openai-compatible"]
    assert [value for value, _label in terminal.requests[0]["values"]] == [
        "openai-compatible/current",
        "openai-compatible/next",
    ]


def test_fork_picker_forks_selected_completed_turn(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    monkeypatch.setattr(core, "save_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "create_session_id", lambda prefix="session": "fork-2")
    old_agent = FakeAgent(mode="plan")
    new_agent = FakeAgent(session_id="fork-2", mode="plan")
    monkeypatch.setattr(cli, "_build_agent", lambda *_args, **_kwargs: new_agent)
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "two"},
    ]
    state = {"id": "session-1", "agent": old_agent}

    handled = asyncio.run(cli.handle_command_async(
        "/fork-picker",
        history,
        state,
        "sys",
        object(),
        FakeTerminalInput(1),
    ))

    assert handled is True
    from nz_coder.protocol.message_schema import legacy_messages

    assert legacy_messages(history) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "one"},
    ]
    assert state == {"id": "fork-2", "agent": new_agent}


def test_handle_command_clear_clears_history_and_scratchpad(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history = [{"role": "user", "content": "x"}]
    agent = FakeAgent()
    session_state = {"id": "session-1", "agent": agent}

    handled = cli.handle_command("/clear", history, session_state, "sys", object())

    assert handled is True
    assert history == []
    assert agent.cleared is True


def test_handle_command_compact_uses_current_agent_snapshot(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    agent = FakeAgent()
    history = [{"role": "user", "content": "long history"}]
    session_state = {"id": "session-1", "agent": agent}

    handled = cli.handle_command(
        "/compact keep decisions",
        history,
        session_state,
        "sys",
        object(),
    )

    assert handled is True
    assert history == [{"role": "user", "content": "compacted"}]
    assert agent.compact_focus == "keep decisions"


def test_handle_command_unknown_command_returns_false(monkeypatch):
    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    history: list[dict] = []
    session_state = {"id": "session-1", "agent": FakeAgent()}

    handled = cli.handle_command("/missing", history, session_state, "sys", object())

    assert handled is False


def test_handle_command_model_switch_rebuilds_agent_in_same_session(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    selections = iter((
        ModelSelection("openai", "old", source="workspace"),
        ModelSelection("anthropic", "claude-test", source="workspace"),
    ))
    monkeypatch.setattr(core, "active_model_selection", lambda: next(selections))
    selected = []
    monkeypatch.setattr(
        core,
        "save_model_selection",
        lambda provider, model, variant=None: selected.append((provider, model, variant)),
    )
    saved = []
    monkeypatch.setattr(core, "save_session", lambda history, **kwargs: saved.append((history, kwargs)))
    old_agent = FakeAgent(mode="plan")
    new_agent = FakeAgent(mode="plan")
    session_state = {"id": "session-1", "agent": old_agent}
    built = []

    def build_agent(system_prompt, renderer, session_id, permission_mode=None):
        built.append((system_prompt, renderer, session_id, permission_mode))
        return new_agent

    monkeypatch.setattr(cli, "_build_agent", build_agent)
    context_history = [{"role": "user", "content": "keep me"}]
    handled = cli.handle_command(
        "/model anthropic/claude-test thinking",
        context_history,
        session_state,
        "system",
        object(),
    )

    assert handled is True
    assert selected == [("anthropic", "claude-test", "thinking")]
    assert built[0][2:] == ("session-1", "plan")
    assert session_state["agent"] is new_agent
    assert old_agent.closed is True
    assert saved[0][0] == context_history
    assert "Switched model" in fake_console.messages[-1]


def test_handle_command_model_switch_rolls_back_selection_on_build_failure(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    previous = ModelSelection("openai", "old", variant="high", source="workspace")
    monkeypatch.setattr(core, "active_model_selection", lambda: previous)
    selected = []
    monkeypatch.setattr(
        core,
        "save_model_selection",
        lambda provider, model, variant=None: selected.append((provider, model, variant)),
    )
    old_agent = FakeAgent()
    session_state = {"id": "session-1", "agent": old_agent}

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(cli, "_build_agent", fail_build)
    handled = cli.handle_command(
        "/model anthropic/claude-test",
        [],
        session_state,
        "system",
        object(),
    )

    assert handled is True
    assert selected[-1] == ("openai", "old", "high")
    assert session_state["agent"] is old_agent
    assert old_agent.closed is False
    assert "provider unavailable" in fake_console.messages[-1]


def test_handle_command_fork_copies_complete_turn_and_replaces_agent(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    old_agent = FakeAgent(mode="plan")
    new_agent = FakeAgent(session_id="fork-1", mode="plan")
    session_state = {"id": "session-1", "agent": old_agent}
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "also done"},
    ]
    saved = []
    monkeypatch.setattr(core, "save_session", lambda messages, **kwargs: saved.append((list(messages), kwargs)))
    monkeypatch.setattr(core, "load_session", lambda _session_id: {"title": "Parser repair"})
    monkeypatch.setattr(core, "create_session_id", lambda prefix="session": "fork-1")
    monkeypatch.setattr(cli, "_build_agent", lambda *_args, **_kwargs: new_agent)

    handled = cli.handle_command("/fork 1", history, session_state, "system", object())

    assert handled is True
    from nz_coder.protocol.message_schema import legacy_messages

    assert legacy_messages(history) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "done"},
    ]
    assert session_state == {"id": "fork-1", "agent": new_agent}
    assert old_agent.closed is True
    assert saved[0][1]["session_id"] == "session-1"
    assert saved[-1][1]["session_id"] == "fork-1"
    assert saved[-1][1]["title"] == "Parser repair (fork #1)"
    assert saved[-1][1]["parent_session_id"] == "session-1"
    assert saved[-1][1]["model"] is None
    assert "Workspace files were not changed" in fake_console.messages[-1]


def test_handle_command_fork_build_failure_restores_original_session(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    old_agent = FakeAgent()
    session_state = {"id": "session-1", "agent": old_agent}
    history = [{"role": "user", "content": "first"}]
    activated = []
    monkeypatch.setattr(core, "save_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "create_session_id", lambda prefix="session": "fork-1")
    monkeypatch.setattr(core, "activate_session", lambda value: activated.append(value) or value)

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("cannot initialize")

    monkeypatch.setattr(cli, "_build_agent", fail_build)

    handled = cli.handle_command("/fork", history, session_state, "system", object())

    assert handled is True
    assert history == [{"role": "user", "content": "first"}]
    assert session_state == {"id": "session-1", "agent": old_agent}
    assert old_agent.closed is False
    assert activated == ["session-1"]
    assert "cannot initialize" in fake_console.messages[-1]


def test_handle_command_fork_child_clone_failure_closes_new_agent_and_restores(monkeypatch):
    from nz_coder.interface.commands.handlers import core
    from nz_coder.runtime.agent import subagent

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    old_agent = FakeAgent()
    new_agent = FakeAgent(session_id="fork-1")
    session_state = {"id": "session-1", "agent": old_agent}
    history = [{"role": "user", "content": "first"}]
    activated = []
    monkeypatch.setattr(core, "save_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "load_session", lambda _session_id: {"title": "First"})
    monkeypatch.setattr(core, "create_session_id", lambda prefix="session": "fork-1")
    monkeypatch.setattr(core, "activate_session", lambda value: activated.append(value) or value)
    monkeypatch.setattr(cli, "_build_agent", lambda *_args, **_kwargs: new_agent)

    def fail_clone(*_args, **_kwargs):
        raise RuntimeError("child clone failed")

    monkeypatch.setattr(subagent, "clone_referenced_subagents", fail_clone)

    handled = cli.handle_command("/fork", history, session_state, "system", object())

    assert handled is True
    assert session_state == {"id": "session-1", "agent": old_agent}
    assert new_agent.closed is True
    assert old_agent.closed is False
    assert activated == ["session-1"]
    assert "child clone failed" in fake_console.messages[-1]


def test_handle_command_undo_alias_and_redo_sync_history(monkeypatch):
    from nz_coder.interface.commands.handlers import core

    fake_console = FakeConsole()
    monkeypatch.setattr(cli, "console", fake_console)
    saved = []
    monkeypatch.setattr(
        core,
        "save_session",
        lambda history, **kwargs: saved.append(list(history)),
    )

    class Tracker:
        def undo(self, history):
            history.clear()
            return "Undid agent changes:\n- restored app.py"

        def redo(self, history):
            history.append({"role": "user", "content": "restored"})
            return "Redid agent changes:\n- restored app.py"

    agent = FakeAgent()
    agent.change_tracker = Tracker()
    history = [{"role": "user", "content": "edit"}]
    session_state = {"id": "session-1", "agent": agent}

    assert cli.handle_command(
        "/revert-last",
        history,
        session_state,
        "sys",
        object(),
    )
    assert history == []
    assert saved[-1] == []

    assert cli.handle_command(
        "/redo",
        history,
        session_state,
        "sys",
        object(),
    )
    assert history == [{"role": "user", "content": "restored"}]
    assert saved[-1] == history
