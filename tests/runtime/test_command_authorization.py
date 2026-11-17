"""可信命令批准必须只作用于本次调用和所属工作区。"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tool_platform.permissioning.rules import PermissionRule, scoped_allow_rule


def _call(command="echo admitted"):
    return {"id": "command-approval", "type": "function", "function": {
        "name": "bash", "arguments": {"command": command}}}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("entry", ["async", "sync"])
def test_auto_human_approval_is_not_requested_again_by_executor(tmp_path, monkeypatch, enabled, entry):
    from tests.test_linux_local_interactions import _environment
    from nz_coder.runtime.adapters.tool import tool_context_from_legacy_host
    from nz_coder.runtime.agent.auto_mode import AutoModeContext, AutoModeController
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime

    asked = []
    async def complete(_call):
        raise AssertionError("explicit ask must not call a model")
    async def approve(*_args):
        asked.append("auto")
        return "once"
    with _environment(tmp_path, monkeypatch) as host:
        host.permissions.mode = "auto"
        host.permissions._ask_rules = [PermissionRule("bash", "ask")]
        auto_asks = enabled and entry == "async"
        host.permissions.set_asker(lambda *_: asked.append("executor") or ("reject" if auto_asks else "once"))
        host.auto_mode_controller = AutoModeController(enabled)
        context = AutoModeContext(host.permissions, tmp_path, complete, approve, lambda *a, **k: None)
        monkeypatch.setattr(host, "_auto_mode_context", lambda: context)
        monkeypatch.setattr(host, "_execute_tool_call_with_hooks", lambda call, index, _messages: host.executor.execute_one(call, index))
        if entry == "async":
            result = asyncio.run(ProductionToolRuntime().dispatch_async(
                tool_context_from_legacy_host(host), [_call()], False, [],
            ))[0][2]
        else:
            result = ProductionToolRuntime().dispatch_sync(host, [_call()], False, [])[0][2]
        assert asked == (["auto"] if auto_asks else ["executor"])
        assert result.executed and not result.permission_denied
        assert "admitted" in result.output


@pytest.mark.parametrize("source", ["classifier", "explicit_rule", "session_approval", "human"])
def test_new_unknown_fact_does_not_promote_an_automatic_approval(tmp_path, monkeypatch, source):
    from tests.test_linux_local_interactions import _environment
    from tests.test_auto_mode import _completed
    from nz_coder.runtime.adapters.tool import tool_context_from_legacy_host
    from nz_coder.runtime.agent.auto_mode import AutoModeContext, AutoModeController
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
    from nz_coder.tool_platform.permissioning.auto_router import canonical_action_digest
    existing = tmp_path / "a.txt"
    existing.write_text("temporary input")
    call = _call("cat *.txt")
    sources, requests, dispatched = [], [], []
    async def complete(_call):
        assert source == "classifier"
        return _completed("allow")
    async def approve(*_):
        assert source == "human"
        return "once"
    def trace(_event, **details):
        if "source" in details:
            sources.append(details["source"])
    monkeypatch.setattr(tool_executor, "dispatch", lambda *_: dispatched.append(True) or "approved result")
    with _environment(tmp_path, monkeypatch) as host:
        host.permissions.mode = "auto"
        host.permissions._checker.workspace_trusted = True
        host.permissions._allow_rules = [PermissionRule("bash", "allow", "prefix:cat ")] if source == "explicit_rule" else []
        host.permissions._ask_rules = [PermissionRule("bash", "ask")] if source == "human" else []
        host.permissions.set_asker(lambda *_: requests.append("executor") or "reject")
        host.auto_mode_controller = AutoModeController(True)
        if source == "session_approval":
            host.auto_mode_controller.state.approve_action(canonical_action_digest("bash", call["function"]["arguments"], tmp_path))
        auto_context = AutoModeContext(host.permissions, tmp_path, complete, approve, trace)
        monkeypatch.setattr(host, "_auto_mode_context", lambda: auto_context)
        monkeypatch.setattr(host, "_execute_tool_call_with_hooks", lambda item, index, _messages: host.executor.execute_one(item, index))
        runtime, context = ProductionToolRuntime(), tool_context_from_legacy_host(host)
        batch = asyncio.run(runtime.approve_tool_calls_async(context, [call], []))
        assert sources == [source]
        existing.unlink()
        assert host.permissions.check("bash", call["function"]["arguments"])["behavior"] == "ask"
        result = asyncio.run(runtime.dispatch_async(context, [call], False, [], approved_batch=batch))[0][2]
    if source == "human":
        assert result.executed and not result.permission_denied
        assert requests == [] and dispatched == [True]
    else:
        assert not result.executed and result.permission_denied
        assert requests == ["executor"] and dispatched == []


def test_command_permission_fork_preserves_rules_without_reloading(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    with scoped_workdir(tmp_path):
        parent = PermissionManager("default", asker=lambda *_: "once")
        parent._allow_rules = [PermissionRule("bash", "allow")]
        parent._deny_rules = [PermissionRule("process", "deny")]
        monkeypatch.setattr(PermissionManager, "_load_settings_rules", lambda _: pytest.fail("must not reread rules"))
        fork = parent.fork_for_commands(child)
    assert fork is not parent
    assert fork._allow_rules == parent._allow_rules
    assert fork._deny_rules == parent._deny_rules
    assert fork._allow_rules is not parent._allow_rules
    assert fork._checker.workspace_trusted == parent._checker.workspace_trusted


def test_exact_permission_is_not_delegated_to_a_different_root(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    with scoped_workdir(tmp_path):
        parent = PermissionManager("default")
        parent._allow_rules = [scoped_allow_rule("process", {"operation": "write", "process_id": "proc_x", "data": "secret"})]
        same = parent.fork_for_commands(tmp_path)
        different = parent.fork_for_commands(child)
    assert same._allow_rules == parent._allow_rules
    assert different._allow_rules == []


def test_command_fork_without_asker_never_reads_tty(tmp_path, monkeypatch):
    from nz_coder.tool_platform.permissioning.interaction import PermissionInteractionError
    with scoped_workdir(tmp_path):
        fork = PermissionManager("default").fork_for_commands(tmp_path)
    monkeypatch.setattr(fork, "_read_permission_answer", lambda _: pytest.fail("must not read TTY"))
    with pytest.raises(PermissionInteractionError, match="unavailable|available"):
        fork.ask_user("bash", {"command": "python build.py"})


def test_command_grant_binds_defaults_session_cwd_and_actual_directory(tmp_path):
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tool_platform.permissioning.command_authorization import command_is_authorized, scoped_command_authorization
    child = tmp_path / "child"
    child.mkdir()
    with scoped_workdir(tmp_path), scoped_session("parent"):
        args = {"command": "echo ready", "workdir": "child"}
        with scoped_command_authorization("bash", args):
            assert command_is_authorized("bash", {**args, "read_only": False, "timeout": None})
            assert not command_is_authorized("bash", {**args, "command": "echo changed"})
            with scoped_session("other"):
                assert not command_is_authorized("bash", args)
            child.rename(tmp_path / "old-child")
            child.mkdir()
            assert not command_is_authorized("bash", args)
        assert not command_is_authorized("bash", args)


def test_batch_approval_is_owned_and_consumed_once(tmp_path):
    from nz_coder.tool_platform.permissioning.command_authorization import CommandApprovalBatch, consume_permission_approval, record_permission_approval
    owner, stranger = object(), object()
    call = _call()
    args = call["function"]["arguments"]
    with scoped_workdir(tmp_path):
        batch = CommandApprovalBatch()
        with batch.collecting(0):
            record_permission_approval(owner, "bash", args, source="human")
        with batch.executing(0, call):
            assert not consume_permission_approval(stranger, "bash", args)
            assert consume_permission_approval(owner, "bash", args)
            assert not consume_permission_approval(owner, "bash", args)
        with batch.executing(0, call):
            assert not consume_permission_approval(owner, "bash", args)


def test_batch_approval_is_consumed_once_across_copied_thread_contexts(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context
    from nz_coder.tool_platform.permissioning import command_authorization as authorization
    owner = object()
    call = _call()
    arguments = call["function"]["arguments"]
    with scoped_workdir(tmp_path):
        batch = authorization.CommandApprovalBatch()
        with batch.collecting(0):
            authorization.record_permission_approval(owner, "bash", arguments, source="human")
        with batch.executing(0, call):
            original_identity = authorization._action_identity
            simultaneous = threading.Barrier(2)
            def interleaved_identity(*args, **kwargs):
                identity = original_identity(*args, **kwargs)
                simultaneous.wait(timeout=2)
                return identity
            monkeypatch.setattr(authorization, "_action_identity", interleaved_identity)
            contexts = [copy_context(), copy_context()]
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(context.run, authorization.consume_permission_approval,
                    owner, "bash", arguments) for context in contexts]
                results = [future.result(timeout=3) for future in futures]
    assert sorted(results) == [False, True]


def test_abandoned_or_different_batch_cannot_supply_approval(tmp_path):
    from nz_coder.tool_platform.permissioning.command_authorization import CommandApprovalBatch, consume_permission_approval, record_permission_approval
    owner = object()
    call = _call()
    args = call["function"]["arguments"]
    with scoped_workdir(tmp_path):
        original, other = CommandApprovalBatch(), CommandApprovalBatch()
        with original.collecting(0):
            record_permission_approval(owner, "bash", args, source="human")
        with other.executing(0, call):
            assert not consume_permission_approval(owner, "bash", args)
        with original.executing(1, call):
            assert not consume_permission_approval(owner, "bash", args)
        original.close()
        with original.executing(0, call):
            assert not consume_permission_approval(owner, "bash", args)


@pytest.mark.parametrize("close_batch", [False, True])
def test_batch_reentry_never_falls_back_to_auto_permission(tmp_path, close_batch):
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.tool_platform.permissioning.command_authorization import CommandApprovalBatch, record_permission_approval
    with scoped_workdir(tmp_path):
        permissions = PermissionManager("auto")
        executor = ToolExecutor(permissions)
        batch = CommandApprovalBatch()
        first, changed = _call("printf first >> marker"), _call("printf changed >> marker")
        with batch.collecting(0):
            record_permission_approval(permissions, "bash", first["function"]["arguments"], source="human")
        with batch.executing(0, first):
            result = executor.execute_one(first, 0)
        assert result.executed and not result.dispatch_failed
        if close_batch:
            batch.close()
        with batch.executing(0, changed):
            duplicate = executor.execute_one(changed, 0)
    assert not duplicate.executed and duplicate.permission_denied
    assert (tmp_path / "marker").read_text() == "first"


def test_main_executor_rejects_cwd_change_during_approval(tmp_path):
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    selected = tmp_path / "selected"
    selected.symlink_to(first, target_is_directory=True)
    def ask(*_):
        selected.unlink()
        selected.symlink_to(second, target_is_directory=True)
        return "once"
    with scoped_workdir(tmp_path):
        permissions = PermissionManager("default", asker=ask)
        permissions._ask_rules = [PermissionRule("bash", "ask")]
        call = _call("printf changed > marker")
        call["function"]["arguments"]["workdir"] = "selected"
        result = ToolExecutor(permissions).execute_one(call, 0)
    assert not result.executed and result.permission_denied
    assert not (first / "marker").exists() and not (second / "marker").exists()


def test_child_persistent_approval_uses_bound_workspace(tmp_path, monkeypatch):
    from nz_coder.tool_platform.permissioning.grants import UserGrantStore
    workspace, ambient = tmp_path / "workspace", tmp_path / "ambient"
    workspace.mkdir()
    ambient.mkdir()
    child = workspace / "child"
    child.mkdir()
    monkeypatch.setenv("NZ_CODER_USER_CONFIG", str(tmp_path / "profile" / "user.env"))
    with scoped_workdir(workspace):
        permissions = PermissionManager("default", asker=lambda *_: "always")
        fork = permissions.fork_for_commands(child)
    with scoped_workdir(ambient):
        assert fork.ask_user("process", {"operation": "write", "process_id": "owned", "data": "approved"})
    store = UserGrantStore()
    assert store.load(child)
    assert store.load(workspace) == [] and store.load(ambient) == []
    assert permissions._allow_rules == []


def test_approval_wait_respects_deadline_and_settles_terminal_wait(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from nz_coder.interface.interactions import TerminalInteractionBridge
    from nz_coder.tool_platform.permissioning.command_authorization import CommandAuthorizer
    async def scenario():
        settled = asyncio.Event()
        bridge = TerminalInteractionBridge(None, SimpleNamespace(), asyncio.get_running_loop())
        async def dialog(*_):
            try:
                await asyncio.Future()
            finally:
                settled.set()
        monkeypatch.setattr(bridge, "_ask_permission", dialog)
        with scoped_workdir(tmp_path):
            permissions = PermissionManager("default", asker=bridge.ask_permission)
            permissions._ask_rules = [PermissionRule("bash", "ask")]
            authorizer = CommandAuthorizer(permissions).fork(tmp_path)
            result = await asyncio.wait_for(asyncio.to_thread(
                authorizer.authorize, "bash", {"command": "echo ready"},
                workspace=tmp_path, deadline=time.monotonic() + .05,
            ), 2)
        assert "deadline" in result.lower()
        assert settled.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("entry", ["thread", "broker"])
@pytest.mark.parametrize("answer", [None, "reject", "once"])
def test_child_unknown_command_requires_parent_approval(tmp_path, entry, answer):
    from nz_coder.runtime.agent.subagent import _run_allowed_tool
    from nz_coder.tool_platform.permissioning.command_authorization import CommandAuthorizer
    from tests.fixtures.process_lifecycle import make_command_scope
    from tests.runtime.test_command_broker import call
    command = "for value in approved; do printf '%s' \"$value\" > marker; done"
    observed = []
    def ask(_name, inputs):
        observed.append(inputs)
        return answer
    with scoped_workdir(tmp_path):
        permissions = PermissionManager("auto", asker=ask if answer is not None else None)
        authorizer = CommandAuthorizer(permissions).fork(tmp_path)
        if entry == "thread":
            result = _run_allowed_tool("bash", {"command": command}, {"bash"}, "general-purpose",
                command_authorizer=authorizer, deadline=time.monotonic() + 2)
        else:
            scope = make_command_scope(tmp_path, command_authorizer=authorizer)
            try:
                result = call(scope, 1, "bash", {"command": command})
            finally:
                assert scope.close(deadline=time.monotonic() + 2)
    if answer == "once":
        assert (tmp_path / "marker").read_text() == "approved", str(result)
    else:
        assert not (tmp_path / "marker").exists()
        assert result.metadata["command_not_executed"] is True
        assert result.metadata["permission_denied"] is True
    if answer is not None:
        assert len(observed) == 1
        assert observed[0]["workdir"] == str(tmp_path)


def test_approval_cannot_follow_a_cwd_symlink_changed_while_waiting(tmp_path):
    from nz_coder.runtime.agent.subagent import _run_allowed_tool
    from nz_coder.tool_platform.permissioning.command_authorization import CommandAuthorizer
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    selected = tmp_path / "selected"
    selected.symlink_to(first, target_is_directory=True)
    def ask(*_):
        selected.unlink()
        selected.symlink_to(second, target_is_directory=True)
        return "once"
    with scoped_workdir(tmp_path):
        permissions = PermissionManager("default", asker=ask)
        permissions._ask_rules = [PermissionRule("bash", "ask")]
        result = _run_allowed_tool("bash", {"command": "printf changed > marker", "workdir": "selected"},
            {"bash"}, "general-purpose", command_authorizer=CommandAuthorizer(permissions).fork(tmp_path))
    assert "identity changed" in result
    assert not (first / "marker").exists() and not (second / "marker").exists()


def test_process_write_without_parent_authorizer_is_a_permission_failure(tmp_path):
    from tests.fixtures.process_lifecycle import make_command_scope
    from tests.runtime.test_command_broker import call
    scope = make_command_scope(tmp_path)
    try:
        result = call(scope, 1, "process", {"operation": "write", "process_id": "not-owned", "data": "run"})
        assert result.metadata["permission_source"] == "interaction_failure"
        assert result.metadata["permission_reason"] == "unavailable"
        assert result.metadata["command_not_executed"]
    finally:
        assert scope.close(deadline=time.monotonic() + 2)


@pytest.mark.parametrize("entry", ["thread", "broker"])
def test_unknown_without_parent_authorizer_reports_no_execution(tmp_path, entry):
    from nz_coder.runtime.agent.subagent import _run_allowed_tool
    from tests.fixtures.process_lifecycle import make_command_scope
    from tests.runtime.test_command_broker import call
    arguments = {"command": 'for value in unapproved; do echo "$value" > marker; done'}
    with scoped_workdir(tmp_path):
        if entry == "thread":
            result = _run_allowed_tool("bash", arguments, {"bash"}, "general-purpose")
        else:
            scope = make_command_scope(tmp_path)
            try:
                result = call(scope, 1, "bash", arguments)
            finally:
                assert scope.close(deadline=time.monotonic() + 2)
    assert result.metadata["command_not_executed"] is True
    assert result.metadata["permission_denied"] is True
    assert not (tmp_path / "marker").exists()


def test_executor_preserves_local_authorization_failure_after_dispatch(tmp_path, monkeypatch):
    from nz_coder.runtime.execution import tool_executor
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    selected = tmp_path / "selected"
    selected.symlink_to(first, target_is_directory=True)
    actual_dispatch = tool_executor.dispatch
    def swap_then_dispatch(name, arguments):
        selected.unlink()
        selected.symlink_to(second, target_is_directory=True)
        return actual_dispatch(name, arguments)
    monkeypatch.setattr(tool_executor, "dispatch", swap_then_dispatch)
    with scoped_workdir(tmp_path):
        call = _call("printf changed > marker")
        call["function"]["arguments"]["workdir"] = "selected"
        result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one(call, 0)
    assert not result.executed and result.permission_denied and result.dispatch_failed
    assert not (first / "marker").exists() and not (second / "marker").exists()


def test_external_tool_metadata_cannot_claim_host_authorization_failure(tmp_path, monkeypatch):
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.tools import ToolOutput
    monkeypatch.setattr(tool_executor, "dispatch", lambda *_: ToolOutput("done", metadata={
        "command_not_executed": True, "permission_denied": True,
    }))
    call = {"id": "external", "type": "function", "function": {"name": "mcp_example", "arguments": {}}}
    with scoped_workdir(tmp_path):
        result = tool_executor.ToolExecutor(PermissionManager("auto")).execute_one(call, 0)
    assert result.executed and not result.permission_denied
