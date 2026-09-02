"""Secret-free effective configuration and provenance for operators."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path

from rich.console import Console
from rich.table import Table

from nz_coder.foundation import config
from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.mcp.config import load_mcp_server_configs
from nz_coder.providers.models import active_model_selection
from nz_coder.runtime.process.workdir import current_workdir


def collect_effective_config(workspace: Path | None = None) -> dict[str, dict]:
    """Collect documented product controls without exposing credential values."""
    root = (workspace or current_workdir()).resolve()
    snapshot = load_config_snapshot(root)
    selection = active_model_selection(root)
    model_source = "environment" if "MODEL_ID" in os.environ else selection.source
    effort_source = "environment" if "MODEL_VARIANT" in os.environ else selection.source
    permission_source = "environment" if "PERMISSION_MODE" in os.environ else "configuration"
    retrieval_source = (
        "environment" if "NZ_REPO_RETRIEVAL_STRATEGY" in os.environ else "default"
    )
    semantic_model = os.environ.get("NZ_SEMANTIC_MODEL", "").strip()
    semantic_dependency = importlib.util.find_spec("sentence_transformers") is not None
    if semantic_model:
        semantic_status = "configured" if semantic_dependency else "dependency-missing"
        semantic_source = "environment"
    else:
        semantic_status = "dependency-ready" if semantic_dependency else "disabled"
        semantic_source = "default"
    try:
        mcp = load_mcp_server_configs(workspace=root)
        enabled_mcp = [item.name for item in mcp if item.enabled]
        mcp_source = ", ".join(sorted({item.source for item in mcp})) or "configuration"
    except (OSError, ValueError) as exc:
        enabled_mcp = [f"config-error:{type(exc).__name__}"]
        mcp_source = "invalid"
    from nz_coder.intelligence.analyzers import AnalyzerRegistry

    parser_probe = AnalyzerRegistry().capability_probe()
    parser_capability = {
        language: str(item.get("capability_tier") or "unavailable")
        for language, item in sorted(parser_probe.items())
    }
    try:
        from nz_coder.http_service.daemon import daemon_status

        daemon_endpoint = str(daemon_status("default").get("endpoint") or "stopped")
    except (OSError, ValueError):
        daemon_endpoint = "unavailable"
    return {
        "provider": {"value": selection.provider, "source": selection.source},
        "model": {"value": selection.model_id, "source": model_source},
        "reasoning_effort": {
            "value": selection.variant or "default",
            "source": effort_source,
        },
        "permission_mode": {"value": config.PERMISSION_MODE, "source": permission_source},
        "retrieval_strategy": {
            "value": os.environ.get("NZ_REPO_RETRIEVAL_STRATEGY", "guidance"),
            "source": retrieval_source,
        },
        "semantic_status": {"value": semantic_status, "source": semantic_source},
        "enabled_mcp": {"value": enabled_mcp, "source": mcp_source},
        "parser_capability": {"value": parser_capability, "source": "installation"},
        "process_tier": {
            "value": "pty" if os.name == "posix" else "pipe",
            "source": "platform",
        },
        "daemon_endpoint": {"value": daemon_endpoint, "source": "daemon state"},
        "config_provenance": {
            "value": snapshot.public(),
            "source": "typed-snapshot",
        },
        "config_issues": {
            "value": [
                {"key": item.key, "message": item.message, "source": item.source.value}
                for item in snapshot.issues
            ],
            "source": "typed-snapshot",
        },
    }


def config_main(argv: list[str] | None = None) -> int:
    """Render `nz-coder config show` in human or stable machine form."""
    parser = argparse.ArgumentParser(prog="nz-coder config")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="Show effective product configuration")
    show.add_argument("--sources", action="store_true")
    show.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    values = collect_effective_config()
    projected = values if args.sources else {
        key: record["value"] for key, record in values.items()
    }
    if args.json:
        print(json.dumps(projected, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    table = Table(title="NZ-Coder effective config", expand=True)
    table.add_column("Setting", style="cyan", no_wrap=True)
    table.add_column("Value")
    if args.sources:
        table.add_column("Source")
    for key, record in values.items():
        value = record["value"]
        rendered = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
        row = [key, rendered]
        if args.sources:
            row.append(str(record["source"]))
        table.add_row(*row)
    Console().print(table)
    return 0


__all__ = ["collect_effective_config", "config_main"]
