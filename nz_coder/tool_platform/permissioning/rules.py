"""Permission rules and settings loading."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from nz_coder.runtime.workdir import current_workdir


class PermissionRule(NamedTuple):
    """A single permission rule with optional content matcher."""

    tool: str
    behavior: str
    content: str = ""

    def matches(self, tool_name: str, tool_input: dict) -> bool:
        if self.tool != tool_name.lower():
            return False
        if not self.content:
            return True
        if self.content.startswith("prefix:"):
            prefix = self.content[len("prefix:"):]
            if tool_name.lower() == "bash":
                cmd = tool_input.get("command", "")
                return cmd.startswith(prefix)
        return False


def parse_rules(raw: list[str], behavior: str) -> list[PermissionRule]:
    """Parse serialized rule strings into PermissionRule objects."""
    rules = []
    for item in raw or []:
        value = item.strip()
        match = re.match(r"^(\w+)\((.+)\)$", value)
        if match:
            rules.append(PermissionRule(match.group(1).lower(), behavior, match.group(2)))
        elif value:
            rules.append(PermissionRule(value.lower(), behavior))
    return rules


def load_rules_from_settings(settings_path: Path | None = None) -> tuple[list[PermissionRule], list[PermissionRule], list[PermissionRule]]:
    """Load allow/deny/ask rules from .nz-coder/settings.json."""
    path = settings_path or (current_workdir() / ".nz-coder" / "settings.json")
    if not path.exists():
        return [], [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        perms = data.get("permissions", {})
        allow_rules = parse_rules(perms.get("allow", []), "allow")
        deny_rules = parse_rules(perms.get("deny", []), "deny")
        ask_rules = parse_rules(perms.get("ask", []), "ask")
        return allow_rules, deny_rules, ask_rules
    except Exception:
        return [], [], []


def first_matching_rule(rules: list[PermissionRule], tool_name: str, tool_input: dict) -> PermissionRule | None:
    """Return the first rule matching the tool invocation."""
    for rule in rules:
        if rule.matches(tool_name, tool_input):
            return rule
    return None
