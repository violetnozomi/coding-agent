"""本地 CLI 收尾必须保留准确终态及运行时已经提交的历史。"""
from __future__ import annotations

import asyncio
import copy
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

from nz_coder.interface import cli
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from tests.test_cli_commands import FakeAgent
from tests.test_http_service import _fake_factory


SESSION_ID = "local-terminal-state"


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(tmp_path.parent / f"{tmp_path.name}-user.env"))
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path.parent / f"{tmp_path.name}-trust.json"))
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, width=100))
    monkeypatch.setattr(cli, "create_session_id", lambda: SESSION_ID)
    monkeypatch.setattr(cli, "record_recent_model", lambda *_: None)
    monkeypatch.setattr(cli, "current_memory_manager", lambda: SimpleNamespace(load_all=lambda: None))
    monkeypatch.setattr(cli, "build", lambda **_: "Offline terminal state test.")
    original_input = cli.TerminalInput
    inputs = []

    def make_input(**kwargs):
        value = original_input(**kwargs, interactive=False)
        inputs.append(value)
        return value

    monkeypatch.setattr(cli, "TerminalInput", make_input)

    def install(agent, read):
        monkeypatch.setattr(cli, "_build_agent", lambda *_, **__: agent)
        monkeypatch.setattr(cli, "read_user_query", read)

    return install, inputs, output


def _exact_path():
    return sessions.session_dir() / f"{SESSION_ID}.json"


def _read_exact():
    return json.loads(_exact_path().read_text(encoding="utf-8"))


def _assert_discovered(workspace, status, message_count):
    from nz_coder.http_service import SessionManager

    manager = SessionManager(
        agent_factory=_fake_factory, workspace_roots=[workspace], restore_saved=True,
    )
    try:
        info = next(item for item in manager.list()["sessions"] if item["id"] == SESSION_ID)
        assert info["status"] == status
        assert info["runtime_status"] == status
        assert info["message_count"] == message_count
        if status != "dormant":
            assert manager.get(SESSION_ID).info()["status"] == status
    finally:
        manager.close()


@pytest.mark.parametrize("status", ["completed", "error", "max_turns", "cancelled", "interrupted"])
def test_cli_keeps_runtime_commit_and_projects_reminder_only_in_memory(tmp_path, monkeypatch, terminal, status):
    install, _, _ = terminal
    agent = FakeAgent(session_id=SESSION_ID)
    agent.tool_calls_this_run = 3
    agent.used_save_memory = False
    from nz_coder.protocol.message_schema import ensure_message_identities

    body = {"role": "assistant", "content": "DURABLE_ASSISTANT"}
    ensure_message_identities([body], SESSION_ID)

    live_history = []
    committed = []
    async def run(history, **_):
        live_history.append(history)
        history.append(copy.deepcopy(body))
        sessions.save_session(history, session_id=SESSION_ID, run_status=status)
        committed.append(_exact_path().read_bytes())
        return {"status": status}

    agent.run = run
    queries = iter(("Perform this task.", "exit"))
    install(agent, lambda: next(queries))
    with scoped_workdir(tmp_path):
        asyncio.run(cli._run_cli())
        saved = _read_exact()
        assert saved["run_status"] == status
        assert saved["messages"][1] == body
        assert _exact_path().read_bytes() == committed[0]
        assert not any(item.get("_nz_synthetic") for item in saved["messages"])
        reminders = [item for item in live_history[0] if item.get("_nz_synthetic")]
        assert bool(reminders) == (status not in {"cancelled", "interrupted"})
        if reminders:
            assert "substantial task" in reminders[0]["content"]
        _assert_discovered(tmp_path, status, len(saved["messages"]))


