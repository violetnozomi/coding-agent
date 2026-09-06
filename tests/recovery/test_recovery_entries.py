"""CLI, SDK and HTTP entries share owned-file recovery and safe refusals."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

import pytest

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.process.checkpoint_runtime import checkpoint_execution
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.runtime.session.session_revert import RecoveryError, SessionReverter
from nz_coder.state.sessions import load_session, save_session
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir


def _owned(root, session_id="entry-session"):
    messages = [
        {"role": "user", "content": "edit app", "_nz_message_id": "msg-user", "_nz_session_id": session_id},
        {"role": "assistant", "content": "edited", "_nz_message_id": "msg-step", "_nz_session_id": session_id},
    ]
    target = root / "app.py"
    target.write_bytes(b"before\r\n")
    ledger = ToolLedger(root)
    execution = ledger.register(
        session_id=session_id, interaction_id="interaction-entry", assistant_step_id="msg-step",
        agent_id="agent-entry", call_id="call-entry", tool="write_file", tool_input={"path": "app.py"},
    )
    with checkpoint_execution(ledger, execution["execution_id"]):
        WorkspaceFileAccess(root).write_bytes("app.py", b"after\r\n")
    with scoped_workdir(root):
        save_session(messages, session_id=session_id, activate=False)
    reverter = SessionReverter(
        WorkspaceSnapshotStore(root, root / ".nz-coder" / "snapshots"),
        root / ".nz-coder" / "optional-hint.json", session_id=session_id,
    )
    return messages, reverter, target


def _cli_context(messages, reverter):
    output = []
    agent = SimpleNamespace(
        session_reverter=reverter, revert_message=reverter.revert,
        unrevert_message=reverter.unrevert, change_tracker=None,
        permissions=SimpleNamespace(mode="auto"),
    )
    return SimpleNamespace(
        agent=agent, history=messages, controller=None, session_id="entry-session",
        console=SimpleNamespace(print=output.append), output=output,
    )


@pytest.mark.parametrize("use_controller", [False, True])
def test_cli_owned_undo_redo_has_one_history_owner_and_no_hint_dependency(tmp_path, monkeypatch, use_controller):
    from nz_coder.interface.commands.handlers import core

    messages, reverter, target = _owned(tmp_path)
    ctx = _cli_context(messages, reverter)
    if use_controller:
        ctx.controller = SimpleNamespace(undo=reverter.revert, redo=reverter.unrevert)
        ctx.agent.session_reverter = None
    monkeypatch.setattr(core, "save_session", lambda *_args, **_kwargs: pytest.fail("entry must not save history twice"))
    core.handle_undo(ctx)
    assert target.read_bytes() == b"before\r\n" and messages == []
    reverter.state_path.unlink(missing_ok=True)
    core.handle_redo(ctx)
    assert target.read_bytes() == b"after\r\n" and len(messages) == 2
    with scoped_workdir(tmp_path):
        assert len(load_session("entry-session")["messages"]) == 2


@pytest.mark.parametrize("call_id", ["call-shell", "call-shell[bold]\x1b[2J\nspoof"])
def test_cli_real_mixed_journal_reports_unrestored_scope_and_never_reexecutes(tmp_path, call_id):
    import io
    from rich.console import Console
    from nz_coder.interface.commands.handlers import core
    from nz_coder.tools import bash  # noqa: F401

    messages, reverter, target = _owned(tmp_path)
    ledger = ToolLedger(tmp_path)
    shell = ledger.register(session_id="entry-session", interaction_id="interaction-entry", assistant_step_id="msg-step",
                            agent_id="agent-entry", call_id=call_id, tool="bash", tool_input={"command": "SECRET_ARG_NEVER_DISPLAY"})
    with ledger.connection() as db:
        db.execute("UPDATE executions SET execution_state='succeeded' WHERE execution_id=?", (shell["execution_id"],))
    ctx = _cli_context(messages, reverter)
    output = io.StringIO()
    ctx.console = Console(file=output, force_terminal=False, color_system=None, width=240)
    results = []
    def undo(history):
        result = reverter.revert(history)
        results.append(result)
        return result
    def redo(history):
        result = reverter.unrevert(history)
        results.append(result)
        return result
    ctx.controller = SimpleNamespace(undo=undo, redo=redo)
    core.handle_undo(ctx)
    text = output.getvalue()
    assert target.read_bytes() == b"before\r\n"
    assert results[-1].status == "completed" and results[-1].unsupported_tools == (call_id,)
    assert "not automatically undone" in text and "call-shell" in text
    assert "app.py" in text and "Inspect" in text
    assert "SECRET_ARG" not in text and str(tmp_path) not in text and "\x1b" not in text
    if "[bold]" in call_id:
        assert "[bold]" in text and "\\u001b" in text and "\\u000a" in text
    output.seek(0)
    output.truncate(0)
    core.handle_redo(ctx)
    text = output.getvalue()
    assert target.read_bytes() == b"after\r\n"
    assert results[-1].status == "completed" and results[-1].unsupported_tools == (call_id,)
    assert "not re-executed" in text and "only recorded files" in text
    assert "SECRET_ARG" not in text and "\x1b" not in text
    assert len(ledger.executions("entry-session")) == 2


def test_cli_pending_recovery_retains_operation_status_and_safe_conflict_path(tmp_path, monkeypatch):
    from nz_coder.interface.commands.handlers import core
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal

    messages, reverter, _target = _owned(tmp_path)
    ctx = _cli_context(messages, reverter)
    def fail(*_args):
        raise OSError("PRIVATE_RAW_FAILURE")
    with monkeypatch.context() as patch:
        patch.setattr(RecoveryJournal, "_commit_history", fail)
        core.handle_undo(ctx)
    with ToolLedger(tmp_path).connection() as db:
        operation_id, = db.execute("SELECT operation_id FROM recovery_operations").fetchone()
    text = "\n".join(ctx.output)
    assert operation_id in text and "recovery_required" in text
    assert "PRIVATE_RAW_FAILURE" not in text and "delete" not in text.lower()
    ctx.output.clear()
    core.handle_redo(ctx)
    text = "\n".join(ctx.output)
    assert operation_id in text and "recovery_required" in text
    assert "(not created)" not in text


def test_cli_real_external_edit_conflict_keeps_path_and_status(tmp_path):
    from nz_coder.interface.commands.handlers import core

    messages, reverter, target = _owned(tmp_path)
    target.write_bytes(b"user edit")
    ctx = _cli_context(messages, reverter)
    core.handle_undo(ctx)
    text = "\n".join(ctx.output)
    assert "app.py" in text and "conflicted" in text and "Refused" in text
    assert "Undid" not in text
    assert target.read_bytes() == b"user edit" and len(messages) == 2


def test_cli_refuses_legacy_unknown_ownership_without_global_fallback(tmp_path, monkeypatch):
    from nz_coder.interface.commands.handlers import core

    messages, reverter, target = _owned(tmp_path)
    ctx = _cli_context(messages, reverter)
    ctx.agent.session_reverter = None
    def blind_restore(**_kwargs):
        target.write_bytes(b"BLIND RESTORE")
        return "Legacy restore ran"

    monkeypatch.setattr(core, "undo_latest", blind_restore, raising=False)
    before = copy.deepcopy(messages)
    core.handle_undo(ctx)
    assert target.read_bytes() == b"after\r\n"
    assert messages == before
    assert "ownership" in str(ctx.output[-1]).lower()


def test_cli_coordinator_refuses_legacy_history_without_owned_receipts(tmp_path):
    from nz_coder.interface.commands.handlers import core

    target = tmp_path / "user.py"
    target.write_bytes(b"user-owned edit")
    messages = [{"role": "user", "content": "legacy request", "_nz_message_id": "msg-legacy"}]
    reverter = SessionReverter(
        WorkspaceSnapshotStore(tmp_path, tmp_path / ".nz-coder" / "snapshots"),
        tmp_path / ".nz-coder" / "hint.json", session_id="legacy-session",
    )
    ctx = _cli_context(messages, reverter)
    before = copy.deepcopy(messages)
    core.handle_undo(ctx)
    assert target.read_bytes() == b"user-owned edit" and messages == before
    assert "ownership" in str(ctx.output[-1]).lower()


def test_sdk_undo_redo_and_noop_recover_do_not_build_provider(tmp_path, monkeypatch):
    from nz_coder.sdk import AgentClient

    _messages, _reverter, target = _owned(tmp_path)
    monkeypatch.setattr("nz_coder.sdk.build_native_sdk_runner", lambda: pytest.fail("recovery must not build a Provider"))
    client = AgentClient()
    undone = asyncio.run(client.undo_session(workspace=tmp_path, session_id="entry-session"))
    assert undone.status == "completed" and target.read_bytes() == b"before\r\n"
    redone = asyncio.run(client.redo_session(workspace=tmp_path, session_id="entry-session"))
    assert redone.status == "completed" and target.read_bytes() == b"after\r\n"
    assert asyncio.run(client.recover_session(workspace=tmp_path, session_id="entry-session")) is None


def test_sdk_recovery_hint_uses_explicit_workspace_not_ambient_scope(tmp_path, monkeypatch):
    from nz_coder.sdk import AgentClient

    workspace = tmp_path / "workspace"
    ambient = tmp_path / "host"
    workspace.mkdir()
    ambient.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(ambient / "state"))
    _messages, _reverter, target = _owned(workspace)
    with scoped_workdir(ambient):
        assert asyncio.run(AgentClient().undo_session(workspace=workspace, session_id="entry-session")).status == "completed"
    assert target.read_bytes() == b"before\r\n"


@pytest.mark.parametrize("session_id", ["../escape", "missing-session", "active", "latest"])
def test_sdk_recovery_refuses_invalid_or_missing_session(tmp_path, session_id):
    from nz_coder.sdk import AgentClient

    _owned(tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(AgentClient().undo_session(workspace=tmp_path, session_id=session_id))


def test_sdk_recover_finishes_a_real_pending_journal(tmp_path, monkeypatch):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal
    from nz_coder.sdk import AgentClient

    _messages, _reverter, target = _owned(tmp_path)

    def fail_save(*_args):
        raise OSError("private storage failure")

    with monkeypatch.context() as patch:
        patch.setattr(RecoveryJournal, "_commit_history", fail_save)
        with pytest.raises(RecoveryError) as interrupted:
            asyncio.run(AgentClient().undo_session(workspace=tmp_path, session_id="entry-session"))
    recovered = asyncio.run(AgentClient().recover_session(workspace=tmp_path, session_id="entry-session"))
    assert recovered.operation_id == interrupted.value.result.operation_id
    assert recovered.status == "completed" and target.read_bytes() == b"before\r\n"
    with scoped_workdir(tmp_path):
        assert load_session("entry-session")["messages"] == []


def test_sdk_cannot_recover_a_live_owned_session(tmp_path):
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run
    from nz_coder.sdk import AgentClient

    _messages, _reverter, target = _owned(tmp_path)
    with recovery_run(tmp_path, "entry-session"):
        with pytest.raises(RecoveryError):
            asyncio.run(AgentClient().undo_session(workspace=tmp_path, session_id="entry-session"))
    assert target.read_bytes() == b"after\r\n"


def test_sdk_cancellation_waits_for_the_recovery_worker_to_settle(tmp_path, monkeypatch):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal
    from nz_coder.sdk import AgentClient

    _messages, _reverter, target = _owned(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original = RecoveryJournal._apply_file

    def pause(self, item):
        entered.set()
        assert release.wait(3), "test did not release recovery worker"
        return original(self, item)

    monkeypatch.setattr(RecoveryJournal, "_apply_file", pause)

    async def cancel():
        task = asyncio.create_task(AgentClient().undo_session(workspace=tmp_path, session_id="entry-session"))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done(), "SDK returned cancellation before owned effects settled"
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert target.read_bytes() == b"before\r\n"
    with scoped_workdir(tmp_path):
        assert load_session("entry-session")["messages"] == []


@pytest.fixture
def recovery_http(tmp_path):
    from nz_coder.http_service import SessionHTTPService, SessionManager
    from nz_coder.protocol.session_events import SessionEventBus

    def factory(session_id, permission_mode):
        bus = SessionEventBus(session_id=session_id)
        return SimpleNamespace(
            session_id=session_id, permissions=SimpleNamespace(mode=permission_mode),
            event_bus=bus, close=bus.close,
        )

    with scoped_workdir(tmp_path):
        manager = SessionManager(agent_factory=factory, workspace_roots=[tmp_path], restore_saved=False)
        service = SessionHTTPService(port=0, token="recovery-entry-test-token", manager=manager)
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            info = manager.create("auto")
            session = manager.get(info["id"])
            messages, _reverter, target = _owned(tmp_path, info["id"])
            session.history = messages
            yield service, session, target
        finally:
            service.shutdown()
            thread.join(timeout=2)


def _post(service, session_id, operation):
    request = Request(
        f"{service.base_url}/session/{session_id}/{operation}", data=b"{}", method="POST",
        headers={"Authorization": f"Bearer {service.token}", "Content-Type": "application/json"},
    )
    try:
        response = build_opener(ProxyHandler({})).open(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def test_http_owned_undo_redo_and_recover_keep_versioned_results(recovery_http, monkeypatch):
    service, session, target = recovery_http
    monkeypatch.setattr(session, "_persist_transition_locked", lambda: pytest.fail("entry cannot second-save history"))
    status, result = _post(service, session.session_id, "undo")
    assert status == 200 and result["version"] == 1 and result["status"] == "completed"
    assert target.read_bytes() == b"before\r\n"
    session._reverter().state_path.unlink(missing_ok=True)
    status, result = _post(service, session.session_id, "redo")
    assert status == 200 and result["version"] == 1 and target.read_bytes() == b"after\r\n"
    status, result = _post(service, session.session_id, "recover")
    assert status == 200 and result["version"] == 1 and result["recovery_required"] is False


def test_http_conflict_exposes_typed_details_without_overwriting_user_file(recovery_http):
    service, session, target = recovery_http
    target.write_bytes(b"user edit")
    status, result = _post(service, session.session_id, "undo")
    assert status == 409
    assert result["error"]["code"] == "recovery_conflict"
    details = result["error"]["details"]
    assert details["version"] == 1 and details["conflicts"] == ["app.py"]
    assert details["status"] == "conflicted" and details["recovery_required"] is False
    assert target.read_bytes() == b"user edit" and len(session.history) == 2


def test_http_recovery_required_is_safe_and_retry_completes_actual_journal(recovery_http, monkeypatch):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal

    service, session, target = recovery_http
    with monkeypatch.context() as patch:
        patch.setattr(RecoveryJournal, "_commit_history", lambda *_args: (_ for _ in ()).throw(OSError("SECRET backend error")))
        status, result = _post(service, session.session_id, "undo")
    assert status == 409 and result["error"]["code"] == "recovery_required"
    assert "SECRET" not in json.dumps(result)
    operation_id = result["error"]["details"]["operation_id"]
    assert operation_id and result["error"]["details"]["recovery_required"] is True
    status, recovered = _post(service, session.session_id, "recover")
    assert status == 200 and recovered["operation_id"] == operation_id
    assert recovered["status"] == "completed" and session.history == []
    assert target.read_bytes() == b"before\r\n"


@pytest.mark.parametrize("operation", ["undo", "redo", "recover"])
def test_http_busy_recovery_never_mutates_live_session(recovery_http, operation):
    service, session, target = recovery_http
    before = copy.deepcopy(session.history)
    session._run_thread = threading.current_thread()
    try:
        status, result = _post(service, session.session_id, operation)
        assert status == 409 and result["error"]["code"] == "session_busy"
        assert session.history == before and target.read_bytes() == b"after\r\n"
    finally:
        session._run_thread = None


def test_http_typed_error_ignores_raw_exception_text(recovery_http, monkeypatch):
    service, session, _target = recovery_http

    def fail():
        raise RecoveryError("SECRET private path /runtime/token", operation_id="recovery-safe")

    monkeypatch.setattr(session, "undo", fail)
    status, result = _post(service, session.session_id, "undo")
    assert status == 409 and "SECRET" not in json.dumps(result)
    assert result["error"]["details"]["operation_id"] == "recovery-safe"


@pytest.mark.parametrize("operation", ["undo", "redo", "recover"])
def test_http_transition_invalidates_stale_live_completion(recovery_http, monkeypatch, operation):
    from nz_coder.runtime.session.recovery_journal import RecoveryJournal

    service, session, _target = recovery_http
    if operation == "redo":
        assert _post(service, session.session_id, "undo")[0] == 200
    elif operation == "recover":
        with monkeypatch.context() as patch:
            patch.setattr(RecoveryJournal, "_commit_history", lambda *_args: (_ for _ in ()).throw(OSError("interrupted save")))
            assert _post(service, session.session_id, "undo")[0] == 409
    session.status = "completed"
    session.last_result = {"status": "completed", "verified": True}
    status, result = _post(service, session.session_id, operation)
    assert status == 200 and result["status"] == "completed"
    assert session.info()["status"] == "interrupted"
    assert session.info()["last_result"] == {}
    with scoped_workdir(session.workspace):
        assert load_session(session.session_id)["run_status"] == "interrupted"


def test_http_noop_recover_preserves_live_status(recovery_http):
    service, session, _target = recovery_http
    session.status = "completed"
    session.last_result = {"status": "completed"}
    status, result = _post(service, session.session_id, "recover")
    assert status == 200 and result["status"] == "idle"
    assert session.status == "completed" and session.last_result == {"status": "completed"}
