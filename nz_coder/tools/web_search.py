"""Tool: web_search - discover current sources before fetching them."""
from __future__ import annotations

import json

from nz_coder.tools import ToolOutput, current_tool_cancel_event, register
from nz_coder.web_search import search_web


def web_search(query: str, limit: int = 8, timeout: float = 20.0) -> str:
    cancel = current_tool_cancel_event()
    if cancel is not None and cancel.is_set():
        return "Error: Web search cancelled"
    try:
        bounded_limit = max(1, min(int(limit), 20))
        bounded_timeout = max(1.0, min(float(timeout), 60.0))
        provider, results = search_web(
            query,
            limit=bounded_limit,
            timeout=bounded_timeout,
        )
        if cancel is not None and cancel.is_set():
            return "Error: Web search cancelled"
        payload = {
            "query": str(query),
            "provider": provider,
            "result_count": len(results),
            "results": [result.to_dict() for result in results],
            "guidance": (
                "Search snippets are discovery hints, not authoritative evidence. "
                "Use webfetch on the most relevant primary source before relying on a claim."
            ),
        }
        return ToolOutput(
            json.dumps(payload, ensure_ascii=False, indent=2),
            title=f"Web search: {query}",
            metadata={"provider": provider, "result_count": len(results)},
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


register(
    name="web_search",
    description=(
        "Search the public web for current documentation, releases, issues, errors, "
        "compatibility, or advisories when no URL is known. Then use webfetch on a "
        "primary result; do not treat snippets as authoritative."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Focused search query."},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            "timeout": {"type": "number", "minimum": 1, "maximum": 60, "default": 20},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=web_search,
    execution="read",
)


__all__ = ["web_search"]
