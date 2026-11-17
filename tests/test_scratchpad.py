"""Structured working memory and persistence."""
from __future__ import annotations


def test_findings_preserve_certainty_and_basis(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tools.scratchpad import Scratchpad

    scratchpad = Scratchpad()
    with scoped_workdir(tmp_path), scoped_session("scratchpad-test"):
        scratchpad.clear()
        try:
            result = scratchpad.update_section("findings", [{"summary": "confirmed parser entrypoint", "details": "", "certainty": "confirmed", "basis": "parser.py"}])

            assert "[findings]" in result
            block = scratchpad.read()
            assert "confirmed parser entrypoint" in block
            assert "parser.py" in block
            assert "confirmed" in block
            assert "[plan]" not in block
        finally:
            scratchpad.clear()


def test_invalid_scratchpad_category_still_errors():
    from nz_coder.tools.scratchpad import scratchpad

    result = scratchpad.update_section('random', [{"summary": "note", "details": ""}])

    assert result.startswith("Error: invalid working memory section")


def test_scratchpad_persists_across_instances(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session, session_scratchpad_path
    from nz_coder.tools.scratchpad import Scratchpad

    with scoped_workdir(tmp_path), scoped_session("persisted-session"):
        first = Scratchpad()
        assert not first.update_section('lessons', [{"summary": "pytest failed in test_api", "details": ""}]).startswith("Error:")
        assert session_scratchpad_path().exists()

        restored = Scratchpad()
        block = restored.build_prompt_block()
        assert "pytest failed in test_api" in block

        assert not restored.clear().startswith("Error:")
        restarted = Scratchpad()
        assert restarted.read() == "Scratchpad is empty."


def test_todo_persists_priority_and_cancelled_state(tmp_path):
    import json
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session, session_todo_path
    from nz_coder.tools import todo

    with scoped_workdir(tmp_path), scoped_session("todo-session"):
        todo._items_by_session.clear()
        todo._loaded_sessions.clear()
        result = todo.todo_update([
            {
                "content": "obsolete migration",
                "status": "cancelled",
                "priority": "high",
            },
        ])
        assert "[~] obsolete migration" in result
        payload = json.loads(session_todo_path().read_text(encoding="utf-8"))
        assert payload["items"][0]["priority"] == "high"

        todo._items_by_session.clear()
        todo._loaded_sessions.clear()
        assert "[~] obsolete migration" in todo.render()
        assert todo.has_open_items() is False

        assert not todo.clear().startswith("Error:")
        todo._items_by_session.clear()
        todo._loaded_sessions.clear()
        assert todo.render() == "No todos."


def test_scratchpad_restores_in_fresh_python_process(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from nz_coder.tools.scratchpad import Scratchpad

    with scoped_workdir(tmp_path), scoped_session("fresh-process"):
        first = Scratchpad()
        assert not first.update_section('focus', [{"summary": "persist across process restart", "details": ""}]).startswith("Error:")

    script = (
        "import sys; from pathlib import Path; from nz_coder.foundation import config; "
        "config.WORKDIR = Path(sys.argv[1]); from nz_coder.state.sessions import activate_session; "
        "activate_session('fresh-process'); from nz_coder.tools.scratchpad import Scratchpad; "
        "print(Scratchpad().read())"
    )
    env = os.environ.copy()
    root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "persist across process restart" in completed.stdout
