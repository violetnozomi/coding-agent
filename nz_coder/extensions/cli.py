"""CLI for the unified extension metadata registry."""
from __future__ import annotations

import argparse
import json

from nz_coder.extensions.registry import ExtensionDescriptor, ExtensionRegistry


def build_parser() -> argparse.ArgumentParser:
    """Build the ``nz-coder extensions`` parser."""
    parser = argparse.ArgumentParser(prog="nz-coder extensions")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List all projected extensions")
    listing.add_argument("--kind", choices=("skill", "hook", "tool_pack", "mcp_server", "error"))
    listing.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="Show one extension descriptor")
    status.add_argument("extension_id")
    status.add_argument("--json", action="store_true")
    reload_command = commands.add_parser(
        "reload", help="Reload supported owners and refresh extension metadata",
    )
    reload_command.add_argument("--json", action="store_true")
    for action in ("enable", "disable"):
        control = commands.add_parser(
            action, help=f"{action.title()} one supported extension",
        )
        control.add_argument("extension_id")
        control.add_argument("--json", action="store_true")
    return parser


def extensions_main(
    argv: list[str] | None = None,
    *,
    registry: ExtensionRegistry | None = None,
) -> int:
    """Run extension inspection without loading optional code or starting MCP."""
    args = build_parser().parse_args(argv)
    current = registry or ExtensionRegistry()
    if args.command == "reload":
        current.reload()
        items = current.snapshot()
        if args.json:
            print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
        else:
            print(f"Reloaded extension metadata ({len(items)} entries).")
            for item in items:
                print(_format_descriptor(item))
        return 1 if any(item.kind == "error" for item in items) else 0
    if args.command in {"enable", "disable"}:
        try:
            result = current.set_enabled(
                args.extension_id,
                args.command == "enable",
            )
        except ValueError as exc:
            print(f"Error: {exc}")
            return 1
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(
                f"{result['extension_id']} [{result['status']}] "
                f"enabled={str(result['enabled']).lower()}"
            )
        return 2 if result.get("restart_required") else 0
    items = current.snapshot()
    if args.command == "list":
        if args.kind:
            items = [item for item in items if item.kind == args.kind]
        if args.json:
            print(json.dumps([item.to_dict() for item in items], ensure_ascii=False, indent=2))
        elif not items:
            print("No extensions found.")
        else:
            for item in items:
                print(_format_descriptor(item))
        return 1 if any(item.kind == "error" for item in items) else 0
    item = next(
        (entry for entry in items if entry.extension_id == args.extension_id),
        None,
    )
    if item is None:
        print(f"Error: Unknown extension '{args.extension_id}'")
        return 1
    if args.json:
        print(json.dumps(item.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_descriptor(item, verbose=True))
    return 1 if item.kind == "error" else 0


def _format_descriptor(item: ExtensionDescriptor, *, verbose: bool = False) -> str:
    trust = "trusted" if item.trusted else "untrusted"
    line = (
        f"{item.extension_id} [{item.status}] source={item.source} "
        f"scope={item.scope} lifecycle={item.lifecycle} {trust}"
    )
    if verbose or item.error:
        capabilities = ", ".join(item.capabilities) or "none"
        effects = ", ".join(f"{name}:{effect}" for name, effect in item.effects) or "none"
        permissions = ", ".join(item.permissions) or "none"
        line += (
            f"\n  capabilities: {capabilities}"
            f"\n  effects: {effects}"
            f"\n  permissions: {permissions}"
        )
        if item.description:
            line += f"\n  description: {item.description}"
        if item.error:
            line += f"\n  error: {item.error}"
    return line


if __name__ == "__main__":
    raise SystemExit(extensions_main())
