"""Tool: web_search - discover current sources before fetching them."""
from __future__ import annotations

import json
import math

from nz_coder.protocol.public_error import PublicInputError, format_public_error
from nz_coder.tools import ToolOutput, current_tool_cancel_event, register
from nz_coder.capabilities.web_search import search_web


def web_search(query: str, limit: int = 8, timeout: float = 20.0) -> str:
    cancel = current_tool_cancel_event()
    if cancel is not None and cancel.is_set():
        return "Error: Web search cancelled"
    try:
        bounded_limit = max(1, min(int(limit), 20))
        if isinstance(timeout, bool):
            raise PublicInputError("timeout must be a positive finite number")
        bounded_timeout = float(timeout)
        if not math.isfinite(bounded_timeout) or bounded_timeout <= 0:
            raise PublicInputError("timeout must be a positive finite number")
        bounded_timeout = min(bounded_timeout, 60.0)
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
        return format_public_error(exc)
    except Exception as exc:
        return format_public_error(exc)


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
    side_effect="reads-network",
    plan_mode_allowed=True,
)


__all__ = ["web_search"]
