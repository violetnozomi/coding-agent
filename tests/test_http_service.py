"""Integration tests for the optional loopback Session HTTP service."""
from __future__ import annotations

import asyncio
import copy
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
from nz_coder.runtime.process.workdir import current_workdir, scoped_workdir
from nz_coder.runtime.process.process_service import workspace_process_service
from nz_coder.state.sessions import load_session, save_session, session_runtime_dir
from nz_coder.foundation.user_paths import user_storage_layout
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


def test_manager_close_continues_other_sessions_and_retries_failed_cleanup(tmp_path, monkeypatch):
    """一个资源关闭失败时，其他会话仍须清理，失败会话可以重试。"""
    manager = SessionManager(
        agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False,
    )
    first = manager.get(manager.create()["id"])
    second = manager.get(manager.create()["id"])
    close_first = first.agent.close
    blocked = True

    def flaky_close():
        if blocked:
            raise RuntimeError("injected close failure")
        close_first()

    monkeypatch.setattr(first.agent, "close", flaky_close)
    try:
        with pytest.raises(RuntimeError):
            manager.close(timeout=0.03)
        assert second.event_bus._closed is True
        assert first._disposed is False
        assert manager.get(first.session_id) is first
        with pytest.raises(LookupError):
            first.start_run("closed sessions must reject new work")
        blocked = False
        manager.close(timeout=0.03)
        assert first.event_bus._closed is True
        assert manager.list()["sessions"] == []
    finally:
        blocked = False
        close_first()
        second.agent.close()


def test_manager_close_retains_unsettled_run_until_it_can_be_disposed(tmp_path):
    """取消不合作时保留执行及其事件资源，待工作线程退出后再释放。"""
    from nz_coder.http_service.manager import SessionBusyError

    entered = threading.Event()
    release = threading.Event()

    class SlowAgent(FakeAgent):
        async def run(self, messages, **kwargs):
            entered.set()
            while not release.is_set():
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    continue
            return {"status": "completed"}

    manager = SessionManager(
        agent_factory=SlowAgent, workspace_roots=[tmp_path], restore_saved=False,
    )
    session = manager.get(manager.create()["id"])
    error = None
    try:
        session.start_run("offline run")
        assert entered.wait(2)
        try:
            manager.close(timeout=0.02)
        except SessionBusyError as exc:
            error = exc
        assert session._disposed is False
        assert session.event_bus._closed is False
        assert manager.get(session.session_id) is session
        assert isinstance(error, SessionBusyError)
        with pytest.raises(LookupError):
            session.start_run("shutdown already requested")
    finally:
        release.set()
        assert session.wait(timeout=2)
        manager.close(timeout=0.1)
        session.agent.close()
    assert session._disposed is True
    assert session.event_bus._closed is True


def test_http_service_close_can_retry_failed_session_cleanup(tmp_path, monkeypatch):
    manager = SessionManager(
        agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False,
    )
    session = manager.get(manager.create()["id"])
    close_agent = session.agent.close
    blocked = True

    def flaky_close():
        if blocked:
            raise RuntimeError("injected close failure")
        close_agent()

    monkeypatch.setattr(session.agent, "close", flaky_close)
    service = SessionHTTPService(port=0, manager=manager)
    try:
        with pytest.raises(RuntimeError):
            service.close_after_serve()
        blocked = False
        service.close_after_serve()
        assert session.event_bus._closed is True
        assert manager.list()["sessions"] == []
    finally:
        blocked = False
        service.close_after_serve()
        close_agent()


def test_manager_retains_agent_with_reported_cleanup_failure(tmp_path):
    """真实 Agent.close 只记录失败时，管理器也必须保留资源所有权。"""
    from nz_coder.http_service.manager import SessionBusyError
    from nz_coder.runtime.execution.loop import AgentLoop

    class Resource:
        blocked = True
        closed = False

        def close(self):
            if self.blocked:
                raise RuntimeError("injected MCP cleanup failure")
            self.closed = True

    resource = Resource()

    def factory(session_id, permission_mode):
        return AgentLoop(
            "offline cleanup", permission_mode=permission_mode,
            client=SimpleNamespace(), trace_enabled=False, sidecar_verifier=False,
            session_id=session_id, event_bus=SessionEventBus(session_id=session_id),
        )

    manager = SessionManager(
        agent_factory=factory, workspace_roots=[tmp_path], restore_saved=False,
    )
    session = manager.get(manager.create()["id"])
    session.agent._mcp_runtime = resource
    try:
        with pytest.raises(SessionBusyError):
            manager.close(timeout=0.03)
        assert session.agent.environment_cleanup_failures == {"mcp": "RuntimeError"}
        assert session._disposed is False
        assert manager.get(session.session_id) is session
        assert resource.closed is False
        resource.blocked = False
        manager.close(timeout=0.03)
        assert session.agent.environment_cleanup_complete is True
        assert resource.closed is True
        assert manager.list()["sessions"] == []
    finally:
        resource.blocked = False
        manager.close(timeout=0.1)
        session.agent.close()


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


def test_remote_message_detail_keeps_model_notes_separate_across_turns(local_service):
    from nz_coder.interface.backend import RemoteTerminalBackend

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session()["id"]
    backend = RemoteTerminalBackend(client, session_id)
    session = local_service.manager.get(session_id)
    session.history.extend([
        {"role": "user", "content": "first request"},
        {"role": "assistant", "content": "The command did not run.",
         "_nz_model_note": "First model note."},
        {"role": "user", "content": "pending internal follow-up", "_nz_synthetic": True},
        {"role": "assistant", "content": "withdrawn answer", "_nz_visible": False,
         "_nz_model_note": "Withdrawn note."},
        {"role": "user", "content": "second request"},
        {"role": "assistant", "content": "Checks passed.", "_nz_model_note": "Second model note."},
    ])
    assert "First model note." not in json.dumps(backend.messages())
    assert "First model note." not in backend.export()["markdown"]
    first = backend.message_detail(1)
    assert "The command did not run." in first and "模型说明" in first and "First model note." in first
    assert "Withdrawn note." not in first and "Second model note." not in first
    assert "pending internal follow-up" not in first
    assert "Second model note." in backend.message_detail(None)
    assert backend.message_detail(3) is None


