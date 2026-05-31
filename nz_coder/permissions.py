"""Permission system: deny → mode → allow → ask pipeline.

改进点（对标 Claude Code permissions.ts）：
  - 新增 acceptEdits 模式：允许文件编辑，不允许任意 bash
  - 新增基于内容的规则：Bash(prefix:git) 格式
  - deny 规则优先：预先屏蔽特定工具/命令前缀
  - ask_user 展示命令/文件路径而非 JSON blob
  - 规则来源分层：session > project > user（后两个来自设置文件）
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import NamedTuple

from nz_coder import config
from nz_coder.command_policy import classify_bash, is_known_read_only_command

MODES = ("default", "auto", "plan", "acceptEdits")

WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "apply_patch",
    "replace_lines",
    "python_structural_edit",
    "write_files_batch",
    "scaffold_project",
    "save_memory",
    "delete_memory",
}
READ_TOOLS = {
    "read_file", "list_directory", "grep_search", "glob_search", "list_memories",
    "project_profile", "plan_verification", "analyze_impact",
    "analyze_project_requirements", "create_project_blueprint",
    "plan_project_acceptance", "verify_project_build",
}


class PermissionRule(NamedTuple):
    """A single permission rule with optional content matcher.

    Examples:
      PermissionRule("bash", "allow")                  → allow all bash
      PermissionRule("bash", "allow", "prefix:git ")   → allow bash starting with "git "
      PermissionRule("bash", "deny",  "prefix:rm ")    → deny bash starting with "rm "
      PermissionRule("write_file", "deny")             → deny all write_file
    """
    tool: str           # lowercase tool name
    behavior: str       # "allow" | "deny" | "ask"
    content: str = ""   # optional content matcher (currently "prefix:<text>")

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


def _parse_rules(raw: list[str], behavior: str) -> list[PermissionRule]:
    """Parse a list of rule strings into PermissionRule objects.

    Rule string format:  "tool_name"  or  "tool_name(prefix:git)"
    """
    rules = []
    for s in (raw or []):
        s = s.strip()
        m = re.match(r"^(\w+)\((.+)\)$", s)
        if m:
            rules.append(PermissionRule(m.group(1).lower(), behavior, m.group(2)))
        elif s:
            rules.append(PermissionRule(s.lower(), behavior))
    return rules


class PermissionManager:
    def __init__(self, mode: str = None):
        self.mode = mode or config.PERMISSION_MODE
        if self.mode not in MODES:
            self.mode = "default"

        # Rules loaded from .nz-coder/settings.json (project-level)
        self._deny_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._load_settings_rules()

    def _load_settings_rules(self) -> None:
        """Load permission rules from .nz-coder/settings.json if it exists."""
        settings_path = config.WORKDIR / ".nz-coder" / "settings.json"
        if not settings_path.exists():
            return
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            perms = data.get("permissions", {})
            self._allow_rules = _parse_rules(perms.get("allow", []), "allow")
            self._deny_rules  = _parse_rules(perms.get("deny", []),  "deny")
            self._ask_rules   = _parse_rules(perms.get("ask", []),   "ask")
        except Exception:
            pass  # malformed settings — ignore

    def add_allow(self, rule_str: str) -> None:
        """Dynamically add an allow rule for this session (e.g. user approved once)."""
        rules = _parse_rules([rule_str], "allow")
        self._allow_rules.extend(rules)

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """Returns {"behavior": "allow"|"deny"|"ask", "reason": str}."""

        # ── Step 1: Deny rules (highest priority, checked before anything else) ──
        for rule in self._deny_rules:
            if rule.matches(tool_name, tool_input):
                detail = f"({rule.content})" if rule.content else ""
                return {"behavior": "deny", "reason": f"Denied by rule: {tool_name}{detail}"}

        # ── Step 2: Bash-specific classification ──────────────────────────────
        if tool_name == "bash":
            command = tool_input.get("command", "")
            classification = classify_bash(command)

            if classification["dangerous"]:
                return {"behavior": "deny", "reason": f"Blocked: {classification['reason']}"}

            # plan mode: no shell at all
            if self.mode == "plan":
                if classification["mutating"] or not is_known_read_only_command(command):
                    return {"behavior": "deny", "reason": f"Plan mode: shell blocked ({classification['reason']})"}
                return {"behavior": "allow", "reason": "Plan mode: read-only shell allowed"}

            # Check session allow rules for bash prefix
            for rule in self._allow_rules:
                if rule.matches(tool_name, tool_input):
                    return {"behavior": "allow", "reason": f"Rule: {rule.content or rule.tool}"}

            # auto: allow all bash
            if self.mode == "auto":
                return {"behavior": "allow", "reason": "auto mode"}

            # acceptEdits: bash follows default logic (read-only allow, mutating/unknown ask)
            # Only file-editing tools get the blanket allow in acceptEdits
            if self.mode == "acceptEdits":
                if classification["mutating"] or not is_known_read_only_command(command):
                    return {"behavior": "ask", "reason": f"acceptEdits mode: bash needs approval ({classification['reason']})"}
                return {"behavior": "allow", "reason": "acceptEdits mode: read-only shell"}

            # default: ask for mutating, allow read-only
            if classification["mutating"] or not is_known_read_only_command(command):
                return {"behavior": "ask", "reason": f"Shell command needs approval: {classification['reason']}"}
            return {"behavior": "allow", "reason": "Read-only shell command"}

        # ── Step 3: plan mode blocks all writes ───────────────────────────────
        if self.mode == "plan":
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny", "reason": "Plan mode: write operations blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        # ── Step 4: always-allowed read tools ─────────────────────────────────
        if tool_name in READ_TOOLS or tool_name in ("todo", "compact", "task", "load_skill",
                                                      "recall_memory", "list_memories",
                                                      "read_scratchpad"):
            return {"behavior": "allow", "reason": "Safe tool"}

        # ── Step 5: session allow rules ───────────────────────────────────────
        for rule in self._allow_rules:
            if rule.matches(tool_name, tool_input):
                return {"behavior": "allow", "reason": f"Rule: {rule.content or rule.tool}"}

        # ── Step 6: ask rules ─────────────────────────────────────────────────
        for rule in self._ask_rules:
            if rule.matches(tool_name, tool_input):
                return {"behavior": "ask", "reason": f"Ask rule: {rule.content or rule.tool}"}

        # ── Step 7: auto / acceptEdits ────────────────────────────────────────
        if self.mode == "auto":
            return {"behavior": "allow", "reason": "Auto mode"}

        # acceptEdits: allow file edits, ask for everything else
        if self.mode == "acceptEdits":
            if tool_name in WRITE_TOOLS:
                return {"behavior": "allow", "reason": "acceptEdits mode"}
            return {"behavior": "ask", "reason": f"acceptEdits mode: {tool_name} needs approval"}

        # ── Step 8: default mode asks for writes ──────────────────────────────
        if tool_name in WRITE_TOOLS:
            return {"behavior": "ask", "reason": f"Write operation: {tool_name}"}

        return {"behavior": "allow", "reason": "Default allow"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """Interactive confirmation. Returns True if user approves.

        改进：展示有意义的摘要而非原始 JSON。
        """
        summary = _format_tool_summary(tool_name, tool_input)
        print(f"\n  [Permission] {summary}")
        try:
            answer = input("  Allow? (y/n/a=always/p=always-prefix): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer == "a":
            self.mode = "auto"
            return True
        if answer == "p":
            # Add a prefix allow rule so this prefix is always allowed
            if tool_name == "bash":
                cmd = tool_input.get("command", "")
                # Use first word as prefix (e.g. "git" from "git status")
                prefix = cmd.split()[0] if cmd.split() else cmd
                self.add_allow(f"bash(prefix:{prefix} )")
                print(f"  [Permission] Added allow rule: bash(prefix:{prefix} )")
            return True
        return answer in ("y", "yes")


def _format_tool_summary(tool_name: str, tool_input: dict) -> str:
    """Human-readable tool call summary for permission prompts.

    对标 Claude Code description() 方法：展示文件路径、命令内容而非 JSON。
    """
    if tool_name == "bash":
        cmd = tool_input.get("command", "")
        return f"bash: {cmd[:120]}"
    if tool_name in ("write_file", "edit_file", "replace_lines", "apply_patch"):
        path = tool_input.get("path", "")
        if not path and tool_name == "apply_patch":
            changes = tool_input.get("changes", [])
            paths = [c.get("path", "") for c in changes if isinstance(c, dict)]
            path = ", ".join(p for p in paths if p)
        return f"{tool_name}: {path}"
    if tool_name == "python_structural_edit":
        path = tool_input.get("path", "")
        targets = []
        for r in tool_input.get("replacements", []):
            if isinstance(r, dict):
                targets.append(r.get("target", ""))
        return f"python_structural_edit: {path} — {', '.join(targets) or '(insertions)'}"
    if tool_name in ("save_memory", "delete_memory"):
        name = tool_input.get("name", "")
        return f"{tool_name}: {name}"
    # fallback: show tool name + truncated JSON
    preview = json.dumps(tool_input, ensure_ascii=False)[:150]
    return f"{tool_name}: {preview}"
