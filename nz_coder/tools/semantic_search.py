"""Optional semantic code retrieval bound back to structural identities."""
from __future__ import annotations

import json

from nz_coder.intelligence.service import workspace_repo_intelligence
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import register


def semantic_search(query: str, path: str = "", limit: int = 10) -> str:
    """Search experimental embedding chunks; structural tools remain fallback."""
    try:
        service = workspace_repo_intelligence(current_workdir(), max_files=5000)
        if service is None:
            return "Error: repository intelligence service unavailable"
        result = service.semantic_search(
            query, path=path.strip() or None, limit=max(1, min(50, int(limit))),
            wait_budget_ms=2_000,
        )
        encoded = json.dumps(result, ensure_ascii=False, indent=2, default=str)
        return encoded[:40_000] + ("\n[truncated]" if len(encoded) > 40_000 else "")
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


register(
    name="semantic_search",
    description=(
        "Optionally locate code from business-language intent using embedding similarity. "
        "Results include file spans and structural symbol/module identities. Use grep for exact "
        "text and repo_context for call, module, process, or impact relationships."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["query"],
    },
    handler=semantic_search,
    execution="read",
)


__all__ = ["semantic_search"]