@pytest.mark.parametrize("second_result", [None, {}, {"status": "unknown-terminal"}])
def test_unknown_result_cannot_inherit_previous_success(tmp_path, monkeypatch, terminal, second_result):
    install, _, _ = terminal
    agent = FakeAgent(session_id=SESSION_ID)
    calls = 0
    observed = []

    async def run(history, **_):
        nonlocal calls
        calls += 1
        history.append({"role": "assistant", "content": f"answer-{calls}"})
        if calls == 1:
            sessions.save_session(history, session_id=SESSION_ID, run_status="completed")
            return {"status": "completed"}
        # 与真实 Runner 一致，每轮由生产者提交；CLI 无权代存旧历史。
        status = second_result.get("status") if isinstance(second_result, dict) else None
        sessions.save_session(history, session_id=SESSION_ID, run_status=status)
        return second_result

    def read():
        if calls:
            observed.append(_read_exact())
        return ("First request.", "Another request.", "exit")[calls]

    agent.run = run
    install(agent, read)
    with scoped_workdir(tmp_path):
        asyncio.run(cli._run_cli())
        assert observed[0]["run_status"] == "completed"
        saved = _read_exact()
        if second_result and second_result.get("status"):
            assert saved["run_status"] == "unknown-terminal"
        else:
            assert "run_status" not in saved
        assert [item["content"] for item in saved["messages"]] == [
            "First request.", "answer-1", "Another request.", "answer-2",
        ]
        _assert_discovered(tmp_path, "dormant", 4)


@pytest.mark.parametrize("saved_state", ["missing", "corrupt", "read_error", "wrong_id", "bad_messages", "missing_workspace"])
def test_preopen_failure_never_rewrites_unavailable_canonical_history(
    tmp_path, monkeypatch, terminal, saved_state,
):
    install, inputs, output = terminal
    agent = FakeAgent(session_id=SESSION_ID)
    agent.tool_calls_this_run = 4
    agent.used_save_memory = False
    transcripts = []

    async def run(*_, **__):
        raise RuntimeError("pre-open failure")

    def read():
        if transcripts:
            return "exit"
        if inputs and getattr(agent, "attempted", False):
            transcripts.append(inputs[0].transcript_provider().markdown())
            return "exit"
        agent.attempted = True
        return "Rejected before opening the run."

    agent.run = run
    install(agent, read)
    with scoped_workdir(tmp_path):
        path = _exact_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        before = None
        if saved_state == "corrupt":
            before = b"{invalid-json"
        elif saved_state in {"read_error", "wrong_id", "bad_messages", "missing_workspace"}:
            payload = {
                "session_id": "impostor" if saved_state == "wrong_id" else SESSION_ID,
                "workspace": str(tmp_path),
                "messages": "invalid" if saved_state == "bad_messages" else [
                    {"role": "assistant", "content": "UNTRUSTED_RESTORE"},
                ],
                "run_status": "completed",
            }
            if saved_state == "missing_workspace":
                payload.pop("workspace")
            before = json.dumps(payload).encode()
        if before is not None:
            path.write_bytes(before)
        if saved_state == "read_error":
            import os
            original_open = os.open

            def unavailable(candidate, *args, **kwargs):
                if Path(candidate) == path:
                    raise PermissionError("session read unavailable")
                return original_open(candidate, *args, **kwargs)

            monkeypatch.setattr(os, "open", unavailable)
        if saved_state in {"corrupt", "read_error", "wrong_id", "missing_workspace"}:
            with pytest.raises(sessions.SessionReadError) as error:
                asyncio.run(cli._run_cli())
            assert error.value.kind == ("unreadable" if saved_state == "read_error" else "corrupt")
            assert path.read_bytes() == before
            assert transcripts == []
        else:
            asyncio.run(cli._run_cli())
            assert path.read_bytes() == before if before is not None else not path.exists()
            assert "substantial task" not in transcripts[0]
            assert "UNTRUSTED_RESTORE" not in transcripts[0]
            assert "Run failed" in output.getvalue()
            assert agent.closed


