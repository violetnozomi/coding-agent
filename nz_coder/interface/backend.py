"""Small terminal-backend boundary shared by embedded and remote surfaces."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import queue
from typing import Any, Protocol

from nz_coder.http_service.client import NZCoderClient


class TerminalBackend(Protocol):
    """Operations a terminal frontend needs from a product runtime."""

    session_id: str

    def info(self) -> dict[str, Any]: ...
    def attach_snapshot(self) -> dict[str, Any]: ...
    def start_run(
        self, message: str, *, attachments=(), allowed_tools=(),
        model: str | None = None, command_digest: str | None = None,
    ) -> dict[str, Any]: ...
    def abort(self) -> bool: ...
    def events(self, *, last_event_id: str | None = None) -> Iterator[dict]: ...
    def close(self) -> None: ...


class RemoteControlBackend(TerminalBackend, Protocol):
    """Additional daemon-owned controls used by an attached terminal."""

    def sessions(self) -> list[dict[str, Any]]: ...
    def messages(self) -> list[dict[str, Any]]: ...
    def diff(self) -> list[dict[str, Any]]: ...
    def rename(self, title: str) -> dict[str, Any]: ...
    def fork(self, turn: int | None = None) -> dict[str, Any]: ...
    def delete(self) -> bool: ...
    def undo(self) -> dict[str, Any]: ...
    def redo(self) -> dict[str, Any]: ...
    def export(self) -> dict[str, Any]: ...
    def processes(self) -> list[dict[str, Any]]: ...
    def process(self, process_id: str) -> dict[str, Any]: ...
    def process_read(self, process_id: str, **options) -> dict[str, Any]: ...
    def process_write(self, process_id: str, data: str) -> dict[str, Any]: ...
    def process_resize(self, process_id: str, *, rows: int, cols: int) -> dict[str, Any]: ...
    def process_kill(self, process_id: str) -> dict[str, Any]: ...
    def children(self) -> list[dict[str, Any]]: ...
    def child(self, child_id: str) -> dict[str, Any]: ...
    def agents(self) -> list[dict[str, Any]]: ...
    def workflows(self) -> dict[str, Any]: ...
    def workflow(self, run_id: str) -> dict[str, Any]: ...
    def control_workflow(self, run_id: str, action: str) -> dict[str, Any]: ...
    def prepare_workflow(self, name: str, arguments: dict) -> dict[str, Any]: ...
    def start_workflow(
        self, name: str, arguments: dict, *, approval_digest: str,
    ) -> dict[str, Any]: ...
    def memory(self) -> dict[str, Any]: ...
    def memory_proposal(self, fingerprint: str) -> dict[str, Any]: ...
    def review_memory(
        self, fingerprint: str, action: str, *, reason: str = "",
    ) -> dict[str, Any]: ...
    def select_session(self, session_id: str) -> dict[str, Any]: ...


class RemoteTerminalBackend:
    """Terminal backend backed by the existing authenticated HTTP service."""

    mode = "remote"

    def __init__(self, client: NZCoderClient, session_id: str):
        self.client = client
        self.session_id = str(session_id)

    def info(self) -> dict[str, Any]:
        return self.client.get_session(self.session_id)

    def attach_snapshot(self) -> dict[str, Any]:
        return self.client.attach_snapshot(self.session_id)

    def start_run(
        self, message: str, *, attachments=(), allowed_tools=(),
        model: str | None = None, command_digest: str | None = None,
    ) -> dict[str, Any]:
        options = {"attachments": attachments, "allowed_tools": allowed_tools}
        if model:
            options["model"] = model
        if command_digest:
            options["command_digest"] = command_digest
        return self.client.run(self.session_id, message, **options)

    def commands(self) -> list[dict[str, Any]]:
        return self.client.list_commands(self.session_id)

    def expand_command(self, name: str, arguments: str = "") -> dict[str, Any]:
        return self.client.expand_command(self.session_id, name, arguments)

    def extensions(self) -> list[dict[str, Any]]:
        return self.client.list_extensions(self.session_id)

    def skills(self) -> list[dict[str, Any]]:
        return self.client.list_skills(self.session_id)

    def mcps(self) -> list[dict[str, Any]]:
        return self.client.list_mcps(self.session_id)

    def abort(self) -> bool:
        return bool(self.client.abort(self.session_id).get("aborted"))

    def sessions(self) -> list[dict[str, Any]]:
        return self.client.list_sessions()

    def messages(self) -> list[dict[str, Any]]:
        return self.client.messages(self.session_id)

    def diff(self) -> list[dict[str, Any]]:
        return self.client.diff(self.session_id)

    def rename(self, title: str) -> dict[str, Any]:
        return self.client.rename_session(self.session_id, title)

    def fork(self, turn: int | None = None) -> dict[str, Any]:
        return self.client.fork_session(self.session_id, turn)

    def delete(self) -> bool:
        return self.client.delete_session(self.session_id)

    def undo(self) -> dict[str, Any]:
        return self.client.undo_session(self.session_id)

    def redo(self) -> dict[str, Any]:
        return self.client.redo_session(self.session_id)

    def export(self) -> dict[str, Any]:
        return self.client.export_session(self.session_id)

    def processes(self) -> list[dict[str, Any]]:
        return self.client.list_processes(self.session_id)

    def process(self, process_id: str) -> dict[str, Any]:
        return self.client.get_process(self.session_id, process_id)

    def process_read(self, process_id: str, **options) -> dict[str, Any]:
        return self.client.read_process(self.session_id, process_id, **options)

    def process_write(self, process_id: str, data: str) -> dict[str, Any]:
        return self.client.write_process(self.session_id, process_id, data)

    def process_resize(self, process_id: str, *, rows: int, cols: int) -> dict[str, Any]:
        return self.client.resize_process(
            self.session_id, process_id, rows=rows, cols=cols
        )

    def process_kill(self, process_id: str) -> dict[str, Any]:
        return self.client.kill_process(self.session_id, process_id)

    def children(self) -> list[dict[str, Any]]:
        return self.client.list_children(self.session_id)

    def child(self, child_id: str) -> dict[str, Any]:
        return self.client.get_child(self.session_id, child_id)

    def agents(self) -> list[dict[str, Any]]:
        return self.client.list_agents(self.session_id)

    def workflows(self) -> dict[str, Any]:
        return self.client.list_workflows(self.session_id)

    def workflow(self, run_id: str) -> dict[str, Any]:
        return self.client.get_workflow(self.session_id, run_id)

    def control_workflow(self, run_id: str, action: str) -> dict[str, Any]:
        return self.client.control_workflow(self.session_id, run_id, action)

    def prepare_workflow(self, name: str, arguments: dict) -> dict[str, Any]:
        return self.client.prepare_workflow(self.session_id, name, arguments)

    def start_workflow(
        self, name: str, arguments: dict, *, approval_digest: str,
    ) -> dict[str, Any]:
        return self.client.start_workflow(
            self.session_id,
            name,
            arguments,
            approval_digest=approval_digest,
        )

    def memory(self) -> dict[str, Any]:
        return self.client.memory_status(self.session_id)

    def memory_proposal(self, fingerprint: str) -> dict[str, Any]:
        return self.client.get_memory_proposal(self.session_id, fingerprint)

    def review_memory(
        self, fingerprint: str, action: str, *, reason: str = "",
    ) -> dict[str, Any]:
        return self.client.review_memory(
            self.session_id, fingerprint, action, reason=reason
        )

    def select_session(self, session_id: str) -> dict[str, Any]:
        info = self.client.get_session(session_id)
        self.session_id = str(info["id"])
        return info

    def events(self, *, last_event_id: str | None = None) -> Iterator[dict]:
        """Open exactly one cursor-bound stream; the frontend owns resync."""
        yield from self.client.events(
            self.session_id,
            replay=0,
            last_event_id=last_event_id,
            reconnect_attempts=2,
        )

    def reply_permission(self, request_id: str, reply: str, *, message: str = "") -> bool:
        return self.client.reply_permission(
            self.session_id,
            request_id,
            reply,
            message=message,
        )

    def reply_question(self, request_id: str, answers: list[list[str]]) -> bool:
        return self.client.reply_question(self.session_id, request_id, answers)

    def reject_question(self, request_id: str) -> bool:
        return self.client.reject_question(self.session_id, request_id)

    def close(self) -> None:
        return None


@dataclass
class EmbeddedTerminalBackend:
    """Thin adapter for the existing local Agent/controller pair.

    The embedded CLI remains the owner of its current turn loop.  This adapter
    gives shared render/front-end code a stable view and deliberately does not
    copy Agent execution or interaction state.
    """

    agent: Any
    controller: Any
    session_id: str
    mode: str = "embedded"

    def info(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "status": "running" if getattr(self.controller, "running", False) else "idle",
            "workspace": str(getattr(self.agent, "workspace", "")),
            "model": str(getattr(self.agent, "model_id", "unknown")),
        }

    def attach_snapshot(self) -> dict[str, Any]:
        events = [event.to_dict() for event in self.agent.event_bus.recent()]
        cursor = events[-1].get("meta", {}) if events else {}
        return {
            "schema_version": 1,
            "session": self.info(),
            "messages": [],
            "pending": {"permissions": [], "questions": []},
            "cursor": {
                "event_id": cursor.get("event_id", ""),
                "sequence": cursor.get("sequence", 0),
            },
        }

    def start_run(
        self, message: str, *, attachments=(), allowed_tools=(),
        model: str | None = None, command_digest: str | None = None,
    ) -> dict[str, Any]:
        raise RuntimeError("embedded turn execution is owned by the local terminal loop")

    def abort(self) -> bool:
        cancel = getattr(self.controller, "cancel", None)
        return bool(cancel() if callable(cancel) else False)

    def events(self, *, last_event_id: str | None = None) -> Iterator[dict]:
        subscription = self.agent.event_bus.subscribe(
            replay=0,
            after_event_id=last_event_id,
        ) if last_event_id else self.agent.event_bus.subscribe(replay=0)
        try:
            while True:
                try:
                    yield subscription.get(timeout=0.5).to_dict()
                except queue.Empty:
                    continue
                except (StopIteration, RuntimeError):
                    return
        finally:
            subscription.close()

    def close(self) -> None:
        return None


__all__ = [
    "EmbeddedTerminalBackend",
    "RemoteControlBackend",
    "RemoteTerminalBackend",
    "TerminalBackend",
]
