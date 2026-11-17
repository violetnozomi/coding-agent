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
        if self.content.startswith("input-sha256:"):
            expected = self.content[len("input-sha256:"):]
            return expected == _input_fingerprint(tool_input)
        if tool_name.lower() != "bash":
            return False
        command = str(tool_input.get("command") or "")
        if self.behavior == "deny":
            candidates = _deny_command_candidates(command)
        elif _has_shell_composition(command):
            return False
        else:
            candidates = (command,)
        return any(_matches_bash_content(self.content, candidate) for candidate in candidates)

    @property
    def allows_unknown(self) -> bool:
        """未知动作只能使用明确的工具委托或完整输入许可。"""
        return self.behavior == "allow" and (not self.content or self.content.startswith("input-sha256:"))


def _matches_bash_content(content: str, command: str) -> bool:
    if content.startswith("prefix:"):
        return command.startswith(content[len("prefix:"):])
    if content.startswith("argv-prefix:"):
        try:
            expected = json.loads(content[len("argv-prefix:"):])
            actual = shlex.split(command, posix=os.name != "nt")
        except (json.JSONDecodeError, TypeError, ValueError):
            return False
        return (isinstance(expected, list) and bool(expected)
                and all(isinstance(item, str) for item in expected)
                and actual[:len(expected)] == expected)
    return content == "family:pytest" and _bash_command_family(command) == "pytest"


def _deny_command_candidates(command: str, *, depth: int = 0) -> tuple[str, ...]:
    """已确认的每个执行入口都受 deny 约束；数据字符串不是子命令。"""
    from nz_coder.foundation.shell_syntax import (
        ShellSyntaxError, command_options, executable_words, inline_source, python_effects, shell_commands,
        substitution_commands,
    )
    if depth > 8:
        return ()
    candidates = [command]
    try:
        segments = shell_commands(command, partial=True)
    except ShellSyntaxError:
        return tuple(candidates)
    for segment in segments:
        words = executable_words(segment)
        if words and not segment.opaque:
            candidates.append(shlex.join(word.value for word in words))
        children = substitution_commands(segment)
        try:
            children += command_options(segment).commands if not segment.opaque else ()
            program = inline_source(segment) if not segment.opaque else None
            if program:
                children += (python_effects(program[1]).commands if program[0] == "python"
                             else (program[1],) if program[0] == "shell" else ())
        except ShellSyntaxError:
            pass
        for child in children:
            candidates.extend(_deny_command_candidates(child, depth=depth + 1))
    return tuple(candidates)


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


def load_rules_from_bytes(
    payload: bytes | None,
) -> tuple[list[PermissionRule], list[PermissionRule], list[PermissionRule]]:
    """Parse permission rules from immutable Project Control bytes."""
    if payload is None:
        return [], [], []
    try:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            return [], [], []
        permissions = data.get("permissions", {})
        if not isinstance(permissions, dict):
            return [], [], []
        allow_rules = parse_rules(permissions.get("allow", []), "allow")
        deny_rules = parse_rules(permissions.get("deny", []), "deny")
        ask_rules = parse_rules(permissions.get("ask", []), "ask")
        return allow_rules, deny_rules, ask_rules
    except (AttributeError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
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
    from nz_coder.tool_platform.policies.shell_path_analysis import analyze_shell_paths
    from nz_coder.tools.bash import _resolve_bash_workdir

    cwd, error = _resolve_bash_workdir(tool_input.get("workdir"))
    unknown = bool(error) or analyze_shell_paths(command, current_workdir(), command_cwd=cwd).kind == "unsupported"
    if _has_shell_composition(command) or unknown:
        return PermissionRule("bash", "allow", f"input-sha256:{_input_fingerprint(tool_input)}")
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