def test_remote_history_turns_fork_prefix_and_missing_archive(local_service):
    from nz_coder.interface.submission import build_user_submission
    from nz_coder.interface.timeline import TurnHistory
    from nz_coder.protocol.message_schema import COMPACTION_KEY, MESSAGE_ID_KEY, ensure_message_identities
    from nz_coder.state import context, sessions
    from tests.fixtures.compaction_summary import summary_json
    from tests.test_context_budget import _CompactionClient

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session()["id"]
    parent = local_service.manager.get(session_id)
    literal = "<session-summary>A real submitted XML example</session-summary>"
    original = []
    for number, prompt in enumerate(("repeat", "repeat", literal, "fourth", "latest"), start=1):
        user = build_user_submission(prompt, (), workspace=parent.workspace, session_id=session_id,
                                     agent="build", provider_id="offline", model_id="offline")
        original.extend([user,
                         {"role": "assistant", "content": f"Receipt {number}", "_nz_model_note": f"Draft {number}"}])
    ensure_message_identities(original, session_id)
    with sessions.scoped_session(session_id):
        compacted = context.auto_compact(original, _CompactionClient(summary_json("second-turn-only detail")), "offline")
        ensure_message_identities(compacted, session_id)
        archive = sessions.session_transcript_dir() / compacted[0][COMPACTION_KEY]["archive"].rsplit("/", 1)[-1]
    parent.history[:] = copy.deepcopy(compacted)
    first = client.message_detail(session_id, 1)
    assert "Receipt 1" in first and "Draft 1" in first and "Draft 2" not in first
    assert "Draft 2" in client.message_detail(session_id, 2)
    view = TurnHistory.load(compacted, session_id=session_id, workspace=parent.workspace)
    assert literal in view.detail(session_id, view.select(3))
    assert literal in client.message_detail(session_id, 3)
    projection = client.timeline(session_id, 3)
    assert [row["number"] for row in projection["turns"]] == [3, 4, 5]
    assert "Draft" not in json.dumps(projection) and "second-turn-only" not in json.dumps(projection)
    assert "<session-summary>" in client.messages(session_id)[0]["content"]
    assert client.message_detail(session_id, 6) is None
    child_id = client.fork_session(session_id, 1)["id"]
    child = local_service.manager.get(child_id)
    assert [m["content"] for m in child.history] == ["repeat", "Receipt 1"]
    assert child.history[0][MESSAGE_ID_KEY] != original[0][MESSAGE_ID_KEY]
    assert child.history[1]["_nz_parent_id"] == child.history[0][MESSAGE_ID_KEY]

    archive.unlink()
    assert "Draft 5" in client.message_detail(session_id, None)
    partial = client.timeline(session_id)
    assert partial["notice"] and partial["turns"] and all(row["number"] is None for row in partial["turns"])
    for operation in (lambda: client.message_detail(session_id, 1), lambda: client.fork_session(session_id)):
        with pytest.raises(NZCoderHTTPError) as error:
            operation()
        assert (error.value.status, error.value.code) == (409, "history_unavailable")
    assert parent.history == compacted
    assert client.get_session(session_id)["children"] == [child_id]
    assert "Draft 1" in client.message_detail(child_id, 1)


def test_historical_fork_keeps_task_receipt_without_opening_a_later_child_session(local_service, monkeypatch):
    from nz_coder.interface import cli
    from nz_coder.protocol.message_schema import ensure_message_identities
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.session.model import Session
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    from nz_coder.state import sessions
    from tests.test_cli_commands import FakeAgent, FakeConsole

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=3)
    parent_id = client.create_session()["id"]
    parent = local_service.manager.get(parent_id)
    workspace = parent.workspace
    task = subagent._new_subagent_state(parent_id, "explore", ["read_file"])
    task["status"] = "completed"
    task_id = task["session_id"]
    subagent._save_subagent_state(parent_id, task, workspace)
    child_history = [{"role": "user", "content": "original task"}, {"role": "assistant", "content": "original receipt"},
                     {"role": "user", "content": "later child requirement"}]
    LegacyJsonSessionStore().save_session(Session.create(task_id, child_history, workspace=workspace,
                                                         parent_session_id=parent_id))
    receipt = {"id": "part-task", "message_id": "msg-task", "type": "tool", "tool": "task", "call_id": "t",
               "state": {"status": "completed", "input": {"prompt": "original task"}, "output": "original receipt",
                         "title": "explore", "time": {"start": 1, "end": 2}, "metadata": {"child_session_id": task_id}}}
    original = [{"role": "user", "content": "inspect"}, {"role": "assistant", "content": "original receipt",
                "_nz_message_id": "msg-task", "_nz_session_id": parent_id, "_nz_parts": [receipt]},
                {"role": "user", "content": "later parent turn", "_nz_time": {"created": 3}},
                {"role": "assistant", "content": "done"}]
    ensure_message_identities(original, parent_id)
    sessions.save_session(original, session_id=parent_id, activate=False)
    parent.history[:] = copy.deepcopy(original)
    monkeypatch.setattr(cli, "console", FakeConsole())
    monkeypatch.setattr(cli, "_build_agent", lambda _sys, _renderer, sid, **kwargs: FakeAgent(sid))

    def local_fork(number):
        history = copy.deepcopy(original)
        state = {"id": parent_id, "agent": FakeAgent(parent_id)}
        assert cli.handle_command(f"/fork {number}", history, state, "sys", object())
        assert state["id"] != parent_id
        return state["id"]

    for fork_id in (local_fork(1), client.fork_session(parent_id, 1)["id"]):
        saved = sessions.load_session(fork_id)["messages"]
        tool = next(part for part in saved[1]["_nz_parts"] if part["type"] == "tool")
        assert tool["state"]["output"] == "original receipt"
        assert "later" not in json.dumps(saved)
        assert client.list_children(fork_id) == []
        with pytest.raises(NZCoderHTTPError) as error:
            client.get_child(fork_id, task_id)
        assert error.value.status == 404
    current_id = local_fork(2)
    clone, = client.list_children(current_id)
    assert clone["session_id"] != task_id
    assert "later child requirement" in json.dumps(client.get_child(current_id, clone["session_id"]))


def test_http_session_run_messages_and_sse_replay(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)

    assert client.health() == {"status": "ok", "service": "nz-coder"}
    created = client.create_session("auto")
    session_id = created["id"]
    assert created["status"] == "idle"
    assert created["permission_mode"] == "auto"
    private_sessions = user_storage_layout(
        Path(created["workspace"])
    ).workspace_state / "sessions"
    persisted = private_sessions / f"{session_id}.json"
    artifacts = private_sessions / "_artifacts" / session_id
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

    assert [item["id"] for item in client.list_sessions()["sessions"]] == [session_id]
    assert client.delete_session(session_id) is True
    assert not persisted.exists()
    assert not artifacts.exists()
    with pytest.raises(NZCoderHTTPError) as exc_info:
        client.get_session(session_id)
    assert exc_info.value.status == 404
    assert exc_info.value.code == "session_not_found"


