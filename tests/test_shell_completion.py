"""Contracts for generated product shell completions."""
from __future__ import annotations

import io


def test_completion_scripts_cover_commands_flags_and_modes():
    from nz_coder.interface.completion import completion_main

    for shell in ("bash", "zsh", "fish"):
        stdout = io.StringIO()
        stderr = io.StringIO()
        assert completion_main([shell], stdout=stdout, stderr=stderr) == 0
        script = stdout.getvalue()
        assert "run" in script
        assert "--permission-mode" in script
        assert "--output" in script
        assert "jsonl" in script
        assert "acceptEdits" in script
        assert stderr.getvalue() == ""


def test_completion_rejects_unknown_shell_without_traceback():
    from nz_coder.interface.completion import completion_main

    stdout = io.StringIO()
    stderr = io.StringIO()
    assert completion_main(["powershell"], stdout=stdout, stderr=stderr) == 2
    assert stdout.getvalue() == ""
    assert "bash, zsh, or fish" in stderr.getvalue()


def test_top_level_cli_dispatches_completion(monkeypatch):
    from nz_coder.interface import cli

    captured = []
    monkeypatch.setattr(
        "nz_coder.interface.completion.completion_main",
        lambda args: captured.append(args) or 0,
    )
    assert cli.main(["completion", "zsh"]) == 0
    assert captured == [["zsh"]]
