"""Local stdio and remote HTTP Model Context Protocol integration."""
from __future__ import annotations

from nz_coder.mcp.client import (
    MCPClient,
    MCPError,
    MCPRequestError,
    MCPTimeoutError,
)
from nz_coder.mcp.config import (
    MCPOAuthConfig,
    MCPServerConfig,
    load_mcp_server_configs,
    mcp_config_paths,
    mcp_config_revision,
)
from nz_coder.mcp.http_client import MCPHTTPClient
from nz_coder.mcp.sse_client import MCPLegacySSEClient
from nz_coder.mcp.oauth import (
    MCPAuthenticationRequired,
    MCPOAuthError,
    MCPOAuthManager,
    PendingOAuth,
)
from nz_coder.mcp.runtime import (
    MCPRuntime,
    MCPServerStatus,
    current_mcp_runtime,
    scoped_mcp_runtime,
)
from nz_coder.mcp.trust import MCPTrustStore

__all__ = [
    "MCPClient",
    "MCPError",
    "MCPHTTPClient",
    "MCPLegacySSEClient",
    "MCPAuthenticationRequired",
    "MCPOAuthError",
    "MCPOAuthManager",
    "MCPRequestError",
    "MCPOAuthConfig",
    "MCPRuntime",
    "MCPServerConfig",
    "MCPServerStatus",
    "MCPTimeoutError",
    "MCPTrustStore",
    "PendingOAuth",
    "current_mcp_runtime",
    "load_mcp_server_configs",
    "mcp_config_paths",
    "mcp_config_revision",
    "scoped_mcp_runtime",
]