def test_preopen_failure_restores_exact_previous_commit_without_claiming_new_success(
    tmp_path, monkeypatch, terminal,
):
    install, inputs, _ = terminal
    agent = FakeAgent(session_id=SESSION_ID)
    agent.tool_calls_this_run = 4
    agent.used_save_memory = False
    transcripts = []
    reads = 0

    async def run(*_, **__):
        raise RuntimeError("request was never accepted")

    def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return "Rejected current request."
        transcripts.append(inputs[0].transcript_provider().markdown())
        return "exit"

    agent.run = run
    install(agent, read)
    with scoped_workdir(tmp_path):
        sessions.save_session([
            {"role": "user", "content": "Previous accepted request."},
            {"role": "assistant", "content": "PREVIOUS_COMMITTED_BODY"},
        ], session_id=SESSION_ID, run_status="completed")
        before = _exact_path().read_bytes()
        sessions.save_session([
            {"role": "assistant", "content": "WRONG_LATEST_ALIAS"},
        ], session_id="other-session", run_status="error")
        asyncio.run(cli._run_cli())
        assert _exact_path().read_bytes() == before
        assert "PREVIOUS_COMMITTED_BODY" in transcripts[0]
        assert "Rejected current request." not in transcripts[0]
        assert "WRONG_LATEST_ALIAS" not in transcripts[0]
        assert "substantial task" not in transcripts[0]