@pytest.mark.parametrize("oversized_input", [False, True], ids=["empty-workspace", "workspace-over-budget"])
def test_http_default_native_run_commits_result_history_and_settled_state(monkeypatch, tmp_path, oversized_input):
    import os

    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    from nz_coder.sdk import AgentClient

    if oversized_input:
        limit = WorkspaceSnapshotStore(tmp_path, tmp_path / ".nz-coder").max_file_bytes
        with (tmp_path / "fixture.bin").open("wb") as item:
            item.truncate(limit + 1)

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="OFFLINE_DONE", tool_calls=[]),
                    finish_reason="stop",
                )],
                usage=None,
            )

    runtime = ResolvedModelRuntime(
        provider_id="offline", model_id="offline-model",
        request_model_id="offline-model", variant=None,
        provider=Provider(), client=object(), owns_client=False,
        capabilities=ModelCapabilities(
            provider="offline", model_id="offline-model", supports_streaming=False,
        ),
    )
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _: runtime)
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda _: runtime)
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token="test-token-1234567890")
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        workspace_id = next(
            item["id"] for item in manager.list_workspaces()
            if item["path"] == str(tmp_path)
        )
        session_id = client.create_session("auto", workspace_id=workspace_id)["id"]
        session = manager.get(session_id)
        assert session.agent is None
        assert isinstance(session.client, AgentClient)
        client.run(session_id, "Reply with OFFLINE_DONE.", model="offline/offline-model")
        assert session.wait(timeout=10)

        info = client.get_session(session_id)
        assert info["status"] == "completed_unverified", info
        assert info["running"] is False
        assert info["last_error"] == ""
        result = info["last_result"]
        assert result["status"] == "completed_unverified"
        assert result["metadata"]["authored_content"] == "OFFLINE_DONE"
        evidence = result["metadata"]["verification"]
        assert evidence["tests_passed"] is None
        assert evidence["review_pending"] is True
        if oversized_input:
            assert evidence["mutation_scope_unknown"] is True
        elif os.scandir in os.supports_fd:
            assert evidence["mutation_scope_unknown"] is False
        answer = result["answer"]
        assert "本轮没有已采信的测试结果。" in answer
        assert "（来源：VerificationDomain）" in answer
        assert "交付评审尚未确认" in answer
        assert "OFFLINE_DONE" not in answer
        assert "/message" in answer
        detail = client.message_detail(session_id, 1)
        assert "模型说明（执行声明以本轮回执为准）" in detail
        assert "\n```\nOFFLINE_DONE\n```" in detail
        if evidence["mutation_scope_unknown"]:
            assert evidence["verification_needed"] is True
            assert "工作区范围尚未确认" in answer
            assert "没有已采信的验证执行" not in answer
        else:
            assert evidence["verification_needed"] is False
            assert "未评估：本次没有已采信的验证执行。" in answer
            assert "没有待完成的必需检查" in answer
            assert "工作区范围尚未确认" not in answer
        expected = [("user", "Reply with OFFLINE_DONE."), ("assistant", answer)]
        assert [(item["role"], item["content"]) for item in session.history] == expected
        assert [(item["role"], item["content"]) for item in client.messages(session_id)] == expected
        with scoped_workdir(tmp_path):
            persisted = load_session(session_id)
        assert persisted["run_status"] == "completed_unverified"
        assert [(item["role"], item["content"]) for item in persisted["messages"]] == expected
        snapshot = session.attach_snapshot()
        assert snapshot["settled"] is True
        assert snapshot["session"]["last_result"] == info["last_result"]
        events = session.event_bus.recent(100)
        event_types = [event.type for event in events]
        assert "session.run.failed" not in event_types
        completed = next(event for event in events if event.type == "session.run.completed")
        settled = next(event for event in events if event.type == "session.run.settled")
        assert completed.sequence < settled.sequence
        assert settled.properties == {"status": "completed_unverified", "persisted": True}
        assert completed.session_id == settled.session_id == session_id
        assert completed.run_id == settled.run_id == snapshot["active_interaction_run_id"]
        assert session.event_bus.publish("caller.after_run", {}).sequence > settled.sequence
    finally:
        service.shutdown()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_http_cancelled_requirement_stops_followup_at_completion_guidance_budget(monkeypatch, tmp_path):
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.state.sessions import session_runtime_state_path

    class Provider:
        name = "offline"

        def __init__(self):
            self.followup = False
            self.followup_calls = 0
            self.guidance_counts = []
            self.allowed_followup_calls = 3

        def create_completion(self, _client, **kwargs):
            if not self.followup:
                message = SimpleNamespace(content="", tool_calls=[SimpleNamespace(
                    id="cancelled-write", type="function", function=SimpleNamespace(
                        name="write_file",
                        arguments=json.dumps({"path": "never-created.txt", "content": "CANCEL_ME"}),
                    ),
                )])
                finish = "tool_calls"
            else:
                self.followup_calls += 1
                self.guidance_counts.append(sum(
                    "<stop-hook-guidance>" in str(item.get("content", ""))
                    for item in kwargs["messages"]
                ))
                # 旧实现继续循环时立即报错，离线红灯不能依赖大轮数兜底。
                assert self.followup_calls <= self.allowed_followup_calls, "completion gate requested a fourth natural answer"
                message = SimpleNamespace(content="AFTER_CANCEL_OK", tool_calls=[])
                finish = "stop"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason=finish)], usage=None,
            )

    provider = Provider()
    runtime = ResolvedModelRuntime(
        provider_id="offline", model_id="offline-model",
        request_model_id="offline-model", variant=None,
        provider=provider, client=object(), owns_client=False,
        capabilities=ModelCapabilities(
            provider="offline", model_id="offline-model", supports_streaming=False,
        ),
    )
    from dataclasses import replace
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _: replace(runtime))
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda _: replace(runtime))
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    service = SessionHTTPService(port=0, manager=manager, token="test-token-1234567890")
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        workspace_id = next(
            item["id"] for item in manager.list_workspaces() if item["path"] == str(tmp_path)
        )
        session_id = client.create_session("default", workspace_id=workspace_id)["id"]
        session = manager.get(session_id)
        client.run(
            session_id,
            "Create never-created.txt containing CANCEL_ME using write_file. Do not use bash or any other tool.",
            model="offline/offline-model",
        )
        pending = _wait_for_pending(lambda: client.pending_permissions(session_id))
        assert pending[0]["tool_input"]["path"] == "never-created.txt"
        assert client.abort(session_id) == {"aborted": True}
        assert session.wait(timeout=10)
        cancelled = client.attach_snapshot(session_id)
        assert cancelled["settled"] is True
        assert cancelled["session"]["status"] == "cancelled"
        assert client.pending_permissions(session_id) == []
        assert not (tmp_path / "never-created.txt").exists()
        with scoped_workdir(tmp_path):
            before = json.loads(session_runtime_state_path(session_id).read_text())
        assert before["completion_gate_prompts"] == 0

        provider.followup = True
        client.run(
            session_id, "Do not call any tool. Reply with exactly AFTER_CANCEL_OK.",
            model="offline/offline-model",
        )
        assert session.wait(timeout=10)
        assert provider.followup_calls == 3
        assert provider.guidance_counts == [0, 1, 2]
        snapshot = client.attach_snapshot(session_id)
        info = client.get_session(session_id)
        assert snapshot["settled"] is True
        assert info["status"] == info["last_result"]["status"] == "blocked"
        assert info["running"] is False
        assert info["last_error"] == ""
        assert info["pending_interaction_count"] == 0
        assert client.pending_permissions(session_id) == []
        assert client.pending_questions(session_id) == []
        summary = info["last_result"]["answer"]
        assert "completion feedback" in summary.lower() and "exhausted" in summary.lower()
        assert "R1" in summary
        assert "AFTER_CANCEL_OK" not in summary
        assert info["last_result"]["metadata"]["authored_content"] == "AFTER_CANCEL_OK"
        assert session.history[-1]["content"] == client.messages(session_id)[-1]["content"] == summary
        with scoped_workdir(tmp_path):
            persisted = load_session(session_id)
            state = json.loads(session_runtime_state_path(session_id).read_text())
        assert persisted["run_status"] == "blocked"
        assert persisted["messages"][-1]["content"] == summary
        assert state["completion_gate_prompts"] == 0
        assert sum('<stop-hook-guidance>' in str(m.get('content', ''))
                   for m in persisted['messages'] if m.get('_nz_synthetic')) == 2
        assert state["requested_paths"] == ["never-created.txt"]
        from nz_coder.runtime.verification.domain.checkpoint import restore_domain
        from nz_coder.runtime.verification.domain.models import RunScope, TaskScope, WorkspaceVersion
        checkpoint = state["verification_domain"]
        restored_domain = restore_domain(checkpoint, lock=threading.RLock(),
            scope=RunScope(TaskScope(**checkpoint["scope"]["task"]), "check-recovery", "test"),
            version=WorkspaceVersion(**checkpoint["version"]))
        assert [(item.requirement.id, item.status) for item in restored_domain.snapshot().requirements] == [("R1", "pending")]
        assert not (tmp_path / "never-created.txt").exists()
        settled = [event for event in session.event_bus.recent(100) if event.type == "session.run.settled"]
        assert [event.properties for event in settled] == [
            {"status": "cancelled", "persisted": True},
            {"status": "blocked", "persisted": True},
        ]
        assert settled[-1].run_id == snapshot["active_interaction_run_id"]
        assert snapshot["session"]["last_result"] == info["last_result"]
        assert persisted["messages"][-1]["_nz_end_state"]["reason"] == "errored"
        assert state["active"] is True

        from nz_coder.protocol.message_part_reducer import MessagePartReducer

        live = MessagePartReducer()
        for event in session.event_bus.recent(1000):
            if event.run_id == snapshot["active_interaction_run_id"]:
                live.apply_event(event.to_dict())
        last_message_id = persisted["messages"][-1]["_nz_message_id"]
        assert "".join(
            part.get("text", "") for part in live.parts(last_message_id)
            if part.get("type") == "text"
        ) == summary

        # 重建管理器只恢复已结清结果，不能自动开启新的模型运行。
        restored_manager = SessionManager(workspace_roots=[tmp_path])
        try:
            lazy = next(item for item in restored_manager.list()["sessions"] if item["id"] == session_id)
            assert lazy["status"] == "blocked"
            recovered = restored_manager.get(session_id).info()
            assert recovered["status"] == recovered["last_result"]["status"] == "blocked"
            assert recovered["last_result"]["answer"] == summary
            assert recovered["running"] is False
            assert provider.followup_calls == 3
        finally:
            restored_manager.close()

        # 显式继续开启新的补救额度，旧累计调用与未满足需求保持。
        provider.allowed_followup_calls = 6
        client.run(session_id, "go on", model="offline/offline-model")
        assert session.wait(timeout=10)
        assert provider.followup_calls == 6
        assert provider.guidance_counts == [0, 1, 2, 0, 1, 2]
        continued = client.get_session(session_id)
        assert continued["status"] == "blocked"
        assert continued["running"] is False
        with scoped_workdir(tmp_path):
            continued_state = json.loads(session_runtime_state_path(session_id).read_text())
        assert continued_state["completion_gate_prompts"] == 0
        assert provider.guidance_counts[-3:] == [0, 1, 2]
        assert continued_state["turn_count"] == 3
        assert continued_state["requested_paths"] == ["never-created.txt"]
        assert continued_state["verification_domain"]["definition"]["contract"] == state["verification_domain"]["definition"]["contract"]
    finally:
        service.shutdown()
        thread.join(timeout=2)
        assert not thread.is_alive()


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
            from nz_coder.foundation.workspace_trust import current_config_snapshot

            captured.update(kwargs)
            captured["control_fingerprint"] = current_config_snapshot(
                session.workspace
            ).control_fingerprint
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
        assert captured["control_fingerprint"] == session.config_snapshot.control_fingerprint

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
    control = session.memory_control()
    receipt = control.submit({"action": "create", "name": "style", "description": "Formatting",
        "type": "user", "content": "Use spaces"}, operation_id="propose")
    proposal_id = receipt["proposal_id"]
    status = client.memory_status(session.session_id)
    assert status["pending"][0]["id"] == proposal_id
    inspected = client.get_memory_proposal(session.session_id, proposal_id)
    assert inspected["context"]["session_id"] == session.session_id
    assert inspected["proposal_revision"] == 1
    reviewed = client.review_memory(session.session_id, proposal_id, "approve",
        expected_proposal_revision=1, operation_id="approve")
    assert reviewed["status"] == "saved"
    assert manager.read(reviewed["memory_id"])["content"] == "Use spaces"
    assert client.memory_status(session.session_id)["pending"] == []


