from __future__ import annotations

from nz_coder.http_service.client import NZCoderHTTPError
from nz_coder.interface.backend import RemoteTerminalBackend
from nz_coder.interface.remote import _remote_command_registry


class _GapClient:
    def __init__(self):
        self.event_calls = 0
        self.snapshots = 0

    def events(self, *_args, **_kwargs):
        self.event_calls += 1

        def stream():
            raise NZCoderHTTPError(410, "event_cursor_expired", "gone")
            yield  # pragma: no cover

        return stream()

    def attach_snapshot(self, _session_id):
        self.snapshots += 1
        return {
            "settled": True,
            "session": {"id": "session-1", "running": False},
            "messages": [],
            "events": [],
            "pending": {"permissions": [], "questions": []},
            "cursor": {"event_id": "fresh", "sequence": 2},
        }


def test_cursor_expiry_has_single_resync_owner():
    client = _GapClient()
    backend = RemoteTerminalBackend(client, "session-1")

    import pytest

    with pytest.raises(NZCoderHTTPError, match="event_cursor_expired"):
        list(backend.events(last_event_id="expired"))
    assert client.event_calls == 1
    assert client.snapshots == 0


class _ControlClient:
    def __init__(self):
        self.selected = "parent"

    def get_session(self, session_id):
        self.selected = session_id
        return {"id": session_id, "status": "idle"}

    def list_sessions(self):
        return [{"id": self.selected}]

    def messages(self, session_id):
        return [{"role": "user", "content": session_id}]

    def diff(self, session_id):
        return [{"file": f"{session_id}.py"}]

    def rename_session(self, session_id, title):
        return {"id": session_id, "title": title}

    def fork_session(self, session_id, turn):
        return {"id": "child", "parent_session_id": session_id, "turn": turn}

    def delete_session(self, _session_id):
        return True

    def undo_session(self, session_id):
        return {"session_id": session_id, "operation": "undo"}

    def redo_session(self, session_id):
        return {"session_id": session_id, "operation": "redo"}

    def export_session(self, session_id):
        return {"session_id": session_id, "markdown": "# Transcript"}

    def list_processes(self, session_id):
        return [{"process_id": "proc_1", "owner_session_id": session_id}]

    def get_process(self, session_id, process_id):
        return {"process_id": process_id, "owner_session_id": session_id}

    def read_process(self, session_id, process_id, **options):
        return {"process_id": process_id, "session_id": session_id, **options}

    def kill_process(self, session_id, process_id):
        return {"process_id": process_id, "owner_session_id": session_id, "status": "killed"}

    def write_process(self, session_id, process_id, data):
        return {"process_id": process_id, "owner_session_id": session_id, "data": data}

    def resize_process(self, session_id, process_id, *, rows, cols):
        return {
            "process_id": process_id,
            "owner_session_id": session_id,
            "rows": rows,
            "cols": cols,
        }

    def list_children(self, session_id):
        return [{"session_id": "agent-child", "parent_session_id": session_id}]

    def get_child(self, session_id, child_id):
        return {"session_id": child_id, "parent_session_id": session_id}

    def run(self, session_id, message, *, attachments=(), allowed_tools=(), model=None):
        return {
            "session_id": session_id,
            "message": message,
            "attachments": list(attachments),
            **({"allowed_tools": list(allowed_tools)} if allowed_tools else {}),
            **({"model": model} if model else {}),
        }

    def list_commands(self, session_id):
        return [{"name": "review", "session_id": session_id}]

    def expand_command(self, session_id, name, arguments):
        return {
            "name": name,
            "prompt": f"Review {arguments}",
            "session_id": session_id,
        }

    def list_extensions(self, session_id):
        return [{"extension_id": "skill:review", "session_id": session_id}]

    def list_skills(self, session_id):
        return [{"name": "review", "session_id": session_id}]

    def list_mcps(self, session_id):
        return [{"name": "docs", "session_id": session_id}]


