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
