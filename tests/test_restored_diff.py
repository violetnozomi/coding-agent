"""Recorded Agent diff selection must survive Session/environment reconstruction."""
from __future__ import annotations

from io import StringIO
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from rich.console import Console

from nz_coder.interface.commands.handlers.core import _resume_session, handle_diff
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.interface.session_controller import TerminalSessionController
from nz_coder.runtime.execution.composition import build_product_environment
from nz_coder.state.changes import ChangeTracker
from nz_coder.state.sessions import activate_session, save_session, session_change_dir
from nz_coder.state.workdir import scoped_workdir


def _record(workspace, session_id="session-a", run_id="edit", after="agent\n"):
    with scoped_workdir(workspace):
        tracker = ChangeTracker(run_id, session_change_dir(session_id))
        tracker.record_before("app.py", True, "original\n")
        (workspace / "app.py").write_text(after)
        tracker.record_after("app.py", True, after)
        return tracker


def _controller(workspace, session_id, tracker):
    return TerminalSessionController(SimpleNamespace(
        workdir=workspace, session_id=session_id, change_tracker=tracker,
    ))


@pytest.mark.parametrize("controller_enabled", [True, False])
def test_resume_real_environment_keeps_recorded_diff(tmp_path, monkeypatch, controller_enabled):
    from nz_coder.runtime.model_gateway import ProductionModelGateway

    def no_model(*args, **kwargs):
        pytest.fail("Diff/restore must not request inference")

    monkeypatch.setattr(ProductionModelGateway, "complete_sync", no_model)
    monkeypatch.setattr(ProductionModelGateway, "complete_stream_sync", no_model)
    for key, value in {"API_KEY": "offline-only", "API_BASE_URL": "http://127.0.0.1:1",
                       "MODEL_PROVIDER": "openai-compatible", "MODEL_ID": "deepseek-v4-pro",
                       "MODEL_VARIANT": "", "NZ_CONSTRAINT_BOUNDARY_SELFTEST_ENABLED": "false"}.items():
        monkeypatch.setenv(key, value)
    output = StringIO()
    console = Console(file=output, width=160)
    environments = []

    def build(prompt, renderer, session_id, permission_mode=None):
        environment = build_product_environment(prompt, session_id=session_id, permission_mode=permission_mode)
        environments.append(environment)
        return environment

    with scoped_workdir(tmp_path):
        recorded = _record(tmp_path)
        save_session([{"role": "user", "content": "edit app"}], session_id="session-a")
        old = build("Local test", None, "session-new")
        state = {"id": "session-new", "agent": old}
        if controller_enabled:
            state["controller"] = TerminalSessionController(old)
        context = CommandContext([], state, "Local test", None, console, build)
        try:
            _resume_session(context, "session-a")
            tracker = context.agent.change_tracker
            assert tracker is not None and not tracker.path.exists()
            assert tracker.path != recorded.path and recorded.path.exists()
            before = recorded.path.read_bytes()
            handle_diff(context)
            assert "+agent" in output.getvalue()
            assert "-original" in output.getvalue()
            assert context.session_id == "session-a"
            assert recorded.path.read_bytes() == before
            assert not tracker.path.exists() and tracker.changed_paths() == []
        finally:
            for environment in environments:
                environment.close()


