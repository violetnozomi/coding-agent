"""MCP server lifecycle and context-local dynamic tool bindings."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import base64
import binascii
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nz_coder.foundation import config
from nz_coder.protocol.attachments import SUPPORTED_IMAGE_MIMES, make_image_attachment
from nz_coder.mcp.client import MCPClient, MCPError, MCPRequestError
from nz_coder.mcp.config import (
    MCPServerConfig,
    load_mcp_server_configs,
    mcp_config_revision,
)
from nz_coder.mcp.http_client import MCPHTTPClient
from nz_coder.mcp.sse_client import MCPLegacySSEClient
from nz_coder.mcp.oauth import (
    MCPAuthenticationRequired,
    MCPOAuthManager,
)
from nz_coder.runtime.process.workdir import current_workdir
from nz_coder.tools import ToolOutput


_ACTIVE_MCP_RUNTIME: ContextVar[object | None] = ContextVar(
    "nz_coder_active_mcp_runtime",
    default=None,
)


@contextmanager
def scoped_mcp_runtime(runtime):
    """Bind one MCP runtime to the current Agent execution context."""
    token = _ACTIVE_MCP_RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _ACTIVE_MCP_RUNTIME.reset(token)


def current_mcp_runtime():
    """Return the run-owned MCP runtime, never a process-global fallback."""
    return _ACTIVE_MCP_RUNTIME.get()


@dataclass(frozen=True)
class MCPServerStatus:
    """Safe status record that excludes commands, environment, and secrets."""

    name: str
    status: str
    tool_count: int = 0
    error: str = ""


class MCPRuntime:
    """Own reusable MCP clients, caches, bindings, and lifecycle controls."""

    def __init__(
        self,
        configs: list[MCPServerConfig],
        *,
        client_factory=MCPClient,
        oauth_manager: MCPOAuthManager | None = None,
        workspace: Path | None = None,
        config_loader=None,
        config_revision=None,
        config_refresh_loader=None,
    ):
        self.configs = list(configs)
        self._client_factory = client_factory
        self._oauth_manager = oauth_manager or MCPOAuthManager()
        self.clients: dict[str, MCPClient] = {}
        self._pending_clients: dict[str, MCPClient] = {}
        self.statuses: dict[str, MCPServerStatus] = {}
        self._bindings: list[dict[str, Any]] = []
        self._definitions: dict[str, list[dict[str, Any]]] = {}
        self._server_generations: dict[str, int] = {}
        self._next_server_generation = 0
        self._prompts: dict[str, list[dict[str, Any]]] = {}
        self._resources: dict[str, list[dict[str, Any]]] = {}
        self._start_state = "new"
        self._ready_event = threading.Event()
        self._closing = False
        self._state_lock = threading.RLock()
        self._owned_clients: set[MCPClient] = set()
        self._start_thread: threading.Thread | None = None
        self._change_handler = None
        self._workspace = (workspace or current_workdir()).resolve()
        self._config_loader = config_loader
        self._config_revision_loader = config_revision
        self._config_refresh_loader = config_refresh_loader
        self._loaded_config_revision = (
            config_revision() if callable(config_revision) else ""
        )
        self._configuration_error = ""
        self._reconcile_lock = threading.RLock()

    @classmethod
    def configured(
        cls,
        *,
        workspace: Path | None = None,
        config_snapshot=None,
    ) -> "MCPRuntime":
        """Build the explicitly enabled runtime without starting subprocesses."""
        root = (workspace or current_workdir()).resolve()
        legacy_globals = config_snapshot is None
        if config_snapshot is None:
            from nz_coder.foundation.workspace_trust import current_config_snapshot

            config_snapshot = current_config_snapshot(root)
        enabled = (
            config.MCP_ENABLED
            if legacy_globals
            else config_snapshot.get_bool("NZ_MCP_ENABLED", False)
        )
        if not enabled:
            return cls([], workspace=root)

        selected_snapshot = [config_snapshot]

        def loader():
            return load_mcp_server_configs(
                workspace=root,
                project_control_snapshot=selected_snapshot[0].project_control,
                config_snapshot=(None if legacy_globals else selected_snapshot[0]),
            )

        def revision():
            return mcp_config_revision(
                root,
                project_control_snapshot=selected_snapshot[0].project_control,
                config_snapshot=(None if legacy_globals else selected_snapshot[0]),
            )

        def refresh_loader():
            from nz_coder.foundation.workspace_trust import load_config_snapshot

            selected_snapshot[0] = load_config_snapshot(root)
            return load_mcp_server_configs(
                workspace=root,
                project_control_snapshot=selected_snapshot[0].project_control,
            )

        return cls(
            loader(),
            workspace=root,
            # A top-level Run owns one immutable config epoch. Changes are
            # observed only when the next Run creates its own runtime.
            config_loader=loader if legacy_globals else None,
            config_revision=revision if legacy_globals else None,
            config_refresh_loader=refresh_loader if legacy_globals else None,
        )

    def start(self) -> "MCPRuntime":
        """Connect enabled servers in parallel and wait for the initial result."""
        with self._state_lock:
            if self._closing or self._start_state == "ready":
                return self
            if self._start_state == "starting":
                starter = False
                enabled = []
            else:
                starter = True
                self._start_state = "starting"
                self._ready_event.clear()
                enabled = []
                for server in self.configs:
                    if server.enabled and server.trusted:
                        enabled.append(server)
                        self.statuses[server.name] = MCPServerStatus(
                            server.name,
                            "connecting",
                        )
                    elif not server.enabled:
                        self.statuses[server.name] = MCPServerStatus(
                            server.name,
                            "disabled",
                        )
                    else:
                        self.statuses[server.name] = MCPServerStatus(
                            server.name,
                            "untrusted",
                        )
        if not starter:
            self._ready_event.wait()
            return self
        try:
            if enabled:
                with ThreadPoolExecutor(
                    max_workers=min(8, len(enabled)),
                    thread_name_prefix="nz-mcp-start",
                ) as executor:
                    futures = {
                        executor.submit(self._create_server, server): server
                        for server in enabled
                    }
                    for future in as_completed(futures):
                        server = futures[future]
                        try:
                            client, definitions, prompts, resources = future.result()
                        except Exception as exc:
                            with self._state_lock:
                                current = self.statuses.get(server.name)
                                if current is not None and current.status == "connecting":
                                    self.statuses[server.name] = self._failure_status(
                                        server,
                                        exc,
                                    )
                            continue
                        with self._state_lock:
                            if (
                                self._closing
                                or self._pending_clients.get(server.name) is not client
                            ):
                                accepted = False
                            else:
                                accepted = True
                                self._pending_clients.pop(server.name, None)
                                self.clients[server.name] = client
                                self._advance_server_generation_locked(server.name)
                                self._definitions[server.name] = definitions
                                self._prompts[server.name] = prompts
                                self._resources[server.name] = resources
                                self._rebuild_bindings_locked()
                        if accepted:
                            self._watch_client(server, client)
                        else:
                            client.close()
                            with self._state_lock:
                                self._owned_clients.discard(client)
            return self
        finally:
            with self._state_lock:
                if not self._closing:
                    self._start_state = "ready"
                self._ready_event.set()

    def start_background(self) -> "MCPRuntime":
        """Begin the initial parallel connection without blocking the caller."""
        with self._state_lock:
            if self._start_state != "new" or self._closing:
                return self
            thread = threading.Thread(
                target=self.start,
                name="nz-mcp-runtime-start",
                daemon=True,
            )
            self._start_thread = thread
            thread.start()
        return self

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Wait for a background initial connection to finish."""
        with self._state_lock:
            state = self._start_state
            start_thread = self._start_thread
        if state == "ready":
            return True
        if state == "new" and start_thread is None:
            return False
        return self._ready_event.wait(timeout=timeout)

    def tool_bindings(self) -> list[dict[str, Any]]:
        """Return definitions consumable by ``scoped_dynamic_tools``."""
        self.reconcile_config_if_changed()
        with self._state_lock:
            return list(self._bindings)

    def prompt_definitions(self) -> list[dict[str, Any]]:
        """Return cached prompt metadata without making wire requests."""
        with self._state_lock:
            return self._cached_records(self._prompts)

    def resource_definitions(self) -> list[dict[str, Any]]:
        """Return cached resource metadata without making wire requests."""
        with self._state_lock:
            return self._cached_records(self._resources)

    def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        with self._state_lock:
            client = self.clients.get(server_name)
        if client is None:
            raise MCPError(f"MCP server '{server_name}' is not connected")
        return client.get_prompt(prompt_name, arguments)

    def read_resource(self, server_name: str, uri: str) -> dict[str, Any]:
        with self._state_lock:
            client = self.clients.get(server_name)
        if client is None:
            raise MCPError(f"MCP server '{server_name}' is not connected")
        return client.read_resource(uri)

    def status_summary(self) -> list[dict[str, Any]]:
        """Return deterministic, secret-free status rows for tracing."""
        self.reconcile_config_if_changed()
        with self._state_lock:
            statuses = list(self.statuses.values())
            configuration_error = self._configuration_error
        if configuration_error:
            statuses.append(MCPServerStatus("$config", "failed", error=configuration_error))
        return [
            {
                "name": status.name,
                "status": status.status,
                "tool_count": status.tool_count,
                "error": status.error,
            }
            for status in sorted(statuses, key=lambda item: item.name)
        ]

    def extension_snapshot(self) -> list[dict[str, Any]]:
        """Project live MCP generations into the unified extension contract."""
        self.reconcile_config_if_changed()
        with self._state_lock:
            configs = list(self.configs)
            statuses = dict(self.statuses)
            definitions = {
                name: list(items) for name, items in self._definitions.items()
            }
            prompts = {
                name: list(items) for name, items in self._prompts.items()
            }
            resources = {
                name: list(items) for name, items in self._resources.items()
            }
        result = []
        for server in sorted(configs, key=lambda item: item.name):
            status = statuses.get(server.name)
            tools = tuple(
                sorted(
                    str(item.get("name") or "")
                    for item in definitions.get(server.name, [])
                    if str(item.get("name") or "")
                )
            )
            result.append(
                {
                    "name": server.name,
                    "source": server.source,
                    "trusted": server.trusted,
                    "enabled": server.enabled,
                    "status": status.status if status is not None else (
                        "configured" if server.enabled and server.trusted else (
                            "disabled" if not server.enabled else "untrusted"
                        )
                    ),
                    "transport": server.transport,
                    "tools": tools,
                    "tool_effects": tuple(
                        (name, server.effect_for(name)) for name in tools
                    ),
                    "prompt_count": len(prompts.get(server.name, [])),
                    "resource_count": len(resources.get(server.name, [])),
                    "error": status.error if status is not None else "",
                }
            )
        return result

    def connect(self, server_name: str) -> MCPServerStatus:
        """Connect or retry one configured server synchronously."""
        server = self._server_config(server_name)
        if not server.trusted:
            raise MCPError(
                f"MCP server '{server_name}' project command is not trusted"
            )
        with self._state_lock:
            if self._closing:
                raise MCPError("MCP runtime is closed")
            current = self.statuses.get(server_name)
            if current is not None and current.status in {"connected", "connecting"}:
                return current
            self.statuses[server_name] = MCPServerStatus(server_name, "connecting")
        try:
            client, definitions, prompts, resources = self._create_server(server)
        except Exception as exc:
            status = self._failure_status(server, exc)
            with self._state_lock:
                current = self.statuses.get(server_name)
                if current is not None and current.status == "connecting":
                    self.statuses[server_name] = status
                    return status
                return current or status
        with self._state_lock:
            if (
                self._closing
                or self._pending_clients.get(server_name) is not client
            ):
                accepted = False
            else:
                accepted = True
                self._pending_clients.pop(server_name, None)
                self.clients[server_name] = client
                self._advance_server_generation_locked(server_name)
                self._definitions[server_name] = definitions
                self._prompts[server_name] = prompts
                self._resources[server_name] = resources
                self._rebuild_bindings_locked()
                status = self.statuses[server_name]
        if not accepted:
            client.close()
            raise MCPError("MCP runtime is closed")
        self._watch_client(server, client)
        with self._state_lock:
            if self.clients.get(server_name) is not client or self._closing:
                current = self.statuses.get(server_name)
                if current is not None:
                    return current
                raise MCPError("MCP runtime is closed")
            status = self.statuses[server_name]
            self._notify_change("connected", server_name)
            return status

    def disconnect(self, server_name: str) -> MCPServerStatus:
        """Disconnect one server and immediately remove its live bindings."""
        self._server_config(server_name)
        with self._state_lock:
            client = self.clients.pop(server_name, None)
            pending = self._pending_clients.pop(server_name, None)
            self._definitions.pop(server_name, None)
            self._prompts.pop(server_name, None)
            self._resources.pop(server_name, None)
            self._rebuild_bindings_locked()
            status = MCPServerStatus(server_name, "disabled")
            self.statuses[server_name] = status
            if client is not None:
                self._owned_clients.discard(client)
            if pending is not None:
                self._owned_clients.discard(pending)
            self._notify_change("disconnected", server_name)
        if client is not None:
            client.close()
        if pending is not None and pending is not client:
            pending.close()
        return status

    def reconnect(self, server_name: str) -> MCPServerStatus:
        """Replace one client process and refresh all of its cached features."""
        self.disconnect(server_name)
        return self.connect(server_name)

    def reconcile_config_if_changed(self) -> bool:
        """Poll cheap config metadata and reconcile only after a revision change."""
        if not callable(self._config_loader) or not callable(self._config_revision_loader):
            return False
        try:
            revision = self._config_revision_loader()
        except Exception as exc:
            with self._state_lock:
                self._configuration_error = type(exc).__name__
            return False
        with self._state_lock:
            if revision == self._loaded_config_revision:
                return False
        return self.reload_config(revision=revision)

    def reload_config(self, *, revision: str | None = None) -> bool:
        """Load and reconcile config, retaining the live generation on invalid input."""
        if not callable(self._config_loader):
            return False
        with self._reconcile_lock:
            try:
                if revision is None and callable(self._config_refresh_loader):
                    configs = self._config_refresh_loader()
                    revision = (
                        self._config_revision_loader()
                        if callable(self._config_revision_loader)
                        else None
                    )
                else:
                    if revision is None and callable(self._config_revision_loader):
                        revision = self._config_revision_loader()
                    configs = self._config_loader()
            except Exception as exc:
                with self._state_lock:
                    self._loaded_config_revision = revision or self._loaded_config_revision
                    self._configuration_error = type(exc).__name__
                self._notify_change("config_failed", "$config")
                return False
            self.reconcile(configs)
            with self._state_lock:
                self._loaded_config_revision = revision or self._loaded_config_revision
                self._configuration_error = ""
            return True

    def reconcile(self, configs: list[MCPServerConfig]) -> dict[str, list[str]]:
        """Apply added, removed, and changed definitions to a running runtime."""
        names = [server.name for server in configs]
        if len(names) != len(set(names)):
            raise MCPError("MCP configuration contains duplicate server names")
        with self._reconcile_lock:
            with self._state_lock:
                if self._closing:
                    raise MCPError("MCP runtime is closed")
                old = {server.name: server for server in self.configs}
                new = {server.name: server for server in configs}
                removed = sorted(set(old) - set(new))
                added = sorted(set(new) - set(old))
                changed = sorted(
                    name for name in set(old) & set(new) if old[name] != new[name]
                )
                active = self._start_state in {"starting", "ready"}

            for name in removed + changed:
                self._retire_server(name)

            with self._state_lock:
                self.configs = list(configs)
                for name in removed:
                    self.statuses.pop(name, None)
                for name in added + changed:
                    server = new[name]
                    status = "disabled" if not server.enabled else (
                        "untrusted" if not server.trusted else "disconnected"
                    )
                    self.statuses[name] = MCPServerStatus(name, status)
                self._rebuild_bindings_locked()

            for name in removed:
                self._notify_change("config_removed", name)
            for name in added:
                self._notify_change("config_added", name)
            for name in changed:
                self._notify_change("config_changed", name)

            if active:
                for name in added + changed:
                    server = new[name]
                    if server.enabled and server.trusted:
                        self.connect(name)
            return {"added": added, "removed": removed, "changed": changed}

    def _retire_server(self, server_name: str) -> None:
        """Remove one live/pending generation without requiring current config membership."""
        with self._state_lock:
            client = self.clients.pop(server_name, None)
            pending = self._pending_clients.pop(server_name, None)
            self._definitions.pop(server_name, None)
            self._prompts.pop(server_name, None)
            self._resources.pop(server_name, None)
            if client is not None:
                self._owned_clients.discard(client)
            if pending is not None:
                self._owned_clients.discard(pending)
            self._rebuild_bindings_locked()
        if client is not None:
            client.close()
        if pending is not None and pending is not client:
            pending.close()

    def set_change_handler(self, handler) -> None:
        """Receive best-effort lifecycle/cache change notifications."""
        if handler is not None and not callable(handler):
            raise ValueError("MCP change handler must be callable")
        with self._state_lock:
            self._change_handler = handler

    def close(self) -> None:
        """Close every connected server and clear live bindings."""
        with self._state_lock:
            self._closing = True
            self._start_state = "closed"
            self._ready_event.set()
            clients = list(self._owned_clients)
            start_thread = self._start_thread
        for client in clients:
            client.close()
        if (
            start_thread is not None
            and start_thread is not threading.current_thread()
        ):
            start_thread.join(timeout=2)
        with self._state_lock:
            self.clients.clear()
            self._pending_clients.clear()
            self._bindings.clear()
            self._definitions.clear()
            self._prompts.clear()
            self._resources.clear()
            self._owned_clients.clear()

    def _create_server(self, server: MCPServerConfig):
        remote_headers: dict[str, str] = {}
        if server.transport == "streamable_http" and self._client_factory is MCPClient:
            remote_headers = server.resolved_headers()
            if not any(name.lower() == "authorization" for name in remote_headers):
                authorization = self._oauth_manager.authorization_header(server)
                if authorization:
                    remote_headers["Authorization"] = authorization
            client = MCPHTTPClient(
                name=server.name,
                url=server.url,
                headers=remote_headers,
                startup_timeout_seconds=server.startup_timeout_seconds,
                tool_timeout_seconds=server.tool_timeout_seconds,
            )
        else:
            client = self._client_factory(
                name=server.name,
                command=server.command,
                cwd=server.cwd,
                environment=server.environment_dict(),
                startup_timeout_seconds=server.startup_timeout_seconds,
                tool_timeout_seconds=server.tool_timeout_seconds,
            )
        with self._state_lock:
            if self._closing:
                client.close()
                raise MCPError("MCP runtime is closed")
            self._owned_clients.add(client)
            self._pending_clients[server.name] = client
        try:
            return self._discover_server(server, client)
        except Exception as primary_error:
            self._discard_pending_client(server.name, client)
            if (
                isinstance(primary_error, MCPAuthenticationRequired)
                or server.transport != "streamable_http"
                or self._client_factory is not MCPClient
            ):
                raise
            legacy = MCPLegacySSEClient(
                name=server.name,
                url=server.url,
                headers=remote_headers,
                startup_timeout_seconds=server.startup_timeout_seconds,
                tool_timeout_seconds=server.tool_timeout_seconds,
            )
            with self._state_lock:
                if self._closing:
                    legacy.close()
                    raise MCPError("MCP runtime is closed") from primary_error
                self._owned_clients.add(legacy)
                self._pending_clients[server.name] = legacy
            try:
                return self._discover_server(server, legacy)
            except Exception as legacy_error:
                self._discard_pending_client(server.name, legacy)
                raise legacy_error from primary_error

    def _discover_server(self, server: MCPServerConfig, client: MCPClient):
        client.start()
        definitions = client.list_tools()
        self._build_bindings(server, client, definitions, set())
        prompts = self._optional_list(client, "list_prompts")
        resources = self._optional_list(client, "list_resources")
        return client, definitions, prompts, resources

    def _discard_pending_client(self, server_name: str, client: MCPClient) -> None:
        client.close()
        with self._state_lock:
            self._owned_clients.discard(client)
            if self._pending_clients.get(server_name) is client:
                self._pending_clients.pop(server_name, None)

    @staticmethod
    def _optional_list(client: MCPClient, method: str) -> list[dict[str, Any]]:
        callback = getattr(client, method, None)
        if not callable(callback):
            return []
        try:
            result = callback()
        except MCPRequestError as exc:
            if exc.code == -32601:
                return []
            raise
        return result if isinstance(result, list) else []

    def _watch_client(self, server: MCPServerConfig, client: MCPClient) -> None:
        error_setter = getattr(client, "set_transport_error_handler", None)
        if callable(error_setter):
            error_setter(lambda error: self._fail_client(server, client, error))
            with self._state_lock:
                if self.clients.get(server.name) is not client or self._closing:
                    return
        setter = getattr(client, "set_notification_handler", None)
        if not callable(setter):
            return
        setter(
            "notifications/tools/list_changed",
            lambda _params: self._refresh_cache(server, client, "tools"),
        )
        setter(
            "notifications/prompts/list_changed",
            lambda _params: self._refresh_cache(server, client, "prompts"),
        )
        setter(
            "notifications/resources/list_changed",
            lambda _params: self._refresh_cache(server, client, "resources"),
        )

    def _refresh_cache(
        self,
        server: MCPServerConfig,
        client: MCPClient,
        kind: str,
    ) -> None:
        try:
            if kind == "tools":
                result = client.list_tools()
                self._build_bindings(server, client, result, set())
            elif kind == "prompts":
                result = client.list_prompts()
            else:
                result = client.list_resources()
        except MCPRequestError as exc:
            if exc.code == -32601 and kind != "tools":
                result = []
            else:
                self._fail_client(server, client, exc)
                return
        except Exception as exc:
            self._fail_client(server, client, exc)
            return
        with self._state_lock:
            if self.clients.get(server.name) is not client or self._closing:
                return
            if kind == "tools":
                self._definitions[server.name] = result
                self._rebuild_bindings_locked()
            elif kind == "prompts":
                self._prompts[server.name] = result
            else:
                self._resources[server.name] = result
            self._notify_change(f"{kind}_changed", server.name)

    def _fail_client(
        self,
        server: MCPServerConfig,
        client: MCPClient,
        error: Exception,
    ) -> None:
        """Atomically retire a generation whose transport/cache refresh failed."""
        with self._state_lock:
            if self.clients.get(server.name) is not client or self._closing:
                return
            self.clients.pop(server.name, None)
            self._definitions.pop(server.name, None)
            self._prompts.pop(server.name, None)
            self._resources.pop(server.name, None)
            self._owned_clients.discard(client)
            self._rebuild_bindings_locked()
            self.statuses[server.name] = self._failure_status(server, error)
            self._notify_change("failed", server.name)
        client.close()

    def _failure_status(
        self,
        server: MCPServerConfig,
        error: Exception,
    ) -> MCPServerStatus:
        if isinstance(error, MCPAuthenticationRequired) and server.oauth is not None:
            try:
                self._oauth_manager.invalidate_tokens(
                    server,
                    rejected_authorization=error.rejected_authorization,
                )
            except Exception:
                pass
        status = (
            "needs_auth"
            if isinstance(error, MCPAuthenticationRequired) and server.oauth is not None
            else "failed"
        )
        return MCPServerStatus(
            server.name,
            status,
            error=type(error).__name__,
        )

    def _rebuild_bindings_locked(self) -> None:
        bindings: list[dict[str, Any]] = []
        public_names: set[str] = set()
        for server in self.configs:
            client = self.clients.get(server.name)
            definitions = self._definitions.get(server.name)
            if client is None or definitions is None:
                continue
            server_bindings = self._build_bindings(
                server,
                client,
                definitions,
                public_names,
            )
            bindings.extend(server_bindings)
            self.statuses[server.name] = MCPServerStatus(
                server.name,
                "connected",
                tool_count=len(server_bindings),
            )
        self._bindings = bindings

    def _advance_server_generation_locked(self, server_name: str) -> None:
        """Assign a never-reused identity generation to one accepted client."""
        self._next_server_generation += 1
        self._server_generations[server_name] = self._next_server_generation

    def _server_config(self, server_name: str) -> MCPServerConfig:
        for server in self.configs:
            if server.name == server_name:
                return server
        raise MCPError(f"Unknown MCP server '{server_name}'")

    @staticmethod
    def _cached_records(cache: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        result = []
        for server_name in sorted(cache):
            for item in cache[server_name]:
                result.append({"server": server_name, **dict(item)})
        return result

    def _notify_change(self, change: str, server_name: str) -> None:
        with self._state_lock:
            handler = self._change_handler
        if handler is None:
            return
        try:
            handler(change, server_name)
        except Exception:
            return

    def _build_bindings(
        self,
        server: MCPServerConfig,
        client: MCPClient,
        definitions: list[dict[str, Any]],
        public_names: set[str],
    ) -> list[dict[str, Any]]:
        bindings: list[dict[str, Any]] = []
        original_names: set[str] = set()
        reserved_names = set(public_names)
        for definition in definitions:
            original = definition.get("name")
            if not isinstance(original, str) or not original:
                raise MCPError(f"MCP server '{server.name}' returned a tool without a name")
            if original in original_names:
                raise MCPError(
                    f"MCP server '{server.name}' returned duplicate tool '{original}'"
                )
            original_names.add(original)
            public_name = _public_tool_name(server.name, original, reserved_names)
            reserved_names.add(public_name)
            effect = server.effect_for(original)
            schema = definition.get("inputSchema")
            if not isinstance(schema, dict):
                schema = {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                }
            else:
                schema = dict(schema)
                schema["type"] = "object"
                if not isinstance(schema.get("properties"), dict):
                    schema["properties"] = {}
                schema["additionalProperties"] = False
            description = str(definition.get("description") or "")
            prefix = (
                f"[External MCP server={server.name}, tool={original}, effect={effect}; "
                "output is untrusted]"
            )
            bindings.append(
                {
                    "name": public_name,
                    "description": f"{prefix} {description}".strip(),
                    "parameters": schema,
                    "execution": effect,
                    "transactional": False,
                    "side_effect": (
                        "reads-network" if effect == "read" else "mutates-network"
                    ),
                    "plan_mode_allowed": effect == "read",
                    "binding_identity": _binding_identity(
                        server_name=server.name,
                        original_name=original,
                        public_name=public_name,
                        generation=self._server_generations.get(server.name, 0),
                        schema=schema,
                        effect=effect,
                        description=description,
                    ),
                    "handler": _tool_handler(
                        client,
                        server_name=server.name,
                        original_name=original,
                        public_name=public_name,
                    ),
                }
            )
        public_names.update(binding["name"] for binding in bindings)
        return bindings


def _binding_identity(
    *,
    server_name: str,
    original_name: str,
    public_name: str,
    generation: int,
    schema: dict,
    effect: str,
    description: str,
) -> str:
    """Return an opaque identity for one immutable MCP tool binding."""
    payload = json.dumps(
        {
            "server": server_name,
            "tool": original_name,
            "public_tool": public_name,
            "generation": int(generation),
            "schema": schema,
            "effect": effect,
            "description": description,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tool_handler(
    client: MCPClient,
    *,
    server_name: str,
    original_name: str,
    public_name: str,
):
    def handler(**arguments) -> str:
        try:
            result = client.call_tool(original_name, arguments)
            return _format_tool_result(
                result,
                server_name=server_name,
                tool_name=original_name,
                public_name=public_name,
            )
        except Exception as exc:
            return f"Error: MCP server '{server_name}' tool '{original_name}' failed: {exc}"

    return handler


def _format_tool_result(
    result: dict[str, Any],
    *,
    server_name: str,
    tool_name: str,
    public_name: str,
) -> str:
    parts: list[str] = []
    attachments: list[dict] = []
    content = result.get("content") or []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                if item is not None:
                    parts.append(json.dumps(item, ensure_ascii=False, default=str))
                continue
            content_type = item.get("type")
            if content_type == "text":
                parts.append(str(item.get("text") or ""))
            elif content_type == "image":
                attachment = _mcp_image_attachment(
                    item.get("data"),
                    item.get("mimeType"),
                )
                if attachment is not None and len(attachments) < 4:
                    attachments.append(attachment)
                else:
                    parts.append("[MCP image omitted: unsupported or invalid payload]")
            elif content_type == "resource" and isinstance(item.get("resource"), dict):
                resource = item["resource"]
                if resource.get("text"):
                    parts.append(str(resource["text"]))
                if resource.get("blob") is not None:
                    attachment = _mcp_image_attachment(
                        resource.get("blob"),
                        resource.get("mimeType"),
                        filename=str(resource.get("uri") or ""),
                    )
                    if attachment is not None and len(attachments) < 4:
                        attachments.append(attachment)
                    else:
                        parts.append(
                            "[MCP resource blob omitted: unsupported or invalid payload]"
                        )
            else:
                parts.append(json.dumps(item, ensure_ascii=False, default=str))
    structured = result.get("structuredContent")
    if structured is not None:
        parts.append(json.dumps(structured, ensure_ascii=False, default=str))
    text = "\n".join(part for part in parts if part).strip() or "(no output)"
    if result.get("isError"):
        return f"Error: MCP server '{server_name}' tool '{tool_name}': {text}"
    output = (
        f"<mcp-output tool=\"{public_name}\" untrusted=\"true\">\n"
        f"{text}\n"
        "</mcp-output>"
    )
    return ToolOutput(
        output,
        metadata=(
            dict(result["metadata"])
            if isinstance(result.get("metadata"), dict)
            else {}
        ),
        attachments=attachments,
    )


def _mcp_image_attachment(
    data: Any,
    mime: Any,
    *,
    filename: str = "",
) -> dict | None:
    """Convert one MCP image/blob item without leaking raw base64 into text."""
    if not isinstance(data, str) or not isinstance(mime, str):
        return None
    if mime not in SUPPORTED_IMAGE_MIMES:
        return None
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    try:
        return make_image_attachment(decoded, mime, filename=filename)
    except ValueError:
        return None


def _public_tool_name(server_name: str, tool_name: str, used: set[str]) -> str:
    raw = f"mcp_{server_name}_{tool_name}"
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_") or "mcp_tool"
    candidate = safe[:64]
    if candidate not in used and raw == safe and len(safe) <= 64:
        return candidate
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
    candidate = f"{safe[:55].rstrip('_')}_{digest}"
    if candidate in used:
        raise MCPError(f"MCP tool name collision after normalization: {raw}")
    return candidate
