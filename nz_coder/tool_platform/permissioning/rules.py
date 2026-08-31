"""Permission rules and settings loading."""
from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from pathlib import Path
from typing import NamedTuple

from nz_coder.foundation.private_paths import harden_private_path
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
        return PermissionRule(normalized, "allow")
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


def persist_allow_rule(
    rule: PermissionRule,
    settings_path: Path | None = None,
) -> None:
    """Atomically add one allow rule to owner-private project settings."""
    path = _validated_settings_path(settings_path)
    payload = _read_settings_for_update(path)
    permissions = payload.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("Permission settings must be a JSON object")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list) or any(not isinstance(item, str) for item in allow):
        raise ValueError("permissions.allow must be a list of strings")
    serialized = _serialize_rule(rule)
    if serialized not in allow:
        allow.append(serialized)
    _atomic_write_settings(path, payload)


def _serialize_rule(rule: PermissionRule) -> str:
    if rule.content:
        return f"{rule.tool}({rule.content})"
    return rule.tool


def _validated_settings_path(settings_path: Path | None) -> Path:
    root = current_workdir().resolve()
    path = Path(settings_path) if settings_path is not None else root / ".nz-coder" / "settings.json"
    if path.exists() and path.is_symlink():
        raise ValueError("Permission settings cannot be a symbolic link")
    if path.parent.exists() and path.parent.is_symlink():
        raise ValueError("Permission settings directory cannot be a symbolic link")
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Permission settings must remain inside the workspace") from exc
    return resolved


def _read_settings_for_update(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid permission settings: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Permission settings must be a JSON object")
    return payload


def _atomic_write_settings(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_security = harden_private_path(path.parent)
    if not directory_security.hardened:
        raise PermissionError(directory_security.detail)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        temporary_security = harden_private_path(temporary)
        if not temporary_security.hardened:
            raise PermissionError(temporary_security.detail)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        file_security = harden_private_path(path)
        if not file_security.hardened:
            raise PermissionError(file_security.detail)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
