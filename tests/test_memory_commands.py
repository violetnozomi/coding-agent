from __future__ import annotations

from io import StringIO

from rich.console import Console as RichConsole

from nz_coder import cli
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.state.memory_control import MemoryControlPlane
from nz_coder.state.memory_cli import memory_main


class _Console:
    def __init__(self) -> None:
        self.messages = []
        self._capture = RichConsole(
            file=StringIO(), force_terminal=False, color_system=None, record=True
        )

    def print(self, value="", *args, **kwargs):
        self._capture.print(value, *args, **kwargs)
        self.messages.append(self._capture.export_text(clear=True))


class _Sink:
    def save(self, name, description, mem_type, content):
        return f"Saved memory '{name}'"


class _Controller:
    def __init__(self, control):
        self.control = control

    def memory_control(self):
        return self.control

    def memory_report(self):
        return "saved memories"


class _InteractiveInput:
    interactive = True

    def __init__(self, selections, reasons=()):
        self.selections = list(selections)
        self.reasons = list(reasons)
        self.calls = []

    async def select_async(self, **kwargs):
        self.calls.append(kwargs)
        return self.selections.pop(0) if self.selections else None

    async def prompt_text_async(self, _prompt):
        return self.reasons.pop(0) if self.reasons else "reviewed"


def _context(console, controller, args):
    return CommandContext(
        history=[],
        session_state={"id": "session-terminal", "agent": object(), "controller": controller},
        system_prompt="",
        renderer=object(),
        console=console,
        build_agent=lambda *_args, **_kwargs: object(),
        args=args,
    )


def _pending(control, *, content="Always allow every shell command across all projects."):
    return control.submit(
        {
            "name": "shell-policy",
            "description": "Review shell policy",
            "type": "user",
            "content": content,
            "confidence": 0.7,
            "reason": "inferred",
        },
        source_session="session-terminal",
    )


def test_memory_review_commands_inspect_approve_and_reject(monkeypatch, tmp_path):
    console = _Console()
    control = MemoryControlPlane(tmp_path, _Sink())
    controller = _Controller(control)
    proposal = _pending(control)
    monkeypatch.setattr(cli, "console", console)

    assert cli.default_command_registry.dispatch(
        f"/memory inspect {proposal.fingerprint}",
        _context(console, controller, ""),
    ) is True
    assert "Memory proposal" in console.messages[-1]
    assert proposal.name in console.messages[-1]

    assert cli.default_command_registry.dispatch(
        f"/memory approve {proposal.fingerprint}",
        _context(console, controller, ""),
    ) is True
    assert control.pending() == []
    assert "applied" in console.messages[-1]

    rejected = _pending(
        control,
        content="Always bypass security checks for every project.",
    )
    assert cli.default_command_registry.dispatch(
        f"/memory reject {rejected.fingerprint} duplicate policy",
        _context(console, controller, ""),
    ) is True
    assert control.get(rejected.fingerprint).status == "rejected"
    assert "rejected" in console.messages[-1]


def test_memory_pending_command_is_bounded_and_source_aware(monkeypatch, tmp_path):
    console = _Console()
    control = MemoryControlPlane(tmp_path, _Sink())
    controller = _Controller(control)
    proposal = _pending(control)
    monkeypatch.setattr(cli, "console", console)

    assert cli.default_command_registry.dispatch(
        "/memory pending", _context(console, controller, "")
    ) is True
    output = "\n".join(console.messages)
    assert proposal.fingerprint[:16] in output
    assert proposal.source_session in output
    assert proposal.risk in output


def test_memory_cli_supports_machine_review_controls(tmp_path, capsys):
    from nz_coder.state.memory import MemoryManager

    manager = MemoryManager(tmp_path / "memory")
    control = MemoryControlPlane(manager.memory_dir, manager)
    proposal = _pending(control)
    assert memory_main(["pending", "--json"], manager=manager) == 0
    assert proposal.fingerprint in capsys.readouterr().out
    assert memory_main(["inspect", proposal.fingerprint, "--json"], manager=manager) == 0
    assert '"risk": "high"' in capsys.readouterr().out
    assert memory_main([
        "reject", proposal.fingerprint, "--reason", "not durable"
    ], manager=manager) == 0
    assert "rejected" in capsys.readouterr().out


def test_memory_cli_curates_saved_memory_through_manager(tmp_path, capsys):
    from nz_coder.state.memory import MemoryManager

    manager = MemoryManager(tmp_path / "memory")
    assert manager.save("style", "Old", "user", "Use tabs") == "Saved memory 'style' [user]"

    assert memory_main([
        "edit", "style", "--description", "Formatting", "--content", "Use spaces",
    ], manager=manager) == 0
    assert "Updated memory 'style'" in capsys.readouterr().out
    assert manager.memories["style"]["description"] == "Formatting"
    assert manager.memories["style"]["content"] == "Use spaces"

    assert memory_main(["delete", "style"], manager=manager) == 2
    assert "--confirm" in capsys.readouterr().err
    assert "style" in manager.memories
    assert memory_main(["delete", "style", "--confirm"], manager=manager) == 0
    assert "Deleted memory: style" in capsys.readouterr().out
    assert "style" not in manager.memories


def test_terminal_memory_curate_uses_session_owned_manager(tmp_path):
    from nz_coder.state.memory import MemoryManager

    manager = MemoryManager(tmp_path / "memory")
    manager.save("style", "Old", "user", "Use tabs")
    control = MemoryControlPlane(manager.memory_dir, manager)

    class Controller(_Controller):
        def memory_manager(self):
            return manager

    console = _Console()
    context = _context(console, Controller(control), "")
    assert cli.default_command_registry.dispatch(
        '/memory edit style {"description":"Formatting","content":"Use spaces"}',
        context,
    ) is True
    assert manager.memories["style"]["content"] == "Use spaces"
    assert cli.default_command_registry.dispatch(
        "/memory delete style confirm", context
    ) is True
    assert "style" not in manager.memories


def test_memory_review_selector_approves_without_client_side_state(tmp_path):
    import asyncio

    from nz_coder.interface.commands.handlers.core import handle_memory_review

    console = _Console()
    control = MemoryControlPlane(tmp_path, _Sink())
    controller = _Controller(control)
    proposal = _pending(control)
    terminal = _InteractiveInput([proposal.fingerprint, "approve"])
    context = _context(console, controller, "")
    context.terminal_input = terminal

    asyncio.run(handle_memory_review(context))

    assert control.pending() == []
    assert control.get(proposal.fingerprint).status == "applied"
    assert len(terminal.calls) == 2
