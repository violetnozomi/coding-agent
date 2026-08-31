"""Command-line OAuth controls for configured remote MCP servers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from nz_coder.mcp.client import MCPError
from nz_coder.mcp.config import MCPServerConfig, load_mcp_server_configs
from nz_coder.mcp.oauth import MCPOAuthManager
from nz_coder.mcp.trust import MCPTrustStore
from nz_coder.foundation import config


def mcp_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nz-coder mcp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    auth = subparsers.add_parser("auth", help="Authorize one remote MCP server")
    auth.add_argument("server")
    auth.add_argument("--no-browser", action="store_true")
    auth.add_argument("--timeout", type=float, default=300.0)
    status = subparsers.add_parser("status", help="Show secret-free OAuth status")
    status.add_argument("server")
    logout = subparsers.add_parser("logout", help="Remove stored OAuth credentials")
    logout.add_argument("server")
    subparsers.add_parser("list", help="Show merged MCP servers and trust state")
    trust = subparsers.add_parser("trust", help="Trust one project-local command fingerprint")
    trust.add_argument("server")
    untrust = subparsers.add_parser("untrust", help="Remove trust for one project-local command")
    untrust.add_argument("server")
    smoke = subparsers.add_parser("smoke", help="Run an opt-in live MCP interoperability check")
    smoke.add_argument("server")
    smoke.add_argument("--tool", default="")
    smoke.add_argument("--arguments", default="{}")
    smoke.add_argument("--confirm-live", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "smoke":
            if not args.confirm_live:
                print(
                    f"Dry run only: no MCP connection opened for {args.server}. "
                    "Add --confirm-live to connect and enumerate capabilities."
                )
                return 0
            server = _named_config(args.server, Path.cwd())
            if not server.enabled or not server.trusted:
                raise ValueError("MCP smoke requires an enabled and trusted server")
            from nz_coder.mcp.runtime import MCPRuntime

            runtime = MCPRuntime([server], workspace=Path.cwd())
            try:
                runtime.start()
                status_rows = runtime.status_summary()
                status = next(
                    (item for item in status_rows if item["name"] == server.name),
                    None,
                )
                if status is None or status["status"] != "connected":
                    detail = status.get("error") if status else "missing status"
                    raise ValueError(f"MCP connection failed: {detail}")
                bindings = runtime.tool_bindings()
                print(
                    f"{server.name}: connected, tools={len(bindings)}, "
                    f"prompts={len(runtime.prompt_definitions())}, "
                    f"resources={len(runtime.resource_definitions())}"
                )
                if args.tool:
                    selected = next(
                        (
                            item for item in bindings
                            if item["name"] == args.tool
                            or item["name"].endswith(f"__{args.tool}")
                        ),
                        None,
                    )
                    if selected is None:
                        raise ValueError(f"MCP smoke tool not found: {args.tool}")
                    arguments = json.loads(args.arguments)
                    if not isinstance(arguments, dict):
                        raise ValueError("MCP smoke arguments must be a JSON object")
                    result = selected["handler"](**arguments)
                    if str(result).startswith("Error:"):
                        raise ValueError(str(result))
                    print(f"tool={selected['name']}: ok, chars={len(str(result))}")
                return 0
            finally:
                runtime.close()
        if args.command == "list":
            servers = load_mcp_server_configs(workspace=Path.cwd())
            if not servers:
                print("No configured MCP servers.")
            for server in servers:
                trust_state = "trusted" if server.trusted else "untrusted"
                print(
                    f"{server.name}: {server.transport}, source={server.source}, "
                    f"enabled={str(server.enabled).lower()}, {trust_state}"
                )
            return 0
        if args.command in {"trust", "untrust"}:
            workspace = Path.cwd().resolve()
            server = _named_config(args.server, workspace)
            if server.transport != "stdio" or server.source != "project":
                raise ValueError("Only project-local stdio servers require command trust")
            store = MCPTrustStore(Path(config.MCP_TRUST_STORE))
            if args.command == "trust":
                store.trust(workspace, server.name, server.fingerprint)
                print(f"{server.name}: trusted {server.fingerprint[:12]}")
            else:
                removed = store.remove(workspace, server.name)
                print(f"{server.name}: {'untrusted' if removed else 'not_trusted'}")
            return 0
        server = _server_config(args.server)
        manager = MCPOAuthManager()
        if args.command == "status":
            print(f"{server.name}: {manager.status(server)}")
            return 0
        if args.command == "logout":
            removed = manager.remove(server.name)
            print(f"{server.name}: {'removed' if removed else 'not_authenticated'}")
            return 0
        pending = manager.begin_auth(server)
        print("Open this URL to authorize the MCP server:")
        print(pending.authorization_url)
        if not args.no_browser:
            pending.open_browser()
        pending.finish(timeout=args.timeout)
        print(f"{server.name}: authenticated")
        return 0
    except (ValueError, MCPError) as exc:
        print(f"Error: {exc}")
        return 1


def _server_config(name: str) -> MCPServerConfig:
    server = _named_config(name, Path.cwd())
    if server.transport != "streamable_http":
        raise ValueError(f"MCP server '{name}' is not remote")
    if server.oauth is None:
        raise ValueError(f"MCP server '{name}' has OAuth disabled")
    return server


def _named_config(name: str, workspace: Path) -> MCPServerConfig:
    for server in load_mcp_server_configs(workspace=workspace):
        if server.name == name:
            return server
    raise ValueError(f"Unknown MCP server '{name}'")
