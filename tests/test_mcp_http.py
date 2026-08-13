"""Tests for the bounded MCP Streamable HTTP transport and runtime wiring."""
from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nz_coder.mcp import MCPHTTPClient, MCPRuntime, load_mcp_server_configs


class _MCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        message = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.messages.append((message, dict(self.headers)))
        if self.server.redirect_url:
            self.send_response(307)
            self.send_header("Location", self.server.redirect_url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        method = message.get("method")
        if method == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        results = {
            "initialize": {
                "protocolVersion": self.server.protocol_version,
                "serverInfo": {"name": "http-fixture", "version": "1"},
                "capabilities": {
                    "tools": {},
                    "prompts": {},
                    "resources": {},
                },
            },
            "tools/list": {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo input",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                        },
                    }
                ]
            },
            "tools/call": {
                "content": [
                    {
                        "type": "text",
                        "text": "remote:" + str(message.get("params", {}).get("arguments", {}).get("value", "")),
                    }
                ]
            },
            "prompts/list": {"prompts": [{"name": "review"}]},
            "prompts/get": {
                "description": "review",
                "messages": [{"role": "user", "content": {"type": "text", "text": "Review"}}],
            },
            "resources/list": {"resources": [{"uri": "memo://one", "name": "one"}]},
            "resources/read": {
                "contents": [{"uri": "memo://one", "text": "remote resource"}]
            },
        }
        response = {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": results[method],
        }
        payload = json.dumps(response).encode("utf-8")
        if method in self.server.sse_methods or method in self.server.open_sse_methods:
            payload = b"event: message\ndata: " + payload + b"\n\n"
            content_type = "text/event-stream"
        else:
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if method not in self.server.open_sse_methods:
            self.send_header("Content-Length", str(len(payload)))
        else:
            self.send_header("Connection", "close")
        if method == "initialize":
            self.send_header("Mcp-Session-Id", self.server.session_id)
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        if method in self.server.open_sse_methods:
            time.sleep(0.5)

    def do_GET(self):
        self.server.get_headers.append(dict(self.headers))
        if self.server.get_status:
            self.send_response(self.server.get_status)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.server.get_event:
            payload = (
                b"event: message\ndata: "
                + json.dumps(self.server.get_event).encode("utf-8")
                + b"\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        self.server.delete_headers.append(dict(self.headers))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, _format, *_args):
        return


@contextmanager
def _http_mcp_server(
    *,
    sse_methods=(),
    open_sse_methods=(),
    get_event=None,
    get_status=0,
    redirect_url="",
    session_id="fixture-session",
):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MCPHandler)
    server.messages = []
    server.get_headers = []
    server.delete_headers = []
    server.sse_methods = set(sse_methods)
    server.open_sse_methods = set(open_sse_methods)
    server.get_event = get_event
    server.get_status = get_status
    server.redirect_url = redirect_url
    server.protocol_version = "2025-06-18"
    server.session_id = session_id
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_remote_config_requires_safe_url_and_environment_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("NZ_TEST_MCP_AUTH", "Bearer secret-value")
    configs = load_mcp_server_configs(
        {
            "remote": {
                "type": "remote",
                "url": "https://mcp.example.test/api",
                "headers": {"X-Client": "nz-coder"},
                "header_env": {"Authorization": "NZ_TEST_MCP_AUTH"},
                "tool_effects": {"lookup": "read"},
            }
        },
        workspace=tmp_path,
    )

    assert configs[0].transport == "streamable_http"
    assert configs[0].command == ()
    assert configs[0].resolved_headers() == {
        "Authorization": "Bearer secret-value",
        "X-Client": "nz-coder",
    }


