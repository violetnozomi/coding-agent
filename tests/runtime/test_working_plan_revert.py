"""历史转换必须先持久化失效路线，不能重启后读回未来计划。"""
from __future__ import annotations

import copy
import json

import pytest

from nz_coder.runtime.agent.planning_runtime import invalidate_working_plan
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_snapshot import SnapshotError
from nz_coder.runtime.session.session_revert import SessionReverter
from nz_coder.state.sessions import session_runtime_state_path
from tests.test_session_revert import _history


def test_undo_and_redo_invalidate_route_before_workspace_conversion(tmp_path):
    store, app, messages = _history(tmp_path)
    state = RuntimeState(plan_text="Future route", plan_revision=5, plan_last_commit={"call_id": "future"})
    path = tmp_path / ".nz-coder" / "runtime.json"
    state.save(path, active=False)
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json",
                              before_transition=lambda: invalidate_working_plan(state, path=path))
    reverter.revert(messages)
    assert app.read_text() == "before\n" and state.plan_text == ""
    restored = RuntimeState()
    assert restored.load(path, allow_inactive=True) and restored.plan_revision == 0
    state.plan_text, state.plan_revision = "Route built after undo", 1
    state.save(path, active=False)
    reverter.unrevert(messages)
    assert app.read_text() == "after\n" and state.plan_text == ""
    assert json.loads(path.read_text())["plan_revision"] == 0


def test_invalidation_failure_preserves_workspace_history_and_route(monkeypatch, tmp_path):
    store, app, messages = _history(tmp_path)
    before = copy.deepcopy(messages)
    state = RuntimeState(plan_text="Route", plan_revision=3)
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(state, "_write_snapshot", fail)
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json",
        before_transition=lambda: invalidate_working_plan(state, path=tmp_path / ".nz-coder" / "runtime.json"))
    with pytest.raises(SnapshotError, match="invalidation"):
        reverter.revert(messages)
    assert messages == before and app.read_text() == "after\n"
    assert state.plan_text == "Route" and not reverter.state_path.exists()


def test_later_workspace_conflict_can_leave_advisory_route_empty(tmp_path):
    store, app, messages = _history(tmp_path)
    state = RuntimeState(plan_text="Route", plan_revision=3)
    app.write_text("user edit\n")
    reverter = SessionReverter(store, tmp_path / ".nz-coder" / "revert.json",
        before_transition=lambda: invalidate_working_plan(state))
    with pytest.raises(SnapshotError, match="workspace changed"):
        reverter.revert(messages)
    assert state.plan_text == "" and len(messages) == 2
    assert app.read_text() == "user edit\n"


def test_http_undo_redo_clear_unloaded_runtime_plan(tmp_path):
    from nz_coder.http_service import SessionManager
    from tests.test_http_service import _fake_factory
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False)
    try:
        session = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))["id"])
        _, app, messages = _history(tmp_path, store=session._reverter().store, session_id=session.session_id)
        session.history[:] = messages
        with scoped_workdir(tmp_path):
            path = session_runtime_state_path(session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": False, "plan_text": "Future", "plan_revision": 9,
                                   "plan_last_commit": {"call_id": "future"}, "mutation_generation": 37}))
        session.undo()
        assert app.read_text() == "before\n" and json.loads(path.read_text())["plan_text"] == ""
        assert json.loads(path.read_text())["mutation_generation"] == 37
        data = json.loads(path.read_text())
        data["plan_text"] = "After undo"
        path.write_text(json.dumps(data))
        session.redo()
        assert app.read_text() == "after\n" and json.loads(path.read_text())["plan_text"] == ""
    finally:
        manager.close()


def test_http_fork_does_not_copy_parent_runtime_plan(tmp_path):
    from nz_coder.http_service import SessionManager
    from tests.test_http_service import _fake_factory
    manager = SessionManager(agent_factory=_fake_factory, workspace_roots=[tmp_path], restore_saved=False)
    try:
        parent = manager.get(manager.create(workspace_id=manager.workspaces.id_for(tmp_path))["id"])
        _, _, messages = _history(tmp_path, store=parent._reverter().store, session_id=parent.session_id)
        parent.history[:] = messages
        with scoped_workdir(tmp_path):
            path = session_runtime_state_path(parent.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"active": False, "plan_text": "Parent route", "plan_revision": 3}))
        child = manager.get(manager.fork(parent.session_id)["id"])
        with scoped_workdir(tmp_path):
            child_path = session_runtime_state_path(child.session_id)
        assert not child_path.exists()
        assert json.loads(path.read_text())["plan_text"] == "Parent route"
    finally:
        manager.close()


@pytest.mark.parametrize("fail_save", [False, True])
def test_cli_file_level_fallback_clears_route_or_refuses_conversion(monkeypatch, tmp_path, fail_save):
    from io import StringIO
    from types import SimpleNamespace
    from rich.console import Console
    from nz_coder.interface.commands.registry import CommandContext
    from nz_coder.interface.commands.handlers.core import handle_undo, handle_redo
    from nz_coder.state.changes import ChangeTracker

    state = RuntimeState(plan_text="Future route", plan_revision=5)
    with scoped_workdir(tmp_path):
        path = session_runtime_state_path("plan-cli")
        state.save(path, active=False)
        app = tmp_path / "app.py"
        app.write_text("before\n")
        tracker = ChangeTracker(run_id="plan-cli", change_dir=tmp_path / ".nz-coder" / "changes")
        tracker.record_before("app.py", True, "before\n")
        app.write_text("after\n")
        tracker.record_after("app.py", True, "after\n")
        output = StringIO()
        agent = SimpleNamespace(workdir=tmp_path, runtime_state=state, change_tracker=tracker,
                                permissions=SimpleNamespace(mode="auto"))
        ctx = CommandContext(history=[], session_state={"agent": agent, "id": "plan-cli"},
                             system_prompt="system", renderer=None, console=Console(file=output),
                             build_agent=lambda *_: agent)
        if fail_save:
            def fail(*_args):
                raise OSError("disk full")
            monkeypatch.setattr(state, "_write_snapshot", fail)
        handle_undo(ctx)
        if fail_save:
            assert app.read_text() == "after\n" and state.plan_text == "Future route"
            assert "Refused" in output.getvalue()
        else:
            assert app.read_text() == "before\n" and state.plan_text == ""
            state.plan_text, state.plan_revision = "After undo", 1
            state.save(path, active=False)
            handle_redo(ctx)
            assert app.read_text() == "after\n" and state.plan_text == ""
            assert json.loads(path.read_text())["plan_revision"] == 0
