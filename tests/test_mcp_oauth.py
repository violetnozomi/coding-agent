"""End-to-end and security tests for the MCP OAuth authorization lifecycle."""
from __future__ import annotations

import http.client
import json
import os
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from nz_coder.mcp import MCPRuntime, load_mcp_server_configs
from nz_coder.mcp.auth_store import MCPOAuthStore
from nz_coder.mcp.cli import mcp_main
from nz_coder.mcp.oauth import MCPOAuthManager


class _OAuthMCPHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        origin = self.server.origin
        if self.path.startswith("/.well-known/oauth-protected-resource"):
            self._json(
                200,
                {
                    "resource": origin + "/mcp",
                    "authorization_servers": [origin],
                },
            )
            return
        if self.path == "/.well-known/oauth-authorization-server":
            self._json(
                200,
                {
                    "issuer": origin,
                    "authorization_endpoint": self.server.authorization_endpoint,
                    "token_endpoint": origin + "/token",
                    "registration_endpoint": origin + "/register",
                },
            )
            return
        if self.path.startswith("/mcp"):
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        if self.path == "/register":
            self.server.registrations.append(json.loads(payload.decode("utf-8")))
            self._json(
                201,
                {
                    "client_id": "dynamic-client",
                    "client_secret": "dynamic-secret",
                    "token_endpoint_auth_method": "client_secret_post",
                },
            )
            return
        if self.path == "/token":
            form = parse_qs(payload.decode("utf-8"), keep_blank_values=True)
            self.server.token_forms.append(form)
            grant = form.get("grant_type", [""])[0]
            suffix = "refreshed" if grant == "refresh_token" else "initial"
            self._json(
                200,
                {
                    "access_token": f"access-{suffix}",
                    "token_type": "Bearer",
                    "refresh_token": "refresh-token",
                    "expires_in": 3600,
                    "scope": "tools.read",
                },
            )
            return
        if self.path == "/mcp":
            if self.server.reject_mcp_auth:
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            message = json.loads(payload.decode("utf-8"))
            method = message.get("method")
            self.server.mcp_headers.append(dict(self.headers))
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            results = {
                "initialize": {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "oauth-fixture", "version": "1"},
                    "capabilities": {"tools": {}},
                },
                "tools/list": {"tools": []},
            }
            if method in results:
                self._json(
                    200,
                    {"jsonrpc": "2.0", "id": message["id"], "result": results[method]},
                    session=method == "initialize",
                )
            else:
                self._json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": message["id"],
                        "error": {"code": -32601, "message": "not supported"},
                    },
                )
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_DELETE(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status, value, *, session=False):
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if session:
            self.send_header("Mcp-Session-Id", "oauth-session")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


@contextmanager
def _oauth_mcp_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _OAuthMCPHandler)
    server.origin = f"http://127.0.0.1:{server.server_port}"
    server.registrations = []
    server.token_forms = []
    server.mcp_headers = []
    server.reject_mcp_auth = False
    server.authorization_endpoint = server.origin + "/authorize"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _remote(tmp_path: Path, origin: str, callback_port: int):
    return load_mcp_server_configs(
        {
            "remote": {
                "url": origin + "/mcp",
                "allow_insecure_http": True,
                "oauth": {
                    "scope": "tools.read",
                    "redirect_uri": (
                        f"http://127.0.0.1:{callback_port}/mcp/oauth/callback"
                    ),
                },
                "startup_timeout_seconds": 2,
                "tool_timeout_seconds": 2,
            }
        },
        workspace=tmp_path,
    )[0]


