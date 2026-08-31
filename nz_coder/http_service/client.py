"""Small standard-library client for the optional local Session HTTP API."""
from __future__ import annotations

import json
import math
import re
import time
from typing import Iterator
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from nz_coder.foundation.json_safety import reject_nonstandard_json_constant

_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class NZCoderHTTPError(RuntimeError):
    """HTTP error response returned by the local NZ-Coder service."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"HTTP {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class NZCoderClient:
    """Authenticated client for session CRUD, runs, aborts, and SSE events."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        normalized = base_url.rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("NZCoderClient only accepts a loopback HTTP base URL")
        if isinstance(timeout, bool):
            raise ValueError("client timeout must be a positive finite number")
        try:
            request_timeout = float(timeout)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("client timeout must be a positive finite number") from exc
        if (
            not math.isfinite(request_timeout)
            or request_timeout <= 0
            or request_timeout > 600
        ):
            raise ValueError(
                "client timeout must be a positive finite number no greater than 600 seconds"
            )
        self.base_url = normalized
        self.token = token
        self.timeout = request_timeout
        # This client is loopback-only. Environment HTTP proxies must never
        # receive the bearer token or intercept localhost traffic.
        self._opener = build_opener(ProxyHandler({}))

    def health(self) -> dict:
        return self._request("GET", "/health", authenticated=False)

    def list_sessions(self) -> list[dict]:
        return self._request("GET", "/session")

    def list_workspaces(self) -> list[dict]:
        return self._request("GET", "/workspace")

    def list_instruction_files(
        self,
        scope: str = "project",
        workspace_id: str | None = None,
    ) -> dict:
        query = {"scope": scope}
        if workspace_id is not None:
            query["workspace_id"] = workspace_id
        return self._request("GET", f"/instruction-files?{urlencode(query)}")

    def create_instruction_file(
        self,
        scope: str = "project",
        workspace_id: str | None = None,
    ) -> dict:
        path = self._instruction_path("/instruction-files", workspace_id)
        return self._request("POST", path, {"scope": scope})

    def set_instruction_file_enabled(
        self,
        scope: str,
        filename: str,
        enabled: bool,
        workspace_id: str | None = None,
    ) -> dict:
        base = (
            f"/instruction-files/{quote(scope, safe='')}/"
            f"{quote(filename, safe='')}/enabled"
        )
        return self._request(
            "PATCH",
            self._instruction_path(base, workspace_id),
            {"enabled": enabled},
        )

    def delete_instruction_file(
        self,
        scope: str,
        filename: str,
        workspace_id: str | None = None,
    ) -> bool:
        base = (
            f"/instruction-files/{quote(scope, safe='')}/"
            f"{quote(filename, safe='')}"
        )
        payload = self._request(
            "DELETE",
            self._instruction_path(base, workspace_id),
        )
        return bool(payload.get("ok"))

    @staticmethod
    def _instruction_path(path: str, workspace_id: str | None) -> str:
        if workspace_id is None:
            return path
        return f"{path}?{urlencode({'workspace_id': workspace_id})}"

    def create_session(
        self,
        permission_mode: str | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        body = {}
        if permission_mode is not None:
            body["permission_mode"] = permission_mode
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        return self._request("POST", "/session", body)

    def get_session(self, session_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}")

    def rename_session(self, session_id: str, title: str) -> dict:
        return self._request("PATCH", f"/session/{session_id}", {"title": title})

    def fork_session(self, session_id: str, turn: int | None = None) -> dict:
        body = {"turn": int(turn)} if turn is not None else {}
        return self._request("POST", f"/session/{session_id}/fork", body)

    def undo_session(self, session_id: str) -> dict:
        return self._request("POST", f"/session/{session_id}/undo", {})

    def redo_session(self, session_id: str) -> dict:
        return self._request("POST", f"/session/{session_id}/redo", {})

    def export_session(self, session_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}/export")

    def messages(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/messages")

    def diff(self, session_id: str) -> list[dict]:
        """Fetch the latest bounded snapshot-derived Session diff."""
        return self._request("GET", f"/session/{session_id}/diff")

    def snapshot(self, session_id: str) -> dict:
        """Fetch an idle structured message snapshot and resume cursor."""
        return self._request("GET", f"/session/{session_id}/snapshot")

    def attach_snapshot(self, session_id: str) -> dict:
        """Fetch a running-safe terminal baseline and resume cursor."""
        return self._request("GET", f"/session/{session_id}/attach")

    def shutdown(self, *, nonce: str = "") -> bool:
        """Request graceful shutdown from a daemon-enabled service."""
        payload = self._request("POST", "/shutdown", {"nonce": nonce})
        return bool(payload.get("stopping"))

    def run(
        self,
        session_id: str,
        message: str,
        *,
        attachments=(),
        allowed_tools=(),
        model: str | None = None,
    ) -> dict:
        """Start a run and let the daemon resolve workspace-owned attachments."""
        body = {"message": message}
        paths = [str(path) for path in attachments]
        if paths:
            body["attachments"] = paths
        tools = [str(name) for name in allowed_tools]
        if tools:
            body["allowed_tools"] = tools
        if model:
            body["model"] = str(model)
        return self._request("POST", f"/session/{session_id}/run", body)

    def list_commands(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/command")
        return list(payload.get("commands") or [])

    def expand_command(self, session_id: str, name: str, arguments: str = "") -> dict:
        encoded = quote(str(name), safe="")
        return self._request(
            "POST",
            f"/session/{session_id}/command/{encoded}/expand",
            {"arguments": str(arguments)},
        )

    def list_extensions(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/extension")
        return list(payload.get("extensions") or [])

    def list_skills(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/skill")
        return list(payload.get("skills") or [])

    def list_mcps(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/mcp")
        return list(payload.get("mcps") or [])

    def list_agents(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/agent")
        return list(payload.get("agents") or [])

    def list_workflows(self, session_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}/workflow")

    def get_workflow(self, session_id: str, run_id: str) -> dict:
        encoded = quote(str(run_id), safe="")
        return self._request("GET", f"/session/{session_id}/workflow/{encoded}")

    def control_workflow(self, session_id: str, run_id: str, action: str) -> dict:
        if action not in {"pause", "resume", "stop"}:
            raise ValueError("workflow action must be pause, resume, or stop")
        encoded = quote(str(run_id), safe="")
        return self._request(
            "POST",
            f"/session/{session_id}/workflow/{encoded}/{action}",
            {},
        )

    def prepare_workflow(self, session_id: str, name: str, arguments: dict) -> dict:
        return self._request(
            "POST",
            f"/session/{session_id}/workflow/prepare",
            {"name": str(name), "arguments": dict(arguments)},
        )

    def start_workflow(
        self,
        session_id: str,
        name: str,
        arguments: dict,
        *,
        approval_digest: str,
    ) -> dict:
        return self._request(
            "POST",
            f"/session/{session_id}/workflow/run",
            {
                "name": str(name),
                "arguments": dict(arguments),
                "approval_digest": str(approval_digest),
            },
        )

    def memory_status(self, session_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}/memory")

    def get_memory_proposal(self, session_id: str, fingerprint: str) -> dict:
        encoded = quote(str(fingerprint), safe="")
        return self._request("GET", f"/session/{session_id}/memory/{encoded}")

    def review_memory(
        self,
        session_id: str,
        fingerprint: str,
        action: str,
        *,
        reason: str = "",
    ) -> dict:
        if action not in {"approve", "reject"}:
            raise ValueError("memory action must be approve or reject")
        encoded = quote(str(fingerprint), safe="")
        return self._request(
            "POST",
            f"/session/{session_id}/memory/{encoded}/{action}",
            {"reason": str(reason)} if action == "reject" else {},
        )

    def abort(self, session_id: str) -> dict:
        return self._request("POST", f"/session/{session_id}/abort", {})

    def list_processes(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/process")
        return list(payload.get("processes") or [])

    def get_process(self, session_id: str, process_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}/process/{process_id}")

    def read_process(
        self,
        session_id: str,
        process_id: str,
        *,
        cursor: int | None = None,
        tail_bytes: int | None = None,
        max_bytes: int | None = None,
        wait_seconds: float = 0.0,
    ) -> dict:
        body = {"wait_seconds": wait_seconds}
        for key, value in (("cursor", cursor), ("tail_bytes", tail_bytes), ("max_bytes", max_bytes)):
            if value is not None:
                body[key] = value
        return self._request(
            "POST",
            f"/session/{session_id}/process/{process_id}/read",
            body,
        )

    def kill_process(self, session_id: str, process_id: str) -> dict:
        return self._request(
            "POST",
            f"/session/{session_id}/process/{process_id}/kill",
            {},
        )

    def write_process(self, session_id: str, process_id: str, data: str) -> dict:
        return self._request(
            "POST",
            f"/session/{session_id}/process/{process_id}/write",
            {"data": str(data)},
        )

    def resize_process(
        self, session_id: str, process_id: str, *, rows: int, cols: int,
    ) -> dict:
        return self._request(
            "POST",
            f"/session/{session_id}/process/{process_id}/resize",
            {"rows": int(rows), "cols": int(cols)},
        )

    def pending_permissions(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/permission")

    def reply_permission(
        self,
        session_id: str,
        request_id: str,
        reply: str,
        *,
        message: str = "",
    ) -> bool:
        payload = {"reply": reply}
        if message:
            payload["message"] = message
        result = self._request(
            "POST",
            f"/session/{session_id}/permission/{request_id}/reply",
            payload,
        )
        return bool(result.get("replied"))

    def pending_questions(self, session_id: str) -> list[dict]:
        return self._request("GET", f"/session/{session_id}/question")

    def list_children(self, session_id: str) -> list[dict]:
        payload = self._request("GET", f"/session/{session_id}/children")
        return list(payload.get("children") or [])

    def get_child(self, session_id: str, child_id: str) -> dict:
        return self._request("GET", f"/session/{session_id}/children/{child_id}")

    def reply_question(
        self,
        session_id: str,
        request_id: str,
        answers: list[list[str]],
    ) -> bool:
        result = self._request(
            "POST",
            f"/session/{session_id}/question/{request_id}/reply",
            {"answers": answers},
        )
        return bool(result.get("replied"))

    def reject_question(self, session_id: str, request_id: str) -> bool:
        result = self._request(
            "POST",
            f"/session/{session_id}/question/{request_id}/reject",
            {},
        )
        return bool(result.get("rejected"))

    def delete_session(self, session_id: str) -> bool:
        payload = self._request("DELETE", f"/session/{session_id}")
        return bool(payload.get("deleted"))

    def events(
        self,
        session_id: str,
        *,
        replay: int = 256,
        event_types: list[str] | None = None,
        last_event_id: str | None = None,
        reconnect_attempts: int = 0,
        reconnect_delay: float = 0.25,
    ) -> Iterator[dict]:
        if (
            not isinstance(reconnect_attempts, int)
            or isinstance(reconnect_attempts, bool)
            or reconnect_attempts < 0
        ):
            raise ValueError("reconnect_attempts must be a non-negative integer")
        if last_event_id is not None and (
            not isinstance(last_event_id, str) or not last_event_id
            or not _EVENT_ID_RE.fullmatch(last_event_id)
        ):
            raise ValueError("last_event_id must be a valid event ID")
        delay = float(reconnect_delay)
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("reconnect_delay must be a non-negative finite number")
        query = {"session_id": session_id, "replay": str(replay)}
        if event_types:
            query["types"] = ",".join(event_types)
        cursor = last_event_id
        reconnects = 0
        while True:
            headers = {"Authorization": f"Bearer {self.token}"}
            if cursor is not None:
                headers["Last-Event-ID"] = cursor
            request = Request(
                f"{self.base_url}/event?{urlencode(query)}",
                headers=headers,
                method="GET",
            )
            try:
                response = self._opener.open(request, timeout=self.timeout)
                with response:
                    for payload, frame_id in self._iter_sse_response(response):
                        yield payload
                        if frame_id:
                            cursor = frame_id
            except HTTPError as exc:
                self._raise_http_error(exc)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                if reconnects >= reconnect_attempts:
                    raise
            if reconnects >= reconnect_attempts:
                return
            reconnects += 1
            if delay:
                time.sleep(delay)

    def resilient_events(
        self,
        session_id: str,
        *,
        event_types: list[str] | None = None,
        reconnect_attempts: int = 1,
        reconnect_delay: float = 0.25,
        resync_attempts: int = 3,
        settle_timeout: float = 30.0,
        settle_poll_interval: float = 0.1,
    ) -> Iterator[dict]:
        """Stream from a snapshot cursor and automatically repair known gaps.

        Synthetic ``server.snapshot`` frames establish each new reducer
        baseline. A ``server.event_gap`` frame is forwarded before the client
        waits for an idle snapshot and reconnects from its fresh cursor.
        """
        if (
            not isinstance(resync_attempts, int)
            or isinstance(resync_attempts, bool)
            or resync_attempts < 0
        ):
            raise ValueError("resync_attempts must be a non-negative integer")
        timeout = float(settle_timeout)
        poll_interval = float(settle_poll_interval)
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("settle_timeout must be a non-negative finite number")
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError(
                "settle_poll_interval must be a positive finite number"
            )

        snapshot = self._snapshot_when_idle(
            session_id,
            timeout=timeout,
            poll_interval=poll_interval,
        )
        yield {"type": "server.snapshot", "properties": snapshot}
        cursor = str(snapshot["cursor"]["event_id"])
        resyncs = 0

        while True:
            gap = False
            stream = self.events(
                session_id,
                replay=0,
                event_types=event_types,
                last_event_id=cursor,
                reconnect_attempts=reconnect_attempts,
                reconnect_delay=reconnect_delay,
            )
            try:
                for payload in stream:
                    yield payload
                    if payload.get("type") == "server.event_gap":
                        gap = True
                        break
            except NZCoderHTTPError as exc:
                if exc.code != "event_cursor_expired":
                    raise
                gap = True
                yield {
                    "type": "server.event_gap",
                    "properties": {
                        "reason": "event_cursor_expired",
                        "resume_required": True,
                    },
                }
            finally:
                stream.close()
            if not gap:
                return
            if resyncs >= resync_attempts:
                raise RuntimeError("event stream exceeded its snapshot resync limit")
            resyncs += 1
            snapshot = self._snapshot_when_idle(
                session_id,
                timeout=timeout,
                poll_interval=poll_interval,
            )
            yield {"type": "server.snapshot", "properties": snapshot}
            cursor = str(snapshot["cursor"]["event_id"])

    def _snapshot_when_idle(
        self,
        session_id: str,
        *,
        timeout: float,
        poll_interval: float,
    ) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            try:
                return self.snapshot(session_id)
            except NZCoderHTTPError as exc:
                if exc.code != "session_busy" or time.monotonic() >= deadline:
                    raise
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    @staticmethod
    def _iter_sse_response(response):
        data_lines: list[str] = []
        frame_id = ""
        for raw_line in response:
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line:
                if data_lines:
                    yield json.loads(
                        "\n".join(data_lines),
                        parse_constant=reject_nonstandard_json_constant,
                    ), frame_id
                data_lines = []
                frame_id = ""
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
            elif line.startswith("id:"):
                frame_id = line[3:].lstrip(" ")

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        authenticated: bool = True,
    ):
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(
                body,
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(
                    response.read().decode("utf-8"),
                    parse_constant=reject_nonstandard_json_constant,
                )
        except HTTPError as exc:
            self._raise_http_error(exc)

    @staticmethod
    def _raise_http_error(exc: HTTPError) -> None:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            error = {}
        raise NZCoderHTTPError(
            exc.code,
            str(error.get("code") or "http_error"),
            str(error.get("message") or exc.reason),
        ) from exc
