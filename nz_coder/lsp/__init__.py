"""Language Server Protocol support for semantic code navigation."""
from __future__ import annotations

from .client import LSPClient, LSPError, LSPResponseError, LSPTimeoutError
from .manager import (
    client_startup_error,
    client_status_summary,
    close_all_clients,
    close_workspace_clients,
    get_client_for_file,
)
from .servers import (
    ResolvedServer,
    available_server_summary,
    resolve_server,
    trust_server,
    untrust_server,
)

__all__ = [
    "LSPClient",
    "LSPError",
    "LSPResponseError",
    "LSPTimeoutError",
    "ResolvedServer",
    "available_server_summary",
    "client_startup_error",
    "client_status_summary",
    "close_all_clients",
    "close_workspace_clients",
    "get_client_for_file",
    "resolve_server",
    "trust_server",
    "untrust_server",
]
