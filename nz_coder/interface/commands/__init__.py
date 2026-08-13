"""Slash-command subsystem for the terminal CLI."""
from __future__ import annotations

from .handlers import register_default_commands
from .registry import Command, CommandContext, CommandRegistry


def build_default_registry() -> CommandRegistry:
    registry = CommandRegistry()
    register_default_commands(registry)
    return registry


default_command_registry = build_default_registry()

__all__ = [
    "Command",
    "CommandContext",
    "CommandRegistry",
    "build_default_registry",
    "default_command_registry",
]
