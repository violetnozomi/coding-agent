"""Legacy MCP SSE fallback interoperability tests."""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nz_coder.mcp import MCPLegacySSEClient, MCPRuntime, load_mcp_server_configs
from nz_coder.tools import dispatch, scoped_dynamic_tool_provider


class _LegacyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address):
        super().__init__(address, _LegacyHandler)
        self.events: queue.Queue = queue.Queue()


class _LegacyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def do_GET(self):
        if self.path != "/sse":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b"event: endpoint\ndata: /messages\n\n")
        self.wfile.flush()
        while True:
            payload = self.server.events.get()
            if payload is None:
                return
            try:
                encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                self.wfile.write(b"event: message\ndata: " + encoded + b"\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    def do_POST(self):
        if self.path != "/messages":
            self.send_error(405)
            return
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length).decode("utf-8"))
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        result = None
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "legacy-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [{
                    "name": "echo",
                    "description": "legacy echo",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                }]
            }
        elif method == "tools/call":
            value = (params.get("arguments") or {}).get("value")
            result = {"content": [{"type": "text", "text": f"legacy:{value}"}]}
        elif method in {"prompts/list", "resources/list"}:
            field = "prompts" if method == "prompts/list" else "resources"
            result = {field: []}
        if request_id is not None:
            self.server.events.put({"jsonrpc": "2.0", "id": request_id, "result": result or {}})
        self.send_response(202)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_runtime_falls_back_from_streamable_http_to_legacy_sse(tmp_path):
    server = _LegacyServer(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/sse"
    config = load_mcp_server_configs(
        {
            "legacy": {
                "type": "remote",
                "url": url,
                "allow_insecure_http": True,
                "oauth": False,
                "tool_effects": {"echo": "read"},
                "startup_timeout_seconds": 2,
                "tool_timeout_seconds": 2,
            }
        },
        workspace=tmp_path,
    )[0]
    runtime = MCPRuntime([config]).start()
    try:
        assert isinstance(runtime.clients["legacy"], MCPLegacySSEClient)
        assert runtime.status_summary() == [{
            "name": "legacy",
            "status": "connected",
            "tool_count": 1,
            "error": "",
        }]
        with scoped_dynamic_tool_provider(runtime.tool_bindings):
            output = dispatch("mcp_legacy_echo", {"value": "ok"})
        assert "legacy:ok" in output
    finally:
        runtime.close()
        server.events.put(None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
