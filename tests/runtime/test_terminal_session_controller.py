"""Terminal product surface must enter the canonical AgentClient boundary."""
from __future__ import annotations

import inspect


def test_cli_builds_product_environment_not_legacy_agent_loop():
    from nz_coder.interface import cli

    source = inspect.getsource(cli._build_agent)
    assert "build_product_environment" in source
    assert "build_coding_agent" not in source


def test_cli_executes_through_terminal_session_controller():
    from nz_coder.interface import cli

    source = inspect.getsource(cli._run_cli_impl)
    assert "controller.run" in source
    assert "agent.run(" not in source


def test_command_replacement_updates_terminal_controller():
    from nz_coder.interface.commands import CommandContext

    class Controller:
        def __init__(self):
            self.environment = None

        def replace_environment(self, environment):
            self.environment = environment

    old = object()
    new = object()
    controller = Controller()
    context = CommandContext(
        history=[],
        session_state={"id": "old", "agent": old, "controller": controller},
        system_prompt="prompt",
        renderer=object(),
        console=object(),
        build_agent=lambda *_args: new,
    )
    context.replace_agent("new", new)
    assert controller.environment is new


def test_command_replacement_resets_product_session_title_when_surface_tracks_it():
    from nz_coder.interface.commands import CommandContext

    old = object()
    new = object()
    state = {"id": "old", "agent": old, "session_title": "Old title"}
    context = CommandContext(
        history=[],
        session_state=state,
        system_prompt="prompt",
        renderer=object(),
        console=object(),
        build_agent=lambda *_args: new,
    )

    context.replace_agent("new", new)

    assert state["session_title"] == ""
