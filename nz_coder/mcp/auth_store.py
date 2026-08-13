"""URL-bound, permission-restricted persistence for MCP OAuth credentials."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any

from nz_coder.mcp.client import MCPError

_MAX_STORE_BYTES = 1024 * 1024
_STORE_LOCK = threading.RLock()


def default_auth_store_path() -> Path:
    """Return the user-level credential path, never a project path."""
    configured = os.environ.get("NZ_MCP_AUTH_STORE", "").strip()
    if configured:
        return Path(os.path.abspath(Path(configured).expanduser()))
    return Path(os.path.abspath(Path.home() / ".nz-coder" / "oauth" / "mcp-auth.json"))


class MCPOAuthStore:
    """Persist tokens and dynamic client information with mode ``0600``."""

    def __init__(self, path: Path | None = None):
        self.path = Path(os.path.abspath(Path(path or default_auth_store_path()).expanduser()))

    def get(self, server_name: str, server_url: str) -> dict[str, Any] | None:
        with _STORE_LOCK:
            entry = self._load().get("servers", {}).get(server_name)
        if not isinstance(entry, dict) or entry.get("server_url") != server_url:
            return None
        return json.loads(json.dumps(entry))

    def set_fields(
        self,
        server_name: str,
        server_url: str,
        **fields: Any,
    ) -> None:
        with _STORE_LOCK:
            document = self._load()
            servers = document.setdefault("servers", {})
            current = servers.get(server_name)
            entry = (
                dict(current)
                if isinstance(current, dict) and current.get("server_url") == server_url
                else {"server_url": server_url}
            )
            for key, value in fields.items():
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            servers[server_name] = entry
            self._write(document)

    def remove(self, server_name: str) -> bool:
        with _STORE_LOCK:
            document = self._load()
            servers = document.setdefault("servers", {})
            existed = server_name in servers
            servers.pop(server_name, None)
            if existed:
                self._write(document)
            return existed

    def status(self, server_name: str, server_url: str) -> str:
        entry = self.get(server_name, server_url)
        tokens = entry.get("tokens") if entry else None
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return "not_authenticated"
        expires_at = tokens.get("expires_at")
        if isinstance(expires_at, (int, float)) and expires_at <= time.time():
            return "expired"
        return "authenticated"

    def _load(self) -> dict[str, Any]:
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {"version": 1, "servers": {}}
        except OSError as exc:
            raise MCPError("Unable to inspect MCP OAuth credential store") from exc
        try:
            parent_info = self.path.parent.lstat()
        except OSError as exc:
            raise MCPError("Unable to inspect MCP OAuth credential directory") from exc
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or stat.S_ISLNK(parent_info.st_mode)
            or stat.S_IMODE(parent_info.st_mode) != 0o700
        ):
            raise MCPError("MCP OAuth credential directory permissions must be 0700")
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise MCPError("MCP OAuth credential store must be a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise MCPError("MCP OAuth credential store permissions must be 0600")
        if info.st_size > _MAX_STORE_BYTES:
            raise MCPError("MCP OAuth credential store exceeds 1 MiB")
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            fd = os.open(self.path, flags)
            try:
                opened = os.fstat(fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or stat.S_IMODE(opened.st_mode) != 0o600
                    or opened.st_size > _MAX_STORE_BYTES
                ):
                    raise MCPError("MCP OAuth credential store is invalid")
                chunks: list[bytes] = []
                remaining = _MAX_STORE_BYTES + 1
                while remaining > 0:
                    chunk = os.read(fd, min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                os.close(fd)
            payload = b"".join(chunks)
            if len(payload) > _MAX_STORE_BYTES:
                raise MCPError("MCP OAuth credential store exceeds 1 MiB")
            value = json.loads(payload.decode("utf-8"))
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError("Unable to read MCP OAuth credential store") from exc
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("servers"), dict)
        ):
            raise MCPError("MCP OAuth credential store schema is invalid")
        return value

    def _write(self, document: dict[str, Any]) -> None:
        payload = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(payload) > _MAX_STORE_BYTES:
            raise MCPError("MCP OAuth credential store exceeds 1 MiB")
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            parent_info = parent.lstat()
        except OSError as exc:
            raise MCPError("Unable to inspect MCP OAuth credential directory") from exc
        if not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode):
            raise MCPError("MCP OAuth credential directory must be a directory")
        if stat.S_IMODE(parent_info.st_mode) != 0o700:
            raise MCPError("MCP OAuth credential directory permissions must be 0700")
        temporary = parent / f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(temporary, flags, 0o600)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
