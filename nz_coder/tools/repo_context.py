"""Bounded operation-based access to persistent repository module intelligence."""
from __future__ import annotations

import json

from nz_coder.intelligence.service import workspace_repo_intelligence
from nz_coder.runtime.workdir import current_workdir
from nz_coder.runtime.execution_context import repo_intelligence_mode
from nz_coder.tools import register


def repo_context(
    operation: str,
    module: str = "",
    kind: str = "auto",
    limit: int = 50,
    refresh: bool = False,
) -> str:
    """Build/query the workspace graph without claiming embedding semantics."""
    try:
        service = workspace_repo_intelligence(current_workdir(), max_files=5000)
        if service is None:
            return "Error: repository intelligence service unavailable"
        if refresh:
            service.prewarm(max_files=5000)
            service.wait_ready(timeout=10)
        bounded = max(1, min(int(limit), 200))
        if operation == "overview":
            result = service.overview(limit=bounded)
        elif operation == "module_context":
            result = service.module_context(module)
        elif operation == "relationship_scan":
            result = service.relationship_scan(module, limit=bounded)
        elif operation == "cyclic_dependencies":
            result = service.cyclic_dependencies(limit=bounded)
        elif operation == "changed_scope":
            result = service.changed_scope(limit=bounded, node_limit=bounded)
        elif operation == "symbol_context":
            result = service.symbol_context(module, bounded)
        elif operation == "process_context":
            result = service.process_context(module, max_depth=4, limit=bounded)
        elif operation == "symbol_search":
            result = service.search_symbols(module, limit=bounded)
        elif operation == "lookup":
            if repo_intelligence_mode() != "lookup":
                return "Error: unified structural lookup is disabled for this runtime tier"
            result = service.intent_lookup(module, kind=kind, limit=bounded)
        elif operation == "runtime_metrics":
            result = service.metrics()
        else:
            return "Error: operation must be overview, module_context, relationship_scan, cyclic_dependencies, changed_scope, lookup, symbol_search, symbol_context, process_context, or runtime_metrics"
        encoded = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        return encoded[:40_000] + ("\n[truncated]" if len(encoded) > 40_000 else "")
    except Exception as error:
        return f"Error: {error}"


register(
    name="repo_context",
    description=(
        "Query persistent lexical/structural repository intelligence: overview, "
        "module dependencies/dependents, unified structural intent lookup, symbol/process call contexts, dependency cycles, or changed scope."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": [
                "overview", "module_context", "relationship_scan",
                "cyclic_dependencies", "changed_scope", "symbol_search",
                "lookup", "symbol_context", "process_context", "runtime_metrics",
            ]},
            "module": {"type": "string"},
            "kind": {"type": "string", "enum": ["auto", "symbol", "module", "process"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            "refresh": {"type": "boolean"},
        },
        "required": ["operation"],
    },
    handler=repo_context,
    execution="read",
)


__all__ = ["repo_context"]
