"""Permission rules and settings loading."""
from __future__ import annotations

import json
import hashlib
import os
import re
import shlex
from pathlib import Path
from typing import NamedTuple

from nz_coder.runtime.process.workdir import current_workdir


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
                cmd = str(tool_input.get("command") or "")
                if _has_shell_composition(cmd):
                    return False
                return cmd.startswith(prefix)
        if self.content.startswith("input-sha256:"):
            expected = self.content[len("input-sha256:"):]
            return expected == _input_fingerprint(tool_input)
        if self.content.startswith("argv-prefix:") and tool_name.lower() == "bash":
            command = str(tool_input.get("command") or "")
            if _has_shell_composition(command):
                return False
            try:
                expected = json.loads(self.content[len("argv-prefix:"):])
                actual = shlex.split(
                    command,
                    posix=os.name != "nt",
                )
            except (json.JSONDecodeError, TypeError, ValueError):
                return False
            if (
                not isinstance(expected, list)
                or not expected
                or any(not isinstance(item, str) for item in expected)
            ):
                return False
            return actual[:len(expected)] == expected
        if self.content == "family:pytest" and tool_name.lower() == "bash":
            command = str(tool_input.get("command") or "")
            if _has_shell_composition(command):
                return False
            return _bash_command_family(command) == "pytest"
        return False


def _has_shell_composition(command: str) -> bool:
    """Reject reusable prefixes when another shell expression is present."""
    return bool(re.search(
        r"(?:&&|\|\||[;&|\n`<>]|\$\(|\)\s*(?:$|[;&|]))",
        str(command or ""),
    ))


def _bash_command_family(command: str) -> str:
    """Return a narrow reusable family for safe validation commands."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    lowered = [token.lower() for token in tokens]
    if not lowered:
        return ""
    if lowered[0] in {"pytest", "py.test"}:
        return "pytest"
    if (
        len(lowered) >= 3
        and re.fullmatch(r"python(?:3(?:\.\d+)?)?", lowered[0])
        and lowered[1:3] == ["-m", "pytest"]
    ):
        return "pytest"
    return ""


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


def scoped_allow_rule(tool_name: str, tool_input: dict) -> PermissionRule:
    """Return the narrow persistent rule for one approved invocation."""
    normalized = str(tool_name or "").strip().lower()
    if not normalized:
        raise ValueError("Permission rule requires a tool name")
    if normalized != "bash":
        return PermissionRule(
            normalized,
            "allow",
            f"input-sha256:{_input_fingerprint(tool_input)}",
        )
    command = str(tool_input.get("command") or "").strip()
    family = _bash_command_family(command)
    if family:
        return PermissionRule("bash", "allow", f"family:{family}")
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError as exc:
        raise ValueError(f"Cannot scope malformed shell command: {exc}") from exc
    if not tokens:
        raise ValueError("Cannot persist an empty shell command")
    encoded = json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))
    return PermissionRule("bash", "allow", f"argv-prefix:{encoded}")


def _input_fingerprint(tool_input: dict) -> str:
    try:
        payload = json.dumps(
            tool_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Permission input cannot be scoped safely") from exc
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def persist_allow_rule(
    rule: PermissionRule,
    settings_path: Path | None = None,
) -> None:
    """Add one allow rule to the user-private store, never project settings."""
    from nz_coder.tool_platform.permissioning.grants import UserGrantStore

    UserGrantStore(settings_path).add(current_workdir(), _serialize_rule(rule))


def _serialize_rule(rule: PermissionRule) -> str:
    if rule.content:
        return f"{rule.tool}({rule.content})"
    return rule.tool