def test_http_memory_records_versions_and_operation_receipts(local_service, tmp_path):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    session = local_service.manager.get(session_id)
    session.agent._mm = MemoryManager(tmp_path / "memory")
    command = {"action": "create", "name": "规则", "type": "project", "content": "使用中文注释"}
    created = client.mutate_memory(session_id, command, operation_id="create")
    assert created["status"] == "saved"
    memory_id = created["memory_id"]
    assert client.mutate_memory(session_id, command, operation_id="create") == created
    assert client.mutate_memory(session_id, {**command, "content": "另一内容"},
                                operation_id="create")["status"] == "operation_conflict"
    assert client.list_memories(session_id)["items"][0]["id"] == memory_id
    search_result = client.search_memories(session_id, "中文")
    assert search_result["items"][0]["id"] == memory_id
    assert "semantic_status" in search_result
    assert tuple(search_result["generation"]) == session.agent._mm.generation()
    assert client.read_memory(session_id, memory_id)["revision"] == 1
    updated = client.mutate_memory(session_id, {"action": "update", "memory_id": memory_id,
        "expected_revision": 1, "content": "只改相关文件"}, operation_id="update")
    assert updated["revision"] == 2
    stale = client.mutate_memory(session_id, {"action": "archive", "memory_id": memory_id,
        "expected_revision": 1}, operation_id="stale")
    assert stale["status"] == "conflict"
    assert client.read_memory(session_id, memory_id, revision=1)["content"] == command["content"]
    assert [item["revision"] for item in client.memory_history(session_id, memory_id)] == [1, 2]
    assert client.mutate_memory(session_id, {"action": "assess", "memory_id": memory_id,
        "expected_revision": 2, "judgment": {"coverage": "partial", "result": "inconclusive",
        "assessment": "只检查注释", "covered_claims": "注释语言"}}, operation_id="assess")["status"] == "saved"


def test_cli_http_and_model_memory_search_keep_semantics_and_session_settings(local_service, tmp_path, monkeypatch, capsys):
    from nz_coder.runtime.core.run_settings import RunSettings, current_run_settings, scoped_run_settings
    from nz_coder.state import memory as memory_module
    from nz_coder.state.memory_cli import memory_main
    from nz_coder.state.memory_retrieval import reindex_memory

    class Encoder:
        identity = "product-test-deterministic-v1"
        available = True

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setenv("NZ_MEMORY_RETRIEVAL_MODE", "hybrid")
    monkeypatch.setenv("NZ_MEMORY_EMBEDDING_MODEL", "/offline-product-fixture")
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    session = local_service.manager.get(session_id)
    manager = MemoryManager(tmp_path / "memory")
    session.agent._mm = manager
    encoder = Encoder()
    factory_settings = []

    def configured_encoder():
        settings = current_run_settings()
        factory_settings.append(settings)
        assert settings.snapshot is session.config_snapshot
        assert settings.memory_retrieval_mode == "hybrid"
        assert settings.memory_embedding_model == "/offline-product-fixture"
        return None if encoder.available is None else encoder

    monkeypatch.setattr(memory_module, "configured_embedding_provider", configured_encoder)
    receipt = client.mutate_memory(session_id, {"action": "create", "name": "Build guide",
        "description": "Local build", "content": "Compile the package using local toolchain"},
        operation_id="create-english")
    memory_id = receipt["memory_id"]
    assert reindex_memory(manager.store, encoder)["status"] == "ready"
    query = "编译流程"
    assert manager.store.search(query) == []
    settings = RunSettings.from_snapshot(session.config_snapshot)
    for available, expected_status in ((True, "ready"), (False, "unavailable"), (None, "unavailable")):
        encoder.available = available
        with scoped_run_settings(settings), memory_module.bind_memory_manager(manager):
            assert memory_main(["search", query], manager=manager) == 0
            cli_result = json.loads(capsys.readouterr().out)
            model_result = json.loads(memory_module._recall_memory(query=query))
        http_result = client.search_memories(session_id, query)
        for result in (cli_result, http_result, model_result):
            assert result["semantic_status"] == expected_status
            assert tuple(result["generation"]) == manager.generation()
            assert [item["id"] for item in result["items"]] == ([memory_id] if available else [])
            if available:
                assert result["items"][0]["retrieval_sources"] == ["semantic"]
    assert len(factory_settings) >= 6


