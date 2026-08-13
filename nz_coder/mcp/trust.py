"""User-owned trust records for project-local MCP command execution."""
from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path


class MCPTrustStore:
    """Persist command fingerprints outside project-controlled configuration."""

    _lock = threading.RLock()

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()

    @staticmethod
    def _key(workspace: Path, server_name: str) -> str:
        return f"{workspace.resolve()}::{server_name}"

    def is_trusted(self, workspace: Path, server_name: str, fingerprint: str) -> bool:
        if not fingerprint:
            return False
        return self._read().get(self._key(workspace, server_name)) == fingerprint

    def trust(self, workspace: Path, server_name: str, fingerprint: str) -> None:
        if not fingerprint:
            raise ValueError("MCP command fingerprint cannot be empty")
        with self._lock:
            entries = self._read_unlocked()
            entries[self._key(workspace, server_name)] = fingerprint
            self._write_unlocked(entries)

    def remove(self, workspace: Path, server_name: str) -> bool:
        with self._lock:
            entries = self._read_unlocked()
            removed = entries.pop(self._key(workspace, server_name), None) is not None
            if removed:
                self._write_unlocked(entries)
            return removed

    def _read(self) -> dict[str, str]:
        with self._lock:
            return self._read_unlocked()

    def _read_unlocked(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid MCP trust store: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Invalid MCP trust store format")
        entries = payload.get("entries")
        if not isinstance(entries, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in entries.items()
        ):
            raise ValueError("Invalid MCP trust store entries")
        return dict(entries)

    def _write_unlocked(self, entries: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(
                    {"version": 1, "entries": dict(sorted(entries.items()))},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()