def test_remote_config_rejects_control_characters_from_header_environment(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("NZ_TEST_MCP_AUTH", "Bearer secret\nInjected: value")
    remote = load_mcp_server_configs(
        {
            "remote": {
                "url": "https://mcp.example.test/api",
                "header_env": {"Authorization": "NZ_TEST_MCP_AUTH"},
            }
        },
        workspace=tmp_path,
    )[0]
    with pytest.raises(ValueError, match="invalid header value") as captured:
        remote.resolved_headers()
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize(
    "remote, expected",
    [
        ({"url": "http://example.test/mcp"}, "loopback"),
        ({"url": "http://127.0.0.1/mcp"}, "loopback"),
        ({"url": "https://user:pass@example.test/mcp"}, "credentials"),
        ({"url": "https://example.test/mcp#token"}, "fragment"),
        ({"url": "https://example.test:bad/mcp"}, "invalid port"),
        ({"url": "https://example.test/mcp", "cwd": "."}, "cannot include cwd"),
        (
            {"url": "https://example.test/mcp", "headers": {"Authorization": "secret"}},
            "header_env",
        ),
        (
            {"url": "https://example.test/mcp", "headers": {"X-API-Key": "secret"}},
            "header_env",
        ),
        (
            {"url": "https://example.test/mcp", "headers": {"Cookie": "secret"}},
            "header_env",
        ),
        (
            {"url": "https://example.test/mcp", "headers": {"X-Client": "bad\tvalue"}},
            "invalid header value",
        ),
        (
            {"url": "https://example.test/mcp", "headers": {"X-Client": "snowman-☃"}},
            "invalid header value",
        ),
        (
            {"url": "https://example.test/mcp", "headers": {"Host": "evil"}},
            "cannot override",
        ),
    ],
)
def test_remote_config_rejects_unsafe_transport_values(tmp_path, remote, expected):
    with pytest.raises(ValueError, match=expected):
        load_mcp_server_configs({"remote": remote}, workspace=tmp_path)


def test_remote_config_allows_explicit_loopback_http_for_development(tmp_path):
    config = load_mcp_server_configs(
        {
            "remote": {
                "url": "http://127.0.0.1:8765/mcp",
                "allow_insecure_http": True,
            }
        },
        workspace=tmp_path,
    )[0]
    assert config.url == "http://127.0.0.1:8765/mcp"


def test_http_client_supports_sse_responses_and_reuses_session_header():
    with _http_mcp_server(sse_methods={"tools/list"}) as (server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            headers={"Authorization": "Bearer test"},
            startup_timeout_seconds=2,
            tool_timeout_seconds=2,
        )
        try:
            result = client.start()
            tools = client.list_tools()
        finally:
            client.close()

    assert result["serverInfo"]["name"] == "http-fixture"
    assert [tool["name"] for tool in tools] == ["echo"]
    tools_headers = next(
        headers for message, headers in server.messages if message.get("method") == "tools/list"
    )
    assert tools_headers["Mcp-Session-Id"] == "fixture-session"
    assert tools_headers["Mcp-Protocol-Version"] == "2025-06-18"
    assert tools_headers["Authorization"] == "Bearer test"
    assert server.delete_headers[0]["Mcp-Session-Id"] == "fixture-session"


def test_http_client_returns_before_open_post_sse_stream_reaches_eof():
    with _http_mcp_server(open_sse_methods={"tools/list"}) as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=2,
            tool_timeout_seconds=2,
        )
        try:
            client.start()
            started = time.monotonic()
            assert [item["name"] for item in client.list_tools()] == ["echo"]
            assert time.monotonic() - started < 0.4
        finally:
            client.close()


def test_http_client_delivers_get_stream_notifications_with_early_replay():
    notification = {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
        "params": {"reason": "fixture"},
    }
    received = []
    ready = threading.Event()
    with _http_mcp_server(get_event=notification) as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=2,
            tool_timeout_seconds=2,
        )
        try:
            client.start()
            # The finite fixture stream may deliver before registration. The
            # client must retain and replay that list-change edge.
            time.sleep(0.05)
            client.set_notification_handler(
                "notifications/tools/list_changed",
                lambda params: (received.append(params), ready.set()),
            )
            assert ready.wait(timeout=1)
        finally:
            client.close()
    assert received == [{"reason": "fixture"}]


