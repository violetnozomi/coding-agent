"""User-owned trust records for project-supplied MCP capabilities."""
from __future__ import annotations

from pathlib import Path

from nz_coder.foundation.workspace_trust import WorkspaceTrustStore


class MCPTrustStore:
    """Compatibility facade over the unified exact-workspace trust store."""

    def __init__(self, path: Path):
        self.path = path.expanduser().absolute()
        self._store = WorkspaceTrustStore(self.path)

    def is_trusted(self, workspace: Path, server_name: str, fingerprint: str) -> bool:
        return self._store.is_trusted(
            workspace,
            f"mcp:{server_name}",
            fingerprint,
        )

    def trust(self, workspace: Path, server_name: str, fingerprint: str) -> None:
        self._store.trust(workspace, f"mcp:{server_name}", fingerprint)

    def remove(self, workspace: Path, server_name: str) -> bool:
        return self._store.remove(workspace, f"mcp:{server_name}")