def test_remote_backend_session_and_process_controls_delegate_to_runtime_truth():
    client = _ControlClient()
    backend = RemoteTerminalBackend(client, "parent")

    assert backend.sessions() == [{"id": "parent"}]
    assert backend.messages()[0]["content"] == "parent"
    assert backend.rename("Renamed")["title"] == "Renamed"
    assert backend.fork(1)["parent_session_id"] == "parent"
    assert backend.undo()["operation"] == "undo"
    assert backend.redo()["operation"] == "redo"
    assert backend.export()["markdown"] == "# Transcript"
    assert backend.processes()[0]["owner_session_id"] == "parent"
    assert backend.process("proc_1")["process_id"] == "proc_1"
    assert backend.process_read("proc_1", cursor=2)["cursor"] == 2
    assert backend.process_kill("proc_1")["status"] == "killed"
    assert backend.children()[0]["session_id"] == "agent-child"
    assert backend.child("agent-child")["parent_session_id"] == "parent"
    backend.select_session("child")
    assert backend.session_id == "child"


def test_remote_backend_submits_attachment_paths_to_daemon_truth():
    backend = RemoteTerminalBackend(_ControlClient(), "parent")

    result = backend.start_run("Inspect", attachments=["src/app.py"])

    assert result == {
        "session_id": "parent",
        "message": "Inspect",
        "attachments": ["src/app.py"],
    }


def test_remote_backend_forwards_per_run_model_and_process_io():
    backend = RemoteTerminalBackend(_ControlClient(), "parent")

    result = backend.start_run("Review", model="provider/model")
    written = backend.process_write("proc_1", "hello\n")
    resized = backend.process_resize("proc_1", rows=40, cols=120)

    assert result["model"] == "provider/model"
    assert written["data"] == "hello\n"
    assert (resized["rows"], resized["cols"]) == (40, 120)


def test_remote_backend_resolves_custom_commands_on_daemon():
    backend = RemoteTerminalBackend(_ControlClient(), "parent")

    assert backend.commands() == [{"name": "review", "session_id": "parent"}]
    assert backend.expand_command("review", "src")["prompt"] == "Review src"


def test_remote_backend_reads_extension_status_from_daemon():
    backend = RemoteTerminalBackend(_ControlClient(), "parent")

    assert backend.extensions()[0]["extension_id"] == "skill:review"
    assert backend.skills()[0]["name"] == "review"
    assert backend.mcps()[0]["name"] == "docs"


def test_remote_command_catalog_exposes_only_implemented_phase_two_controls():
    registry = _remote_command_registry()
    names = {item.name for item in registry.visible_commands()}
    aliases = {
        alias: item.name
        for item in registry.visible_commands()
        for alias in item.aliases
    }

    assert {
        "sessions", "resume", "fork", "processes", "subagents",
        "extensions", "skills", "mcps",
    } <= names
    assert "memory" in names
    assert {"model", "compact"}.isdisjoint(names)
    assert aliases["process"] == "processes"
    assert aliases["attach"] == "resume"


def test_remote_command_catalog_includes_daemon_resolved_prompt_commands():
    registry = _remote_command_registry([
        {"name": "review", "description": "Review a path", "source": "project"},
        {"name": "help", "description": "Must not shadow built-in", "source": "project"},
    ])

    commands = {item.name: item for item in registry.visible_commands()}
    assert commands["review"].category == "Custom"
    assert commands["review"].description == "Review a path"
    assert commands["help"].category == "Remote"


def test_remote_location_and_shell_contract_are_unambiguous():
    from nz_coder.interface.remote import (
        _remote_location_label,
        _remote_submission_error,
    )

    assert _remote_location_label("http://127.0.0.1:8765", local_daemon=True) == (
        "LOCAL DAEMON"
    )
    assert _remote_location_label("https://agent.example.test/api", local_daemon=False) == (
        "REMOTE · agent.example.test"
    )
    assert "disabled" in _remote_submission_error("!pytest -q", (), False).lower()
    assert "client-local" in _remote_submission_error(
        "review", (object(),), False
    ).lower()
    assert _remote_submission_error("review", (), False) == ""