def test_http_legacy_memory_reads_keep_explicit_migration_diagnostic(local_service, tmp_path):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    session = local_service.manager.get(session_id)
    memory_dir = tmp_path / "legacy-memory"
    memory_dir.mkdir()
    legacy = memory_dir / "note.md"
    original = b"---\nname: legacy\ntype: project\n---\nKeep this original.\n"
    legacy.write_bytes(original)
    session.agent._mm = MemoryManager(memory_dir)

    for path in ("", "/records", "/search?q=legacy", "/proposals/missing"):
        with pytest.raises(NZCoderHTTPError) as error:
            client._request("GET", f"/session/{session_id}/memory{path}")
        assert error.value.code == "memory_unavailable"
        assert "nz-coder memory migrate" in error.value.message
    assert legacy.read_bytes() == original
    assert not (memory_dir / "memory.sqlite3").exists()


def test_http_memory_rejects_unknown_identity_and_missing_versions(local_service, tmp_path):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    session = local_service.manager.get(session_id)
    session.agent._mm = MemoryManager(tmp_path / "memory")
    route = f"/session/{session_id}/memory/mutate"
    invalid = [
        {"command": {"action": "create", "name": "test", "content": "body"}, "operation_id": "x", "scope_id": "forged"},
        {"command": {"action": "create", "name": "test", "content": "body", "capability": "manage"}, "operation_id": "x"},
        {"command": {"action": "update", "memory_id": "x", "content": "body"}, "operation_id": "x"},
        {"command": {"action": "apply_proposal", "proposal_id": "x", "expected_proposal_revision": 1,
                     "expected_revision": True}, "operation_id": "x"},
        {"command": {"action": "create", "name": "test", "content": "body"}},
    ]
    for body in invalid:
        with pytest.raises(NZCoderHTTPError) as error:
            client._request("POST", route, body)
        assert error.value.status == 400
    with pytest.raises(NZCoderHTTPError) as error:
        client._request("POST", f"/session/{session_id}/memory/proposals/unknown/approve", {
            "expected_proposal_revision": 1, "operation_id": "approve",
            "related_records": [{"memory_id": "other", "expected_revision": 1, "scope_id": "forged"}],
        })
    assert error.value.status == 400
    assert session.agent._mm.list_records()["items"] == []


def test_http_native_memory_before_first_run_and_after_restore(tmp_path):
    session_id = None
    memory_id = None
    for restored in (False, True):
        with scoped_workdir(tmp_path):
            manager = SessionManager(workspace_roots=[tmp_path], restore_saved=restored)
            service = SessionHTTPService(port=0, token="test-token-1234567890", manager=manager)
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            try:
                client = NZCoderClient(service.base_url, service.token, timeout=2)
                if not restored:
                    workspace_id = next(item["id"] for item in client.list_workspaces() if item["path"] == str(tmp_path))
                    session_id = client.create_session("default", workspace_id=workspace_id)["id"]
                session = manager.get(session_id)
                assert session.agent is None
                if not restored:
                    created = client.mutate_memory(session_id, {"action": "create", "name": "无需模型",
                        "content": "第一次运行前保存"}, operation_id="native-create")
                    assert created["status"] == "saved"
                    memory_id = created["memory_id"]
                    assert client.mutate_memory(session_id, {"action": "update", "memory_id": memory_id,
                        "expected_revision": 1, "content": "已更新"}, operation_id="native-update")["revision"] == 2
                assert client.read_memory(session_id, memory_id)["content"] == "已更新"
                assert client.memory_history(session_id, memory_id)[0]["content"] == "第一次运行前保存"
                assert client.list_memories(session_id)["items"][0]["id"] == memory_id
                assert session.agent is None
            finally:
                service.shutdown()
                thread.join(timeout=2)


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


def test_project_command_catalog_rotates_on_next_submission(local_service):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    created = client.create_session("auto")
    session_id = created["id"]
    workspace = Path(created["workspace"])
    commands = workspace / ".nz-coder" / "commands"
    commands.mkdir(parents=True)
    (commands / "repo-review.md").write_text(
        "---\ndescription: Review a path\nallowed_tools:\n  - read_file\n"
        "---\nReview $ARGUMENTS",
        encoding="utf-8",
    )

    assert all(item["name"] != "repo-review" for item in client.list_commands(session_id))
    with pytest.raises(NZCoderHTTPError) as exc_info:
        client.expand_command(session_id, "repo-review", "src/app.py")
    assert exc_info.value.code == "invalid_request"

    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore().trust(
        workspace, "workspace-control", snapshot.control_fingerprint
    )
    listed = [
        item for item in client.list_commands(session_id)
        if item["name"] == "repo-review"
    ]
    expanded = client.expand_command(session_id, "repo-review", "src/app.py")

    assert listed == [{
        "name": "repo-review",
        "description": "Review a path",
        "source": "project",
        "allowed_tools": ["read_file"],
        "model": None,
    }]
    assert expanded == {
        "name": "repo-review",
        "prompt": "Review src/app.py",
        "source": "project",
        "allowed_tools": ["read_file"],
        "model": None,
        "command_digest": expanded["command_digest"],
    }
    assert len(expanded["command_digest"]) == 64

    (commands / "repo-review.md").write_text(
        "---\ndescription: Changed\nallowed_tools:\n  - bash\n"
        "---\nChanged $ARGUMENTS",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="command expansion is stale"):
        local_service.manager.start_run(
            session_id,
            expanded["prompt"],
            allowed_tools=expanded["allowed_tools"],
            model=expanded["model"],
            command_digest=expanded["command_digest"],
        )
    assert client.get_session(session_id)["status"] == "idle"
    assert local_service.manager.get(session_id).history == []

    with pytest.raises(NZCoderHTTPError) as stale:
        client.run(
            session_id,
            expanded["prompt"],
            allowed_tools=expanded["allowed_tools"],
            model=expanded["model"],
            command_digest=expanded["command_digest"],
        )
    assert stale.value.status == 400
    assert stale.value.code == "invalid_request"
    assert client.get_session(session_id)["status"] == "idle"
    assert local_service.manager.get(session_id).history == []


