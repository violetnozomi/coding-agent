"""Explicit trust controls for workspace-local language servers."""
from __future__ import annotations

import argparse
from pathlib import Path

from .servers import resolve_server, trust_server, untrust_server


def lsp_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nz-coder lsp")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "trust", "untrust"):
        child = subparsers.add_parser(command)
        child.add_argument("file")
    args = parser.parse_args(argv)
    workspace = Path.cwd().resolve()
    source = (workspace / args.file).resolve(strict=False)
    try:
        source.relative_to(workspace)
    except ValueError:
        print("Error: LSP source file must remain inside the workspace")
        return 1
    try:
        if args.command == "trust":
            server = trust_server(source, workspace)
            print(f"{server.server_id}: trusted {server.fingerprint[:12]}")
            return 0
        if args.command == "untrust":
            removed = untrust_server(source, workspace)
            print("untrusted" if removed else "not_trusted")
            return 0
        server = resolve_server(source, workspace)
        if server is None:
            print("not-installed")
        else:
            state = "trusted" if server.trusted else "trust-required"
            print(f"{server.server_id}: {state}, source={server.source}")
        return 0
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1


__all__ = ["lsp_main"]
