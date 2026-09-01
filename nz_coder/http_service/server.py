"""Authenticated loopback HTTP and SSE transport for managed Agent sessions."""
from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from nz_coder.foundation.json_safety import (
    json_safe_value,
    reject_nonstandard_json_constant,
)
from nz_coder.protocol.session_events import EventCursorExpiredError, iter_sse
from nz_coder.protocol.public_error import to_public_error
from nz_coder.state.instructions import (
    create_instruction_file,
    delete_instruction_file,
    list_instruction_files,
    set_instruction_file_enabled,
)

from .interactions import InteractionNotFoundError
from .manager import SessionBusyError, SessionManager, SessionNotFoundError
from nz_coder.runtime.process.process_service import (
    ProcessNotFoundError,
    ProcessOwnershipError,
    ProcessStateError,
    workspace_process_service,
)
from nz_coder.interface.timeline import format_transcript
from .workspaces import WorkspaceNotFoundError

_MAX_REQUEST_BYTES = 1024 * 1024
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}


class _LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, service: "SessionHTTPService"):
        self.service = service
        super().__init__(address, _SessionRequestHandler)


class _SessionRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "NZCoderLocal/1"

    @property
    def service(self) -> "SessionHTTPService":
        return self.server.service

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def log_message(self, format: str, *args) -> None:
        return

    def _dispatch(self, method: str) -> None:
        self._response_committed = False
        try:
            parsed = urlsplit(self.path)
            if method == "GET" and parsed.path == "/health":
                self._json(HTTPStatus.OK, self.service.health())
                return
            self._authorize()
            if method == "POST" and parsed.path == "/shutdown":
                if not self.service.allow_shutdown:
                    self._error(
                        HTTPStatus.NOT_FOUND,
                        "route_not_found",
                        "route was not found",
                    )
                    return
                body = self._read_json()
                self._reject_unknown(body, {"nonce"})
                expected = str(self.service.runtime_identity.get("nonce") or "")
                if expected and not hmac.compare_digest(
                    str(body.get("nonce") or "").encode("utf-8"),
                    expected.encode("utf-8"),
                ):
                    raise PermissionError("daemon identity did not match")
                self._json(HTTPStatus.ACCEPTED, {"stopping": True})
                self.service.request_shutdown()
                return
            if parsed.path == "/event" and method == "GET":
                self._serve_events(parse_qs(parsed.query))
                return
            segments = [unquote(item) for item in parsed.path.split("/") if item]
            if segments and segments[0] == "instruction-files":
                self._route_instruction_files(
                    method,
                    segments,
                    parse_qs(parsed.query),
                )
                return
            self._route_session(method, segments)
        except WorkspaceNotFoundError:
            self._error(
                HTTPStatus.NOT_FOUND,
                "workspace_not_found",
                "workspace was not found or is not authorized",
            )
        except EventCursorExpiredError:
            self._error(
                HTTPStatus.GONE,
                "event_cursor_expired",
                "event cursor is outside the bounded replay window",
            )
        except SessionNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "session_not_found", "session was not found")
        except InteractionNotFoundError:
            self._error(
                HTTPStatus.NOT_FOUND,
                "interaction_not_found",
                "interaction request was not found or was already resolved",
            )
        except ProcessNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "process_not_found", "process was not found")
        except ProcessOwnershipError as exc:
            self._error(HTTPStatus.FORBIDDEN, "process_forbidden", str(exc))
        except ProcessStateError as exc:
            self._error(HTTPStatus.CONFLICT, "process_state_error", str(exc))
        except SessionBusyError as exc:
            self._error(HTTPStatus.CONFLICT, "session_busy", str(exc))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, "forbidden", str(exc))
        except _ResponseSent:
            return
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            public = to_public_error(exc)
            self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                public.code,
                public.message,
            )

    def _route_session(self, method: str, segments: list[str]) -> None:
        if segments == ["workspace"] and method == "GET":
            self._json(HTTPStatus.OK, self.service.manager.list_workspaces())
            return
        if segments == ["session"]:
            if method == "GET":
                self._json(HTTPStatus.OK, self.service.manager.list())
                return
            if method == "POST":
                body = self._read_json()
                self._reject_unknown(body, {"permission_mode", "workspace_id"})
                info = self.service.manager.create(
                    body.get("permission_mode"),
                    body.get("workspace_id"),
                )
                self._json(HTTPStatus.CREATED, info)
                return
        if len(segments) >= 2 and segments[0] == "session":
            if len(segments) == 2 and method == "DELETE":
                self.service.manager.remove(segments[1])
                self._json(HTTPStatus.OK, {"deleted": True})
                return
            session = self.service.manager.get(segments[1])
            if len(segments) == 2:
                if method == "GET":
                    self._json(HTTPStatus.OK, self.service.manager.info(session.session_id))
                    return
                if method == "PATCH":
                    body = self._read_json()
                    self._reject_unknown(body, {"title"})
                    self._json(HTTPStatus.OK, self.service.manager.rename(session.session_id, body.get("title")))
                    return
            if len(segments) == 3 and segments[2] == "messages" and method == "GET":
                self._json(HTTPStatus.OK, session.messages())
                return
            if len(segments) == 3 and segments[2] == "diff" and method == "GET":
                self._json(HTTPStatus.OK, session.diff())
                return
            if len(segments) == 3 and segments[2] == "export" and method == "GET":
                self._json(HTTPStatus.OK, {
                    "session_id": session.session_id,
                    "markdown": format_transcript(session.session_id, session.messages(), title=session.title),
                })
                return
            if len(segments) == 3 and segments[2] == "fork" and method == "POST":
                body = self._read_json()
                self._reject_unknown(body, {"turn"})
                raw_turn = body.get("turn")
                turn = int(raw_turn) if raw_turn is not None else None
                self._json(HTTPStatus.CREATED, self.service.manager.fork(session.session_id, turn))
                return
            if len(segments) == 3 and segments[2] in {"undo", "redo"} and method == "POST":
                body = self._read_json()
                self._reject_unknown(body, set())
                operation = getattr(session, segments[2])
                self._json(HTTPStatus.OK, operation())
                return
            if len(segments) == 3 and segments[2] == "snapshot" and method == "GET":
                self._json(HTTPStatus.OK, session.snapshot())
                return
            if len(segments) == 3 and segments[2] == "attach" and method == "GET":
                self._json(HTTPStatus.OK, session.attach_snapshot())
                return
            if len(segments) == 3 and segments[2] == "permission" and method == "GET":
                self._json(HTTPStatus.OK, session.interactions.list("permission"))
                return
            if len(segments) == 3 and segments[2] == "question" and method == "GET":
                self._json(HTTPStatus.OK, session.interactions.list("question"))
                return
            if len(segments) == 3 and segments[2] == "children" and method == "GET":
                from nz_coder.runtime.agent.subagent import list_subagent_sessions

                self._json(
                    HTTPStatus.OK,
                    {"children": list_subagent_sessions(session.session_id, session.workspace)},
                )
                return
            if len(segments) == 4 and segments[2] == "children" and method == "GET":
                from nz_coder.runtime.agent.subagent import load_subagent_session

                child = load_subagent_session(session.session_id, segments[3], session.workspace)
                if not child:
                    raise SessionNotFoundError(segments[3])
                self._json(HTTPStatus.OK, child)
                return
            if len(segments) == 3 and segments[2] == "process" and method == "GET":
                service = workspace_process_service(session.workspace)
                values = []
                for handle in service.list(owner_session_id=session.session_id):
                    item = handle.to_dict()
                    try:
                        result = service.read(
                            handle.process_id,
                            owner_session_id=session.session_id,
                            cursor=-1,
                            max_bytes=1,
                        )
                        item.update({
                            "buffer_start_cursor": result.buffer_start_cursor,
                            "buffer_end_cursor": result.buffer_end_cursor,
                            "buffer_bytes": result.buffer_end_cursor - result.buffer_start_cursor,
                            "pty_tier": "pty" if handle.tty else "pipe",
                        })
                    except (ProcessNotFoundError, ProcessOwnershipError):
                        pass
                    values.append(item)
                self._json(HTTPStatus.OK, {"processes": values})
                return
            if len(segments) == 3 and segments[2] == "command" and method == "GET":
                self._json(HTTPStatus.OK, {"commands": session.commands()})
                return
            if len(segments) == 3 and segments[2] in {"extension", "skill", "mcp"} and method == "GET":
                items = session.extensions()
                if segments[2] == "skill":
                    self._json(HTTPStatus.OK, {
                        "skills": [item for item in items if item.get("kind") == "skill"],
                    })
                elif segments[2] == "mcp":
                    self._json(HTTPStatus.OK, {
                        "mcps": [item for item in items if item.get("kind") == "mcp_server"],
                    })
                else:
                    self._json(HTTPStatus.OK, {"extensions": items})
                return
            if len(segments) == 3 and segments[2] == "agent" and method == "GET":
                self._json(HTTPStatus.OK, {"agents": session.agents()})
                return
            if len(segments) == 3 and segments[2] == "memory" and method == "GET":
                self._json(HTTPStatus.OK, session.memory_status())
                return
            if len(segments) == 4 and segments[2] == "memory" and method == "GET":
                self._json(HTTPStatus.OK, session.memory_proposal(segments[3]))
                return
            if (
                len(segments) == 5
                and segments[2] == "memory"
                and segments[4] in {"approve", "reject"}
                and method == "POST"
            ):
                body = self._read_json()
                self._reject_unknown(
                    body, {"reason"} if segments[4] == "reject" else set()
                )
                self._json(
                    HTTPStatus.OK,
                    session.review_memory(
                        segments[3], segments[4], str(body.get("reason") or "")
                    ),
                )
                return
            if len(segments) == 3 and segments[2] == "workflow" and method == "GET":
                self._json(HTTPStatus.OK, session.workflows())
                return
            if (
                len(segments) == 4
                and segments[2] == "workflow"
                and segments[3] in {"prepare", "run"}
                and method == "POST"
            ):
                body = self._read_json()
                allowed = {"name", "arguments"}
                if segments[3] == "run":
                    allowed.add("approval_digest")
                self._reject_unknown(body, allowed)
                name = body.get("name")
                arguments = body.get("arguments", {})
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("workflow name must be a non-empty string")
                if not isinstance(arguments, dict):
                    raise ValueError("workflow arguments must be an object")
                if segments[3] == "prepare":
                    result = session.prepare_workflow(name, arguments)
                else:
                    result = session.start_workflow(
                        name,
                        arguments,
                        str(body.get("approval_digest") or ""),
                    )
                self._json(HTTPStatus.OK, result)
                return
            if len(segments) == 4 and segments[2] == "workflow" and method == "GET":
                self._json(HTTPStatus.OK, session.workflow(segments[3]))
                return
            if (
                len(segments) == 5
                and segments[2] == "workflow"
                and segments[4] in {"pause", "resume", "stop"}
                and method == "POST"
            ):
                body = self._read_json()
                self._reject_unknown(body, set())
                self._json(
                    HTTPStatus.OK,
                    session.control_workflow(segments[3], segments[4]),
                )
                return
            if (
                len(segments) == 5
                and segments[2] == "command"
                and segments[4] == "expand"
                and method == "POST"
            ):
                body = self._read_json()
                self._reject_unknown(body, {"arguments"})
                self._json(
                    HTTPStatus.OK,
                    session.expand_command(segments[3], body.get("arguments", "")),
                )
                return
            if len(segments) == 4 and segments[2] == "process" and method == "GET":
                service = workspace_process_service(session.workspace)
                handle = service.get(segments[3], owner_session_id=session.session_id)
                item = handle.to_dict()
                result = service.read(segments[3], owner_session_id=session.session_id, cursor=-1, max_bytes=1)
                item.update({
                    "buffer_start_cursor": result.buffer_start_cursor,
                    "buffer_end_cursor": result.buffer_end_cursor,
                    "buffer_bytes": result.buffer_end_cursor - result.buffer_start_cursor,
                    "pty_tier": "pty" if handle.tty else "pipe",
                })
                self._json(HTTPStatus.OK, item)
                return
            if len(segments) == 5 and segments[2] == "process" and method == "POST":
                body = self._read_json()
                operation = segments[4]
                allowed = {
                    "read": {"cursor", "tail_bytes", "max_bytes", "wait_seconds"},
                    "write": {"data"},
                    "resize": {"rows", "cols"},
                    "kill": set(),
                }
                if operation not in allowed:
                    self._error(HTTPStatus.NOT_FOUND, "route_not_found", "process operation was not found")
                    return
                self._reject_unknown(body, allowed[operation])
                service = workspace_process_service(session.workspace)
                process_id = segments[3]
                if operation == "read":
                    result = service.read(
                        process_id,
                        owner_session_id=session.session_id,
                        cursor=body.get("cursor"),
                        tail_bytes=body.get("tail_bytes"),
                        max_bytes=body.get("max_bytes"),
                        wait_seconds=body.get("wait_seconds", 0.0),
                        event_bus=session.event_bus,
                    )
                    self._json(HTTPStatus.OK, result.to_dict())
                    return
                if operation == "write":
                    data = body.get("data")
                    if not isinstance(data, str) or not data:
                        raise ValueError("process data must be a non-empty string")
                    result = service.write(
                        process_id,
                        data,
                        owner_session_id=session.session_id,
                        event_bus=session.event_bus,
                    )
                    self._json(HTTPStatus.OK, result.to_dict())
                    return
                if operation == "resize":
                    rows, cols = body.get("rows"), body.get("cols")
                    if (
                        not isinstance(rows, int) or isinstance(rows, bool)
                        or not isinstance(cols, int) or isinstance(cols, bool)
                        or not 1 <= rows <= 1000 or not 1 <= cols <= 1000
                    ):
                        raise ValueError("process rows and cols must be integers from 1 to 1000")
                    result = service.resize(
                        process_id,
                        rows=rows,
                        cols=cols,
                        owner_session_id=session.session_id,
                        event_bus=session.event_bus,
                    )
                    self._json(HTTPStatus.OK, result.to_dict())
                    return
                if operation == "kill":
                    result = service.kill(process_id, owner_session_id=session.session_id, event_bus=session.event_bus)
                    self._json(HTTPStatus.OK, result.to_dict())
                    return
            if len(segments) == 3 and segments[2] == "run" and method == "POST":
                body = self._read_json()
                self._reject_unknown(body, {"message", "attachments", "allowed_tools", "model"})
                info = self.service.manager.start_run(
                    session.session_id,
                    body.get("message"),
                    attachments=body.get("attachments", ()),
                    allowed_tools=body.get("allowed_tools", ()),
                    model=body.get("model"),
                )
                self._json(HTTPStatus.ACCEPTED, info)
                return
            if len(segments) == 3 and segments[2] == "abort" and method == "POST":
                body = self._read_json()
                self._reject_unknown(body, set())
                self._json(HTTPStatus.OK, {"aborted": session.abort()})
                return
            if (
                len(segments) == 5
                and segments[2] == "permission"
                and segments[4] == "reply"
                and method == "POST"
            ):
                body = self._read_json()
                self._reject_unknown(body, {"reply", "message"})
                message = body.get("message", "")
                if not isinstance(message, str):
                    raise ValueError("permission message must be a string")
                session.interactions.reply_permission(
                    segments[3],
                    body.get("reply"),
                    message=message,
                )
                self._json(HTTPStatus.OK, {"replied": True})
                return
            if (
                len(segments) == 5
                and segments[2] == "question"
                and segments[4] == "reply"
                and method == "POST"
            ):
                body = self._read_json()
                self._reject_unknown(body, {"answers"})
                session.interactions.reply_question(segments[3], body.get("answers"))
                self._json(HTTPStatus.OK, {"replied": True})
                return
            if (
                len(segments) == 5
                and segments[2] == "question"
                and segments[4] == "reject"
                and method == "POST"
            ):
                body = self._read_json()
                self._reject_unknown(body, set())
                session.interactions.reject_question(segments[3])
                self._json(HTTPStatus.OK, {"rejected": True})
                return
        self._error(HTTPStatus.NOT_FOUND, "route_not_found", "route was not found")

    def _route_instruction_files(
        self,
        method: str,
        segments: list[str],
        query: dict[str, list[str]],
    ) -> None:
        """Expose InfCode-compatible instruction-file control semantics."""
        unknown_query = sorted(set(query) - {"scope", "workspace_id"})
        if unknown_query:
            raise ValueError(f"unknown query parameter: {unknown_query[0]}")
        workspace_id = self._single_query(query, "workspace_id") or None
        workspace = self.service.manager.workspaces.get(workspace_id)

        if segments == ["instruction-files"] and method == "GET":
            scope = self._single_query(query, "scope") or "project"
            self._json(
                HTTPStatus.OK,
                list_instruction_files(workspace, scope).as_dict(),
            )
            return
        if segments == ["instruction-files"] and method == "POST":
            body = self._read_json()
            self._reject_unknown(body, {"scope"})
            scope = body.get("scope", "project")
            if not isinstance(scope, str):
                raise ValueError("instruction scope must be a string")
            try:
                created = create_instruction_file(workspace, scope)
            except FileExistsError as error:
                raise ValueError("instruction AGENTS.md already exists") from error
            self._json(HTTPStatus.OK, created.as_dict())
            return
        if (
            len(segments) == 4
            and segments[3] == "enabled"
            and method == "PATCH"
        ):
            body = self._read_json()
            self._reject_unknown(body, {"enabled"})
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError("enabled must be a boolean")
            updated = set_instruction_file_enabled(
                workspace,
                segments[1],
                segments[2],
                enabled,
            )
            self._json(HTTPStatus.OK, updated.as_dict())
            return
        if len(segments) == 3 and method == "DELETE":
            delete_instruction_file(workspace, segments[1], segments[2])
            self._json(HTTPStatus.OK, {"ok": True})
            return
        self._error(HTTPStatus.NOT_FOUND, "route_not_found", "route was not found")

    def _serve_events(self, query: dict[str, list[str]]) -> None:
        session_id = self._single_query(query, "session_id", required=True)
        replay_text = self._single_query(query, "replay") or "256"
        try:
            replay = int(replay_text)
        except ValueError as exc:
            raise ValueError("replay must be an integer") from exc
        replay = min(256, max(0, replay))
        types_text = self._single_query(query, "types")
        event_types = None
        if types_text:
            event_types = {item for item in types_text.split(",") if item}
        session = self.service.manager.get(session_id)
        after_event_id = self.headers.get("Last-Event-ID") or None
        subscription = session.event_bus.subscribe(
            event_types,
            max_queue=self.service.event_queue_size,
            replay=0 if after_event_id is not None else replay,
            after_event_id=after_event_id,
        )
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self._response_committed = True
            for frame in iter_sse(
                subscription,
                heartbeat_seconds=self.service.heartbeat_seconds,
            ):
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        finally:
            subscription.close()
            self.close_connection = True

    def _authorize(self) -> None:
        if self.headers.get("Origin"):
            raise PermissionError("browser origins are not allowed in the local API")
        expected = f"Bearer {self.service.token}"
        provided = self.headers.get("Authorization", "")
        if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
            self._error(HTTPStatus.UNAUTHORIZED, "unauthorized", "invalid bearer token")
            raise _ResponseSent

    def _read_json(self) -> dict:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ValueError("Content-Type must be application/json")
        length_text = self.headers.get("Content-Length")
        if length_text is None:
            raise ValueError("Content-Length is required")
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > _MAX_REQUEST_BYTES:
            raise ValueError("request body exceeds 1 MiB limit")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                parse_constant=reject_nonstandard_json_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    @staticmethod
    def _reject_unknown(payload: dict, allowed: set[str]) -> None:
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"unknown request field: {unknown[0]}")

    @staticmethod
    def _single_query(
        query: dict[str, list[str]],
        name: str,
        *,
        required: bool = False,
    ) -> str:
        values = query.get(name, [])
        if len(values) > 1:
            raise ValueError(f"query parameter {name} must appear once")
        value = values[0] if values else ""
        if required and not value:
            raise ValueError(f"query parameter {name} is required")
        return value

    def _json(self, status: int, payload) -> None:
        data = json.dumps(
            json_safe_value(payload),
            ensure_ascii=False,
            default=str,
            allow_nan=False,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self._response_committed = True
        self.wfile.write(data)

    def _error(self, status: int, code: str, message: str) -> None:
        if getattr(self, "_response_committed", False):
            self.close_connection = True
            return
        self.close_connection = True
        self._json(status, {"error": {"code": code, "message": message}})


class _ResponseSent(Exception):
    pass


class SessionHTTPService:
    """Own the loopback server, bearer secret, and managed Agent sessions."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 4096,
        token: str | None = None,
        manager: SessionManager | None = None,
        heartbeat_seconds: float = 10.0,
        interaction_timeout_seconds: float = 300.0,
        workspace_roots: list[str] | None = None,
        restore_saved_sessions: bool = True,
        event_queue_size: int = 256,
        runtime_identity: dict | None = None,
        allow_shutdown: bool = False,
    ):
        if host not in _LOOPBACK_HOSTS:
            raise ValueError("phase-1 HTTP service only accepts 127.0.0.1 or localhost")
        if not isinstance(port, int) or isinstance(port, bool) or not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if token is not None and len(token) < 16:
            raise ValueError("bearer token must contain at least 16 characters")
        if (
            not isinstance(event_queue_size, int)
            or isinstance(event_queue_size, bool)
            or not 1 <= event_queue_size <= 4096
        ):
            raise ValueError("event_queue_size must be between 1 and 4096")
        try:
            heartbeat = float(heartbeat_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("heartbeat must be a positive finite number") from exc
        if not math.isfinite(heartbeat) or heartbeat <= 0:
            raise ValueError("heartbeat must be a positive finite number")
        self.manager = manager or SessionManager(
            interaction_timeout_seconds=interaction_timeout_seconds,
            workspace_roots=workspace_roots,
            restore_saved=restore_saved_sessions,
        )
        self.token = token or secrets.token_urlsafe(32)
        self.heartbeat_seconds = max(0.05, heartbeat)
        self.event_queue_size = event_queue_size
        self.started_at = time.time()
        self.runtime_identity = dict(runtime_identity or {})
        self.allow_shutdown = bool(allow_shutdown)
        self._server = _LocalHTTPServer((host, port), self)
        self._close_lock = threading.Lock()
        self._closed = False

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    @property
    def base_url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}"

    def health(self) -> dict:
        """Return secret-free runtime identity for ownership validation."""
        if not self.runtime_identity:
            return {"status": "ok", "service": "nz-coder"}
        payload = {
            "status": "ok",
            "service": "nz-coder",
            "pid": os.getpid(),
            "started_at": self.started_at,
        }
        if self.runtime_identity:
            payload["runtime"] = dict(self.runtime_identity)
        return payload

    def request_shutdown(self) -> None:
        """Schedule shutdown outside the active request-handler thread."""
        threading.Thread(
            target=self.shutdown,
            name="nz-http-shutdown",
            daemon=True,
        ).start()

    def serve_forever(self) -> None:
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        """Stop a server running in another thread and release all sessions."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._server.shutdown()
        self._server.server_close()
        self.manager.close()

    def close_after_serve(self) -> None:
        """Release resources after serve_forever already returned."""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._server.server_close()
        self.manager.close()
