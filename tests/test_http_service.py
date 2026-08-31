"""Integration tests for the optional loopback Session HTTP service."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from nz_coder.http_service import (
    InteractionBroker,
    NZCoderClient,
    NZCoderHTTPError,
    SessionHTTPService,
    SessionManager,
)
from nz_coder.http_service.manager import build_http_agent
from nz_coder.protocol.message_schema import SESSION_SUMMARY_KEY
from nz_coder.state.memory import MemoryManager, current_memory_manager
from nz_coder.state.memory_control import MemoryControlPlane
from nz_coder.runtime.process.workdir import current_workdir, scoped_workdir
from nz_coder.runtime.process.process_service import workspace_process_service
from nz_coder.state.sessions import load_session, save_session, session_runtime_dir
from nz_coder.protocol.session_events import SessionEventBus, encode_sse
from nz_coder.state.skills import current_skill_loader


class FakeAgent:
    """Small AgentLoop stand-in that preserves the public event lifecycle."""

    def __init__(self, session_id: str, permission_mode: str):
        self.session_id = session_id
        self.workspace = current_workdir()
        self.permissions = SimpleNamespace(mode=permission_mode)
        self.permission_asker = lambda _name, _input: "reject"
        self.question_asker = lambda _questions: None
        self.before_late_question = threading.Event()
        self.allow_late_question = threading.Event()
        self.event_bus = SessionEventBus(
            session_id=session_id,
            run_id=f"run-{session_id}",
            agent_id=f"agent-{session_id}",
        )

    async def run(self, messages: list[dict], stream: bool = False) -> dict:
        self.event_bus.publish("session.run.started", {"stream": stream})
        try:
            request = messages[-1]["content"]
            if request == "wait":
                await asyncio.sleep(30)
            if request == "permission":
                reply = self.permission_asker(
                    "write_file",
                    {"path": "demo.py", "content": "print('ok')\n"},
                )
                answer = f"permission:{reply}"
            elif request in {"question", "late-question"}:
                if request == "late-question":
                    self.before_late_question.set()
                    self.allow_late_question.wait(timeout=2)
                replies = self.question_asker([{
                    "header": "Scope",
                    "question": "Which scope should be changed?",
                    "options": [
                        {"label": "Current file", "description": "Limit the change."},
                        {"label": "Repository", "description": "Change all matches."},
                    ],
                    "multiple": False,
                }])
                answer = f"question:{replies if replies is not None else 'dismissed'}"
            else:
                answer = f"answer:{request}"
            messages.append({"role": "assistant", "content": answer})
            self.event_bus.publish("session.message.completed", {"content": answer})
            self.event_bus.publish("session.run.completed", {"status": "completed"})
            return {"status": "completed", "answer": answer}
        except asyncio.CancelledError:
            self.event_bus.publish("session.run.cancelled", {})
            raise

    def set_interaction_askers(self, *, question_asker=None, permission_asker=None):
        self.question_asker = question_asker
        self.permission_asker = permission_asker

    def close(self) -> None:
        self.event_bus.close()


def _fake_factory(session_id: str, permission_mode: str) -> FakeAgent:
    return FakeAgent(session_id, permission_mode)


def _persistent_fake_factory(session_id: str, permission_mode: str) -> FakeAgent:
    agent = FakeAgent(session_id, permission_mode)
    agent.event_bus = SessionEventBus(
        session_id=session_id,
        journal_path=session_runtime_dir(session_id) / "events.jsonl",
    )
    return agent


def _wait_for_status(client: NZCoderClient, session_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        info = client.get_session(session_id)
        if info["status"] == expected:
            return info
        time.sleep(0.01)
    raise AssertionError(f"session did not reach {expected}")


def _wait_for_pending(fetch):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        pending = fetch()
        if pending:
            return pending
        time.sleep(0.01)
    raise AssertionError("interaction request did not become pending")


def _next_product_event(stream):
    """Read the next journaled event while ignoring transport heartbeats."""
    event = next(stream)
    while event["type"] == "server.heartbeat":
        event = next(stream)
    return event


def _product_events_through(stream, terminal_type: str) -> list[dict]:
    """Collect journaled events through one terminal event type."""
    events = []
    while terminal_type not in [event["type"] for event in events]:
        events.append(_next_product_event(stream))
    return events


@pytest.fixture
def local_service(tmp_path):
    with scoped_workdir(tmp_path):
        manager = SessionManager(
            agent_factory=_fake_factory,
            workspace_roots=[tmp_path],
            restore_saved=False,
        )
        service = SessionHTTPService(
            port=0,
            token="test-token-1234567890",
            manager=manager,
            heartbeat_seconds=0.05,
        )
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            yield service
        finally:
            service.shutdown()
            thread.join(timeout=2)


def test_http_session_run_messages_and_sse_replay(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)

    assert client.health() == {"status": "ok", "service": "nz-coder"}
    created = client.create_session("auto")
    session_id = created["id"]
    assert created["status"] == "idle"
    assert created["permission_mode"] == "auto"
    persisted = Path(created["workspace"]) / ".nz-coder" / "sessions" / f"{session_id}.json"
    artifacts = Path(created["workspace"]) / ".nz-coder" / "sessions" / "_artifacts" / session_id
    assert persisted.exists()
    assert artifacts.exists()

    accepted = client.run(session_id, "hello")
    assert accepted["status"] == "running"
    completed = _wait_for_status(client, session_id, "completed")
    assert completed["last_result"]["answer"] == "answer:hello"
    assert client.messages(session_id) == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "answer:hello"},
    ]

    events = client.events(session_id, replay=10)
    assert next(events) == {"type": "server.connected", "properties": {}}
    assert [next(events)["type"] for _ in range(4)] == [
        "session.run.started",
        "session.message.completed",
        "session.run.completed",
        "session.run.settled",
    ]
    events.close()

    assert [item["id"] for item in client.list_sessions()] == [session_id]
    assert client.delete_session(session_id) is True
    assert not persisted.exists()
    assert not artifacts.exists()
    with pytest.raises(NZCoderHTTPError) as exc_info:
        client.get_session(session_id)
    assert exc_info.value.status == 404
    assert exc_info.value.code == "session_not_found"


def test_http_projects_agents_and_controls_runtime_owned_workflow(local_service):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("default")
    session = local_service.manager.get(created["id"])
    runtime_owner = BackgroundAgentManager(session.workspace, session.session_id)
    session.agent.background_agents = runtime_owner
    runtime_owner.begin_workflow_run("workflow-live", "Live audit")
    try:
        agents = client.list_agents(session.session_id)
        assert agents[0]["name"] == "worker"
        assert {item["name"] for item in agents[1:]} == {
            "explore", "plan", "general-purpose", "reflection",
        }

        listed = client.list_workflows(session.session_id)
        assert listed["runs"][0]["run_id"] == "workflow-live"
        assert client.get_workflow(session.session_id, "workflow-live")["status"] == "running"
        assert client.control_workflow(
            session.session_id, "workflow-live", "pause"
        )["status"] == "paused"
        assert client.control_workflow(
            session.session_id, "workflow-live", "resume"
        )["status"] == "running"
        assert client.control_workflow(
            session.session_id, "workflow-live", "stop"
        )["status"] == "stopped"
    finally:
        runtime_owner.close()


def test_http_remote_workflow_prepare_and_start_require_exact_approval(
    local_service, monkeypatch
):
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.workflows import workflow_resolver

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("default")
    session = local_service.manager.get(created["id"])
    runtime_owner = BackgroundAgentManager(session.workspace, session.session_id)
    session.agent.background_agents = runtime_owner
    captured = {}

    class Handle:
        run_id = "workflow-remote"

        def wait_started(self, timeout):
            assert timeout == 10.0
            return {"run_id": self.run_id, "name": "parallel-investigation", "status": "running"}

    class SDK:
        def __init__(self, manager):
            assert manager is runtime_owner

        def start(self, **kwargs):
            captured.update(kwargs)
            return Handle()

    resolution_calls = 0
    real_resolve = workflow_resolver.resolve_workflow_capsule

    def counted_resolve(*args, **kwargs):
        nonlocal resolution_calls
        resolution_calls += 1
        return real_resolve(*args, **kwargs)

    monkeypatch.setattr(workflow_resolver, "resolve_workflow_capsule", counted_resolve)
    monkeypatch.setattr("nz_coder.runtime.workflows.workflow_sdk.WorkflowHostSDK", SDK)
    try:
        arguments = {"question": "Inspect routing", "max_agents": 2}
        prepared = client.prepare_workflow(
            session.session_id, "parallel-investigation", arguments
        )
        assert prepared["summary"]["writes_files"] is False
        assert prepared["approval_digest"]

        started = client.start_workflow(
            session.session_id,
            "parallel-investigation",
            arguments,
            approval_digest=prepared["approval_digest"],
        )
        assert started["run_id"] == "workflow-remote"
        assert resolution_calls == 2
        assert captured["approval_decision"] == "approve"
        assert captured["approval_digest"] == prepared["approval_digest"]

        with pytest.raises(NZCoderHTTPError) as stale:
            client.start_workflow(
                session.session_id,
                "parallel-investigation",
                {"question": "Different plan", "max_agents": 2},
                approval_digest=prepared["approval_digest"],
            )
        assert stale.value.status == 400
    finally:
        runtime_owner.close()


def test_http_remote_memory_review_uses_session_owned_control_plane(local_service, tmp_path):
    manager = MemoryManager(tmp_path / "memory")
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("default")
    session = local_service.manager.get(created["id"])
    session.agent._mm = manager
    control = MemoryControlPlane(manager.memory_dir, manager)
    proposal = control.submit({
        "name": "style",
        "description": "Formatting",
        "type": "user",
        "content": "Use spaces",
        "confidence": 0.9,
        "reason": "explicit preference",
    }, source_session=session.session_id)

    status = client.memory_status(session.session_id)
    assert status["pending"][0]["fingerprint"] == proposal.fingerprint
    assert client.get_memory_proposal(
        session.session_id, proposal.fingerprint
    )["source_session"] == session.session_id
    reviewed = client.review_memory(
        session.session_id, proposal.fingerprint, "approve"
    )
    assert reviewed["status"] == "applied"
    assert client.memory_status(session.session_id)["pending"] == []


def test_http_run_builds_file_parts_in_authoritative_daemon_workspace(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("auto")
    session_id = created["id"]
    workspace = Path(created["workspace"])
    image = workspace / "screen.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nremote-image")

    accepted = client.run(session_id, "Inspect the image", attachments=["screen.png"])

    assert accepted["status"] == "running"
    _wait_for_status(client, session_id, "completed")
    snapshot = client.snapshot(session_id)
    user = snapshot["messages"][0]
    file_parts = [part for part in user["parts"] if part["type"] == "file"]
    assert file_parts, user
    assert file_parts[0]["filename"] == "screen.png"
    assert file_parts[0]["mime"] == "image/png"


def test_http_run_rejects_attachment_escape_and_symlink(local_service, tmp_path):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("auto")
    session_id = created["id"]
    workspace = Path(created["workspace"])
    outside = tmp_path.parent / "outside-remote-attachment.txt"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)

    for path in (str(outside), "link.txt"):
        with pytest.raises(NZCoderHTTPError) as exc_info:
            client.run(session_id, "Inspect", attachments=[path])
        assert exc_info.value.status == 400
        assert exc_info.value.code == "invalid_request"

    assert client.messages(session_id) == []


def test_http_resolves_project_custom_commands_in_daemon_workspace(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("auto")
    session_id = created["id"]
    commands = Path(created["workspace"]) / ".nz-coder" / "commands"
    commands.mkdir(parents=True)
    (commands / "review.md").write_text(
        "---\ndescription: Review a path\nallowed_tools:\n  - read_file\n"
        "---\nReview $ARGUMENTS",
        encoding="utf-8",
    )

    listed = client.list_commands(session_id)
    expanded = client.expand_command(session_id, "review", "src/app.py")

    assert listed == [{
        "name": "review",
        "description": "Review a path",
        "source": "project",
        "allowed_tools": ["read_file"],
        "model": None,
    }]
    assert expanded == {
        "name": "review",
        "prompt": "Review src/app.py",
        "source": "project",
        "allowed_tools": ["read_file"],
        "model": None,
    }


def test_http_projects_remote_extension_skill_and_mcp_status(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("auto")
    session_id = created["id"]
    workspace = Path(created["workspace"])
    _write = workspace / ".nz-coder" / "skills" / "review" / "SKILL.md"
    _write.parent.mkdir(parents=True)
    _write.write_text(
        "---\nname: review\ndescription: Review changes\n---\nReview.",
        encoding="utf-8",
    )

    extensions = client.list_extensions(session_id)
    skills = client.list_skills(session_id)
    mcps = client.list_mcps(session_id)

    assert any(item["extension_id"] == "skill:review" for item in extensions)
    assert "review" in [item["name"] for item in skills]
    assert mcps == []


def test_http_sse_resumes_strictly_after_last_event_id(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("auto")["id"]
    client.run(session_id, "first")
    _wait_for_status(client, session_id, "completed")

    initial = client.events(session_id, replay=10)
    assert next(initial)["type"] == "server.connected"
    started = next(initial)
    following = next(initial)
    initial.close()

    client.run(session_id, "second")
    _wait_for_status(client, session_id, "completed")
    resumed = client.events(
        session_id,
        last_event_id=started["meta"]["event_id"],
    )
    try:
        assert next(resumed)["type"] == "server.connected"
        assert next(resumed) == following
    finally:
        resumed.close()


def test_http_resilient_stream_closes_snapshot_to_settled_loop(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    stream = client.resilient_events(session_id, reconnect_attempts=0)

    baseline = next(stream)
    assert baseline["type"] == "server.snapshot"
    assert baseline["properties"]["messages"] == []

    client.run(session_id, "closure")
    observed = []
    while "session.run.settled" not in observed:
        observed.append(next(stream)["type"])

    assert observed[0] == "server.connected"
    assert "session.run.started" in observed
    assert "session.message.completed" in observed
    assert observed[-1] == "session.run.settled"
    assert client.snapshot(session_id)["messages"][1]["parts"][0]["text"] == (
        "answer:closure"
    )
    stream.close()


def test_http_idle_snapshot_has_persisted_message_parts_and_atomic_cursor(
    local_service,
):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("auto")["id"]
    client.run(session_id, "first")
    _wait_for_status(client, session_id, "completed")

    snapshot = client.snapshot(session_id)
    assert snapshot["schema_version"] == 1
    assert snapshot["session"]["status"] == "completed"
    assert snapshot["pending"] == {"permissions": [], "questions": []}
    assert len(snapshot["messages"]) == 2
    for record in snapshot["messages"]:
        assert record["info"]["id"].startswith("msg-")
        assert record["info"]["session_id"] == session_id
        assert record["parts"][0]["message_id"] == record["info"]["id"]
        assert record["parts"][0]["id"].startswith("part-")
    assert snapshot["messages"][1]["parts"][0]["text"] == "answer:first"
    assert client.messages(session_id) == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "answer:first"},
    ]

    cursor = snapshot["cursor"]
    assert cursor["event_id"]
    manager_event = local_service.manager.get(session_id).event_bus.recent(1)[0]
    assert manager_event.event_id == cursor["event_id"]
    assert manager_event.type == "session.snapshot.created"

    client.run(session_id, "second")
    _wait_for_status(client, session_id, "completed")
    resumed = client.events(session_id, last_event_id=cursor["event_id"])
    assert next(resumed)["type"] == "server.connected"
    event_types = []
    while "session.run.settled" not in event_types:
        event_types.append(next(resumed)["type"])
    resumed.close()
    assert event_types[0] == "session.run.started"
    assert "session.snapshot.created" not in event_types


def test_http_attach_snapshot_is_available_while_run_is_active(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("auto")["id"]
    client.run(session_id, "wait")
    _wait_for_status(client, session_id, "running")
    local_service.manager.get(session_id).event_bus.publish(
        "message.part.delta",
        {"message_id": "msg-live", "part_id": "part-live", "delta": "partial"},
    )

    snapshot = client.attach_snapshot(session_id)
    assert snapshot["settled"] is False
    assert snapshot["session"]["running"] is True
    assert snapshot["cursor"]["event_id"]
    assert snapshot["pending"] == {"permissions": [], "questions": []}
    assert any(
        event["type"] == "message.part.delta"
        and event["properties"]["delta"] == "partial"
        for event in snapshot["events"]
    )

    resumed = client.events(
        session_id,
        replay=0,
        last_event_id=snapshot["cursor"]["event_id"],
    )
    client.abort(session_id)
    observed = []
    try:
        while "session.run.settled" not in observed:
            observed.append(next(resumed)["type"])
    finally:
        resumed.close()
    assert "session.run.cancelled" in observed


def test_run_continues_after_first_remote_client_detaches(local_service):
    first = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = first.create_session("auto")["id"]
    stream = first.events(session_id, replay=0)
    assert next(stream)["type"] == "server.connected"
    time.sleep(local_service.heartbeat_seconds * 2)
    first.run(session_id, "hello after detach")
    assert _next_product_event(stream)["type"] == "session.run.started"
    stream.close()

    second = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    completed = _wait_for_status(second, session_id, "completed")
    assert completed["running"] is False
    assert second.messages(session_id)[-1]["content"] == "answer:hello after detach"


def test_http_snapshot_and_diff_expose_latest_session_summary(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("auto")["id"]
    session = local_service.manager.get(session_id)
    session.history = [
        {"role": "user", "content": "change it"},
        {
            "role": "assistant",
            "content": "done",
            SESSION_SUMMARY_KEY: {
                "additions": 2,
                "deletions": 1,
                "files": 1,
                "diffs": [{
                    "file": "demo.py",
                    "patch": "@@ -1 +1,2 @@\n-old\n+new\n+line\n",
                    "additions": 2,
                    "deletions": 1,
                    "status": "modified",
                }],
            },
        },
    ]

    assert client.snapshot(session_id)["summary"] == {
        "additions": 2,
        "deletions": 1,
        "files": 1,
    }
    assert client.diff(session_id) == [{
        "file": "demo.py",
        "patch": "@@ -1 +1,2 @@\n-old\n+new\n+line\n",
        "additions": 2,
        "deletions": 1,
        "status": "modified",
    }]


def test_http_snapshot_rejects_running_session(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("auto")["id"]
    client.run(session_id, "wait")

    with pytest.raises(NZCoderHTTPError) as busy:
        client.snapshot(session_id)
    assert busy.value.status == 409
    assert busy.value.code == "session_busy"

    assert client.abort(session_id) == {"aborted": True}
    _wait_for_status(client, session_id, "cancelled")


def test_snapshot_checkpoint_blocks_a_concurrent_run_start(local_service, monkeypatch):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("auto")["id"]
    session = local_service.manager.get(session_id)
    checkpoint_entered = threading.Event()
    checkpoint_release = threading.Event()
    original_checkpoint = session.event_bus.checkpoint

    def blocking_checkpoint(*args, **kwargs):
        checkpoint_entered.set()
        assert checkpoint_release.wait(timeout=2)
        return original_checkpoint(*args, **kwargs)

    monkeypatch.setattr(session.event_bus, "checkpoint", blocking_checkpoint)
    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshot_future = pool.submit(session.snapshot)
        assert checkpoint_entered.wait(timeout=1)
        run_future = pool.submit(session.start_run, "after-snapshot")
        assert not run_future.done()
        checkpoint_release.set()
        snapshot = snapshot_future.result(timeout=2)
        run_future.result(timeout=2)

    assert session.wait(timeout=3)
    cursor_sequence = snapshot["cursor"]["sequence"]
    started = next(
        event
        for event in session.event_bus.recent(20)
        if event.type == "session.run.started"
    )
    assert started.sequence == cursor_sequence + 1


def test_http_sse_expired_cursor_returns_gone(tmp_path):
    def small_replay_factory(session_id: str, permission_mode: str):
        agent = FakeAgent(session_id, permission_mode)
        agent.event_bus = SessionEventBus(
            session_id=session_id,
            replay_capacity=2,
        )
        return agent

    manager = SessionManager(
        agent_factory=small_replay_factory,
        workspace_roots=[tmp_path],
        restore_saved=False,
    )
    service = SessionHTTPService(
        port=0,
        token="test-token-1234567890",
        manager=manager,
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    client = NZCoderClient(service.base_url, service.token, timeout=2)
    try:
        session_id = client.create_session("auto")["id"]
        bus = manager.get(session_id).event_bus
        expired = bus.publish("session.worker.event", {"index": 1})
        bus.publish("session.worker.event", {"index": 2})
        bus.publish("session.worker.event", {"index": 3})

        stream = client.events(session_id, last_event_id=expired.event_id)
        with pytest.raises(NZCoderHTTPError) as error:
            next(stream)
        assert error.value.status == 410
        assert error.value.code == "event_cursor_expired"
    finally:
        service.shutdown()
        thread.join(timeout=2)


def test_http_sse_cursor_survives_service_restart(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    token = "test-token-1234567890"
    first_manager = SessionManager(
        agent_factory=_persistent_fake_factory,
        workspace_roots=[workspace],
        restore_saved=False,
    )
    first_service = SessionHTTPService(
        port=0,
        token=token,
        manager=first_manager,
    )
    first_thread = threading.Thread(target=first_service.serve_forever, daemon=True)
    first_thread.start()
    first_client = NZCoderClient(first_service.base_url, token, timeout=2)
    workspace_id = next(
        item["id"]
        for item in first_client.list_workspaces()
        if item["path"] == str(workspace)
    )
    session_id = first_client.create_session("auto", workspace_id)["id"]
    first_client.run(session_id, "before restart")
    _wait_for_status(first_client, session_id, "completed")
    initial = first_client.events(session_id, replay=10)
    assert next(initial)["type"] == "server.connected"
    cursor_event = next(initial)
    expected_next = next(initial)
    initial.close()
    first_service.shutdown()
    first_thread.join(timeout=2)

    second_manager = SessionManager(
        agent_factory=_persistent_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    second_service = SessionHTTPService(
        port=0,
        token=token,
        manager=second_manager,
    )
    second_thread = threading.Thread(target=second_service.serve_forever, daemon=True)
    second_thread.start()
    second_client = NZCoderClient(second_service.base_url, token, timeout=2)
    try:
        resumed = second_client.events(
            session_id,
            last_event_id=cursor_event["meta"]["event_id"],
        )
        assert next(resumed)["type"] == "server.connected"
        assert next(resumed) == expected_next
        resumed.close()
    finally:
        second_service.shutdown()
        second_thread.join(timeout=2)


def test_http_client_reconnects_with_latest_complete_event_id(monkeypatch):
    first_bus = SessionEventBus(session_id="reconnect-session")
    first = first_bus.publish("session.worker.event", {"index": 1})
    second = first_bus.publish("session.worker.event", {"index": 2})

    class FakeResponse:
        def __init__(self, frames, *, fail=False):
            self.lines = "".join(frames).encode("utf-8").splitlines(keepends=True)
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            yield from self.lines
            if self.fail:
                raise ConnectionResetError("test disconnect")

    responses = [
        FakeResponse([
            encode_sse({"type": "server.connected", "properties": {}}),
            encode_sse(first),
        ], fail=True),
        FakeResponse([
            encode_sse({"type": "server.connected", "properties": {}}),
            encode_sse(second),
        ]),
    ]
    requests = []

    class FakeOpener:
        def open(self, request, timeout):
            requests.append(request)
            return responses.pop(0)

    client = NZCoderClient(
        "http://127.0.0.1:4096",
        "test-token-1234567890",
        timeout=2,
    )
    monkeypatch.setattr(client, "_opener", FakeOpener())

    events = list(client.events(
        "reconnect-session",
        replay=0,
        reconnect_attempts=1,
        reconnect_delay=0,
    ))

    assert [event["type"] for event in events] == [
        "server.connected",
        "session.worker.event",
        "server.connected",
        "session.worker.event",
    ]
    assert requests[0].get_header("Last-event-id") is None
    assert requests[1].get_header("Last-event-id") == first.event_id


def test_http_client_survives_three_delayed_disconnects_without_duplicates(
    monkeypatch,
):
    """A slow SSE connection may reconnect repeatedly without replaying events."""
    bus = SessionEventBus(session_id="slow-reconnect-session")
    published = [
        bus.publish("session.worker.event", {"index": index})
        for index in range(4)
    ]

    class SlowResponse:
        def __init__(self, event, *, fail):
            self.lines = "".join([
                encode_sse({"type": "server.connected", "properties": {}}),
                encode_sse(event),
            ]).encode("utf-8").splitlines(keepends=True)
            self.fail = fail

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            for line in self.lines:
                time.sleep(0.001)
                yield line
            if self.fail:
                raise ConnectionResetError("simulated slow-network disconnect")

    responses = [
        SlowResponse(event, fail=index < 3)
        for index, event in enumerate(published)
    ]
    requests = []

    class SlowOpener:
        def open(self, request, timeout):
            requests.append(request)
            return responses.pop(0)

    client = NZCoderClient(
        "http://127.0.0.1:4096",
        "test-token-1234567890",
        timeout=2,
    )
    monkeypatch.setattr(client, "_opener", SlowOpener())

    events = list(client.events(
        "slow-reconnect-session",
        replay=0,
        reconnect_attempts=3,
        reconnect_delay=0,
    ))

    assert [event.get("properties", {}).get("index") for event in events
            if event["type"] == "session.worker.event"] == [0, 1, 2, 3]
    assert [request.get_header("Last-event-id") for request in requests] == [
        None,
        published[0].event_id,
        published[1].event_id,
        published[2].event_id,
    ]


def test_http_service_requires_token_and_rejects_browser_origin(local_service):
    wrong = NZCoderClient(local_service.base_url, "wrong-token-123456", timeout=2)
    with pytest.raises(NZCoderHTTPError) as exc_info:
        wrong.list_sessions()
    assert exc_info.value.status == 401

    health_request = Request(
        local_service.base_url + "/health",
        headers={"Origin": "https://example.test"},
    )
    with build_opener(ProxyHandler({})).open(health_request, timeout=2) as response:
        assert response.status == 200

    request = Request(
        local_service.base_url + "/session",
        headers={
            "Authorization": f"Bearer {local_service.token}",
            "Origin": "https://example.test",
        },
    )
    with pytest.raises(HTTPError) as origin_error:
        build_opener(ProxyHandler({})).open(request, timeout=2)
    assert origin_error.value.code == 403


def test_http_session_busy_abort_and_delete_boundary(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("default")
    session_id = created["id"]
    persisted = (
        Path(created["workspace"])
        / ".nz-coder"
        / "sessions"
        / f"{session_id}.json"
    )
    other_session_id = client.create_session("default")["id"]

    client.run(session_id, "wait")
    with pytest.raises(NZCoderHTTPError) as busy_run:
        client.run(session_id, "second")
    assert busy_run.value.status == 409
    assert busy_run.value.code == "session_busy"
    with pytest.raises(NZCoderHTTPError) as workspace_busy:
        client.run(other_session_id, "other")
    assert workspace_busy.value.status == 409
    assert "workspace" in workspace_busy.value.message
    with pytest.raises(NZCoderHTTPError) as busy_delete:
        client.delete_session(session_id)
    assert busy_delete.value.status == 409
    assert persisted.exists()

    assert client.abort(session_id) == {"aborted": True}
    _wait_for_status(client, session_id, "cancelled")
    assert client.abort(session_id) == {"aborted": False}
    assert client.delete_session(session_id) is True

    client.run(other_session_id, "other")
    _wait_for_status(client, other_session_id, "completed")
    assert client.delete_session(other_session_id) is True


def test_http_abort_retires_stream_part_before_run_settles(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    first_delta = threading.Event()
    release_stream = threading.Event()
    provider_exited = threading.Event()
    calls = 0

    def chunk(text):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=text,
                tool_calls=None,
                reasoning_content=None,
            ))],
        )

    class Completions:
        def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            call_number = calls

            def response():
                try:
                    if call_number == 1:
                        yield chunk("partial")
                        first_delta.set()
                        assert release_stream.wait(timeout=3)
                        yield chunk("late")
                    else:
                        yield chunk("fresh")
                finally:
                    if call_number == 1:
                        provider_exited.set()

            return response()

    completions = Completions()

    def factory(session_id: str, _permission_mode: str):
        return AgentLoop(
            "test",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=completions),
            ),
            trace_enabled=False,
            session_id=session_id,
            event_bus=SessionEventBus(session_id=session_id),
        )

    monkeypatch.setattr(config, "PLANNING_ENABLED", False)
    manager = SessionManager(
        agent_factory=factory,
        workspace_roots=[tmp_path],
        restore_saved=False,
    )
    service = SessionHTTPService(
        port=0,
        token="test-token-1234567890",
        manager=manager,
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    client = NZCoderClient(service.base_url, service.token, timeout=10)
    try:
        workspace_id = next(
            item["id"]
            for item in client.list_workspaces()
            if item["path"] == str(tmp_path)
        )
        session_id = client.create_session("auto", workspace_id)["id"]

        client.run(session_id, "first")
        assert first_delta.wait(timeout=2)
        assert client.abort(session_id) == {"aborted": True}
        assert client.abort(session_id) == {"aborted": False}

        with pytest.raises(NZCoderHTTPError) as still_busy:
            client.run(session_id, "must-not-overlap")
        assert still_busy.value.status == 409

        release_stream.set()
        _wait_for_status(client, session_id, "cancelled")
        assert provider_exited.is_set()
        events = manager.get(session_id).event_bus.recent(64)
        event_types = [event.type for event in events]
        assert event_types.count("message.part.removed") == 1
        removed_index = event_types.index("message.part.removed")
        cancelled_index = event_types.index("session.run.cancelled")
        settled_index = event_types.index("session.run.settled")
        assert removed_index < cancelled_index < settled_index
        late_parts = [
            event for event in events[removed_index + 1:]
            if event.type.startswith("message.part.")
        ]
        assert [event.type for event in late_parts] == ["message.part.updated"]
        assert late_parts[0].properties["part"]["type"] == "step-finish"
        assert late_parts[0].properties["part"]["reason"] == "cancelled"
        cancelled_assistant = next(
            message for message in manager.get(session_id).history
            if message.get("role") == "assistant"
        )
        assert any(
            part.get("type") == "step-finish" and part.get("reason") == "cancelled"
            for part in cancelled_assistant.get("_nz_parts", [])
        )

        client.run(session_id, "second")
        _wait_for_status(client, session_id, "completed")
        assert calls == 2
    finally:
        release_stream.set()
        service.shutdown()
        thread.join(timeout=2)


def test_http_run_settled_is_the_manager_commit_barrier(local_service, monkeypatch):
    from nz_coder.http_service import manager as manager_module

    persist_entered = threading.Event()
    persist_release = threading.Event()

    def delayed_save_session(*args, **kwargs):
        if kwargs.get("run_status") == "completed":
            persist_entered.set()
            assert persist_release.wait(timeout=2)

    monkeypatch.setattr(manager_module, "save_session", delayed_save_session)
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    events = None
    try:
        client.run(session_id, "hello")
        assert persist_entered.wait(timeout=2)
        events = client.events(session_id, replay=10)
        assert next(events)["type"] == "server.connected"
        assert [next(events)["type"] for _ in range(3)] == [
            "session.run.started",
            "session.message.completed",
            "session.run.completed",
        ]
        assert client.get_session(session_id)["status"] == "running"
        assert client.abort(session_id) == {"aborted": False}

        persist_release.set()
        settled = next(events)
        assert settled["type"] == "session.run.settled"
        assert settled["properties"] == {"status": "completed", "persisted": True}
        info = client.get_session(session_id)
        assert info["status"] == "completed"
        assert info["running"] is False

        client.run(session_id, "again")
        _wait_for_status(client, session_id, "completed")
    finally:
        persist_release.set()
        if events is not None:
            events.close()


def test_http_run_rejects_and_rolls_back_when_acceptance_cannot_persist(
    tmp_path,
    monkeypatch,
):
    from nz_coder.http_service import manager as manager_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=False,
    )
    workspace_id = next(
        item["id"]
        for item in manager.list_workspaces()
        if item["path"] == str(workspace)
    )
    session_id = manager.create("default", workspace_id)["id"]
    session = manager.get(session_id)
    original_save = manager_module.save_session

    def fail_running_save(*args, **kwargs):
        if kwargs.get("run_status") == "running":
            raise OSError("disk unavailable")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(manager_module, "save_session", fail_running_save)
    try:
        with pytest.raises(OSError, match="disk unavailable"):
            session.start_run("must-not-be-accepted")
        assert session.info()["status"] == "failed"
        assert session.info()["running"] is False
        assert session.messages() == []
        with scoped_workdir(workspace):
            rejected = manager_module.load_session(session_id)
        assert rejected["run_status"] == "failed"
        assert rejected["messages"] == []

    finally:
        manager.close()


    monkeypatch.setattr(manager_module, "save_session", original_save)
    restored_manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        restored_info = next(
            item for item in restored_manager.list() if item["id"] == session_id
        )
        assert restored_info["status"] == "failed"
        assert restored_info["runtime_status"] == "failed"
        restored = restored_manager.get(session_id)
        assert restored.messages() == []
        restored.start_run("retry")
        assert restored.wait(timeout=3)
        assert restored.info()["status"] == "completed"
    finally:
        restored_manager.close()


def test_http_session_commit_does_not_depend_on_latest_alias(tmp_path, monkeypatch):
    from nz_coder.state import sessions as sessions_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    original_write = sessions_module._write_json

    def fail_latest_alias(path, payload):
        if path.name == "latest.json":
            raise OSError("alias unavailable")
        return original_write(path, payload)

    monkeypatch.setattr(sessions_module, "_write_json", fail_latest_alias)
    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=False,
    )
    try:
        workspace_id = next(
            item["id"]
            for item in manager.list_workspaces()
            if item["path"] == str(workspace)
        )
        session_id = manager.create("default", workspace_id)["id"]
        session = manager.get(session_id)
        session.start_run("committed")
        assert session.wait(timeout=3)
        assert session.info()["status"] == "completed"

        with scoped_workdir(workspace):
            payload = sessions_module.load_session(session_id)
        assert payload["run_status"] == "completed"
        assert len(payload["messages"]) == 2
    finally:
        manager.close()

    with scoped_workdir(workspace):
        with pytest.raises(OSError, match="alias unavailable"):
            sessions_module.save_session(
                [],
                session_id="strict-cli-alias",
                activate=False,
            )


def test_http_terminal_persistence_failure_restores_as_interrupted(
    tmp_path,
    monkeypatch,
):
    from nz_coder.http_service import manager as manager_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=False,
    )
    workspace_id = next(
        item["id"]
        for item in manager.list_workspaces()
        if item["path"] == str(workspace)
    )
    session_id = manager.create("default", workspace_id)["id"]
    session = manager.get(session_id)
    subscription = session.event_bus.subscribe()
    original_save = manager_module.save_session

    def fail_terminal_save(*args, **kwargs):
        if kwargs.get("run_status") == "completed":
            raise OSError("terminal commit unavailable")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(manager_module, "save_session", fail_terminal_save)
    session.start_run("accepted")
    assert session.wait(timeout=3)
    events = []
    while True:
        try:
            events.append(subscription.get(timeout=0.01))
        except queue.Empty:
            break
    settled = next(event for event in events if event.type == "session.run.settled")
    assert settled.properties == {"status": "completed", "persisted": False}
    with scoped_workdir(workspace):
        accepted = manager_module.load_session(session_id)
    assert accepted["run_status"] == "running"
    assert len(accepted["messages"]) == 1
    manager.close()

    restored_manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        restored = next(
            item for item in restored_manager.list() if item["id"] == session_id
        )
        assert restored["status"] == "interrupted"
        assert restored["message_count"] == 1
    finally:
        restored_manager.close()


def test_http_permission_request_reply_and_late_reply_boundary(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]

    client.run(session_id, "permission")
    pending = _wait_for_pending(lambda: client.pending_permissions(session_id))
    request = pending[0]
    assert request["kind"] == "permission"
    assert request["permission"] == "write_file"
    assert request["tool_input"]["path"] == "demo.py"
    assert client.get_session(session_id)["pending_interaction_count"] == 1
    assert client.get_session(session_id)["runtime_status"] == "waiting_permission"
    reattached = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    reconnect = reattached.attach_snapshot(session_id)
    assert reconnect["pending"]["permissions"][0]["id"] == request["id"]

    with pytest.raises(NZCoderHTTPError) as invalid_reply:
        client.reply_permission(session_id, request["id"], "allow")
    assert invalid_reply.value.status == 400
    assert len(client.pending_permissions(session_id)) == 1
    with pytest.raises(NZCoderHTTPError) as unhashable_reply:
        client.reply_permission(session_id, request["id"], ["once"])
    assert unhashable_reply.value.status == 400
    assert len(client.pending_permissions(session_id)) == 1
    assert reattached.reply_permission(session_id, request["id"], "once") is True
    completed = _wait_for_status(client, session_id, "completed")
    assert completed["pending_interaction_count"] == 0
    assert client.messages(session_id)[-1]["content"] == "permission:once"
    with pytest.raises(NZCoderHTTPError) as late_reply:
        client.reply_permission(session_id, request["id"], "once")
    assert late_reply.value.status == 404
    assert late_reply.value.code == "interaction_not_found"

    events = client.events(session_id, replay=10)
    assert next(events)["type"] == "server.connected"
    event_types = []
    while "session.run.settled" not in event_types:
        event_types.append(next(events)["type"])
    events.close()
    assert [item for item in event_types if item != "session.attach.snapshot.created"] == [
        "session.run.started",
        "permission.asked",
        "permission.replied",
        "session.message.completed",
        "session.run.completed",
        "session.run.settled",
    ]


def test_http_question_reply_validation_reject_and_abort(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]

    client.run(session_id, "question")
    request = _wait_for_pending(lambda: client.pending_questions(session_id))[0]
    assert client.get_session(session_id)["runtime_status"] == "waiting_question"
    reattached = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    reconnect = reattached.attach_snapshot(session_id)
    assert reconnect["pending"]["questions"][0]["id"] == request["id"]
    assert request["questions"][0]["header"] == "Scope"
    with pytest.raises(NZCoderHTTPError) as malformed:
        client.reply_question(session_id, request["id"], [["Current file", "Repository"]])
    assert malformed.value.status == 400
    assert len(client.pending_questions(session_id)) == 1
    assert reattached.reply_question(session_id, request["id"], [["Current file"]]) is True
    _wait_for_status(client, session_id, "completed")
    assert "Current file" in client.messages(session_id)[-1]["content"]

    client.run(session_id, "question")
    rejected = _wait_for_pending(lambda: client.pending_questions(session_id))[0]
    assert client.reject_question(session_id, rejected["id"]) is True
    _wait_for_status(client, session_id, "completed")
    assert client.messages(session_id)[-1]["content"] == "question:dismissed"

    client.run(session_id, "question")
    _wait_for_pending(lambda: client.pending_questions(session_id))
    assert client.abort(session_id) == {"aborted": True}
    _wait_for_status(client, session_id, "cancelled")
    assert client.pending_questions(session_id) == []


def test_remote_session_control_uses_persisted_truth_and_lineage(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    parent = client.create_session("default")
    session_id = parent["id"]
    client.run(session_id, "first")
    _wait_for_status(client, session_id, "completed")
    local_service.manager.get(session_id).model = "provider/inherited-model"

    renamed = client.rename_session(session_id, "Remote parent")
    assert renamed["title"] == "Remote parent"
    child = client.fork_session(session_id, 1)

    assert child["parent_session_id"] == session_id
    assert child["title"] == "Remote parent (fork #1)"
    assert child["model"] == "provider/inherited-model"
    assert client.get_session(session_id)["children"] == [child["id"]]
    assert client.messages(child["id"]) == client.messages(session_id)

    with scoped_workdir(Path(parent["workspace"])):
        persisted_child = load_session(child["id"])
    assert persisted_child["model"] == "provider/inherited-model"

    client.run(child["id"], "child turn")
    _wait_for_status(client, child["id"], "completed")
    assert len(client.messages(child["id"])) == 4
    assert len(client.messages(session_id)) == 2


def test_remote_process_controls_enforce_session_ownership_and_cleanup(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=3)
    owner = client.create_session("default")
    other = client.create_session("default")
    service = workspace_process_service(Path(owner["workspace"]))
    handle = service.start(
        "printf ready; sleep 30",
        cwd=Path(owner["workspace"]),
        owner_session_id=owner["id"],
        tty=False,
        event_bus=local_service.manager.get(owner["id"]).event_bus,
    )
    try:
        reattached = NZCoderClient(local_service.base_url, local_service.token, timeout=3)
        processes = reattached.list_processes(owner["id"])
        assert [item["process_id"] for item in processes] == [handle.process_id]
        assert processes[0]["pty_tier"] == "pipe"
        output = reattached.read_process(
            owner["id"], handle.process_id, cursor=0, max_bytes=1024, wait_seconds=1,
        )
        assert "ready" in output["output"]

        for operation in (
            lambda: client.get_process(other["id"], handle.process_id),
            lambda: client.read_process(other["id"], handle.process_id),
            lambda: client.kill_process(other["id"], handle.process_id),
        ):
            with pytest.raises(NZCoderHTTPError) as denied:
                operation()
            assert denied.value.status == 403
            assert denied.value.code == "process_forbidden"

        written = client.write_process(owner["id"], handle.process_id, "input\n")
        assert written["status"] == "running"
        with pytest.raises(NZCoderHTTPError) as pipe_resize:
            client.resize_process(
                owner["id"], handle.process_id, rows=40, cols=120,
            )
        assert pipe_resize.value.status == 409

        killed = reattached.kill_process(owner["id"], handle.process_id)
        assert killed["status"] == "killed"
    finally:
        service.kill(handle.process_id, owner_session_id=owner["id"])


def test_remote_session_controls_two_persistent_processes_by_identity(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=3)
    owner = client.create_session("default")
    workspace = Path(owner["workspace"])
    service = workspace_process_service(workspace)
    handles = [
        service.start(
            f"printf {label}-ready; sleep 30",
            cwd=workspace,
            owner_session_id=owner["id"],
            tty=False,
            event_bus=local_service.manager.get(owner["id"]).event_bus,
        )
        for label in ("alpha", "beta")
    ]
    try:
        listed = client.list_processes(owner["id"])
        assert {item["process_id"] for item in listed} == {
            handle.process_id for handle in handles
        }
        for handle, label in zip(handles, ("alpha", "beta"), strict=True):
            output = client.read_process(
                owner["id"],
                handle.process_id,
                cursor=0,
                max_bytes=1024,
                wait_seconds=1,
            )
            assert f"{label}-ready" in output["output"]

        killed = client.kill_process(owner["id"], handles[0].process_id)
        assert killed["process_id"] == handles[0].process_id
        assert killed["status"] == "killed"
        remaining = client.get_process(owner["id"], handles[1].process_id)
        assert remaining["process_id"] == handles[1].process_id
        assert remaining["status"] == "running"
        assert client.kill_process(owner["id"], handles[1].process_id)["status"] == "killed"
        assert service.list(owner_session_id=owner["id"], active_only=True) == []
    finally:
        for handle in handles:
            service.kill(handle.process_id, owner_session_id=owner["id"])


def test_remote_session_delete_cleans_owned_processes(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=3)
    owner = client.create_session("default")
    service = workspace_process_service(Path(owner["workspace"]))
    handle = service.start(
        "sleep 30",
        cwd=Path(owner["workspace"]),
        owner_session_id=owner["id"],
        tty=False,
    )

    assert client.delete_session(owner["id"]) is True
    assert service.get(handle.process_id).status in {"cancelled", "killed"}


def test_remote_child_status_reads_existing_subagent_registry(local_service):
    from nz_coder.runtime.agent import subagent as subagent_module

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    parent = client.create_session("default")
    workspace = Path(parent["workspace"])
    state = subagent_module._new_subagent_state(parent["id"], "explore", ["read_file"])
    state.update({
        "status": "completed",
        "model_id": "provider/model",
        "messages": [{"role": "assistant", "content": "done"}],
        "changed_files": ["src/app.py"],
        "verification_status": "passed",
    })
    subagent_module._save_subagent_state(parent["id"], state, workspace)

    reattached = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    children = reattached.list_children(parent["id"])
    assert len(children) == 1
    assert {
        key: children[0][key]
        for key in ("session_id", "agent_type", "status", "model_id", "message_count")
    } == {
        "session_id": state["session_id"],
        "agent_type": "explore",
        "status": "completed",
        "model_id": "provider/model",
        "message_count": 1,
    }
    detail = reattached.get_child(parent["id"], state["session_id"])
    assert detail["changed_files"] == ["src/app.py"]
    assert detail["verification_status"] == "passed"


def test_remote_child_running_disconnect_then_completed_reconnect(
    local_service,
    monkeypatch,
):
    """A child remains daemon-owned while terminal clients come and go."""
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent as subagent_module

    class BlockingMessage:
        content = "child complete"
        tool_calls = []

        def model_dump(self):
            return {"role": "assistant", "content": self.content, "tool_calls": []}

    class BlockingCompletions:
        def create(self, **_kwargs):
            model_started.set()
            assert allow_completion.wait(timeout=10)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=BlockingMessage(), finish_reason="stop")],
                usage=None,
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=BlockingCompletions()),
    )
    monkeypatch.setattr(subagent_module, "OpenAI", lambda **_kwargs: fake_client)
    previous_turns = config.SUBAGENT_MAX_TURNS
    previous_timeout = config.SUBAGENT_TIMEOUT_SECONDS
    config.SUBAGENT_MAX_TURNS = 1
    config.SUBAGENT_TIMEOUT_SECONDS = 30

    first = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    parent = first.create_session("default")
    workspace = Path(parent["workspace"])
    model_started = threading.Event()
    allow_completion = threading.Event()
    result: dict[str, str] = {}

    def child_worker() -> None:
        with scoped_workdir(workspace), subagent_module.scoped_parent_context(
            session_id=parent["id"],
            agent_id="agent-parent",
            trace_id="trace-parent",
        ):
            result["value"] = subagent_module.run_subagent(
                "inspect the workspace",
                agent_type="explore",
            )

    worker = threading.Thread(target=child_worker, name="phase2-child-lifecycle")
    worker.start()
    try:
        assert model_started.wait(timeout=10)
        running = first.list_children(parent["id"])
        assert len(running) == 1
        child_id = running[0]["session_id"]
        assert running[0]["status"] == "running"

        # Dropping this client does not own or cancel the child task.
        del first
        allow_completion.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert "[Subagent status: completed]" in result["value"]

        reattached = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
        completed = reattached.get_child(parent["id"], child_id)
        assert completed["status"] == "completed"
        assert completed["changed_files"] == []
        assert completed["parent_session_id"] == parent["id"]
    finally:
        allow_completion.set()
        worker.join(timeout=5)
        config.SUBAGENT_MAX_TURNS = previous_turns
        config.SUBAGENT_TIMEOUT_SECONDS = previous_timeout


def test_two_clients_cannot_double_answer_permission(local_service):
    first = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    second = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = first.create_session("default")["id"]
    first.run(session_id, "permission")
    request = _wait_for_pending(lambda: first.pending_permissions(session_id))[0]
    barrier = threading.Barrier(2)

    def reply(client, value):
        barrier.wait(timeout=2)
        try:
            return client.reply_permission(session_id, request["id"], value)
        except NZCoderHTTPError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda item: reply(*item), ((first, "once"), (second, "reject"))))

    assert results.count(True) == 1
    assert results.count("interaction_not_found") == 1
    _wait_for_status(first, session_id, "completed")


def test_two_attached_clients_receive_same_events_and_one_permission_effect(local_service):
    first = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    second = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = first.create_session("default")["id"]
    first_events = first.events(session_id, replay=0)
    second_events = second.events(session_id, replay=0)
    assert next(first_events)["type"] == "server.connected"
    assert next(second_events)["type"] == "server.connected"

    time.sleep(local_service.heartbeat_seconds * 2)
    first.run(session_id, "permission")
    try:
        first_started = _next_product_event(first_events)
        first_asked = _next_product_event(first_events)
        second_started = _next_product_event(second_events)
        second_asked = _next_product_event(second_events)
        assert first_started["meta"]["event_id"] == second_started["meta"]["event_id"]
        assert first_asked["type"] == second_asked["type"] == "permission.asked"
        assert first_asked["meta"]["event_id"] == second_asked["meta"]["event_id"]
        request_id = first_asked["properties"]["id"]

        assert first.reply_permission(session_id, request_id, "once") is True
        with pytest.raises(NZCoderHTTPError) as already_resolved:
            second.reply_permission(session_id, request_id, "reject")
        assert already_resolved.value.code == "interaction_not_found"

        first_tail = _product_events_through(first_events, "session.run.settled")
        second_tail = _product_events_through(second_events, "session.run.settled")
        assert [event["meta"]["event_id"] for event in first_tail] == [
            event["meta"]["event_id"] for event in second_tail
        ]
        assert first.messages(session_id)[-1]["content"] == "permission:once"
    finally:
        first_events.close()
        second_events.close()


def test_cross_session_interaction_responses_are_rejected(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    owner_id = client.create_session("default")["id"]
    other_id = client.create_session("default")["id"]

    client.run(owner_id, "permission")
    permission = _wait_for_pending(lambda: client.pending_permissions(owner_id))[0]
    with pytest.raises(NZCoderHTTPError) as wrong_permission_session:
        client.reply_permission(other_id, permission["id"], "once")
    assert wrong_permission_session.value.status == 404
    assert wrong_permission_session.value.code == "interaction_not_found"
    assert client.reply_permission(owner_id, permission["id"], "once") is True
    _wait_for_status(client, owner_id, "completed")

    client.run(owner_id, "question")
    question = _wait_for_pending(lambda: client.pending_questions(owner_id))[0]
    with pytest.raises(NZCoderHTTPError) as wrong_question_session:
        client.reply_question(other_id, question["id"], [["Current file"]])
    assert wrong_question_session.value.status == 404
    assert wrong_question_session.value.code == "interaction_not_found"
    assert client.reply_question(owner_id, question["id"], [["Current file"]]) is True
    _wait_for_status(client, owner_id, "completed")


def test_question_tool_and_http_broker_share_request_identity():
    from nz_coder.tools import dispatch, scoped_tool_call
    from nz_coder.tools.question import (
        scoped_question_asker,
        scoped_question_lifecycle_reporter,
    )

    bus = SessionEventBus(session_id="shared-question-session")
    broker = InteractionBroker(
        session_id="shared-question-session",
        event_bus=bus,
        timeout_seconds=2,
    )
    broker.begin_run()
    lifecycle = []
    questions = [{
        "header": "Scope",
        "question": "Which scope should be changed?",
        "options": [
            {"label": "File", "description": "Current file."},
            {"label": "Repo", "description": "Whole repository."},
        ],
    }]

    def execute_question():
        with (
            scoped_tool_call("call-shared-question"),
            scoped_question_lifecycle_reporter(
                lambda action, payload: lifecycle.append((action, payload)),
            ),
            scoped_question_asker(broker.ask_question),
        ):
            return dispatch("question", {"questions": questions})

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(execute_question)
        pending = _wait_for_pending(lambda: broker.list("question"))[0]
        assert lifecycle[0][0] == "pending"
        assert pending["id"] == lifecycle[0][1]["request_id"]
        assert pending["id"].startswith("question-")
        broker.reply_question(pending["id"], [["File"]])
        result = future.result(timeout=2)

    assert "File" in result
    assert [action for action, _payload in lifecycle] == ["pending", "completed"]
    assert lifecycle[-1][1]["tool_call_id"] == "call-shared-question"
    broker.close()


def test_http_permission_timeout_rejects_and_releases_the_run():
    manager = SessionManager(
        agent_factory=_fake_factory,
        interaction_timeout_seconds=0.05,
        restore_saved=False,
    )
    service = SessionHTTPService(
        port=0,
        token="test-token-1234567890",
        manager=manager,
        heartbeat_seconds=0.05,
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    client = NZCoderClient(service.base_url, service.token, timeout=2)
    try:
        session_id = client.create_session("default")["id"]
        client.run(session_id, "permission")
        _wait_for_status(client, session_id, "completed")
        assert client.messages(session_id)[-1]["content"] == "permission:reject"
        assert client.pending_permissions(session_id) == []

        events = client.events(session_id, replay=10)
        assert next(events)["type"] == "server.connected"
        replay = [next(events) for _ in range(6)]
        events.close()
        replied = next(event for event in replay if event["type"] == "permission.replied")
        assert replied["properties"]["reply"] == "reject"
        assert replied["properties"]["reason"] == "timeout"
    finally:
        service.shutdown()
        thread.join(timeout=2)


def test_http_abort_blocks_an_interaction_that_starts_after_cancel(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    agent = local_service.manager.get(session_id).agent

    client.run(session_id, "late-question")
    assert agent.before_late_question.wait(timeout=2)
    assert client.abort(session_id) == {"aborted": True}
    agent.allow_late_question.set()

    _wait_for_status(client, session_id, "cancelled")
    assert client.pending_questions(session_id) == []
    assert client.messages(session_id)[-1]["content"] == "question:dismissed"


def test_interaction_registration_and_asked_event_are_atomic():
    class BlockingAskedBus(SessionEventBus):
        def __init__(self):
            super().__init__(session_id="atomic-session")
            self.asked_entered = threading.Event()
            self.allow_asked = threading.Event()

        def publish(self, event_type, properties=None):
            if event_type == "permission.asked":
                self.asked_entered.set()
                assert self.allow_asked.wait(timeout=2)
            return super().publish(event_type, properties)

    bus = BlockingAskedBus()
    broker = InteractionBroker(
        session_id="atomic-session",
        event_bus=bus,
        timeout_seconds=2,
    )
    broker.begin_run()
    answer = []
    asker = threading.Thread(
        target=lambda: answer.append(broker.ask_permission("edit_file", {})),
        daemon=True,
    )
    asker.start()
    assert bus.asked_entered.wait(timeout=2)
    request_id = next(iter(broker._pending))

    reply_done = threading.Event()

    def reply():
        broker.reply_permission(request_id, "once")
        reply_done.set()

    replier = threading.Thread(target=reply, daemon=True)
    replier.start()
    assert not reply_done.wait(timeout=0.05)
    bus.allow_asked.set()
    asker.join(timeout=2)
    replier.join(timeout=2)

    assert not asker.is_alive() and not replier.is_alive()
    assert answer == ["once"]
    assert [event.type for event in bus.recent()] == [
        "permission.asked",
        "permission.replied",
    ]


def test_interaction_waiter_cannot_overtake_terminal_event_publish():
    class BlockingTerminalBus(SessionEventBus):
        def __init__(self):
            super().__init__(session_id="terminal-session")
            self.terminal_entered = threading.Event()
            self.allow_terminal = threading.Event()

        def publish(self, event_type, properties=None):
            if event_type == "permission.replied":
                self.terminal_entered.set()
                assert self.allow_terminal.wait(timeout=2)
            return super().publish(event_type, properties)

    bus = BlockingTerminalBus()
    broker = InteractionBroker(
        session_id="terminal-session",
        event_bus=bus,
        timeout_seconds=0.05,
    )
    broker.begin_run()
    answer = []
    asker_done = threading.Event()

    def ask():
        answer.append(broker.ask_permission("edit_file", {}))
        asker_done.set()

    asker = threading.Thread(target=ask, daemon=True)
    asker.start()
    deadline = time.monotonic() + 2
    while not broker.list("permission") and time.monotonic() < deadline:
        time.sleep(0.001)
    request_id = broker.list("permission")[0]["id"]
    replier = threading.Thread(
        target=lambda: broker.reply_permission(request_id, "once"),
        daemon=True,
    )
    replier.start()
    assert bus.terminal_entered.wait(timeout=2)
    assert not asker_done.wait(timeout=0.1)

    bus.allow_terminal.set()
    asker.join(timeout=2)
    replier.join(timeout=2)

    assert not asker.is_alive() and not replier.is_alive()
    assert answer == ["once"]
    assert [event.type for event in bus.recent()] == [
        "permission.asked",
        "permission.replied",
    ]


def test_http_service_rejects_non_loopback_and_short_token():
    manager = SessionManager(agent_factory=_fake_factory)
    with pytest.raises(ValueError, match="only accepts"):
        SessionHTTPService(host="0.0.0.0", port=0, manager=manager)
    with pytest.raises(ValueError, match="at least 16"):
        SessionHTTPService(port=0, token="short", manager=manager)
    with pytest.raises(ValueError, match="port"):
        SessionHTTPService(port=True, manager=manager)
    with pytest.raises(ValueError, match="positive finite"):
        SessionManager(agent_factory=_fake_factory, interaction_timeout_seconds=0)
    with pytest.raises(ValueError, match="positive finite"):
        SessionHTTPService(
            port=0,
            token="test-token-1234567890",
            interaction_timeout_seconds=float("inf"),
        )
    with pytest.raises(ValueError, match="loopback"):
        NZCoderClient("http://example.test:4096", "test-token-1234567890")


@pytest.mark.parametrize("heartbeat", [0, -1, float("inf"), float("nan")])
def test_http_service_rejects_invalid_heartbeat(heartbeat):
    manager = SessionManager(agent_factory=_fake_factory)
    try:
        with pytest.raises(ValueError, match="heartbeat"):
            SessionHTTPService(
                port=0,
                manager=manager,
                heartbeat_seconds=heartbeat,
            )
    finally:
        manager.close()


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_http_client_rejects_invalid_transport_timeout(timeout):
    with pytest.raises(ValueError, match="client timeout"):
        NZCoderClient(
            "http://127.0.0.1:4096",
            "test-token-1234567890",
            timeout=timeout,
        )


@pytest.mark.parametrize("timeout", [-1, True, float("inf"), float("nan")])
def test_managed_session_rejects_invalid_wait_timeout(local_service, timeout):
    session_id = local_service.manager.create()["id"]

    with pytest.raises(ValueError, match="wait timeout"):
        local_service.manager.get(session_id).wait(timeout)


@pytest.mark.parametrize("timeout", [-1, True, float("inf"), float("nan")])
def test_session_manager_rejects_invalid_close_timeout(timeout):
    manager = SessionManager(agent_factory=_fake_factory)
    try:
        with pytest.raises(ValueError, match="close timeout"):
            manager.close(timeout)
        assert manager.create()["id"]
    finally:
        manager.close()


def test_http_client_reports_validation_errors(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    with pytest.raises(NZCoderHTTPError) as invalid_mode:
        client.create_session("unrestricted")
    assert invalid_mode.value.status == 400
    assert invalid_mode.value.code == "invalid_request"

    request = Request(
        local_service.base_url + "/session",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {local_service.token}",
            "Content-Type": "text/plain",
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as content_type_error:
        build_opener(ProxyHandler({})).open(request, timeout=2)
    assert content_type_error.value.code == 400
    assert content_type_error.value.headers.get("Connection") == "close"

    with pytest.raises(ValueError, match="valid event ID"):
        list(client.events("session", last_event_id="bad\nheader"))
    with pytest.raises(ValueError, match="non-negative integer"):
        list(client.events("session", reconnect_attempts=True))
    with pytest.raises(ValueError, match="non-negative finite"):
        list(client.events("session", reconnect_delay=float("nan")))
    with pytest.raises(ValueError, match="Out of range float"):
        client._request("POST", "/session", {"permission_mode": float("nan")})

    nonstandard = Request(
        local_service.base_url + "/session",
        data=b'{"permission_mode": NaN}',
        headers={
            "Authorization": f"Bearer {local_service.token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with pytest.raises(HTTPError) as nonstandard_error:
        build_opener(ProxyHandler({})).open(nonstandard, timeout=2)
    assert nonstandard_error.value.code == 400


def test_http_workspace_registry_and_unknown_workspace(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    workspaces = client.list_workspaces()

    assert len(workspaces) == 1
    assert workspaces[0]["default"] is True
    assert workspaces[0]["path"] == str(current_workdir())
    created = client.create_session("default", workspaces[0]["id"])
    assert created["workspace_id"] == workspaces[0]["id"]
    assert created["workspace"] == str(current_workdir())

    with pytest.raises(NZCoderHTTPError) as missing:
        client.create_session("default", "ws-not-authorized")
    assert missing.value.status == 404
    assert missing.value.code == "workspace_not_found"
    with pytest.raises(NZCoderHTTPError) as malformed:
        client._request("POST", "/session", {"workspace_id": []})
    assert malformed.value.status == 400
    assert malformed.value.code == "invalid_request"


def test_http_instruction_file_control_plane_closes_runtime_loop(tmp_path):
    project = tmp_path / "instructions-project"
    project.mkdir()
    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[project],
        restore_saved=False,
    )
    service = SessionHTTPService(
        port=0,
        token="test-token-1234567890",
        manager=manager,
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=2)
        workspace_id = manager.workspaces.id_for(project)

        assert client.list_instruction_files("project", workspace_id) == {
            "files": [],
            "warnings": [],
        }
        created = client.create_instruction_file("project", workspace_id)
        assert created["id"] == "project:AGENTS.md"
        assert created["enabled"] is True
        (project / "AGENTS.md").write_text("http authority", encoding="utf-8")

        disabled = client.set_instruction_file_enabled(
            "project",
            "AGENTS.md",
            False,
            workspace_id,
        )
        assert disabled["enabled"] is False
        assert client.list_instruction_files("project", workspace_id)["files"][0][
            "enabled"
        ] is False
        from nz_coder.state.instructions import load_instruction_context

        assert "http authority" not in load_instruction_context(project).reminder
        assert client.delete_instruction_file(
            "project",
            "AGENTS.md",
            workspace_id,
        ) is True
        assert not (project / "AGENTS.md").exists()
    finally:
        service.shutdown()
        thread.join(timeout=2)


def test_http_different_workspaces_run_concurrently(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[first, second],
        restore_saved=False,
    )
    service = SessionHTTPService(
        port=0,
        token="test-token-1234567890",
        manager=manager,
    )
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    client = NZCoderClient(service.base_url, service.token, timeout=2)
    try:
        workspace_ids = {
            item["path"]: item["id"] for item in client.list_workspaces()
        }
        first_id = client.create_session(
            "default", workspace_ids[str(first)]
        )["id"]
        second_id = client.create_session(
            "default", workspace_ids[str(second)]
        )["id"]

        assert manager.get(first_id).agent.workspace == first
        assert manager.get(second_id).agent.workspace == second
        assert client.run(first_id, "wait")["status"] == "running"
        assert client.run(second_id, "wait")["status"] == "running"
        assert client.abort(first_id) == {"aborted": True}
        assert client.abort(second_id) == {"aborted": True}
        _wait_for_status(client, first_id, "cancelled")
        _wait_for_status(client, second_id, "cancelled")
    finally:
        service.shutdown()
        thread.join(timeout=2)


def test_http_restart_discovers_and_lazily_restores_session(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    first_manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=False,
    )
    workspace_id = next(
        item["id"]
        for item in first_manager.list_workspaces()
        if item["path"] == str(workspace)
    )
    created = first_manager.create("default", workspace_id)
    session_id = created["id"]
    first_manager.start_run(session_id, "hello")
    assert first_manager.get(session_id).wait(timeout=3)
    first_snapshot = first_manager.get(session_id).snapshot()
    first_ids = [item["info"]["id"] for item in first_snapshot["messages"]]
    session_path = workspace / ".nz-coder" / "sessions" / f"{session_id}.json"
    persisted = json.loads(session_path.read_text(encoding="utf-8"))
    assert persisted["message_schema_version"] == 1
    assert all("_nz_message_id" in item for item in persisted["messages"])
    first_manager.close()
    persisted.pop("run_status")
    session_path.write_text(json.dumps(persisted), encoding="utf-8")

    second_manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        dormant = next(
            item for item in second_manager.list() if item["id"] == session_id
        )
        assert dormant["status"] == "dormant"
        assert dormant["restored"] is True
        assert dormant["workspace_id"] == workspace_id
        assert dormant["message_count"] == 2
        assert session_id not in second_manager._sessions

        restored_snapshot = second_manager.get(session_id).snapshot()
        assert [
            item["info"]["id"] for item in restored_snapshot["messages"]
        ] == first_ids

        second_manager.start_run(session_id, "again")
        restored = second_manager.get(session_id)
        assert restored.wait(timeout=3)
        assert restored.info()["status"] == "completed"
        assert restored.info()["restored"] is True
        assert restored.messages() == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "answer:hello"},
            {"role": "user", "content": "again"},
            {"role": "assistant", "content": "answer:again"},
        ]
    finally:
        second_manager.close()


def test_http_restore_marks_an_unsettled_accepted_run_as_interrupted(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with scoped_workdir(workspace):
        save_session(
            [{"role": "user", "content": "accepted-before-crash"}],
            mode="default",
            session_id="interrupted-session",
            activate=False,
            run_status="running",
        )

    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        discovered = next(
            item for item in manager.list() if item["id"] == "interrupted-session"
        )
        assert discovered["status"] == "interrupted"
        assert discovered["running"] is False
        assert "before the accepted run settled" in discovered["last_error"]

        restored = manager.get("interrupted-session")
        assert restored.info()["status"] == "interrupted"
        assert restored.messages() == [
            {"role": "user", "content": "accepted-before-crash"}
        ]
        restored.start_run("retry")
        assert restored.wait(timeout=3)
        assert restored.info()["status"] == "completed"
    finally:
        manager.close()


def test_http_restore_projects_typed_assistant_finish_and_error(tmp_path):
    from nz_coder.protocol.message_schema import attach_message_identity, set_assistant_error
    from nz_coder.runtime.session.session_processor import SessionProcessor

    workspace = tmp_path / "project"
    workspace.mkdir()
    assistant = {"role": "assistant", "content": "partial"}
    attach_message_identity(
        assistant,
        "msg-provider-error",
        session_id="typed-error-session",
    )
    processor = SessionProcessor(assistant)
    processor.start_step()
    set_assistant_error(
        assistant,
        "provider unavailable",
        name="APIError",
        data={"message": "provider unavailable", "isRetryable": False},
    )
    processor.finish_step("error")
    with scoped_workdir(workspace):
        save_session(
            [
                {"role": "user", "content": "continue"},
                assistant,
            ],
            mode="default",
            session_id="typed-error-session",
            activate=False,
            run_status="error",
        )

    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        restored = manager.get("typed-error-session")
        snapshot = restored.snapshot()
        info = snapshot["messages"][1]["info"]

        assert info["finish"] == "error"
        assert info["error"] == {
            "name": "APIError",
            "data": {
                "message": "provider unavailable",
                "isRetryable": False,
            },
        }
        assert restored.messages()[1] == {
            "role": "assistant",
            "content": "partial",
        }
    finally:
        manager.close()


def test_http_restore_terminates_durable_pending_question_parts(tmp_path):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.session.session_processor import SessionProcessor

    workspace = tmp_path / "project"
    workspace.mkdir()
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-question-crash",
            "type": "function",
            "function": {"name": "question", "arguments": "{}"},
        }],
    }
    attach_message_identity(
        assistant,
        "msg-question-crash",
        session_id="question-crash-session",
    )
    processor = SessionProcessor(assistant)
    processor.register_tool_calls(assistant["tool_calls"])
    processor.start_tools(assistant["tool_calls"])
    processor.start_question("call-question-crash", "question-before-crash", [{
        "header": "Scope",
        "question": "Which scope?",
        "options": [
            {"label": "File", "description": "Current file."},
            {"label": "Repo", "description": "Whole repository."},
        ],
    }])
    with scoped_workdir(workspace):
        save_session(
            [{"role": "user", "content": "ask"}, assistant],
            mode="default",
            session_id="question-crash-session",
            activate=False,
            run_status="running",
        )

    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        restored = manager.get("question-crash-session")
        snapshot = restored.snapshot()
        parts = snapshot["messages"][1]["parts"]
        tool = next(part for part in parts if part["type"] == "tool")
        question = next(part for part in parts if part["type"] == "question")
        assert restored.info()["status"] == "interrupted"
        assert tool["state"]["status"] == "error"
        assert tool["state"]["interrupted"] is True
        assert question["status"] == "terminated"
        assert snapshot["pending"]["questions"] == []
    finally:
        manager.close()


def test_resilient_client_rebaselines_after_an_explicit_gap(monkeypatch):
    client = NZCoderClient(
        "http://127.0.0.1:4096",
        "test-token-1234567890",
    )
    snapshots = iter([
        {"cursor": {"event_id": "cursor-one", "sequence": 1}, "messages": []},
        {"cursor": {"event_id": "cursor-two", "sequence": 8}, "messages": [
            {"info": {"id": "message-one"}, "parts": []},
        ]},
    ])
    cursors = []

    monkeypatch.setattr(client, "snapshot", lambda _session_id: next(snapshots))

    def fake_events(_session_id, **kwargs):
        cursors.append(kwargs["last_event_id"])
        yield {"type": "server.connected", "properties": {}}
        if len(cursors) == 1:
            yield {
                "type": "server.event_gap",
                "properties": {"resume_required": True},
            }
        else:
            yield {
                "type": "session.run.settled",
                "properties": {"status": "completed"},
                "meta": {"event_id": "settled"},
            }

    monkeypatch.setattr(client, "events", fake_events)
    stream = client.resilient_events("session-a", resync_attempts=1)

    assert [next(stream)["type"] for _ in range(6)] == [
        "server.snapshot",
        "server.connected",
        "server.event_gap",
        "server.snapshot",
        "server.connected",
        "session.run.settled",
    ]
    assert cursors == ["cursor-one", "cursor-two"]
    with pytest.raises(StopIteration):
        next(stream)


def test_resilient_client_rebaselines_after_cursor_expiration(monkeypatch):
    client = NZCoderClient(
        "http://127.0.0.1:4096",
        "test-token-1234567890",
    )
    snapshots = iter([
        {"cursor": {"event_id": "expired", "sequence": 1}, "messages": []},
        {"cursor": {"event_id": "fresh", "sequence": 9}, "messages": []},
    ])
    calls = 0
    monkeypatch.setattr(client, "snapshot", lambda _session_id: next(snapshots))

    def fake_events(_session_id, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NZCoderHTTPError(410, "event_cursor_expired", "expired")
        yield {"type": "server.connected", "properties": {}}

    monkeypatch.setattr(client, "events", fake_events)
    stream = client.resilient_events("session-a", resync_attempts=1)

    assert [next(stream)["type"] for _ in range(4)] == [
        "server.snapshot",
        "server.event_gap",
        "server.snapshot",
        "server.connected",
    ]
    with pytest.raises(StopIteration):
        next(stream)


def test_http_restore_skips_corrupt_or_cross_workspace_metadata(tmp_path):
    workspace = tmp_path / "project"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    with scoped_workdir(workspace):
        mismatched_path = save_session(
            [{"role": "user", "content": "unsafe"}],
            mode="default",
            session_id="cross-workspace",
            activate=False,
        )
    payload = json.loads(mismatched_path.read_text(encoding="utf-8"))
    payload["workspace"] = str(other)
    mismatched_path.write_text(json.dumps(payload), encoding="utf-8")
    corrupt_path = mismatched_path.parent / "corrupt-session.json"
    corrupt_path.write_text("{broken", encoding="utf-8")
    missing_workspace = dict(payload)
    missing_workspace["session_id"] = "missing-workspace"
    missing_workspace.pop("workspace")
    (mismatched_path.parent / "missing-workspace.json").write_text(
        json.dumps(missing_workspace),
        encoding="utf-8",
    )
    invalid_workspace = dict(payload)
    invalid_workspace["session_id"] = "invalid-workspace"
    invalid_workspace["workspace"] = "\0"
    (mismatched_path.parent / "invalid-workspace.json").write_text(
        json.dumps(invalid_workspace),
        encoding="utf-8",
    )

    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        restored_ids = {item["id"] for item in manager.list()}
        assert "cross-workspace" not in restored_ids
        assert "corrupt-session" not in restored_ids
        assert "missing-workspace" not in restored_ids
        assert "invalid-workspace" not in restored_ids
    finally:
        manager.close()


def test_workspace_registry_rejects_invalid_roots(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="does not exist"):
        SessionManager(
            agent_factory=_fake_factory,
            workspace_roots=[missing],
            restore_saved=False,
        )

    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="not a directory"):
        SessionManager(
            agent_factory=_fake_factory,
            workspace_roots=[file_path],
            restore_saved=False,
        )

    nested = current_workdir() / "tests"
    with pytest.raises(ValueError, match="must not overlap"):
        SessionManager(
            agent_factory=_fake_factory,
            workspace_roots=[nested],
            restore_saved=False,
        )


def test_http_restore_skips_oversized_session_file(tmp_path, monkeypatch):
    from nz_coder.http_service import manager as manager_module

    workspace = tmp_path / "project"
    workspace.mkdir()
    with scoped_workdir(workspace):
        save_session(
            [{"role": "user", "content": "too large"}],
            mode="default",
            session_id="oversized-session",
            activate=False,
        )
    monkeypatch.setattr(manager_module, "_MAX_SAVED_SESSION_BYTES", 1)

    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        assert "oversized-session" not in {
            item["id"] for item in manager.list()
        }
    finally:
        manager.close()


def test_http_agent_prompt_uses_the_selected_workspace_state(tmp_path, monkeypatch):
    from nz_coder import loop as loop_module
    from nz_coder.runtime.conversation import prompt as prompt_module

    workspace = tmp_path / "project"
    skills_dir = workspace / ".nz-coder" / "skills" / "project-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(
        "---\nname: project-skill\ndescription: Selected workspace skill\n---\nBody\n",
        encoding="utf-8",
    )
    captured = {}

    monkeypatch.setattr(MemoryManager, "load_all", lambda self: None)
    monkeypatch.setattr(
        MemoryManager,
        "build_prompt_block",
        lambda self, **_kwargs: f"memory:{self.memory_dir}",
    )

    def fake_build(*, memory_block, skill_descriptions):
        captured["memory_block"] = memory_block
        captured["skill_descriptions"] = skill_descriptions
        return "workspace prompt"

    class StubAgent:
        def __init__(self, system_prompt, **kwargs):
            captured["system_prompt"] = system_prompt
            captured["memory_manager"] = current_memory_manager()
            captured["skill_loader"] = current_skill_loader()
            captured["kwargs"] = kwargs

    monkeypatch.setattr(prompt_module, "build", fake_build)
    monkeypatch.setattr(loop_module, "AgentLoop", StubAgent)

    with scoped_workdir(workspace):
        build_http_agent("http-workspace-prompt", "default")

    assert captured["memory_block"] == ""
    assert "project-skill" in captured["skill_descriptions"]
    assert captured["memory_manager"].memory_dir == workspace / ".nz-coder" / "memory"
    assert captured["skill_loader"]._project_dir == workspace / ".nz-coder" / "skills"
    assert captured["system_prompt"] == "workspace prompt"
    assert not captured["kwargs"].get("auto_mode_classifier_enabled", False)
    assert captured["kwargs"]["event_bus"]._journal.path == (
        workspace
        / ".nz-coder"
        / "sessions"
        / "_artifacts"
        / "http-workspace-prompt"
        / "runtime"
        / "events.jsonl"
    )


def test_cli_main_dispatches_serve_subcommand(monkeypatch):
    from nz_coder.http_service import cli as service_cli
    from nz_coder.interface import cli

    captured = {}

    def fake_serve_main(argv):
        captured["argv"] = argv
        return 7

    monkeypatch.setattr(service_cli, "serve_main", fake_serve_main)

    assert cli.main(["serve", "--port", "0"]) == 7
    assert captured["argv"] == ["--port", "0"]


def test_serve_main_passes_interaction_timeout_and_closes(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.http_service import cli as service_cli

    captured = {}

    class FakeService:
        base_url = "http://127.0.0.1:4096"
        token = "generated-token-123456"

        def __init__(self, **kwargs):
            captured.update(kwargs)

        def serve_forever(self):
            raise KeyboardInterrupt

        def close_after_serve(self):
            captured["closed"] = True

    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(service_cli, "SessionHTTPService", FakeService)

    result = service_cli.serve_main([
        "--port",
        "4096",
        "--token",
        "test-token-1234567890",
        "--interaction-timeout",
        "12.5",
        "--workspace",
        "/tmp/project-a",
        "--workspace",
        "/tmp/project-b",
    ])

    assert result == 0
    assert captured["interaction_timeout_seconds"] == 12.5
    assert captured["workspace_roots"] == ["/tmp/project-a", "/tmp/project-b"]
    assert captured["closed"] is True
