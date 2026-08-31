"""Agent-facing progressive discovery and per-run unlock of tool schemas."""
from __future__ import annotations

import json

from nz_coder.tool_platform.catalog import ToolCatalog
from nz_coder.tool_platform.search import ToolSearchIndex
from nz_coder.tools import get_catalog_specs, register


def search_and_unlock(query: str, *, max_results: int = 5, specs=None) -> str:
    """Return full matched schemas and expose them on subsequent model turns."""
    # Local import keeps the registry bootstrap acyclic: exposure reads static
    # policy metadata from nz_coder.tools while this module registers itself.
    from nz_coder.tool_platform.exposure import current_exposure_state

    value = str(query or "").strip()
    if not value:
        return "Error: query is required; use select:TOOL or descriptive keywords"
    state = current_exposure_state()
    scoped_specs = state.catalog_specs
    catalog = ToolCatalog.from_specs(
        list(specs)
        if specs is not None
        else list(scoped_specs) if scoped_specs else get_catalog_specs()
    )
    results = ToolSearchIndex(catalog).search(value, limit=max_results)
    if not results:
        return "No tools matched. Use select:EXACT_TOOL_NAME or narrower keywords."
    state.unlock(item.name for item in results)
    return "\n".join(
        json.dumps(item.definition.spec()["function"], ensure_ascii=False, sort_keys=True)
        for item in results
    )


register(
    name="tool_search",
    description=(
        "Discover tools hidden by progressive schema exposure, including workflow, "
        "project creation, memory, advanced repository intelligence, web, MCP/LSP, "
        "planning, and orchestration capabilities. Use select:TOOL_NAME for an exact "
        "schema or descriptive keywords; matching tools are unlocked for the next "
        "model turn in this Session only."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 15},
        },
        "required": ["query"],
    },
    handler=search_and_unlock,
    execution="read",
)


__all__ = ["search_and_unlock"]