def test_http_projects_remote_extension_skill_and_mcp_status(local_service):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

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
    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore().trust(
        workspace, "workspace-control", snapshot.control_fingerprint
    )
    session_id = client.create_session("auto")["id"]

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
    _wait_for_status(client, session_id, "completed")
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
    local_service.manager.get(session_id)._active_event_publisher.publish(
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
        user_storage_layout(Path(created["workspace"])).workspace_state
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

    first_delta = threading.Event()
    release_stream = threading.Event()
    provider_exited = threading.Event()
    calls = 0

    def chunk(text, finish_reason=None):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=text,
                tool_calls=None,
                reasoning_content=None,
            ), finish_reason=finish_reason)],
        )

    class Completions:
        def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            call_number = calls

            def response():
                try:
                    if call_number == 1:
                        yield chunk("UNPUBLISHED_PARTIAL")
                        first_delta.set()
                        assert release_stream.wait(timeout=3)
                        yield chunk("UNPUBLISHED_LATE")
                    else:
                        yield chunk("fresh")
                        yield chunk("", finish_reason="stop")
                finally:
                    if call_number == 1:
                        provider_exited.set()

            return response()

    completions = Completions()

    from dataclasses import replace
    from tests.runtime.test_native_product_runtime import _install_runtime
    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            return completions.create(**kwargs)
    runtime = _install_runtime(monkeypatch, Provider())
    runtime = replace(runtime, capabilities=replace(runtime.capabilities, supports_streaming=True))
    for name in ('nz_coder.runtime.execution.native_sdk.resolve_model_runtime',
                 'nz_coder.runtime.execution.loop.resolve_model_runtime'):
        monkeypatch.setattr(name, lambda _: replace(runtime))
    monkeypatch.setattr(config, "PLANNING_ENABLED", False)
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
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

        client.run(session_id, "first", model="offline/offline-model")
        assert first_delta.wait(timeout=2), manager.get(session_id).info()
        active = client.attach_snapshot(session_id)
        assert active["settled"] is False
        assert "UNPUBLISHED_PARTIAL" not in json.dumps(active)
        assert client.abort(session_id) == {"aborted": True}
        assert client.abort(session_id) == {"aborted": False}

        with pytest.raises(NZCoderHTTPError) as still_busy:
            client.run(session_id, "must-not-overlap")
        assert still_busy.value.status == 409

        release_stream.set()
        cancelled = _wait_for_status(client, session_id, "cancelled")
        assert provider_exited.is_set()
        assert cancelled["last_result"]["answer"] == ""
        assert cancelled["last_result"]["metadata"]["authored_content"] == ""
        events = manager.get(session_id).event_bus.recent(64)
        event_types = [event.type for event in events]
        cancelled_index = event_types.index("session.run.cancelled")
        settled_index = event_types.index("session.run.settled")
        finish_index = next(index for index, event in enumerate(events)
                            if event.type == "message.part.updated"
                            and event.properties["part"].get("type") == "step-finish"
                            and event.properties["part"].get("reason") == "cancelled")
        assert finish_index < cancelled_index < settled_index
        assert not any(event.type.startswith("message.part.") for event in events[cancelled_index + 1:])
        # 模型已经交出第一片，公开事件与消息详情都不应出现取消前后的草稿。
        wire = json.dumps([event.to_dict() for event in events])
        detail = client.message_detail(session_id, 1)
        for marker in ("UNPUBLISHED_PARTIAL", "UNPUBLISHED_LATE"):
            assert marker not in wire
            assert marker not in detail
        cancelled_assistant = next(
            message for message in manager.get(session_id).history
            if message.get("role") == "assistant"
        )
        assert any(
            part.get("type") == "step-finish" and part.get("reason") == "cancelled"
            for part in cancelled_assistant.get("_nz_parts", [])
        )

        second = client.run(session_id, "second", model="offline/offline-model")
        run_id = second["active_interaction_run_id"]
        assert run_id and run_id != events[settled_index].run_id
        session = manager.get(session_id)
        assert session.wait(timeout=10), (
            session.status, [(event.run_id, event.type) for event in session.event_bus.recent(12)]
        )
        info = client.get_session(session_id)
        assert info["active_interaction_run_id"] == run_id
        assert info["status"] == "completed_unverified"
        assert info["running"] is False
        assert info["last_result"]["metadata"]["authored_content"] == "fresh"
        second_events = [
            event for event in session.event_bus.recent(64)
            if event.run_id == run_id and event.type.startswith("session.run.")
        ]
        assert [event.type for event in second_events] == [
            "session.run.started", "session.run.completed", "session.run.settled",
        ]
        assert second_events[-1].properties == {"status": "completed_unverified", "persisted": True}
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
            item for item in restored_manager.list()["sessions"] if item["id"] == session_id
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
    original_write = sessions_module._publish_session_json

    def fail_latest_alias(prepared, path):
        json.loads(prepared.read_text(encoding="utf-8"))
        if path.name == "latest.json":
            raise OSError("alias unavailable")
        return original_write(prepared, path)

    monkeypatch.setattr(sessions_module, "_publish_session_json", fail_latest_alias)
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
            item for item in restored_manager.list()["sessions"] if item["id"] == session_id
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


def test_http_late_reply_cannot_approve_identical_request_in_next_run(local_service):
    client = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
    session_id = client.create_session("default")["id"]
    client.run(session_id, "permission")
    first, = _wait_for_pending(lambda: client.pending_permissions(session_id))
    assert client.abort(session_id) == {"aborted": True}
    assert local_service.manager.get(session_id).wait(timeout=3)

    client.run(session_id, "permission")
    second, = _wait_for_pending(lambda: client.pending_permissions(session_id))
    assert first["tool_input"] == second["tool_input"]
    assert first["id"] != second["id"]
    with pytest.raises(NZCoderHTTPError) as late_reply:
        client.reply_permission(session_id, first["id"], "once")
    assert late_reply.value.code == "interaction_not_found"
    assert [item["id"] for item in client.pending_permissions(session_id)] == [second["id"]]
    assert client.reply_permission(session_id, second["id"], "once") is True
    _wait_for_status(client, session_id, "completed")
    assert client.messages(session_id)[-1]["content"] == "permission:once"


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
        "changed_files": ["src/app.py"],
        "verification_status": "passed",
    })
    subagent_module._save_subagent_state(parent["id"], state, workspace)
    from nz_coder.runtime.session.model import Session
    from nz_coder.runtime.session.store import LegacyJsonSessionStore
    LegacyJsonSessionStore().save_session(Session.create(state['session_id'],
        [{'role': 'assistant', 'content': 'done'}], workspace=workspace, parent_session_id=parent['id']))

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
        assert "[Subagent status: completed_unverified]" in result["value"]

        reattached = NZCoderClient(local_service.base_url, local_service.token, timeout=2)
        completed = reattached.get_child(parent["id"], child_id)
        assert completed["status"] == "completed_unverified"
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


def test_http_permission_timeout_reports_failure_and_releases_the_run():
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
        info = _wait_for_status(client, session_id, "failed")
        assert "timed out" in info["last_error"]
        assert not any(message.get("content") == "permission:reject" for message in client.messages(session_id))
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
    assert [message["content"] for message in client.messages(session_id)] == ["late-question"]
    assert not any(event.type == "question.asked" for event in agent.event_bus.recent())


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


