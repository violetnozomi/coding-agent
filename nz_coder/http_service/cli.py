"""Command-line launcher for the optional loopback Session HTTP service."""
from __future__ import annotations

import argparse
import os

from nz_coder import config

from .server import SessionHTTPService


def serve_main(argv: list[str] | None = None) -> int:
    """Run the authenticated local service until interrupted."""
    parser = argparse.ArgumentParser(prog="nz-coder serve")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4096)
    parser.add_argument(
        "--workspace",
        action="append",
        default=[],
        metavar="PATH",
        help="allow HTTP selection of another workspace root; may be repeated",
    )
    parser.add_argument(
        "--interaction-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for permission/question replies (default: 300)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NZ_HTTP_TOKEN"),
        help="Bearer token; defaults to NZ_HTTP_TOKEN or a generated secret",
    )
    args = parser.parse_args(argv)
    if not config.API_KEY:
        print("Error: API_KEY is required before starting the Session service.")
        return 1
    try:
        service = SessionHTTPService(
            host=args.host,
            port=args.port,
            token=args.token,
            interaction_timeout_seconds=args.interaction_timeout,
            workspace_roots=args.workspace,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2

    print("NZ-Coder local Session service")
    print(f"URL: {service.base_url}")
    print(f"Bearer token: {service.token}")
    print("The service is loopback-only. Press Ctrl-C to stop.")
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping NZ-Coder Session service.")
    finally:
        service.close_after_serve()
    return 0
