"""Optional loopback HTTP API for NZ-Coder sessions and live events."""
from __future__ import annotations

from .client import NZCoderClient, NZCoderHTTPError
from .interactions import InteractionBroker, InteractionNotFoundError
from .manager import SessionBusyError, SessionManager, SessionNotFoundError
from .server import SessionHTTPService
from .daemon import (
    DaemonPaths,
    daemon_main,
    daemon_paths,
    daemon_status,
    start_daemon,
    stop_daemon,
)
from .workspaces import WorkspaceNotFoundError, WorkspaceRegistry
from nz_coder.protocol.session_events import EventCursorExpiredError

__all__ = [
    "NZCoderClient",
    "NZCoderHTTPError",
    "InteractionBroker",
    "InteractionNotFoundError",
    "EventCursorExpiredError",
    "SessionBusyError",
    "SessionHTTPService",
    "SessionManager",
    "SessionNotFoundError",
    "WorkspaceNotFoundError",
    "WorkspaceRegistry",
    "DaemonPaths",
    "daemon_main",
    "daemon_paths",
    "daemon_status",
    "start_daemon",
    "stop_daemon",
]
