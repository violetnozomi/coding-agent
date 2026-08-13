"""Validation for explicitly enabled MCP stdio and remote HTTP servers."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from nz_coder import config
from nz_coder.mcp.trust import MCPTrustStore
from nz_coder.runtime.workdir import current_workdir

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_EFFECTS = frozenset({"read", "serial", "write"})
_HEADER_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = frozenset(
    {
        "accept",
        "connection",
        "content-length",
        "content-type",
        "host",
        "mcp-protocol-version",
        "mcp-session-id",
    }
)
_CREDENTIAL_HEADERS = frozenset(
    {
        "api-key",
        "authorization",
        "cookie",
        "proxy-authorization",
        "x-api-key",
    }
)


@dataclass(frozen=True)
class MCPOAuthConfig:
    """Validated OAuth settings without inline client secrets."""

    client_id: str = ""
    client_secret_env: str = ""
    scope: str = ""
    redirect_uri: str = "http://127.0.0.1:19876/mcp/oauth/callback"
    authorization_server: str = ""

    def resolved_client_secret(self) -> str:
        if not self.client_secret_env:
            return ""
        value = os.environ.get(self.client_secret_env)
        if value is None:
            raise ValueError(
                f"OAuth requires environment variable '{self.client_secret_env}'"
            )
        if not value or any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("OAuth client secret environment value is invalid")
        return value


@dataclass(frozen=True)
class MCPServerConfig:
    """One validated local stdio or remote Streamable HTTP definition."""

    name: str
    command: tuple[str, ...] = ()
    cwd: Path = Path(".")
    environment: tuple[tuple[str, str], ...] = ()
    enabled: bool = True
    startup_timeout_seconds: float = 30.0
    tool_timeout_seconds: float = 30.0
    tool_effects: tuple[tuple[str, str], ...] = ()
    transport: str = "stdio"
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    header_env: tuple[tuple[str, str], ...] = ()
    allow_insecure_http: bool = False
    oauth: MCPOAuthConfig | None = None
    source: str = "explicit"
    trusted: bool = True
    fingerprint: str = ""

    def environment_dict(self) -> dict[str, str]:
        return dict(self.environment)

    def effect_for(self, tool_name: str) -> str:
        """Return an explicit effect or conservative ``serial`` default."""
        return dict(self.tool_effects).get(tool_name, "serial")

    def resolved_headers(self) -> dict[str, str]:
        """Resolve explicitly named credential variables without storing values."""
        result = dict(self.headers)
        for header, env_name in self.header_env:
            value = os.environ.get(env_name)
            if value is None:
                raise ValueError(
                    f"MCP server '{self.name}' requires environment variable '{env_name}'"
                )
            _validate_header_value(value, server=self.name)
            result[header] = value
        return result


def load_mcp_server_configs(
    raw: str | dict[str, Any] | None = None,
    *,
    workspace: Path | None = None,
) -> list[MCPServerConfig]:
    """Load merged user/project/environment config without executing commands."""
    root = (workspace or current_workdir()).resolve()
    origins: dict[str, str]
    if raw is None:
        payload, origins = _load_merged_payload(root)
        source: str | dict[str, Any] = payload
    else:
        source = raw
        origins = {}
    if isinstance(source, str):
        if not source.strip():
            return []
        try:
            payload = json.loads(source)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid NZ_MCP_SERVERS_JSON: {exc}") from exc
    elif isinstance(source, dict):
        payload = dict(source)
    else:
        raise ValueError("NZ_MCP_SERVERS_JSON must be JSON text or an object")
    if not isinstance(payload, dict):
        raise ValueError("NZ_MCP_SERVERS_JSON must decode to an object")
    if "servers" in payload:
        wrapper_unknown = sorted(set(payload) - {"servers"})
        if wrapper_unknown:
            raise ValueError(
                "MCP wrapper has unknown field(s): " + ", ".join(wrapper_unknown)
            )
        payload = payload["servers"]
        if not isinstance(payload, dict):
            raise ValueError("MCP 'servers' must be an object")

    servers: list[MCPServerConfig] = []
    for name, item in payload.items():
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ValueError(
                "MCP server names must be 1-40 characters using letters, digits, '_' or '-'"
            )
        if not isinstance(item, dict):
            raise ValueError(f"MCP server '{name}' config must be an object")
        unknown = sorted(
            set(item)
            - {
                "command",
                "type",
                "url",
                "cwd",
                "env",
                "headers",
                "header_env",
                "allow_insecure_http",
                "oauth",
                "enabled",
                "startup_timeout_seconds",
                "tool_timeout_seconds",
                "tool_effects",
            }
        )
        if unknown:
            raise ValueError(
                f"MCP server '{name}' has unknown field(s): {', '.join(unknown)}"
            )

        origin = origins.get(name, "explicit")
        server_type = item.get("type", "remote" if "url" in item else "local")
        if server_type not in {"local", "remote"}:
            raise ValueError(f"MCP server '{name}' type must be 'local' or 'remote'")
        command = item.get("command")
        url = item.get("url")
        if server_type == "local":
            if url is not None:
                raise ValueError(f"MCP server '{name}' local config cannot include url")
            if (
                not isinstance(command, list)
                or not command
                or not all(isinstance(part, str) and part for part in command)
            ):
                raise ValueError(
                    f"MCP server '{name}' command must be a non-empty string array"
                )
        else:
            if command is not None:
                raise ValueError(f"MCP server '{name}' remote config cannot include command")
            if "cwd" in item:
                raise ValueError(f"MCP server '{name}' remote config cannot include cwd")
            _validate_remote_url(
                url,
                server=name,
                allow_insecure_http=item.get("allow_insecure_http", False),
            )
            command = []

        cwd_value = item.get("cwd", ".")
        if not isinstance(cwd_value, str) or not cwd_value.strip():
            raise ValueError(f"MCP server '{name}' cwd must be a non-empty string")
        cwd_path = Path(cwd_value)
        cwd = (
            (root / cwd_path).resolve()
            if not cwd_path.is_absolute()
            else cwd_path.resolve()
        )
        try:
            cwd.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"MCP server '{name}' cwd escapes workspace: {cwd_value}"
            ) from exc

        env = item.get("env", {})
        if not isinstance(env, dict):
            raise ValueError(f"MCP server '{name}' env must be an object")
        environment: list[tuple[str, str]] = []
        for key, value in env.items():
            if not isinstance(key, str) or not _ENV_RE.fullmatch(key):
                raise ValueError(f"MCP server '{name}' has invalid environment key")
            if not isinstance(value, str):
                raise ValueError(
                    f"MCP server '{name}' environment values must be strings"
                )
            environment.append((key, value))
        if server_type == "remote" and environment:
            raise ValueError(f"MCP server '{name}' remote config cannot include env")

        headers = _validate_headers(item.get("headers", {}), server=name)
        header_env = _validate_header_env(item.get("header_env", {}), server=name)
        if server_type == "local" and (headers or header_env):
            raise ValueError(f"MCP server '{name}' local config cannot include headers")
        duplicate_headers = {key.lower() for key, _ in headers} & {
            key.lower() for key, _ in header_env
        }
        if duplicate_headers:
            raise ValueError(
                f"MCP server '{name}' defines a header in both headers and header_env"
            )
        allow_insecure_http = item.get("allow_insecure_http", False)
        if not isinstance(allow_insecure_http, bool):
            raise ValueError(
                f"MCP server '{name}' allow_insecure_http must be boolean"
            )
        if server_type == "local" and allow_insecure_http:
            raise ValueError(
                f"MCP server '{name}' local config cannot allow insecure HTTP"
            )
        oauth = _validate_oauth(
            item.get("oauth") if "oauth" in item else {},
            server=name,
            remote=server_type == "remote",
            allow_insecure_http=allow_insecure_http,
        )

        effects = item.get("tool_effects", {})
        if not isinstance(effects, dict):
            raise ValueError(f"MCP server '{name}' tool_effects must be an object")
        tool_effects: list[tuple[str, str]] = []
        for tool_name, effect in effects.items():
            if not isinstance(tool_name, str) or not tool_name:
                raise ValueError(f"MCP server '{name}' has an invalid tool effect name")
            if effect not in _EFFECTS:
                choices = ", ".join(sorted(_EFFECTS))
                raise ValueError(
                    f"MCP server '{name}' tool '{tool_name}' effect must be one of: {choices}"
                )
            tool_effects.append((tool_name, effect))

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"MCP server '{name}' enabled must be boolean")
        startup_timeout = _positive_timeout(
            item.get("startup_timeout_seconds", config.MCP_STARTUP_TIMEOUT_SECONDS),
            server=name,
            field="startup_timeout_seconds",
        )
        tool_timeout = _positive_timeout(
            item.get("tool_timeout_seconds", config.MCP_TOOL_TIMEOUT_SECONDS),
            server=name,
            field="tool_timeout_seconds",
        )
        fingerprint = ""
        trusted = True
        if server_type == "local" and origin == "project":
            fingerprint = _command_fingerprint(
                name=name,
                command=command,
                cwd=cwd,
                environment=environment,
            )
            trusted = MCPTrustStore(Path(config.MCP_TRUST_STORE)).is_trusted(
                root,
                name,
                fingerprint,
            )
        servers.append(
            MCPServerConfig(
                name=name,
                command=tuple(command),
                cwd=cwd,
                environment=tuple(sorted(environment)),
                enabled=enabled,
                startup_timeout_seconds=startup_timeout,
                tool_timeout_seconds=tool_timeout,
                tool_effects=tuple(sorted(tool_effects)),
                transport="streamable_http" if server_type == "remote" else "stdio",
                url=str(url or ""),
                headers=tuple(sorted(headers)),
                header_env=tuple(sorted(header_env)),
                allow_insecure_http=allow_insecure_http,
                oauth=oauth,
                source=origin,
                trusted=trusted,
                fingerprint=fingerprint,
            )
        )
    return servers


def mcp_config_paths(workspace: Path) -> tuple[Path, Path, Path]:
    """Return resolved user, project, and trust paths with project confinement."""
    root = workspace.resolve()
    user_path = Path(config.MCP_USER_CONFIG).expanduser().resolve()
    project_value = Path(config.MCP_PROJECT_CONFIG)
    if project_value.is_absolute():
        raise ValueError("NZ_MCP_PROJECT_CONFIG must be workspace-relative")
    project_path = (root / project_value).resolve()
    try:
        project_path.relative_to(root)
    except ValueError as exc:
        raise ValueError("NZ_MCP_PROJECT_CONFIG escapes workspace") from exc
    trust_path = Path(config.MCP_TRUST_STORE).expanduser().resolve()
    return user_path, project_path, trust_path


def mcp_config_revision(workspace: Path) -> str:
    """Return a cheap revision for config/trust polling without reading secrets."""
    paths = mcp_config_paths(workspace)
    records: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            records.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            records.append((str(path), -1, -1))
    payload = json.dumps(
        {"paths": records, "environment": config.MCP_SERVERS_JSON},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_merged_payload(workspace: Path) -> tuple[dict[str, Any], dict[str, str]]:
    user_path, project_path, _ = mcp_config_paths(workspace)
    merged: dict[str, Any] = {}
    origins: dict[str, str] = {}
    for origin, path in (("user", user_path), ("project", project_path)):
        if not path.exists():
            continue
        payload = _read_payload_file(path, origin)
        for name, item in payload.items():
            merged[name] = item
            origins[name] = origin
    if config.MCP_SERVERS_JSON.strip():
        payload = _decode_server_payload(config.MCP_SERVERS_JSON, "NZ_MCP_SERVERS_JSON")
        for name, item in payload.items():
            merged[name] = item
            origins[name] = "environment"
    return merged, origins


def _read_payload_file(path: Path, origin: str) -> dict[str, Any]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError(f"MCP {origin} config exceeds 1 MiB: {path}")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Cannot read MCP {origin} config {path}: {exc}") from exc
    return _decode_server_payload(raw, f"MCP {origin} config {path}")


def _decode_server_payload(raw: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must decode to an object")
    if "servers" in payload:
        unknown = sorted(set(payload) - {"servers"})
        if unknown:
            raise ValueError(f"{label} wrapper has unknown field(s): {', '.join(unknown)}")
        payload = payload["servers"]
    if not isinstance(payload, dict):
        raise ValueError(f"{label} servers must be an object")
    return dict(payload)


def _command_fingerprint(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    environment: list[tuple[str, str]],
) -> str:
    payload = json.dumps(
        {
            "name": name,
            "command": command,
            "cwd": str(cwd),
            "environment": sorted(environment),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _positive_timeout(value: Any, *, server: str, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"MCP server '{server}' {field} must be a finite number")
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"MCP server '{server}' {field} must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(
            f"MCP server '{server}' {field} must be a positive finite number"
        )
    return timeout


def _validate_remote_url(
    value: Any,
    *,
    server: str,
    allow_insecure_http: Any,
) -> None:
    if not isinstance(allow_insecure_http, bool):
        raise ValueError(f"MCP server '{server}' allow_insecure_http must be boolean")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"MCP server '{server}' url must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ValueError(f"MCP server '{server}' url must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"MCP server '{server}' url cannot contain credentials")
    if parsed.fragment:
        raise ValueError(f"MCP server '{server}' url cannot contain a fragment")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError(f"MCP server '{server}' url has an invalid port") from exc
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ValueError(
            f"MCP server '{server}' url contains whitespace or control characters"
        )
    if parsed.scheme == "http":
        loopback = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
        if not (allow_insecure_http and loopback):
            raise ValueError(
                f"MCP server '{server}' HTTP is allowed only for loopback with "
                "allow_insecure_http=true"
            )


def _validate_headers(value: Any, *, server: str) -> list[tuple[str, str]]:
    if not isinstance(value, dict):
        raise ValueError(f"MCP server '{server}' headers must be an object")
    result: list[tuple[str, str]] = []
    for name, header_value in value.items():
        _validate_header_name(name, server=server)
        _validate_header_value(header_value, server=server)
        if name.lower() in _CREDENTIAL_HEADERS:
            raise ValueError(
                f"MCP server '{server}' credential header '{name}' must use header_env"
            )
        result.append((name, header_value))
    return result


def _validate_header_env(value: Any, *, server: str) -> list[tuple[str, str]]:
    if not isinstance(value, dict):
        raise ValueError(f"MCP server '{server}' header_env must be an object")
    result: list[tuple[str, str]] = []
    for name, env_name in value.items():
        _validate_header_name(name, server=server)
        if not isinstance(env_name, str) or not _ENV_RE.fullmatch(env_name):
            raise ValueError(f"MCP server '{server}' has an invalid header environment key")
        result.append((name, env_name))
    return result


def _validate_header_name(value: Any, *, server: str) -> None:
    if not isinstance(value, str) or not _HEADER_RE.fullmatch(value):
        raise ValueError(f"MCP server '{server}' has an invalid header name")
    if value.lower() in _FORBIDDEN_HEADERS:
        raise ValueError(f"MCP server '{server}' cannot override header '{value}'")


def _validate_header_value(value: Any, *, server: str) -> None:
    if not isinstance(value, str) or any(
        ord(character) < 32 or ord(character) == 127
        for character in value
    ):
        raise ValueError(f"MCP server '{server}' has an invalid header value")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError as exc:
        raise ValueError(f"MCP server '{server}' has an invalid header value") from exc


def _validate_oauth(
    value: Any,
    *,
    server: str,
    remote: bool,
    allow_insecure_http: bool,
) -> MCPOAuthConfig | None:
    if not remote:
        if value not in ({}, False, None):
            raise ValueError(f"MCP server '{server}' local config cannot include oauth")
        return None
    if value is False:
        return None
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"MCP server '{server}' oauth must be an object or false")
    unknown = sorted(
        set(value)
        - {
            "client_id",
            "client_secret_env",
            "scope",
            "redirect_uri",
            "authorization_server",
        }
    )
    if unknown:
        raise ValueError(
            f"MCP server '{server}' oauth has unknown field(s): {', '.join(unknown)}"
        )
    client_id = value.get("client_id", "")
    client_secret_env = value.get("client_secret_env", "")
    scope = value.get("scope", "")
    redirect_uri = value.get(
        "redirect_uri",
        "http://127.0.0.1:19876/mcp/oauth/callback",
    )
    authorization_server = value.get("authorization_server", "")
    for field, field_value in {
        "client_id": client_id,
        "client_secret_env": client_secret_env,
        "scope": scope,
        "redirect_uri": redirect_uri,
        "authorization_server": authorization_server,
    }.items():
        if not isinstance(field_value, str):
            raise ValueError(f"MCP server '{server}' oauth {field} must be a string")
        if any(ord(character) < 32 or ord(character) == 127 for character in field_value):
            raise ValueError(f"MCP server '{server}' oauth {field} is invalid")
    if client_secret_env and not _ENV_RE.fullmatch(client_secret_env):
        raise ValueError(
            f"MCP server '{server}' oauth client_secret_env is invalid"
        )
    redirect = urlsplit(redirect_uri)
    try:
        redirect_port = redirect.port
    except ValueError as exc:
        raise ValueError(
            f"MCP server '{server}' oauth redirect_uri has an invalid port"
        ) from exc
    if (
        redirect.scheme != "http"
        or redirect.hostname != "127.0.0.1"
        or not redirect_port
        or not redirect.path.startswith("/")
        or redirect.query
        or redirect.fragment
        or redirect.username is not None
    ):
        raise ValueError(
            f"MCP server '{server}' oauth redirect_uri must use 127.0.0.1 HTTP "
            "with an explicit port and path"
        )
    if authorization_server:
        _validate_remote_url(
            authorization_server,
            server=server,
            allow_insecure_http=allow_insecure_http,
        )
    return MCPOAuthConfig(
        client_id=client_id,
        client_secret_env=client_secret_env,
        scope=scope,
        redirect_uri=redirect_uri,
        authorization_server=authorization_server,
    )