def test_diff_ignores_other_active_session_and_workspace(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    first = _record(tmp_path)
    _record(tmp_path, "session-b", after="other-session\n")
    _record(other, after="other-workspace\n")
    with scoped_workdir(tmp_path):
        empty = ChangeTracker("resumed", session_change_dir("session-a"))
    with scoped_workdir(other):
        activate_session("session-b")
        rendered = _controller(tmp_path, "session-a", empty).diff()
    assert "+agent" in rendered and first.run_id in rendered
    assert "other-session" not in rendered and "other-workspace" not in rendered


@pytest.mark.parametrize("kind", ["current", "empty", "latest-empty", "absent", "corrupt",
                                  "invalid", "workspace", "session", "run", "symlink", "directory",
                                  "unreadable", "bad-content", "bad-path"])
def test_record_selection_distinguishes_missing_empty_and_invalid(tmp_path, kind):
    with scoped_workdir(tmp_path):
        old = _record(tmp_path)
        os.utime(old.path, (1, 1))
        current = ChangeTracker("current", session_change_dir("session-a"))
        if kind == "current":
            current.record_before("app.py", True, "agent\n")
            current.record_after("app.py", True, "next\n")
            os.utime(old.path, (2000000000, 2000000000))
        elif kind in {"empty", "latest-empty"}:
            current._save()
        elif kind == "absent":
            old.path.unlink()
        elif kind == "corrupt":
            current.path.write_text("{")
        elif kind == "invalid":
            current.path.write_text("[]")
        elif kind in {"workspace", "session", "run"}:
            payload = json.loads(old.path.read_text())
            payload["run_id"] = "current"
            payload[{"workspace": "workspace", "session": "session_id", "run": "run_id"}[kind]] = "wrong"
            current.path.write_text(json.dumps(payload))
        elif kind == "symlink":
            current.path.symlink_to(old.path)
        elif kind == "directory":
            current.path.mkdir()
        elif kind == "unreadable":
            current._save()
            current.path.chmod(0)
        elif kind in {"bad-content", "bad-path"}:
            payload = json.loads(old.path.read_text())
            payload["run_id"] = "current"
            payload["changes"][0]["after" if kind == "bad-content" else "path"] = (
                {} if kind == "bad-content" else "../outside"
            )
            current.path.write_text(json.dumps(payload))
        if kind == "latest-empty":
            current = ChangeTracker("not-created", current.change_dir)
        rendered = _controller(tmp_path, "session-a", current).diff()
        if kind == "current":
            assert "+next" in rendered and "-original" not in rendered
        elif kind in {"empty", "latest-empty", "absent"}:
            assert "No agent file changes recorded." in rendered and "+agent" not in rendered
        else:
            assert "Cannot review" in rendered and "+agent" not in rendered


def test_history_review_is_read_only_and_renders_incomplete_and_literal_data(tmp_path):
    with scoped_workdir(tmp_path):
        tracker = _record(tmp_path, after="[red]agent[/red]\x1b[2J\n")
        tracker.record_before("new.txt", False)
        tracker.record_after("new.txt", True, "created\n")
        tracker.record_before("deleted.txt", True, "removed\n")
        tracker.record_after("deleted.txt", False)
        tracker.record_before("interrupted.txt", True, "not-deleted\n")
        tracker.record_before("empty-added.txt", False)
        tracker.record_after("empty-added.txt", True, "")
        tracker.record_before("empty-deleted.txt", True, "")
        tracker.record_after("empty-deleted.txt", False)
        (tmp_path / "app.py").write_text("user-edited-same-file\n")
        (tmp_path / "user.txt").write_text("user-unrelated\n")
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(["git", "-C", str(tmp_path), "add", "user.txt"], check=True)
        paths = [tmp_path / "app.py", tmp_path / "user.txt", tmp_path / ".git/index", tracker.path]
        before = [path.read_bytes() for path in paths]
        records_before = list(tracker.change_dir.iterdir())
        output = StringIO()
        state = {"id": "session-a", "agent": _controller(tmp_path, "session-a", tracker).environment}
        handle_diff(CommandContext([], state, "test", None, Console(file=output), lambda *_: None))
        rendered = output.getvalue()
        assert "+[red]agent[/red]" in rendered and "\x1b" not in rendered
        assert "+created" in rendered and "-removed" in rendered
        assert "## empty-added.txt\n(added file)" in rendered
        assert "## empty-deleted.txt\n(deleted file)" in rendered
        assert "incomplete snapshot" in rendered and "-not-deleted" not in rendered
        assert "historical snapshots" in rendered and "not current disk" in " ".join(rendered.split())
        assert "user-edited" not in rendered and "user-unrelated" not in rendered
        assert [path.read_bytes() for path in paths] == before
        assert list(tracker.change_dir.iterdir()) == records_before


def test_tracker_owned_by_other_session_is_not_a_fallback_hint(tmp_path):
    with scoped_workdir(tmp_path):
        tracker = _record(tmp_path, "session-b")
        rendered = _controller(tmp_path, "session-a", tracker).diff()
        assert "Cannot review" in rendered and "+agent" not in rendered
        assert "No agent file changes" in _controller(tmp_path, "session-empty", None).diff()


def test_historical_review_after_undo_redo_and_read_only_turn(tmp_path):
    from nz_coder.runtime.session.session_revert import SessionReverter
    from tests.test_session_revert import _history

    with scoped_workdir(tmp_path):
        store, app, messages = _history(tmp_path)
        tracker = ChangeTracker("edit", session_change_dir("session-a"))
        tracker.record_before("app.py", True, "before\n")
        tracker.record_after("app.py", True, "after\n")
        reverter = SessionReverter(store, tmp_path / "revert.json", session_id="session-a")
        controller = _controller(tmp_path, "session-a", tracker)
        saved = tracker.path.read_bytes()
        reverter.revert(messages)
        assert app.read_text() == "before\n"
        assert "+after" in controller.diff() and "including undone edits" in controller.diff()
        reverter.unrevert(messages)
        assert app.read_text() == "after\n" and "+after" in controller.diff()
        assert tracker.path.read_bytes() == saved
        controller.environment.change_tracker = ChangeTracker("read-only", tracker.change_dir)
        assert "+after" in controller.diff()
        newer = ChangeTracker("next-edit", tracker.change_dir)
        newer.record_before("app.py", True, "after\n")
        newer.record_after("app.py", True, "newer\n")
        controller.environment.change_tracker = newer
        assert "+newer" in controller.diff() and "-before" not in controller.diff()
