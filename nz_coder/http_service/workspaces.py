"""Operator-authorized workspace registry for the local Session service."""
from __future__ import annotations

import hashlib
from pathlib import Path

from nz_coder.runtime.process.workdir import current_workdir


class WorkspaceNotFoundError(LookupError):
    """Raised when an opaque workspace ID is not authorized by the service."""


class WorkspaceRegistry:
    """Map operator-registered local directories to stable selector IDs."""

    def __init__(self, roots: list[str | Path] | None = None):
        candidates: list[str | Path] = [current_workdir()]
        candidates.extend(roots or [])
        self._paths: dict[str, Path] = {}
        self._ids_by_path: dict[Path, str] = {}
        for candidate in candidates:
            path = Path(candidate).expanduser().resolve()
            if not path.exists():
                raise ValueError(f"workspace does not exist: {path}")
            if not path.is_dir():
                raise ValueError(f"workspace is not a directory: {path}")
            if path in self._ids_by_path:
                continue
            if any(
                registered in path.parents or path in registered.parents
                for registered in self._ids_by_path
            ):
                raise ValueError(
                    f"authorized workspaces must not overlap: {path}"
                )
            workspace_id = _workspace_id(path)
            if workspace_id in self._paths:
                raise ValueError("workspace identifier collision")
            self._paths[workspace_id] = path
            self._ids_by_path[path] = workspace_id
        self.default_id = self._ids_by_path[current_workdir().resolve()]

    def get(self, workspace_id: str | None = None) -> Path:
        """Return one authorized path, defaulting to the service workspace."""
        if workspace_id is not None and (
            not isinstance(workspace_id, str) or not workspace_id
        ):
            raise ValueError("workspace_id must be a non-empty string")
        selected = self.default_id if workspace_id is None else workspace_id
        path = self._paths.get(selected)
        if path is None:
            raise WorkspaceNotFoundError(selected)
        return path

    def id_for(self, path: str | Path) -> str:
        """Return the ID for an exact authorized directory."""
        resolved = Path(path).expanduser().resolve()
        workspace_id = self._ids_by_path.get(resolved)
        if workspace_id is None:
            raise WorkspaceNotFoundError(str(resolved))
        return workspace_id

    def list(self) -> list[dict]:
        """Return JSON-safe workspace descriptions in registration order."""
        return [
            {
                "id": workspace_id,
                "path": str(path),
                "default": workspace_id == self.default_id,
            }
            for workspace_id, path in self._paths.items()
        ]


def _workspace_id(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    return f"ws-{digest}"