def test_http_instruction_delete_keeps_symlink_target_unchanged(tmp_path):
    project = tmp_path / "instructions-project"
    outside = tmp_path / "outside.md"
    project.mkdir()
    outside.write_text("SENTINEL-INSTRUCTION\n", encoding="utf-8")
    (project / "AGENTS.md").symlink_to(outside)
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
        with pytest.raises(NZCoderHTTPError):
            client.delete_instruction_file("project", "AGENTS.md", workspace_id)
    finally:
        service.shutdown()
        thread.join(timeout=2)

    assert outside.read_text(encoding="utf-8") == "SENTINEL-INSTRUCTION\n"


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
    session_path = (
        user_storage_layout(workspace).workspace_state
        / "sessions"
        / f"{session_id}.json"
    )
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
            item for item in second_manager.list()["sessions"] if item["id"] == session_id
        )
        assert dormant["status"] == "dormant"
        assert second_manager.info(session_id)["restored"] is True
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
            item for item in manager.list()["sessions"] if item["id"] == "interrupted-session"
        )
        assert discovered["status"] == "interrupted"
        assert discovered["running"] is False
        assert "before the accepted run settled" in manager.info("interrupted-session")["last_error"]

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
    from nz_coder.protocol.public_error import PublicError, TrustedPublicMessage
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
        TrustedPublicMessage("provider_error", "provider unavailable"),
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
                "public_error": PublicError(
                    "provider_error",
                    "provider unavailable",
                ).to_dict(),
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
        restored_ids = {item["id"] for item in manager.list()["sessions"]}
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

    workspace = tmp_path / "project"
    workspace.mkdir()
    with scoped_workdir(workspace):
        save_session(
            [{"role": "user", "content": "too large"}],
            mode="default",
            session_id="oversized-session",
            activate=False,
        )
    from nz_coder.state import sessions as sessions_module
    monkeypatch.setattr(sessions_module, "_session_byte_limit", lambda: 1)

    manager = SessionManager(
        agent_factory=_fake_factory,
        workspace_roots=[workspace],
        restore_saved=True,
    )
    try:
        result = manager.list()
        assert "oversized-session" not in {item["id"] for item in result["sessions"]}
        assert result["unavailable"][0]["code"] == "resource_limit"
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
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace, "workspace-control", snapshot.control_fingerprint
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
    # Session construction does not freeze project skill descriptions. They
    # are appended from the fresh per-run snapshot by AgentLoop.
    assert captured["skill_descriptions"] == ""
    private_state = user_storage_layout(workspace).workspace_state
    assert captured["memory_manager"].memory_dir == private_state / "memory"
    assert captured["skill_loader"]._project_dir == workspace / ".nz-coder" / "skills"
    assert captured["system_prompt"] == "workspace prompt"
    assert not captured["kwargs"].get("auto_mode_classifier_enabled", False)
    assert captured["kwargs"]["event_bus"]._journal.path == (
        private_state
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


def test_http_close_process_phase_uses_shared_absolute_deadline(tmp_path, monkeypatch):
    from nz_coder.http_service.manager import SessionBusyError
    from nz_coder.runtime.process import process_service as processes
    from tests.fixtures.process_lifecycle import ControlledBackend

    backends = [ControlledBackend(), ControlledBackend(), ControlledBackend()]
    iterator = iter(backends)
    monkeypatch.setattr(
        processes, "create_process_backend", lambda *a, **kw: next(iterator)
    )
    manager = SessionManager(
        agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False
    )
    sessions = [manager.get(manager.create()["id"]) for _ in backends]
    services = []
    records = []
    for session in sessions:
        service = workspace_process_service(session.workspace)
        service._kill_grace_override = 0
        handle = service.start(
            "owned", cwd=session.workspace, owner_session_id=session.session_id
        )
        services.append(service)
        records.append(service._records[handle.process_id])
    deadlines = []
    original = processes.ProcessService.kill_session

    def kill_session(service, session_id, *, deadline=None):
        deadlines.append(deadline)
        return original(service, session_id, deadline=deadline)

    monkeypatch.setattr(processes.ProcessService, "kill_session", kill_session)
    before = time.monotonic()
    try:
        with pytest.raises(SessionBusyError):
            manager.close(timeout=0.04)
        assert len(deadlines) == 3
        assert all(
            deadline == deadlines[0] and deadline is not None for deadline in deadlines
        )
        assert time.monotonic() - before < 0.4
        assert all(record.requested_terminal is not None for record in records)
        for session in sessions:
            assert manager.get(session.session_id) is session
            assert "processes" not in session._dispose_completed
            assert not session._disposed
    finally:
        for backend in backends:
            backend.recover()
        manager.close(timeout=2)
        for service in set(services):
            processes.close_workspace_process_service(service.workspace)


@pytest.mark.parametrize("failure", ["reader", "close"])
def test_http_process_dispose_retries_after_reader_or_close_recovers(
    tmp_path, monkeypatch, failure
):
    from nz_coder.http_service.manager import SessionBusyError
    from nz_coder.runtime.process import process_service as processes
    from tests.fixtures.process_lifecycle import ControlledBackend

    backend = ControlledBackend()
    if failure == "close":
        backend.eof.set()
        backend.close_fault.set()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    manager = SessionManager(
        agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False
    )
    session = manager.get(manager.create()["id"])
    service = workspace_process_service(session.workspace)
    service._kill_grace_override = 0
    handle = service.start(
        "owned", cwd=session.workspace, owner_session_id=session.session_id
    )
    record = service._records[handle.process_id]
    backend.body_exit.set()
    try:
        with pytest.raises(SessionBusyError):
            manager.close(timeout=0.02)
        assert not session._disposed
        assert manager.get(session.session_id) is session
        assert not record.cleanup_done.is_set()
        assert "processes" not in session._dispose_completed
        backend.recover()
        manager.close(timeout=2)
        assert session._disposed
        assert manager.list()["sessions"] == []
    finally:
        backend.recover()
        manager.close(timeout=2)
        processes.close_workspace_process_service(session.workspace)


def test_discovery_isolates_bad_utf8_and_malformed_parts(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with scoped_workdir(workspace):
        good = save_session([{"role": "user", "content": "safe"}], mode="default",
                            session_id="good-session", activate=False)
    payload = json.loads(good.read_text(encoding="utf-8"))
    payload["session_id"] = "bad-parts"
    payload["run_status"] = "running"
    payload["messages"] = [{"role": "assistant", "content": "", "_nz_parts": None}]
    (good.parent / "bad-parts.json").write_text(json.dumps(payload), encoding="utf-8")
    (good.parent / "bad-utf8.json").write_bytes(b"\xff")
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[workspace], restore_saved=True)
    try:
        ids = {item["id"] for item in manager.list()["sessions"]}
        assert "good-session" in ids
        assert "bad-parts" not in ids
        assert "bad-utf8" not in ids
    finally:
        manager.close()


def test_discovery_bounded_read_and_deep_file_isolation(tmp_path, monkeypatch):
    import nz_coder.state.sessions as sessions
    workspace = tmp_path / "project"
    workspace.mkdir()
    with scoped_workdir(workspace):
        good = save_session([{"role": "user", "content": "safe"}], mode="default",
                            session_id="good-deep", activate=False)
    (good.parent / "deep-invalid.json").write_text('{"x":' * 1200 + '0' + '}' * 1200, encoding="utf-8")
    oversized = good.parent / "oversized.json"
    oversized.write_bytes(b"x" * 101)
    reads = []
    original_read = sessions.os.read
    def counted_read(fd, count):
        data = original_read(fd, count)
        reads.append((count, len(data)))
        return data
    monkeypatch.setattr(sessions.os, "read", counted_read)
    with pytest.raises(ValueError):
        sessions._read_session_payload(oversized, maximum_bytes=10)
    assert reads == [(11, 11)]
    monkeypatch.setattr(sessions.os, "read", original_read)
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[workspace], restore_saved=True)
    try:
        ids = {item["id"] for item in manager.list()["sessions"]}
        assert "good-deep" in ids
        assert "deep-invalid" not in ids
    finally:
        manager.close()


