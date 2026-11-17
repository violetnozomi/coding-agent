"""审批回复、交互故障和公开参数使用不同且可核对的结果。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import time

import pytest

from nz_coder.http_service.interactions import InteractionBroker
from nz_coder.permissions import PermissionManager
from nz_coder.protocol.session_events import SessionEventBus
from scripts.delivery_smoke_driver import input_sha256


def _pending(broker):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        pending = broker.list("permission")
        if pending:
            return pending[0]
        time.sleep(.005)
    raise AssertionError("permission was not registered")


@pytest.mark.parametrize("reply", ["no", "unexpected", {"allowed": False}, None])
def test_invalid_adapter_response_is_an_interaction_failure(reply):
    manager = PermissionManager("default", asker=lambda *_args: reply)
    with pytest.raises(RuntimeError, match="permission|approval|Permission|Approval"):
        manager.ask_user("write_file", {"path": "app.py"})


@pytest.mark.parametrize("reply, allowed", [(True, True), (False, False), ("once", True), ("reject", False)])
def test_explicit_approval_adapter_reply_preserves_its_meaning(reply, allowed):
    manager = PermissionManager("default", asker=lambda *_args: reply)
    assert manager.ask_user("write_file", {"path": "app.py"}) is allowed


@pytest.mark.parametrize("stage", ["static", "targeted"])
def test_pending_and_asked_share_public_parameters_and_hash(tmp_path, stage):
    bus = SessionEventBus(session_id="projection")
    broker = InteractionBroker(session_id="projection", event_bus=bus, timeout_seconds=3)
    broker.begin_run()
    original = {"command": "python -m py_compile app.py", "workdir": str(tmp_path),
                "_nz_runtime_verification_stage": stage}
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.ask_permission, "bash", original)
            pending = _pending(broker)
            asked = next(e for e in bus.recent(20) if e.type == "permission.asked")
            try:
                assert not future.done()
                assert pending["id"] == asked.properties["id"]
                assert pending["tool_input"] == asked.properties["tool_input"]
                assert input_sha256("bash", pending["tool_input"]) == input_sha256("bash", asked.properties["tool_input"])
                assert "_nz_runtime_verification_stage" not in pending["tool_input"]
                assert original["_nz_runtime_verification_stage"] == stage
            finally:
                broker.reply_permission(pending["id"], "once")
            assert future.result(timeout=2) == "once"
    finally:
        broker.close()


def test_permission_timeout_is_not_a_user_rejection():
    broker = InteractionBroker(session_id="timeout", event_bus=SessionEventBus(session_id="timeout"), timeout_seconds=.05)
    broker.begin_run()
    try:
        with pytest.raises(RuntimeError, match="timed out|timeout"):
            broker.ask_permission("write_file", {"path": "app.py"})
        assert broker.list() == []
    finally:
        broker.close()


def test_closed_permission_broker_reports_unavailable():
    broker = InteractionBroker(session_id="closed", event_bus=SessionEventBus(session_id="closed"))
    broker.close()
    with pytest.raises(RuntimeError, match="unavailable|not available"):
        broker.ask_permission("write_file", {"path": "app.py"})


def test_cancelled_permission_wait_is_not_a_user_rejection():
    broker = InteractionBroker(session_id="cancel", event_bus=SessionEventBus(session_id="cancel"), timeout_seconds=3)
    broker.begin_run()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(broker.ask_permission, "write_file", {"path": "app.py"})
            _pending(broker)
            broker.cancel_all("cancelled", block_new=True)
            with pytest.raises(RuntimeError, match="cancelled"):
                future.result(timeout=2)
            assert broker.list() == []
    finally:
        broker.close()


@pytest.mark.parametrize("reason", ["unavailable", "timed_out", "invalid_response"])
def test_executor_preserves_interaction_failure_without_claiming_user_rejected(reason):
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError

    def ask(*_args):
        raise PermissionInteractionError(reason)

    result = ToolExecutor(PermissionManager("default", asker=ask)).execute_one({
        "id": "write", "function": {"name": "write_file", "arguments": '{"path":"app.py","content":"x"}'},
    }, 0)
    assert result.approval_stopped and not result.executed
    assert result.metadata["permission_reason"] == reason
    assert result.metadata["permission_source"] == "interaction_failure"
    assert "user" not in result.output.lower()


def test_executor_real_run_cancellation_propagates():
    import asyncio
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError

    def ask(*_args):
        raise PermissionInteractionError("cancelled")

    with pytest.raises(asyncio.CancelledError):
        ToolExecutor(PermissionManager("default", asker=ask)).execute_one({
            "id": "write", "function": {"name": "write_file", "arguments": '{"path":"app.py","content":"x"}'},
        }, 0)


def _real_terminal(workspace):
    from io import StringIO
    from rich.console import Console
    from nz_coder.interface.commands.registry import CommandRegistry
    from nz_coder.interface.terminal_input import TerminalInput
    return TerminalInput(console=Console(file=StringIO()), registry=CommandRegistry(), workspace=workspace, interactive=False)


def test_real_noninteractive_terminal_is_unavailable_and_remote_reply_is_not_sent(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from nz_coder.interface.interactions import TerminalInteractionBridge
    from nz_coder.interface.remote import _resolve_remote_interaction
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError

    async def scenario():
        terminal = _real_terminal(tmp_path)
        bridge = TerminalInteractionBridge(terminal, SimpleNamespace(pause=lambda: None, resume=lambda: None), asyncio.get_running_loop())
        replies = []
        backend = SimpleNamespace(reply_permission=lambda *args: replies.append(args))
        with pytest.raises(PermissionInteractionError) as caught:
            await _resolve_remote_interaction(backend, bridge, {'type': 'permission.asked', 'properties': {
                'id': 'pending', 'permission': 'write_file', 'tool_input': {'path': 'a.py'},
            }})
        assert caught.value.reason == 'unavailable'
        assert replies == []
    asyncio.run(scenario())


def test_real_terminal_eof_is_unavailable_but_questions_still_dismiss(tmp_path, monkeypatch):
    import asyncio
    from nz_coder.interface.terminal_input import FuzzySelector
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError

    async def eof(_self):
        raise EOFError
    monkeypatch.setattr(FuzzySelector, 'run_async', eof)
    terminal = _real_terminal(tmp_path)
    terminal.interactive = True
    async def scenario():
        options = {'title': 'approval', 'values': [('once', 'Allow')]}
        assert await terminal.select_async(**options) is None
        with pytest.raises(PermissionInteractionError) as caught:
            await terminal.select_async(**options, require_interaction=True)
        assert caught.value.reason == 'unavailable'
    asyncio.run(scenario())
