"""High-value TUI scenario assertions for decisions, errors, and rendering."""
from __future__ import annotations

from io import StringIO
from types import SimpleNamespace

from rich.console import Console

from nz_coder.interface.run_renderer import TerminalRunRenderer


class _Stream:
    def set_status(self, _value): pass
    def pause(self): pass
    def resume(self): pass


def _view():
    output = StringIO()
    view = TerminalRunRenderer(
        Console(file=output, force_terminal=False, width=72), _Stream(),
        detail_provider=lambda: "normal",
    )
    view.begin(SimpleNamespace(model_id="test"))
    return view, output


def test_normal_tool_error_is_bounded_actionable_and_traceback_free():
    view, output = _view()
    view.on_tool(
        "bash",
        "Command exited with code 1\nTraceback (most recent call last):\n"
        + "noise\n" * 100
        + "AssertionError: expected 2",
    )
    rendered = output.getvalue()
    assert "Bash · bash" in rendered
    assert "AssertionError" in rendered
    assert len(rendered) < 2000


def test_provider_error_is_categorized_with_next_action_and_no_traceback():
    view, output = _view()
    view._render_assistant_error({
        "name": "ProviderAuthError",
        "data": {
            "message": "Traceback (most recent call last):\nsecret stack\n401 unauthorized",
            "statusCode": 401,
        },
    })
    rendered = output.getvalue()
    assert "Authentication" in rendered
    assert "/connect" in rendered
    assert "Traceback" not in rendered and "secret stack" not in rendered


def test_large_edit_is_summary_first_in_normal_mode():
    view, output = _view()
    view._render_tool({
        "name": "edit_file", "category": "edit", "summary": "src/app.py +242 -88",
        "status": "ok", "duration_ms": 12, "output": "diff-line\n" * 1000,
    })
    rendered = output.getvalue()
    assert "Edit · edit_file" in rendered
    assert "+242 -88" in rendered
    assert rendered.count("diff-line") <= 5
