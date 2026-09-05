"""Safe first-run workspace initialization for the installed CLI."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from nz_coder.foundation.private_paths import harden_private_path


_MINIMAL_ENV = """# NZ-Coder workspace configuration
# Fill the credential for the selected Provider, then run: nz-coder doctor
API_KEY=
MODEL_PROVIDER=openai-compatible
MODEL_ID=deepseek-v4-flash
API_BASE_URL=https://api.deepseek.com

# Native alternatives:
# MODEL_PROVIDER=anthropic
# ANTHROPIC_API_KEY=
# ANTHROPIC_API_BASE_URL=https://api.anthropic.com
# MODEL_PROVIDER=gemini
# GEMINI_API_KEY=
# GEMINI_API_BASE_URL=https://generativelanguage.googleapis.com/v1beta
# MODEL_PROVIDER=openai-responses
# OPENAI_API_KEY=
# OPENAI_API_BASE_URL=https://api.openai.com/v1

PERMISSION_MODE=default
NZ_LSP_ENABLED=1
NZ_MCP_ENABLED=0
"""


def init_main(argv: list[str] | None = None) -> int:
    """Create a private minimal .env without overwriting existing user data."""
    parser = argparse.ArgumentParser(prog="nz-coder init")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path.cwd(),
        help="Workspace directory (default: current directory)",
    )
    args = parser.parse_args(argv)
    root = args.directory.resolve()
    if not root.exists() or not root.is_dir():
        print(f"Error: workspace directory does not exist: {root}")
        return 1
    target = root / ".env"
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"Error: refusing to overwrite existing file: {target}")
        return 1
    except OSError as exc:
        print(f"Error: could not create {target}: {exc}")
        return 1
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(_MINIMAL_ENV)
        security = harden_private_path(target)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    protection = (
        "owner-private permissions"
        if security.hardened
        else "best-effort permissions; verify with nz-coder doctor"
    )
    print(
        f"Created project configuration template {target} with {protection}. "
        "Use nz-coder /connect for credentials, then run: nz-coder doctor"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(init_main())
