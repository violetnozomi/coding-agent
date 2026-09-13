"""Agent-facing discovery and retrieval over the active MCP runtime."""
from __future__ import annotations

import json

from nz_coder.protocol.public_error import format_public_error
from nz_coder.tools import register


_KINDS = frozenset({"server", "tool", "prompt", "resource"})
_OPERATIONS = frozenset({"search", "get_prompt", "read_resource"})


def _mcp_catalog(
    operation: str,
    query: str = "",
    kind: str = "",
    server: str = "",
    name: str = "",
    uri: str = "",
    arguments: dict | None = None,
    limit: int = 20,
) -> str:
    """Search cached MCP metadata or fetch one exact prompt/resource."""
    # Lazy import avoids the established mcp.runtime -> tools registry edge
    # becoming a circular package-initialization dependency.
    from nz_coder.mcp.runtime import current_mcp_runtime

    runtime = current_mcp_runtime()
    if runtime is None:
        return "Error: MCP runtime is not active"
    selected = str(operation or "").strip()
    if selected not in _OPERATIONS:
        return "Error: operation must be search, get_prompt, or read_resource"
    try:
        if selected == "get_prompt":
            if not str(server).strip() or not str(name).strip():
                return "Error: get_prompt requires server and name"
            result = runtime.get_prompt(
                str(server).strip(),
                str(name).strip(),
                dict(arguments or {}),
            )
            return _bounded_json(result)
        if selected == "read_resource":
            if not str(server).strip() or not str(uri).strip():
                return "Error: read_resource requires server and uri"
            return _bounded_json(runtime.read_resource(
                str(server).strip(),
                str(uri).strip(),
            ))
        return _search(runtime, query=query, kind=kind, server=server, limit=limit)
    except Exception as error:
        return format_public_error(error, context=f"MCP {selected} failed: ")


def _search(runtime, *, query: str, kind: str, server: str, limit: int) -> str:
    selected_kind = str(kind or "").strip().lower()
    if selected_kind and selected_kind not in _KINDS:
        return "Error: kind must be server, tool, prompt, or resource"
    selected_server = str(server or "").strip().lower()
    rows: list[dict] = []
    for item in runtime.status_summary():
        rows.append({
            "kind": "server",
            "server": str(item.get("name") or ""),
            "status": str(item.get("status") or ""),
            "tool_count": max(0, int(item.get("tool_count", 0) or 0)),
            "error": str(item.get("error") or "")[:500],
        })
    for item in runtime.tool_bindings():
        rows.append({
            "kind": "tool",
            "server": str(item.get("server") or ""),
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or "")[:1000],
        })
    for item in runtime.prompt_definitions():
        rows.append({
            "kind": "prompt",
            "server": str(item.get("server") or ""),
            "name": str(item.get("name") or ""),
            "description": str(item.get("description") or "")[:1000],
            "arguments": item.get("arguments") if isinstance(item.get("arguments"), list) else [],
        })
    for item in runtime.resource_definitions():
        rows.append({
            "kind": "resource",
            "server": str(item.get("server") or ""),
            "name": str(item.get("name") or ""),
            "uri": str(item.get("uri") or ""),
            "description": str(item.get("description") or "")[:1000],
            "mime_type": str(item.get("mimeType") or item.get("mime_type") or ""),
        })
    needle = " ".join(str(query or "").lower().split())

    def admitted(item: dict) -> bool:
        if selected_kind and item["kind"] != selected_kind:
            return False
        if selected_server and str(item.get("server") or "").lower() != selected_server:
            return False
        return not needle or needle in json.dumps(item, ensure_ascii=False).lower()

    matches = [item for item in rows if admitted(item)]
    matches.sort(key=lambda item: (
        item["kind"], str(item.get("server") or ""),
        str(item.get("name") or item.get("uri") or ""),
    ))
    bounded = max(1, min(int(limit or 20), 50))
    return _bounded_json({
        "total": len(matches),
        "returned": min(len(matches), bounded),
        "items": matches[:bounded],
    })


def _bounded_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return encoded[:50_000] + ("\n[truncated]" if len(encoded) > 50_000 else "")


register(
    name="mcp_catalog",
    description=(
        "Search MCP servers, tools, prompts, and resources without exposing every schema; "
        "then fetch one exact prompt or resource from the active MCP runtime."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": sorted(_OPERATIONS)},
            "query": {"type": "string"},
            "kind": {"type": "string", "enum": ["", *sorted(_KINDS)]},
            "server": {"type": "string"},
            "name": {"type": "string"},
            "uri": {"type": "string"},
            "arguments": {"type": "object"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["operation"],
        "additionalProperties": False,
    },
    handler=_mcp_catalog,
    execution="read",
)
