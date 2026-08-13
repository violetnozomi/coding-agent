"""Permission decision engine."""
from __future__ import annotations

from nz_coder.tool_platform.policies.command_policy import (
    classify_bash,
    is_known_read_only_command,
)
from nz_coder.tools import get_execution_mode

from .modes import normalize_mode
from .rules import PermissionRule, first_matching_rule
from .tool_groups import SAFE_TOOLS, WRITE_TOOLS


def _is_write_tool(name: str) -> bool:
    """Include dynamically registered write effects in permission decisions."""
    return name in WRITE_TOOLS or get_execution_mode(name) == "write"


class PermissionChecker:
    """Compute allow/deny/ask decisions for tool invocations."""

    def __init__(self, mode: str = "default") -> None:
        self.mode = normalize_mode(mode)

    def check(
        self,
        tool_name: str,
        tool_input: dict,
        allow_rules: list[PermissionRule],
        deny_rules: list[PermissionRule],
        ask_rules: list[PermissionRule],
    ) -> dict:
        """Return a permission decision dict."""
        deny_rule = first_matching_rule(deny_rules, tool_name, tool_input)
        if deny_rule is not None:
            detail = f"({deny_rule.content})" if deny_rule.content else ""
            return {
                "behavior": "deny",
                "reason": f"Denied by rule: {tool_name}{detail}",
            }

        if tool_name == "bash":
            return self._check_bash(tool_input, allow_rules)

        if tool_name == "process":
            return self._check_process(tool_input, allow_rules, ask_rules)

        if tool_name.startswith("mcp_"):
            return self._check_mcp(
                tool_name,
                tool_input,
                allow_rules,
                ask_rules,
            )

        if tool_name == "webfetch":
            allow_rule = first_matching_rule(allow_rules, tool_name, tool_input)
            if allow_rule is not None:
                return {
                    "behavior": "allow",
                    "reason": f"Rule: {allow_rule.content or allow_rule.tool}",
                }
            ask_rule = first_matching_rule(ask_rules, tool_name, tool_input)
            if ask_rule is not None:
                return {
                    "behavior": "ask",
                    "reason": f"Ask rule: {ask_rule.content or ask_rule.tool}",
                }
            return {"behavior": "allow", "reason": "Read-only web fetch"}

        if self.mode == "plan":
            if _is_write_tool(tool_name):
                return {"behavior": "deny", "reason": "Plan mode: write operations blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        if tool_name in SAFE_TOOLS:
            return {"behavior": "allow", "reason": "Safe tool"}

        allow_rule = first_matching_rule(allow_rules, tool_name, tool_input)
        if allow_rule is not None:
            return {
                "behavior": "allow",
                "reason": f"Rule: {allow_rule.content or allow_rule.tool}",
            }

        ask_rule = first_matching_rule(ask_rules, tool_name, tool_input)
        if ask_rule is not None:
            return {
                "behavior": "ask",
                "reason": f"Ask rule: {ask_rule.content or ask_rule.tool}",
            }

        if self.mode == "auto":
            return {"behavior": "allow", "reason": "Auto mode"}

        if self.mode == "acceptEdits":
            if _is_write_tool(tool_name):
                return {"behavior": "allow", "reason": "acceptEdits mode"}
            return {
                "behavior": "ask",
                "reason": f"acceptEdits mode: {tool_name} needs approval",
            }

        if _is_write_tool(tool_name):
            return {"behavior": "ask", "reason": f"Write operation: {tool_name}"}

        return {"behavior": "allow", "reason": "Default allow"}

    def _check_mcp(
        self,
        tool_name: str,
        tool_input: dict,
        allow_rules: list[PermissionRule],
        ask_rules: list[PermissionRule],
    ) -> dict:
        """Apply conservative permissions to external MCP side effects."""
        effect = get_execution_mode(tool_name)
        if self.mode == "plan":
            if effect != "read":
                return {
                    "behavior": "deny",
                    "reason": f"Plan mode: MCP {effect} operation blocked",
                }
            return {"behavior": "allow", "reason": "Plan mode: read-only MCP tool"}

        allow_rule = first_matching_rule(allow_rules, tool_name, tool_input)
        if allow_rule is not None:
            return {
                "behavior": "allow",
                "reason": f"Rule: {allow_rule.content or allow_rule.tool}",
            }
        ask_rule = first_matching_rule(ask_rules, tool_name, tool_input)
        if ask_rule is not None:
            return {
                "behavior": "ask",
                "reason": f"Ask rule: {ask_rule.content or ask_rule.tool}",
            }
        if self.mode == "auto":
            return {"behavior": "allow", "reason": "Auto mode"}
        if effect == "read":
            return {"behavior": "allow", "reason": "Read-only MCP tool"}
        return {
            "behavior": "ask",
            "reason": (
                f"MCP {effect} operation needs approval; external side effects "
                "are not covered by the local file transaction"
            ),
        }

    def _check_bash(self, tool_input: dict, allow_rules: list[PermissionRule]) -> dict:
        command = tool_input.get("command", "")
        classification = classify_bash(command)

        if classification["dangerous"]:
            return {"behavior": "deny", "reason": f"Blocked: {classification['reason']}"}

        if self.mode == "plan":
            if classification["mutating"] or not is_known_read_only_command(command):
                return {
                    "behavior": "deny",
                    "reason": f"Plan mode: shell blocked ({classification['reason']})",
                }
            return {"behavior": "allow", "reason": "Plan mode: read-only shell allowed"}

        allow_rule = first_matching_rule(allow_rules, "bash", tool_input)
        if allow_rule is not None:
            return {
                "behavior": "allow",
                "reason": f"Rule: {allow_rule.content or allow_rule.tool}",
            }

        if self.mode == "auto":
            return {"behavior": "allow", "reason": "auto mode"}

        if self.mode == "acceptEdits":
            if classification["mutating"] or not is_known_read_only_command(command):
                return {
                    "behavior": "ask",
                    "reason": (
                        "acceptEdits mode: bash needs approval "
                        f"({classification['reason']})"
                    ),
                }
            return {"behavior": "allow", "reason": "acceptEdits mode: read-only shell"}

        if classification["mutating"] or not is_known_read_only_command(command):
            return {
                "behavior": "ask",
                "reason": f"Shell command needs approval: {classification['reason']}",
            }
        return {"behavior": "allow", "reason": "Read-only shell command"}

    def _check_process(
        self,
        tool_input: dict,
        allow_rules: list[PermissionRule],
        ask_rules: list[PermissionRule],
    ) -> dict:
        """Apply Bash-equivalent policy to start and explicit policy to stdin."""
        operation = str(tool_input.get("operation") or "").strip().lower()
        allow_rule = first_matching_rule(allow_rules, "process", tool_input)
        if allow_rule is not None:
            return {
                "behavior": "allow",
                "reason": f"Rule: {allow_rule.content or allow_rule.tool}",
            }
        ask_rule = first_matching_rule(ask_rules, "process", tool_input)
        if ask_rule is not None:
            return {
                "behavior": "ask",
                "reason": f"Ask rule: {ask_rule.content or ask_rule.tool}",
            }
        if operation == "start":
            return self._check_bash(
                {"command": str(tool_input.get("command") or "")},
                allow_rules,
            )
        if operation == "write":
            if self.mode == "plan":
                return {"behavior": "deny", "reason": "Plan mode: process stdin blocked"}
            if self.mode == "auto":
                return {"behavior": "allow", "reason": "auto mode"}
            return {
                "behavior": "ask",
                "reason": "Writing persistent process stdin needs approval",
            }
        if operation in {"read", "status", "list", "resize", "kill"}:
            return {
                "behavior": "allow",
                "reason": "Control existing Session-owned process",
            }
        return {"behavior": "deny", "reason": "Unknown persistent process operation"}
