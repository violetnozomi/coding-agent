"""Small MCP stdio server used by transport and lifecycle tests."""
from __future__ import annotations

import json
import sys
import time


TOOLS = [
    {
        "name": "echo",
        "description": "Echo one value.",
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    },
    {
        "name": "fail",
        "description": "Return an MCP tool error.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "structured",
        "description": "Return structured content.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "delay",
        "description": "Wait before returning.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
    },
]
PROMPTS = [
    {
        "name": "review",
        "description": "Build a review prompt.",
        "arguments": [{"name": "topic", "required": True}],
    }
]
RESOURCES = [
    {
        "name": "guide",
        "uri": "test://guide",
        "description": "Fixture guide.",
        "mimeType": "text/plain",
    }
]


def _reply(request_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _notify(method: str) -> None:
    sys.stdout.write(
        json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": {}},
            separators=(",", ":"),
        )
        + "\n"
    )
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            continue
        if method == "initialize":
            _reply(
                request_id,
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {
                        "tools": {"listChanged": True},
                        "prompts": {"listChanged": True},
                        "resources": {"listChanged": True},
                    },
                    "serverInfo": {"name": "test-echo", "version": "1"},
                },
            )
        elif method == "tools/list":
            _reply(request_id, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "echo":
                _reply(
                    request_id,
                    {"content": [{"type": "text", "text": f"echo:{arguments.get('value')}"}]},
                )
            elif name == "fail":
                _reply(
                    request_id,
                    {"isError": True, "content": [{"type": "text", "text": "expected failure"}]},
                )
            elif name == "structured":
                _reply(request_id, {"structuredContent": {"answer": 42}})
            elif name == "delay":
                time.sleep(float(arguments.get("seconds", 0)))
                _reply(request_id, {"content": [{"type": "text", "text": "awake"}]})
            else:
                _reply(
                    request_id,
                    error={"code": -32602, "message": f"unknown tool: {name}"},
                )
        elif method == "prompts/list":
            _reply(request_id, {"prompts": PROMPTS})
        elif method == "prompts/get":
            topic = str((params.get("arguments") or {}).get("topic") or "")
            _reply(
                request_id,
                {
                    "description": "Fixture review prompt.",
                    "messages": [{
                        "role": "user",
                        "content": {"type": "text", "text": f"Review {topic}"},
                    }],
                },
            )
        elif method == "resources/list":
            _reply(request_id, {"resources": RESOURCES})
        elif method == "resources/read":
            uri = str(params.get("uri") or "")
            _reply(
                request_id,
                {"contents": [{"uri": uri, "mimeType": "text/plain", "text": "guide-body"}]},
            )
        elif method == "test/change":
            if not any(item.get("name") == "fresh" for item in TOOLS):
                TOOLS.append({
                    "name": "fresh",
                    "description": "A newly announced tool.",
                    "inputSchema": {"type": "object", "properties": {}},
                })
                PROMPTS.append({"name": "fresh-prompt", "description": "New prompt."})
                RESOURCES.append({"name": "fresh-resource", "uri": "test://fresh"})
            _reply(request_id, {"changed": True})
            _notify("notifications/tools/list_changed")
            _notify("notifications/prompts/list_changed")
            _notify("notifications/resources/list_changed")
        else:
            _reply(
                request_id,
                error={"code": -32601, "message": f"unknown method: {method}"},
            )


if __name__ == "__main__":
    main()
