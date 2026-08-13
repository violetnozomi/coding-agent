"""OAuth 2.1 authorization-code/PKCE lifecycle for remote MCP servers."""
from __future__ import annotations

import base64
import hashlib
import html
import json
import math
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nz_coder.mcp.auth_store import MCPOAuthStore
from nz_coder.mcp.client import MCPError
from nz_coder.mcp.config import MCPOAuthConfig, MCPServerConfig

_MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024
_REFRESH_LOCKS_GUARD = threading.Lock()
_REFRESH_LOCKS: dict[str, threading.RLock] = {}


class MCPOAuthError(MCPError):
    """OAuth discovery, callback, registration, or token error."""


class MCPAuthenticationRequired(MCPOAuthError):
    """Raised when a remote server requires an interactive authorization."""

    def __init__(self, message: str, *, rejected_authorization: str = ""):
        super().__init__(message)
        self.rejected_authorization = rejected_authorization


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class OAuthMetadata:
    """Validated endpoints needed by the authorization-code flow."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str = ""


class _CallbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class _CallbackHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        pending = self.server.pending
        parsed = urlsplit(self.path)
        if parsed.path != pending.callback_path:
            self._reply(404, "Authorization callback not found")
            return
        values = parse_qs(parsed.query, keep_blank_values=True)
        states = values.get("state", [])
        if len(states) != 1 or not secrets.compare_digest(states[0], pending.state):
            self._reply(400, "Invalid or expired OAuth state")
            return
        errors = values.get("error", [])
        if errors:
            description = values.get("error_description", errors)
            pending._resolve(error=_safe_display_text(str(description[0]), 500))
            self._reply(200, "Authorization failed")
            return
        codes = values.get("code", [])
        if len(codes) != 1 or not codes[0] or len(codes[0]) > 8192:
            self._reply(400, "Missing or invalid authorization code")
            return
        pending._resolve(code=codes[0])
        self._reply(200, "Authorization successful. You may close this window.")

    def _reply(self, status: int, message: str) -> None:
        body = (
            "<!doctype html><html><body><p>"
            + html.escape(message)
            + "</p></body></html>"
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        return


class PendingOAuth:
    """One in-memory PKCE flow and its exact loopback callback owner."""

    def __init__(
        self,
        *,
        manager: "MCPOAuthManager",
        server_config: MCPServerConfig,
        metadata: OAuthMetadata,
        client: dict[str, Any],
        verifier: str,
        state: str,
        authorization_url: str,
        callback_server: _CallbackServer,
        callback_path: str,
    ):
        self.manager = manager
        self.server_config = server_config
        self.metadata = metadata
        self.client = client
        self.verifier = verifier
        self.state = state
        self.authorization_url = authorization_url
        self.callback_path = callback_path
        self._server = callback_server
        self._event = threading.Event()
        self._code = ""
        self._error = ""
        self._closed = False
        self._lock = threading.Lock()
        self._server.pending = self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"mcp-oauth-{server_config.name}",
            daemon=True,
        )
        self._thread.start()

    def open_browser(self) -> bool:
        """Best-effort browser launch; the URL is always available to print."""
        try:
            return bool(webbrowser.open(self.authorization_url, new=1))
        except Exception:
            return False

    def finish(self, timeout: float = 300.0) -> dict[str, Any]:
        """Wait for the exact state callback, exchange its code, and persist tokens."""
        try:
            if not self._event.wait(timeout=max(0.001, float(timeout))):
                raise MCPOAuthError("OAuth callback timed out")
            if self._error:
                raise MCPOAuthError(f"OAuth authorization failed: {self._error}")
            if not self._code:
                raise MCPOAuthError("OAuth callback did not provide a code")
            tokens = self.manager.exchange_code(
                self.server_config,
                self.metadata,
                self.client,
                code=self._code,
                verifier=self.verifier,
            )
            self.manager.save_tokens(self.server_config, tokens)
            return tokens
        finally:
            self.verifier = ""
            self._code = ""
            self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if not self._event.is_set():
                self._error = "Authorization cancelled"
                self._event.set()
            self.verifier = ""
            self._code = ""
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _resolve(self, *, code: str = "", error: str = "") -> None:
        with self._lock:
            if self._event.is_set() or self._closed:
                return
            self._code = code
            self._error = error
            self._event.set()


class MCPOAuthManager:
    """Discover, authorize, refresh, and remove URL-bound MCP credentials."""

    def __init__(self, store: MCPOAuthStore | None = None):
        self.store = store or MCPOAuthStore()
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def begin_auth(self, server: MCPServerConfig) -> PendingOAuth:
        oauth = self._oauth_config(server)
        metadata = self.discover(server)
        client = self._client_information(server, oauth, metadata)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        state = secrets.token_urlsafe(32)
        redirect = urlsplit(oauth.redirect_uri)
        try:
            callback_server = _CallbackServer(
                ("127.0.0.1", int(redirect.port or 0)),
                _CallbackHandler,
            )
        except OSError as exc:
            raise MCPOAuthError("Unable to bind the OAuth loopback callback") from exc
        query = {
            "response_type": "code",
            "client_id": client["client_id"],
            "redirect_uri": oauth.redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": server.url,
        }
        if oauth.scope:
            query["scope"] = oauth.scope
        authorization_url = _append_query(metadata.authorization_endpoint, query)
        return PendingOAuth(
            manager=self,
            server_config=server,
            metadata=metadata,
            client=client,
            verifier=verifier,
            state=state,
            authorization_url=authorization_url,
            callback_server=callback_server,
            callback_path=redirect.path,
        )

    def authenticate(
        self,
        server: MCPServerConfig,
        *,
        timeout: float = 300.0,
        launch_browser: bool = True,
        on_url=None,
    ) -> dict[str, Any]:
        pending = self.begin_auth(server)
        if on_url is not None:
            on_url(pending.authorization_url)
        if launch_browser:
            pending.open_browser()
        return pending.finish(timeout=timeout)

    def authorization_header(self, server: MCPServerConfig) -> str:
        if server.oauth is None:
            return ""
        lock = _refresh_lock(self.store.path, server.name, server.url)
        with lock:
            # Re-read inside the shared lock. Another Agent may already have
            # rotated and persisted this server's refresh token.
            entry = self.store.get(server.name, server.url)
            tokens = entry.get("tokens") if entry else None
            if not isinstance(tokens, dict):
                return ""
            expires_at = tokens.get("expires_at")
            if isinstance(expires_at, (int, float)) and expires_at <= time.time() + 30:
                refresh_token = tokens.get("refresh_token")
                if not isinstance(refresh_token, str) or not refresh_token:
                    return ""
                try:
                    tokens = self.refresh(server, refresh_token=refresh_token)
                except MCPOAuthError as exc:
                    raise MCPAuthenticationRequired(
                        "Stored OAuth credentials could not be refreshed"
                    ) from exc
            access_token = tokens.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                return ""
            return f"Bearer {access_token}"

    def refresh(self, server: MCPServerConfig, *, refresh_token: str) -> dict[str, Any]:
        with _refresh_lock(self.store.path, server.name, server.url):
            oauth = self._oauth_config(server)
            metadata = self.discover(server)
            entry = self.store.get(server.name, server.url) or {}
            client = self._configured_or_stored_client(oauth, entry)
            if not client:
                raise MCPAuthenticationRequired("OAuth client registration is unavailable")
            form = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client["client_id"],
                "resource": server.url,
            }
            self._apply_client_auth(form, client)
            value = self._json_request(
                metadata.token_endpoint,
                server=server,
                method="POST",
                form=form,
            )
            tokens = self._normalize_tokens(value, previous_refresh=refresh_token)
            self.store.set_fields(server.name, server.url, tokens=tokens)
            return tokens

    def exchange_code(
        self,
        server: MCPServerConfig,
        metadata: OAuthMetadata,
        client: dict[str, Any],
        *,
        code: str,
        verifier: str,
    ) -> dict[str, Any]:
        oauth = self._oauth_config(server)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": oauth.redirect_uri,
            "client_id": client["client_id"],
            "code_verifier": verifier,
            "resource": server.url,
        }
        self._apply_client_auth(form, client)
        value = self._json_request(
            metadata.token_endpoint,
            server=server,
            method="POST",
            form=form,
        )
        return self._normalize_tokens(value)

    def discover(self, server: MCPServerConfig) -> OAuthMetadata:
        oauth = self._oauth_config(server)
        issuer = oauth.authorization_server
        if not issuer:
            protected = None
            for candidate in _protected_resource_candidates(server.url):
                try:
                    protected = self._json_request(candidate, server=server)
                    break
                except MCPOAuthError:
                    continue
            if isinstance(protected, dict):
                resource = protected.get("resource")
                if resource is not None and resource != server.url:
                    raise MCPOAuthError("OAuth protected-resource metadata URL mismatch")
                servers = protected.get("authorization_servers")
                if isinstance(servers, list) and servers and isinstance(servers[0], str):
                    issuer = servers[0]
            if not issuer:
                parsed = urlsplit(server.url)
                issuer = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        metadata_url = _authorization_metadata_url(issuer)
        value = self._json_request(metadata_url, server=server)
        if value.get("issuer") != issuer:
            raise MCPOAuthError("OAuth authorization-server issuer mismatch")
        authorization_endpoint = value.get("authorization_endpoint")
        token_endpoint = value.get("token_endpoint")
        registration_endpoint = value.get("registration_endpoint", "")
        for endpoint in (authorization_endpoint, token_endpoint):
            if not isinstance(endpoint, str) or not endpoint:
                raise MCPOAuthError("OAuth metadata is missing required endpoints")
            self._validate_endpoint(endpoint, server)
        if registration_endpoint:
            if not isinstance(registration_endpoint, str):
                raise MCPOAuthError("OAuth registration endpoint is invalid")
            self._validate_endpoint(registration_endpoint, server)
        return OAuthMetadata(
            issuer=issuer,
            authorization_endpoint=authorization_endpoint,
            token_endpoint=token_endpoint,
            registration_endpoint=registration_endpoint,
        )

    def remove(self, server_name: str) -> bool:
        return self.store.remove(server_name)

    def save_tokens(self, server: MCPServerConfig, tokens: dict[str, Any]) -> None:
        with _refresh_lock(self.store.path, server.name, server.url):
            self.store.set_fields(server.name, server.url, tokens=tokens)

    def invalidate_tokens(
        self,
        server: MCPServerConfig,
        *,
        rejected_authorization: str,
    ) -> None:
        """Drop only the exact token rejected by a stale client generation."""
        if not rejected_authorization.startswith("Bearer "):
            return
        rejected = rejected_authorization[7:]
        with _refresh_lock(self.store.path, server.name, server.url):
            entry = self.store.get(server.name, server.url)
            tokens = entry.get("tokens") if entry else None
            current = tokens.get("access_token") if isinstance(tokens, dict) else None
            if isinstance(current, str) and secrets.compare_digest(current, rejected):
                self.store.set_fields(server.name, server.url, tokens=None)

    def status(self, server: MCPServerConfig) -> str:
        return self.store.status(server.name, server.url)

    def _client_information(
        self,
        server: MCPServerConfig,
        oauth: MCPOAuthConfig,
        metadata: OAuthMetadata,
    ) -> dict[str, Any]:
        entry = self.store.get(server.name, server.url) or {}
        configured = self._configured_or_stored_client(oauth, entry)
        if configured:
            return configured
        if not metadata.registration_endpoint:
            raise MCPAuthenticationRequired(
                "OAuth server requires a pre-registered client_id"
            )
        registration = self._json_request(
            metadata.registration_endpoint,
            server=server,
            method="POST",
            json_body={
                "redirect_uris": [oauth.redirect_uri],
                "client_name": "nz-coder",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        client_id = registration.get("client_id")
        if not isinstance(client_id, str) or not client_id:
            raise MCPOAuthError("OAuth dynamic registration returned no client_id")
        auth_method = registration.get("token_endpoint_auth_method", "none")
        if auth_method not in {"none", "client_secret_post"}:
            raise MCPOAuthError("OAuth server selected an unsupported client auth method")
        client = {"client_id": client_id, "auth_method": auth_method}
        secret = registration.get("client_secret")
        if isinstance(secret, str) and secret:
            client["client_secret"] = secret
        if auth_method == "client_secret_post" and not client.get("client_secret"):
            raise MCPOAuthError("OAuth client_secret_post registration returned no secret")
        expires_at = registration.get("client_secret_expires_at")
        if isinstance(expires_at, (int, float)):
            client["client_secret_expires_at"] = float(expires_at)
        self.store.set_fields(server.name, server.url, client=client)
        return client

    @staticmethod
    def _configured_or_stored_client(
        oauth: MCPOAuthConfig,
        entry: dict[str, Any],
    ) -> dict[str, Any]:
        if oauth.client_id:
            result = {"client_id": oauth.client_id, "auth_method": "none"}
            secret = oauth.resolved_client_secret()
            if secret:
                result["client_secret"] = secret
                result["auth_method"] = "client_secret_post"
            return result
        client = entry.get("client")
        if not isinstance(client, dict) or not isinstance(client.get("client_id"), str):
            return {}
        expires_at = client.get("client_secret_expires_at")
        if (
            isinstance(expires_at, (int, float))
            and expires_at > 0
            and expires_at <= time.time()
        ):
            return {}
        return dict(client)

    @staticmethod
    def _apply_client_auth(form: dict[str, str], client: dict[str, Any]) -> None:
        method = client.get("auth_method")
        if method is None:
            method = "client_secret_post" if client.get("client_secret") else "none"
        if method == "none":
            return
        if method != "client_secret_post":
            raise MCPOAuthError("OAuth client uses an unsupported auth method")
        secret = client.get("client_secret")
        if not isinstance(secret, str) or not secret:
            raise MCPOAuthError("OAuth client_secret_post requires a client secret")
        form["client_secret"] = secret

    @staticmethod
    def _normalize_tokens(
        value: dict[str, Any],
        *,
        previous_refresh: str = "",
    ) -> dict[str, Any]:
        access_token = value.get("access_token")
        token_type = value.get("token_type", "Bearer")
        if not isinstance(access_token, str) or not access_token:
            raise MCPOAuthError("OAuth token response contains no access_token")
        if not isinstance(token_type, str) or token_type.lower() != "bearer":
            raise MCPOAuthError("OAuth token response uses an unsupported token type")
        result: dict[str, Any] = {"access_token": access_token}
        refresh_token = value.get("refresh_token", previous_refresh)
        if isinstance(refresh_token, str) and refresh_token:
            result["refresh_token"] = refresh_token
        expires_in = value.get("expires_in")
        if expires_in is not None:
            if (
                isinstance(expires_in, bool)
                or not isinstance(expires_in, (int, float))
                or not math.isfinite(float(expires_in))
                or float(expires_in) <= 0
            ):
                raise MCPOAuthError("OAuth token response has invalid expires_in")
            result["expires_at"] = time.time() + float(expires_in)
        scope = value.get("scope")
        if isinstance(scope, str) and scope:
            result["scope"] = scope
        return result

    def _json_request(
        self,
        url: str,
        *,
        server: MCPServerConfig,
        method: str = "GET",
        form: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_endpoint(url, server)
        data = None
        headers = {"Accept": "application/json"}
        if form is not None:
            data = urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        try:
            response = self._opener.open(
                request,
                timeout=max(0.001, server.startup_timeout_seconds),
            )
            try:
                payload = response.read(_MAX_OAUTH_RESPONSE_BYTES + 1)
                content_type = response.headers.get_content_type().lower()
            finally:
                response.close()
        except HTTPError as exc:
            raise MCPOAuthError(f"OAuth endpoint returned status {exc.code}") from exc
        except (OSError, URLError) as exc:
            raise MCPOAuthError(
                f"OAuth endpoint request failed: {type(exc).__name__}"
            ) from exc
        if len(payload) > _MAX_OAUTH_RESPONSE_BYTES:
            raise MCPOAuthError("OAuth endpoint response exceeds 1 MiB")
        if content_type != "application/json":
            raise MCPOAuthError("OAuth endpoint returned a non-JSON response")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPOAuthError("OAuth endpoint returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise MCPOAuthError("OAuth endpoint response must be an object")
        return value

    @staticmethod
    def _validate_endpoint(url: str, server: MCPServerConfig) -> None:
        parsed = urlsplit(url)
        loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (
            not url
            or any(ord(character) < 0x21 or ord(character) > 0x7E for character in url)
            or not parsed.hostname
            or parsed.username is not None
            or parsed.fragment
            or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and not (server.allow_insecure_http and loopback))
        ):
            raise MCPOAuthError("OAuth endpoint URL is not allowed")

    @staticmethod
    def _oauth_config(server: MCPServerConfig) -> MCPOAuthConfig:
        if server.transport != "streamable_http" or server.oauth is None:
            raise MCPOAuthError(f"MCP server '{server.name}' does not enable OAuth")
        return server.oauth


def _protected_resource_candidates(server_url: str) -> list[str]:
    parsed = urlsplit(server_url)
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    candidates = [origin + "/.well-known/oauth-protected-resource" + path]
    base = origin + "/.well-known/oauth-protected-resource"
    if base not in candidates:
        candidates.append(base)
    return candidates


def _authorization_metadata_url(issuer: str) -> str:
    parsed = urlsplit(issuer)
    suffix = parsed.path.rstrip("/")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            "/.well-known/oauth-authorization-server" + suffix,
            "",
            "",
        )
    )


def _append_query(url: str, values: dict[str, str]) -> str:
    parsed = urlsplit(url)
    query = parsed.query
    encoded = urlencode(values)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            f"{query}&{encoded}" if query else encoded,
            "",
        )
    )


def _refresh_lock(path, server_name: str, server_url: str) -> threading.RLock:
    key = f"{path}\0{server_name}\0{server_url}"
    with _REFRESH_LOCKS_GUARD:
        lock = _REFRESH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _REFRESH_LOCKS[key] = lock
        return lock


def _safe_display_text(value: str, limit: int) -> str:
    return "".join(
        character if character.isprintable() else "?"
        for character in value[:limit]
    )
