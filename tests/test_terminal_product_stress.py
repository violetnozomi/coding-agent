"""Bounded product stress contracts from the final productization checklist."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.formatted_text import to_formatted_text

from nz_coder.interface.fullscreen import _render_markdown
from nz_coder.interface.terminal_input import scan_workspace_files
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import list_sessions, session_dir
from nz_coder.tool_platform.results import ToolResultBudget, ToolResultProjector


@pytest.mark.parametrize("width", [50, 80, 120, 200])
def test_cjk_emoji_and_ansi_render_at_product_widths(width):
    rendered = "".join(
        text
        for _style, text in to_formatted_text(
            _render_markdown("# 状态 ✅\n\n修复完成 🚀\x1b[2J\x07", width)
        )
    )

    assert "状态" in rendered
    assert "修复完成" in rendered
    assert "\x1b" not in rendered
    assert "\x07" not in rendered


def test_workspace_autocomplete_is_bounded_with_ten_thousand_files(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    for index in range(10_050):
        (source / f"file-{index:05d}.py").touch()

    files = scan_workspace_files(tmp_path)

    assert len(files) == 10_000
    assert files[0] == "src/file-00000.py"
    assert files[-1] == "src/file-09999.py"


def test_session_listing_is_bounded_and_stable_with_one_thousand_sessions(tmp_path):
    with scoped_workdir(tmp_path):
        directory = session_dir()
        directory.mkdir(parents=True)
        for index in range(1_000):
            path = directory / f"session-{index:04d}.json"
            path.write_text(json.dumps({"session_id": path.stem}), encoding="utf-8")
            path.touch()

        sessions = list_sessions(limit=1_000)

    assert len(sessions) == 1_000
    assert all(isinstance(path, Path) for path in sessions)
    assert {path.stem for path in sessions} == {
        f"session-{index:04d}" for index in range(1_000)
    }


@pytest.mark.parametrize("size", [100_000, 1_000_000])
def test_large_and_binary_looking_tool_output_is_bounded_and_recoverable(tmp_path, size):
    artifact = tmp_path / f"full-{size}.txt"
    original = "HEAD\x00\ufffd\n" + ("x" * size) + "\nTAIL-FAILURE"

    projected = ToolResultProjector(
        budget=ToolResultBudget(max_tokens=256),
        artifact_writer=lambda _call_id, output: (
            artifact.write_text(output, encoding="utf-8") or str(artifact)
        ),
    ).project(f"call-{size}", original, tool_name="bash")

    assert projected.metadata["truncated"] is True
    assert projected.metadata["projected_tokens"] <= 256
    assert "TAIL-FAILURE" in projected.text
    assert artifact.read_text(encoding="utf-8") == original


def test_ten_thousand_character_paste_is_bounded_by_clipboard_contract(monkeypatch):
    from nz_coder.interface import terminal_input

    payload = "\u4e2d\U0001f680" * 5_000
    monkeypatch.setattr(terminal_input.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "wl-paste" else None)
    monkeypatch.setattr(
        terminal_input.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=payload.encode("utf-8")),
    )

    assert terminal_input._system_clipboard_text() == payload


def test_product_stress_manifest_names_every_required_boundary():
    from nz_coder.evaluation.product_stress import product_stress_manifest

    required = {
        "width-resize-stream", "cjk-emoji-ansi-binary", "logs-100k-1m",
        "large-transcript", "sessions-1k", "files-10k",
        "multiline-paste-10k", "bracketed-paste", "ctrl-c-input",
        "ctrl-c-agent", "double-ctrl-c", "ctrl-d", "queue-during-run",
        "history-search", "external-editor-failure", "background-agents",
        "persistent-processes", "disconnect-reconnect-loop", "slow-network",
        "event-gap", "permission-reconnect", "question-reconnect",
        "process-reconnect", "child-reconnect", "two-clients", "daemon-restart",
    }

    assert required == {item.case_id for item in product_stress_manifest()}
