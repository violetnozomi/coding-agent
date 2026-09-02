"""Permission decision engine."""
from __future__ import annotations

from nz_coder.tool_platform.policies.command_policy import (
    classify_bash,
    external_workspace_path,
    is_known_read_only_command,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import (
    get_tool_side_effect,
    is_filesystem_mutation_tool,
    is_tool_plan_mode_allowed,
)

from .modes import normalize_mode
from .rules import PermissionRule, first_matching_rule
from .tool_groups import SAFE_STATE_TOOLS


def _is_local_edit(name: str) -> bool:
    """Return whether acceptEdits may authorize this workspace mutation."""
    return is_filesystem_mutation_tool(name)


class PermissionChecker:
    """Compute allow/deny/ask decisions for tool invocations."""

    def __init__(self, mode: str = "default", *, workspace_trusted: bool = True) -> None:
        self.mode = normalize_mode(mode)
        self.workspace_trusted = bool(workspace_trusted)

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
            return self._check_bash(tool_input, allow_rules, ask_rules)

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
            if not is_tool_plan_mode_allowed(tool_name):
                reason = (
                    "Plan mode: write operations blocked"
                    if _is_local_edit(tool_name)
                    else "Plan mode: tool side effects blocked"
                )
                return {
                    "behavior": "deny",
                    "reason": reason,
                }
            return {"behavior": "allow", "reason": "Plan mode: allowed by tool metadata"}

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

        if tool_name in SAFE_STATE_TOOLS:
            return {"behavior": "allow", "reason": "Safe tool"}

        if self.mode == "auto":
            return {"behavior": "allow", "reason": "Auto mode"}

        if self.mode == "acceptEdits":
            if _is_local_edit(tool_name):
                return {"behavior": "allow", "reason": "acceptEdits mode"}
            return {
                "behavior": "ask",
                "reason": f"acceptEdits mode: {tool_name} needs approval",
            }

        if _is_local_edit(tool_name):
            return {"behavior": "ask", "reason": f"Write operation: {tool_name}"}

        if get_tool_side_effect(tool_name) not in {"readonly", "reads-network"}:
            return {
                "behavior": "ask",
                "reason": f"Side-effect operation: {tool_name}",
            }

        return {"behavior": "allow", "reason": "Safe tool"}

    def _check_mcp(
        self,
        tool_name: str,
        tool_input: dict,
        allow_rules: list[PermissionRule],
        ask_rules: list[PermissionRule],
    ) -> dict:
        """Apply conservative permissions to external MCP side effects."""
        effect = get_tool_side_effect(tool_name)
        if self.mode == "plan":
            if not is_tool_plan_mode_allowed(tool_name):
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
        if effect in {"readonly", "reads-network"}:
            return {"behavior": "allow", "reason": "Read-only MCP tool"}
        return {
            "behavior": "ask",
            "reason": (
                f"MCP {effect} operation needs approval; external side effects "
                "are not covered by the local file transaction"
            ),
        }

    def _check_bash(
        self,
        tool_input: dict,
        allow_rules: list[PermissionRule],
        ask_rules: list[PermissionRule],
    ) -> dict:
        command = tool_input.get("command", "")
        escaped = external_workspace_path(command, current_workdir())
        if escaped is not None:
            return {
                "behavior": "deny",
                "reason": f"Blocked: path outside workspace ({escaped})",
            }
        classification = classify_bash(command)

        if classification["dangerous"]:
            return {"behavior": "deny", "reason": f"Blocked: {classification['reason']}"}

        if self.mode == "plan":
            if classification["mutating"] or not is_known_read_only_command(command):
                return {
                    "behavior": "deny",
                    "reason": f"Plan mode: shell blocked ({classification['reason']})",
                }
            if not self.workspace_trusted:
                return {
                    "behavior": "ask",
                    "reason": "Untrusted workspace: shell execution needs approval",
                }
            return {"behavior": "allow", "reason": "Plan mode: read-only shell allowed"}

        allow_rule = first_matching_rule(allow_rules, "bash", tool_input)
        if allow_rule is not None:
            return {
                "behavior": "allow",
                "reason": f"Rule: {allow_rule.content or allow_rule.tool}",
            }
        ask_rule = first_matching_rule(ask_rules, "bash", tool_input)
        if ask_rule is not None:
            return {
                "behavior": "ask",
                "reason": f"Ask rule: {ask_rule.content or ask_rule.tool}",
            }

        if not self.workspace_trusted:
            return {
                "behavior": "ask",
                "reason": "Untrusted workspace: shell execution needs approval",
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
        if operation == "start":
            command = str(tool_input.get("command") or "")
            escaped = external_workspace_path(command, current_workdir())
            if escaped is not None:
                return {
                    "behavior": "deny",
                    "reason": f"Blocked: path outside workspace ({escaped})",
                }
            classification = classify_bash(command)
            if classification["dangerous"]:
                return {
                    "behavior": "deny",
                    "reason": f"Blocked: {classification['reason']}",
                }
            if self.mode == "plan":
                return self._check_bash(
                    {"command": command},
                    allow_rules,
                    ask_rules,
                )
        elif operation == "write" and self.mode == "plan":
            return {
                "behavior": "deny",
                "reason": "Plan mode: process stdin blocked",
            }
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
        if operation == "start" and not self.workspace_trusted:
            return {
                "behavior": "ask",
                "reason": "Untrusted workspace: process execution needs approval",
            }
        if operation == "start":
            return self._check_bash(
                {"command": str(tool_input.get("command") or "")},
                allow_rules,
                ask_rules,
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
