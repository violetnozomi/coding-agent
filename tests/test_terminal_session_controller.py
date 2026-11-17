"""Product adapter tests that exercise installed-module import boundaries."""
from __future__ import annotations

from types import SimpleNamespace

from nz_coder.interface.session_controller import TerminalSessionController


def test_terminal_status_uses_the_canonical_workspace_status(tmp_path, monkeypatch):
    from nz_coder.runtime.process.workdir import scoped_workdir

    environment = SimpleNamespace(
        session_id="session-status",
        permissions=SimpleNamespace(mode="default"),
        model_id="offline/model",
    )
    controller = TerminalSessionController(environment)

    with scoped_workdir(tmp_path):
        report = controller.status_report([{"role": "user", "content": "hello"}])

    assert "# NZ-Coder Status" in report
    assert "session-status" in report
    assert "Conversation messages: 1" in report


def test_terminal_close_preserves_environment_when_process_cleanup_pending(
    tmp_path, monkeypatch
):
    import pytest

    from nz_coder.runtime.process import process_service as processes
    from nz_coder.runtime.process.workdir import scoped_workdir
    from tests.fixtures.process_lifecycle import ControlledBackend

    backend = ControlledBackend()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    closed = []
    environment = SimpleNamespace(
        session_id="terminal-owned", close=lambda: closed.append(True)
    )
    controller = TerminalSessionController(environment)
    with scoped_workdir(tmp_path):
        service = processes.workspace_process_service(tmp_path)
        service._kill_grace_override = 0
        service.start("owned", cwd=tmp_path, owner_session_id=environment.session_id)
        try:
            with pytest.raises(processes.ProcessStateError):
                controller.close()
            assert closed == []
            backend.recover()
            controller.close()
            assert closed == [True]
        finally:
            backend.recover()
            processes.close_workspace_process_service(tmp_path)
