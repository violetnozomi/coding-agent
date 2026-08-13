"""Permission-related CLI commands."""
from __future__ import annotations

from nz_coder.tool_platform.permissions import MODES, PermissionRule

from ..registry import Command, CommandContext, CommandRegistry


def register_permission_commands(registry: CommandRegistry) -> None:
    registry.register(
        Command(
            "permission",
            "Inspect or update permission settings",
            "/permission [mode <default|auto|plan|acceptEdits> | rules]",
            handle_permission,
            category="Permissions",
        )
    )


def handle_permission(ctx: CommandContext) -> None:
    parts = ctx.args.split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""

    if not subcommand:
        manager = ctx.agent.permissions
        ctx.console.print(
            "Permission status\n"
            f"  Current mode: {manager.mode}\n"
            f"  Allow rules: {len(_rule_list(manager, '_allow_rules'))}\n"
            f"  Deny rules: {len(_rule_list(manager, '_deny_rules'))}\n"
            f"  Ask rules: {len(_rule_list(manager, '_ask_rules'))}"
        )
        return

    if subcommand == "mode":
        set_permission_mode(ctx, parts[1].strip() if len(parts) > 1 else "")
        return

    if subcommand == "rules":
        ctx.console.print(_format_rules(ctx.agent.permissions))
        return

    ctx.console.print(
        f"[error]Usage: /permission [mode <{format_mode_usage()}> | rules][/error]"
    )


def set_permission_mode(
    ctx: CommandContext,
    mode: str,
    alias_name: str = "/permission mode",
) -> None:
    if mode in MODES:
        ctx.agent.permissions.mode = mode
        ctx.console.print(f"[success]Permission mode: {mode}[/success]")
        return
    ctx.console.print(f"[error]Usage: {alias_name} <{format_mode_usage()}>[/error]")


def format_mode_usage(valid_only: bool = False) -> tuple[str, ...] | str:
    if valid_only:
        return tuple(MODES)
    return "|".join(MODES)


def _format_rules(manager) -> str:
    groups = [
        ("Allow rules", _rule_list(manager, "_allow_rules")),
        ("Deny rules", _rule_list(manager, "_deny_rules")),
        ("Ask rules", _rule_list(manager, "_ask_rules")),
    ]
    lines = ["Permission rules:"]
    for title, rules in groups:
        lines.append(f"{title}:")
        if not rules:
            lines.append("  (none)")
            continue
        for rule in rules:
            lines.append(f"  - {_format_rule(rule)}")
    return "\n".join(lines)


def _rule_list(manager, name: str) -> list[PermissionRule]:
    return list(getattr(manager, name, []) or [])


def _format_rule(rule: PermissionRule) -> str:
    if rule.content:
        return f"{rule.tool}({rule.content}) -> {rule.behavior}"
    return f"{rule.tool} -> {rule.behavior}"