def test_http_client_dispatches_batched_get_stream_notifications():
    notifications = [
        {
            "jsonrpc": "2.0",
            "method": "notifications/tools/list_changed",
            "params": {"batch": 1},
        },
        {
            "jsonrpc": "2.0",
            "method": "notifications/resources/list_changed",
            "params": {"batch": 2},
        },
    ]
    received = []
    ready = threading.Event()
    with _http_mcp_server(get_event=notifications) as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=2,
            tool_timeout_seconds=2,
        )
        try:
            client.start()
            time.sleep(0.05)
            client.set_notification_handler(
                "notifications/tools/list_changed",
                lambda params: received.append(params),
            )
            client.set_notification_handler(
                "notifications/resources/list_changed",
                lambda params: (received.append(params), ready.set()),
            )
            assert ready.wait(timeout=1)
        finally:
            client.close()
    assert received == [{"batch": 1}, {"batch": 2}]


@pytest.mark.parametrize("session_id", ["bad\tid", "bad id", "x" * 1025])
def test_http_client_rejects_invalid_session_id(session_id):
    with _http_mcp_server(session_id=session_id) as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=1,
            tool_timeout_seconds=1,
        )
        with pytest.raises(Exception, match="invalid session ID"):
            client.start()


def test_http_client_rejects_redirects_without_forwarding_session():
    with _http_mcp_server(redirect_url="http://127.0.0.1:1/stolen") as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            headers={"Authorization": "Bearer test"},
            startup_timeout_seconds=1,
            tool_timeout_seconds=1,
        )
        with pytest.raises(Exception, match="status 307"):
            client.start()


def test_http_client_ignores_ambient_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    with _http_mcp_server() as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=1,
            tool_timeout_seconds=1,
        )
        try:
            assert client.start()["serverInfo"]["name"] == "http-fixture"
        finally:
            client.close()


def test_http_client_does_not_treat_get_auth_failure_as_optional():
    with _http_mcp_server(get_status=401) as (_server, url):
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=1,
            tool_timeout_seconds=1,
        )
        try:
            client.start()
            for _ in range(100):
                if client._transport_error is not None:
                    break
                time.sleep(0.01)
            with pytest.raises(Exception, match="requires authentication"):
                client.list_tools()
        finally:
            client.close()


def test_http_runtime_retires_binding_when_get_stream_fails(tmp_path):
    with _http_mcp_server(get_status=401) as (_server, url):
        remote = load_mcp_server_configs(
            {
                "remote": {
                    "url": url,
                    "allow_insecure_http": True,
                    "startup_timeout_seconds": 1,
                    "tool_timeout_seconds": 1,
                }
            },
            workspace=tmp_path,
        )[0]
        runtime = MCPRuntime([remote]).start()
        try:
            for _ in range(100):
                if runtime.status_summary()[0]["status"] == "needs_auth":
                    break
                time.sleep(0.01)
            assert runtime.status_summary()[0]["status"] == "needs_auth"
            assert runtime.tool_bindings() == []
        finally:
            runtime.close()


def test_http_client_rejects_pre_streamable_protocol_negotiation():
    with _http_mcp_server() as (server, url):
        server.protocol_version = "2024-11-05"
        client = MCPHTTPClient(
            name="remote",
            url=url,
            startup_timeout_seconds=1,
            tool_timeout_seconds=1,
        )
        with pytest.raises(Exception, match="unsupported protocol version"):
            client.start()


def test_http_runtime_closes_tools_prompts_resources_and_session(tmp_path):
    with _http_mcp_server() as (server, url):
        remote = load_mcp_server_configs(
            {
                "remote": {
                    "url": url,
                    "allow_insecure_http": True,
                    "tool_effects": {"echo": "read"},
                    "startup_timeout_seconds": 2,
                    "tool_timeout_seconds": 2,
                }
            },
            workspace=tmp_path,
        )[0]
        runtime = MCPRuntime([remote]).start()
        try:
            bindings = runtime.tool_bindings()
            assert runtime.status_summary() == [
                {"name": "remote", "status": "connected", "tool_count": 1, "error": ""}
            ]
            assert bindings[0]["name"] == "mcp_remote_echo"
            assert "remote:hello" in bindings[0]["handler"](value="hello")
            assert runtime.prompt_definitions()[0]["name"] == "review"
            assert runtime.get_prompt("remote", "review")["description"] == "review"
            assert runtime.resource_definitions()[0]["uri"] == "memo://one"
            assert runtime.read_resource("remote", "memo://one")["contents"][0]["text"] == "remote resource"
        finally:
            runtime.close()

    assert server.delete_headers