def _send_callback(pending, *, state=None, code="fixture-code"):
    redirect = urlsplit(pending.server_config.oauth.redirect_uri)
    connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=2)
    query = f"code={code}&state={state or pending.state}"
    connection.request("GET", f"{redirect.path}?{query}")
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def _send_error_callback(pending, description: str):
    redirect = urlsplit(pending.server_config.oauth.redirect_uri)
    connection = http.client.HTTPConnection(redirect.hostname, redirect.port, timeout=2)
    query = urlencode(
        {
            "error": "access_denied",
            "error_description": description,
            "state": pending.state,
        }
    )
    connection.request("GET", f"{redirect.path}?{query}")
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def test_oauth_dynamic_registration_pkce_callback_runtime_and_refresh(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        pending = manager.begin_auth(remote)
        authorization = urlsplit(pending.authorization_url)
        query = parse_qs(authorization.query)
        assert authorization.path == "/authorize"
        assert query["client_id"] == ["dynamic-client"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["resource"] == [remote.url]
        assert "code_verifier" not in query

        with ThreadPoolExecutor(max_workers=1) as executor:
            completed = executor.submit(pending.finish, 2)
            assert _send_callback(pending) == 200
            tokens = completed.result(timeout=3)

        assert tokens["access_token"] == "access-initial"
        assert fixture.registrations[0]["redirect_uris"] == [
            remote.oauth.redirect_uri
        ]
        authorization_form = fixture.token_forms[0]
        assert authorization_form["grant_type"] == ["authorization_code"]
        assert authorization_form["code"] == ["fixture-code"]
        assert len(authorization_form["code_verifier"][0]) >= 43
        assert authorization_form["client_secret"] == ["dynamic-secret"]

        runtime = MCPRuntime([remote], oauth_manager=manager).start()
        try:
            assert runtime.status_summary()[0]["status"] == "connected"
        finally:
            runtime.close()
        initialize_headers = next(
            headers
            for headers in fixture.mcp_headers
            if headers.get("Mcp-Protocol-Version") == "2025-06-18"
        )
        assert initialize_headers["Authorization"] == "Bearer access-initial"

        entry = store.get(remote.name, remote.url)
        expired = dict(entry["tokens"])
        expired["expires_at"] = time.time() - 1
        store.set_fields(remote.name, remote.url, tokens=expired)
        assert manager.authorization_header(remote) == "Bearer access-refreshed"
        assert fixture.token_forms[-1]["grant_type"] == ["refresh_token"]

    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_oauth_callback_rejects_wrong_state_without_consuming_flow(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        pending = manager.begin_auth(remote)
        try:
            assert _send_callback(pending, state="wrong-state") == 400
            with ThreadPoolExecutor(max_workers=1) as executor:
                completed = executor.submit(pending.finish, 2)
                assert _send_callback(pending) == 200
                completed.result(timeout=3)
        finally:
            pending.close()


def test_oauth_callback_sanitizes_authorization_server_terminal_controls(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        pending = manager.begin_auth(remote)
        with ThreadPoolExecutor(max_workers=1) as executor:
            completed = executor.submit(pending.finish, 2)
            assert _send_error_callback(pending, "\x1b[31mdenied\r\nnext") == 200
            with pytest.raises(Exception) as captured:
                completed.result(timeout=3)
        assert "\x1b" not in str(captured.value)
        assert "\r" not in str(captured.value)
        assert "\n" not in str(captured.value)


def test_oauth_discovery_rejects_terminal_controls_in_authorization_url(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        fixture.authorization_endpoint = fixture.origin + "/authorize/\x1b[31mred"
        remote = _remote(tmp_path, fixture.origin, _free_port())
        with pytest.raises(Exception, match="endpoint URL is not allowed"):
            manager.begin_auth(remote)


def test_oauth_cancel_wakes_pending_waiter(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        pending = manager.begin_auth(remote)
        with ThreadPoolExecutor(max_workers=1) as executor:
            completed = executor.submit(pending.finish, 5)
            pending.close()
            with pytest.raises(Exception, match="cancelled"):
                completed.result(timeout=1)
        assert pending.verifier == ""
        assert pending._code == ""


def test_runtime_rejected_token_is_invalidated_and_reports_needs_auth(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        store.set_fields(
            remote.name,
            remote.url,
            tokens={"access_token": "rejected-token", "expires_at": time.time() + 3600},
            client={"client_id": "keep-client"},
        )
        fixture.reject_mcp_auth = True
        runtime = MCPRuntime([remote], oauth_manager=manager).start()
        try:
            assert runtime.status_summary()[0]["status"] == "needs_auth"
        finally:
            runtime.close()
        entry = store.get(remote.name, remote.url)
        assert "tokens" not in entry
        assert entry["client"]["client_id"] == "keep-client"


def test_mid_session_tool_401_retires_binding_and_invalidates_token(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        store.set_fields(
            remote.name,
            remote.url,
            tokens={"access_token": "later-rejected", "expires_at": time.time() + 3600},
        )
        runtime = MCPRuntime([remote], oauth_manager=manager).start()
        try:
            # The fixture initially exposes no tools, so call the protocol
            # client directly to exercise a post-start request generation.
            client = runtime.clients[remote.name]
            fixture.reject_mcp_auth = True
            with pytest.raises(Exception, match="requires authentication"):
                client.list_tools()
            assert runtime.status_summary()[0]["status"] == "needs_auth"
            assert remote.name not in runtime.clients
            assert runtime.tool_bindings() == []
            assert store.status(remote.name, remote.url) == "not_authenticated"
        finally:
            runtime.close()


def test_stale_client_401_does_not_delete_newer_token(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    manager = MCPOAuthManager(store)
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        store.set_fields(
            remote.name,
            remote.url,
            tokens={"access_token": "old-token", "expires_at": time.time() + 3600},
        )
        runtime = MCPRuntime([remote], oauth_manager=manager).start()
        try:
            manager.save_tokens(
                remote,
                {"access_token": "new-token", "expires_at": time.time() + 3600},
            )
            fixture.reject_mcp_auth = True
            with pytest.raises(Exception, match="requires authentication"):
                runtime.clients[remote.name].list_tools()
            assert store.get(remote.name, remote.url)["tokens"]["access_token"] == "new-token"
        finally:
            runtime.close()


def test_oauth_store_binds_credentials_to_exact_server_url(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    store.set_fields(
        "remote",
        "https://one.example/mcp",
        tokens={"access_token": "secret"},
    )
    assert store.get("remote", "https://one.example/mcp")["tokens"]["access_token"] == "secret"
    assert store.get("remote", "https://two.example/mcp") is None
    os.chmod(store.path, 0o700)
    with pytest.raises(Exception, match="0600"):
        store.get("remote", "https://one.example/mcp")
    os.chmod(store.path, 0o644)
    with pytest.raises(Exception, match="0600"):
        store.get("remote", "https://one.example/mcp")
    os.chmod(store.path, 0o600)
    os.chmod(store.path.parent, 0o755)
    with pytest.raises(Exception, match="0700"):
        store.get("remote", "https://one.example/mcp")


def test_oauth_refresh_is_single_flight_across_managers(tmp_path):
    store = MCPOAuthStore(tmp_path / "credentials" / "mcp-auth.json")
    with _oauth_mcp_server() as fixture:
        remote = _remote(tmp_path, fixture.origin, _free_port())
        store.set_fields(
            remote.name,
            remote.url,
            tokens={
                "access_token": "expired",
                "refresh_token": "refresh-token",
                "expires_at": time.time() - 1,
            },
            client={"client_id": "dynamic-client", "client_secret": "dynamic-secret"},
        )
        managers = [MCPOAuthManager(store) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            headers = list(
                executor.map(lambda manager: manager.authorization_header(remote), managers)
            )
        assert headers == ["Bearer access-refreshed"] * 8
        refreshes = [
            form
            for form in fixture.token_forms
            if form.get("grant_type") == ["refresh_token"]
        ]
        assert len(refreshes) == 1


def test_oauth_store_rejects_symlink_path(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"version":1,"servers":{}}', encoding="utf-8")
    os.chmod(target, 0o600)
    link = tmp_path / "credentials.json"
    link.symlink_to(target)
    with pytest.raises(Exception, match="regular file"):
        MCPOAuthStore(link).get("remote", "https://example.test/mcp")
    target.unlink()
    with pytest.raises(Exception, match="regular file"):
        MCPOAuthStore(link).get("remote", "https://example.test/mcp")


def test_oauth_config_rejects_inline_secret_and_non_loopback_callback(tmp_path):
    with pytest.raises(ValueError, match="unknown field"):
        load_mcp_server_configs(
            {
                "remote": {
                    "url": "https://example.test/mcp",
                    "oauth": {"client_secret": "inline-secret"},
                }
            },
            workspace=tmp_path,
        )
    with pytest.raises(ValueError, match="127.0.0.1"):
        load_mcp_server_configs(
            {
                "remote": {
                    "url": "https://example.test/mcp",
                    "oauth": {"redirect_uri": "https://attacker.test/callback"},
                }
            },
            workspace=tmp_path,
        )


def test_mcp_cli_status_and_logout_never_print_tokens(tmp_path, monkeypatch, capsys):
    store_path = tmp_path / "credentials" / "mcp-auth.json"
    monkeypatch.setenv("NZ_MCP_AUTH_STORE", str(store_path))
    monkeypatch.setattr(
        "nz_coder.config.MCP_SERVERS_JSON",
        json.dumps(
            {
                "remote": {
                    "url": "https://example.test/mcp",
                    "oauth": {"client_id": "client"},
                }
            }
        ),
    )
    store = MCPOAuthStore(store_path)
    store.set_fields(
        "remote",
        "https://example.test/mcp",
        tokens={"access_token": "never-print-this"},
    )
    assert mcp_main(["status", "remote"]) == 0
    assert mcp_main(["logout", "remote"]) == 0
    output = capsys.readouterr().out
    assert "authenticated" in output
    assert "removed" in output
    assert "never-print-this" not in output
