"""Permission system split into modes, rules, checker, and interaction layers."""
from __future__ import annotations

from .checker import PermissionChecker
from .manager import PermissionManager
from .modes import MODES, normalize_mode
from .rules import PermissionRule, first_matching_rule, load_rules_from_settings, parse_rules
from .tool_groups import READ_TOOLS, SAFE_TOOLS, WRITE_TOOLS

__all__ = [
    "MODES",
    "PermissionChecker",
    "PermissionManager",
    "PermissionRule",
    "READ_TOOLS",
    "SAFE_TOOLS",
    "WRITE_TOOLS",
    "first_matching_rule",
    "load_rules_from_settings",
    "normalize_mode",
    "parse_rules",
]