def test_discovery_rejects_invalid_part_shapes_with_diagnostic(tmp_path, caplog):
    workspace = tmp_path / "project"
    workspace.mkdir()
    with scoped_workdir(workspace):
        good = save_session([{"role": "user", "content": "safe"}], mode="default", session_id="good-parts", activate=False)
    payload = json.loads(good.read_text(encoding="utf-8"))
    bad_parts = [
        [{"id": "part-bad", "message_id": "msg-bad", "type": "tool", "state": {"status": []}}],
        1,
        [1],
        [{"id": "part-bad", "message_id": "msg-other", "type": "text", "text": "x"}],
    ]
    originals = {}
    for index, parts in enumerate(bad_parts):
        name = f"bad-shape-{index}"
        item = dict(payload, session_id=name, run_status="running", messages=[{"role": "assistant", "content": "", "_nz_message_id": "msg-bad", "_nz_parts": parts}])
        path = good.parent / (name + ".json")
        path.write_text(json.dumps(item), encoding="utf-8")
        originals[path] = path.read_bytes()
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[workspace], restore_saved=True)
    try:
        ids = {item["id"] for item in manager.list()["sessions"]}
        assert "good-parts" in ids
        assert not any(f"bad-shape-{index}" in ids for index in range(len(bad_parts)))
        for path, original in originals.items():
            assert path.read_bytes() == original
            assert path.stem in caplog.text
    finally:
        manager.close()


def _fork_history_with_snapshots(workspace, session_id):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    from nz_coder.runtime.session.session_processor import SessionProcessor
    from nz_coder.state.sessions import session_snapshot_dir

    with scoped_workdir(workspace):
        store = WorkspaceSnapshotStore(workspace, session_snapshot_dir(session_id))
    app = workspace / "app.py"
    app.write_text("initial\n", encoding="utf-8")
    history, snapshots = [], []
    for index in range(1, 4):
        user = {"role": "user", "content": f"change {index}"}
        assistant = {"role": "assistant", "content": f"done {index}"}
        attach_message_identity(user, f"msg-user-{index}", session_id=session_id)
        attach_message_identity(assistant, f"msg-assistant-{index}", session_id=session_id)
        history.extend([user, assistant])
        processor = SessionProcessor(assistant)
        start = store.track()
        processor.start_step(start)
        app.write_text(f"version {index}\n", encoding="utf-8")
        finish = store.track()
        processor.add_patch(start, ["app.py"])
        processor.finish_step("stop", snapshot=finish)
        loop = AgentLoop.__new__(AgentLoop)
        loop.workspace_snapshots = store
        loop._refresh_snapshot_summaries(history, assistant["_nz_message_id"], finish)
        snapshots.append((start, finish))
    return store, app, history, snapshots


def test_forked_session_can_restore_inherited_snapshot_after_parent_removal(tmp_path):
    from nz_coder.protocol.message_schema import SUMMARY_KEY, SESSION_SUMMARY_KEY
    from nz_coder.runtime.process.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore
    from nz_coder.state.sessions import session_snapshot_dir

    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False)
    try:
        workspace_id = manager.workspaces.id_for(tmp_path)
        parent_id = manager.create(workspace_id=workspace_id)["id"]
        parent = manager.get(parent_id)
        assert parent.workspace == tmp_path
        parent_store, app, history, snapshots = _fork_history_with_snapshots(tmp_path, parent_id)
        parent.history[:] = history
        expected = [parent_store.diff_full(start, finish) for start, finish in snapshots]
        children = [manager.fork(parent_id, turn)["id"] for turn in (1, 2, 3)]
        assert manager.remove(parent_id)
        for turn, child_id in enumerate(children, 1):
            child = manager.get(child_id)
            with scoped_workdir(tmp_path):
                store = WorkspaceSnapshotStore(tmp_path, session_snapshot_dir(child_id))
            for index, (start, finish) in enumerate(snapshots[:turn]):
                assert store.diff_full(start, finish) == expected[index]
                assert child.history[2 * index][SUMMARY_KEY] == history[2 * index][SUMMARY_KEY]
                assert child.history[2 * index + 1][SESSION_SUMMARY_KEY] == history[2 * index + 1][SESSION_SUMMARY_KEY]
            if turn < 3:
                with pytest.raises(SnapshotError):
                    store._load(snapshots[turn][1])
        last = manager.get(children[-1])
        last.undo()
        assert app.read_text(encoding="utf-8") == "version 2\n"
        last.redo()
        assert app.read_text(encoding="utf-8") == "version 3\n"
        from nz_coder.runtime.execution.loop import AgentLoop
        from nz_coder.protocol.message_schema import attach_message_identity
        from nz_coder.runtime.session.session_processor import SessionProcessor
        loop = AgentLoop.__new__(AgentLoop)
        with scoped_workdir(tmp_path):
            loop.workspace_snapshots = WorkspaceSnapshotStore(tmp_path, session_snapshot_dir(children[-1]))
        loop.tracer = SimpleNamespace(log=lambda *args, **kwargs: None)
        user = {"role": "user", "content": "next step"}
        assistant = {"role": "assistant", "content": "done"}
        attach_message_identity(user, "msg-next-user", session_id=children[-1])
        attach_message_identity(assistant, "msg-next-assistant", session_id=children[-1])
        last.history.extend([user, assistant])
        processor = SessionProcessor(assistant)
        processor.start_step(snapshots[-1][1])
        app.write_text("version 4\n", encoding="utf-8")
        finish = loop.workspace_snapshots.track()
        processor.finish_step("stop", snapshot=finish)
        loop._record_step_patch(last.history, processor, finish)
        assert "version 4" in assistant[SESSION_SUMMARY_KEY]["diffs"][0]["patch"]
        assert user[SUMMARY_KEY]["diffs"][0]["file"] == "app.py"
    finally:
        manager.close()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_fork_snapshot_copy_failure_does_not_publish_child(tmp_path, monkeypatch, damage):
    from nz_coder.http_service import manager as manager_module
    from nz_coder.runtime.process.workspace_snapshot import SnapshotError
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False)
    try:
        workspace_id = manager.workspaces.id_for(tmp_path)
        parent_id = manager.create(workspace_id=workspace_id)["id"]
        parent = manager.get(parent_id)
        store, app, history, snapshots = _fork_history_with_snapshots(tmp_path, parent_id)
        parent.history[:] = history
        manifest = store._load(snapshots[0][0])
        digest = next(iter(manifest["files"].values()))["blob"]
        blob = store.root / "blobs" / digest[:2] / digest[2:]
        if damage == "missing":
            blob.unlink()
        else:
            blob.write_bytes(b"corrupt")
        monkeypatch.setattr(manager_module, "create_session_id", lambda prefix: "fork-copy-failure")
        original_history = copy.deepcopy(parent.history)
        with pytest.raises(SnapshotError):
            manager.fork(parent_id, 1)
        assert {info["id"] for info in manager.list()["sessions"]} == {parent_id}
        with scoped_workdir(tmp_path):
            assert load_session("fork-copy-failure") == {}
        assert parent.history == original_history
        assert app.read_text(encoding="utf-8") == "version 3\n"
    finally:
        manager.close()


def test_fork_copies_patch_only_snapshot_reference(tmp_path):
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    from nz_coder.state.sessions import session_snapshot_dir
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False)
    try:
        parent_id = manager.create(workspace_id=manager.workspaces.id_for(tmp_path))["id"]
        parent = manager.get(parent_id)
        store, _, history, snapshots = _fork_history_with_snapshots(tmp_path, parent_id)
        assistant = history[1]
        assistant["_nz_parts"] = [part for part in assistant["_nz_parts"] if part.get("type") == "patch"]
        parent.history[:] = history
        child_id = manager.fork(parent_id, 1)["id"]
        assert manager.remove(parent_id)
        with scoped_workdir(tmp_path):
            child = WorkspaceSnapshotStore(tmp_path, session_snapshot_dir(child_id))
        assert child._load(snapshots[0][0])["snapshot"] == snapshots[0][0]
    finally:
        manager.close()