@pytest.mark.parametrize("boundary", ["checkpoint", "terminal_save"])
def test_real_cancel_preserves_committed_parts_and_next_cli_turn(
    tmp_path, monkeypatch, terminal, boundary,
):
    from nz_coder.providers.normalized import chunk
    from nz_coder.runtime.execution.loop import AgentLoop
    from tests.test_loop_fake import (
        FakeDelta, FakeDeltaToolCall, FakeStreamChunk, FakeStreamingClient,
    )

    install, inputs, _ = terminal
    marker, next_marker = "PARTIAL_BEFORE_CANCEL", "NEXT_TURN_OK"
    first_response = [FakeStreamChunk(FakeDelta(content=marker))]
    if boundary == "checkpoint":
        first_response.append(FakeStreamChunk(FakeDelta(tool_calls=[FakeDeltaToolCall(
            0, name="list_directory", arguments='{"path":".","depth":1}',
        )])))
    first_response.append(chunk(finish_reason="tool_calls" if boundary == "checkpoint" else "stop"))
    client = FakeStreamingClient([
        first_response, [FakeStreamChunk(FakeDelta(content=next_marker)), chunk(finish_reason="stop")],
    ])
    observed = {}
    reads = 0
    entered, release = threading.Event(), threading.Event()
    # 本场景关闭交付评审。终态冻结后到达的取消必须保留已选定的结果。
    final_status = "completed_unverified"
    first_status = "cancelled" if boundary == "checkpoint" else final_status

    def read():
        nonlocal reads
        reads += 1
        if reads == 1:
            return "Reply briefly without using tools."
        if reads == 2:
            observed["first"] = _read_exact()
            observed["transcript"] = inputs[0].transcript_provider().markdown()
            return "Give another brief response."
        return "exit"

    async def scenario(environment):
        runtime = environment.runtime_services.session_runtime
        publish = sessions._publish_session_json

        def observed_publish(prepared, path):
            if path.name != f"{SESSION_ID}.json":
                return publish(prepared, path)
            status = json.loads(prepared.read_text(encoding="utf-8"))["run_status"]
            if boundary == "terminal_save" and status == final_status and not entered.is_set():
                entered.set()
                assert release.wait(5), "terminal writer not released"
            publish(prepared, path)
            if status == first_status and "runtime_commit" not in observed:
                observed["runtime_commit"] = json.loads(path.read_text(encoding="utf-8"))

        monkeypatch.setattr(sessions, "_publish_session_json", observed_publish)
        if boundary == "checkpoint":
            original_checkpoint = runtime.checkpoint
            checkpoint_release = asyncio.Event()

            async def paused_checkpoint(context, status="running"):
                await original_checkpoint(context, status)
                has_settled_tool = any(
                    part.get("type") == "tool" and part.get("state", {}).get("status") == "completed"
                    for message in context.transcript
                    for part in message.get("_nz_parts", [])
                )
                if not entered.is_set() and has_settled_tool and marker in json.dumps(context.transcript):
                    entered.set()
                    await checkpoint_release.wait()

            monkeypatch.setattr(runtime, "checkpoint", paused_checkpoint)
        task = asyncio.create_task(cli._run_cli())
        try:
            assert await asyncio.to_thread(entered.wait, 5), "expected real save boundary not reached"
            assert marker in json.dumps(_read_exact()["messages"])
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(task, 10)
        finally:
            release.set()
            if boundary == "checkpoint":
                checkpoint_release.set()
            if not task.done():
                task.cancel()
                try:
                    await asyncio.wait_for(task, 5)
                except asyncio.CancelledError:
                    pass

    with scoped_workdir(tmp_path):
        environment = AgentLoop(
            "Offline cancellation test.", session_id=SESSION_ID, permission_mode="auto",
            client=client, trace_enabled=False, sidecar_verifier=False,
        )
        install(environment, read)
        asyncio.run(scenario(environment))
        first = observed["first"]
        assert first["run_status"] == first_status
        assert first["messages"] == observed["runtime_commit"]["messages"]
        assert marker in json.dumps(first["messages"])
        assert sum(len(item.get("_nz_parts", [])) for item in first["messages"]) >= 4
        if boundary == "checkpoint":
            assert any(
                part.get("type") == "tool" and part.get("state", {}).get("status") == "completed"
                for message in first["messages"]
                for part in message.get("_nz_parts", [])
            )
            assert "list_directory" in observed["transcript"]
            assert "./ (empty)" in observed["transcript"]
        else:
            assert marker in observed["transcript"]
        assert not any(item.get("_nz_synthetic") for item in first["messages"])
        saved = _read_exact()
        assert saved["run_status"] == final_status
        assert len(saved["messages"]) > len(first["messages"])
        for current, previous in zip(saved["messages"], first["messages"]):
            assert current["_nz_message_id"] == previous["_nz_message_id"]
            assert current["role"] == previous["role"]
            assert current["content"] == previous["content"]
            assert [
                (part["id"], part["type"], part.get("text"), part.get("state", {}).get("status"))
                for part in current.get("_nz_parts", [])
            ] == [
                (part["id"], part["type"], part.get("text"), part.get("state", {}).get("status"))
                for part in previous.get("_nz_parts", [])
            ]
        assert saved["metadata"]["authored_content"] == next_marker
        assert client.chat.completions.calls == 2
        _assert_discovered(tmp_path, final_status, len(saved["messages"]))


def test_runner_commit_failure_is_reported_by_cli_and_closes_owner(tmp_path, monkeypatch, terminal):
    install, _, output = terminal
    agent = FakeAgent(session_id=SESSION_ID)

    async def run(history, **_):
        history.append({"role": "assistant", "content": "runtime finished"})
        sessions.save_session(history, session_id=SESSION_ID, run_status="completed")
        return {"status": "completed"}

    agent.run = run
    queries = iter(("Respond briefly.", "exit"))
    install(agent, lambda: next(queries))
    original_write = sessions._publish_session_json

    def failed_commit(prepared, path):
        json.loads(prepared.read_text(encoding="utf-8"))
        if path == _exact_path():
            raise OSError("terminal commit unavailable")
        return original_write(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", failed_commit)
    with scoped_workdir(tmp_path):
        asyncio.run(cli._run_cli())
        from nz_coder.protocol.public_error import to_public_error
        assert to_public_error(OSError("terminal commit unavailable")).message in output.getvalue()
        assert not _exact_path().exists()
        assert agent.closed
