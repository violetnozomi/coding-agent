"""Registration entry points for CLI command handlers."""
from __future__ import annotations

from ..registry import CommandRegistry
from .core import register_core_commands
from .permission import register_permission_commands
from .workflow import register_workflow_commands


def register_default_commands(registry: CommandRegistry) -> None:
    register_core_commands(registry)
    register_permission_commands(registry)
    register_workflow_commands(registry)
