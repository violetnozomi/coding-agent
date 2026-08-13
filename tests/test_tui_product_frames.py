"""Logical product-frame tests independent of terminal escape sequences."""
from __future__ import annotations

from nz_coder.interface.presentation_tokens import (
    activity_label,
    attachment_chips,
    build_empty_state,
    build_header,
    compact_status,
    responsive_band,
)


def test_empty_state_guides_first_task_without_a_tutorial():
    text = build_empty_state({
        "workspace": r"C:\Users\Violet\My Project",
        "model": "gpt-5.6-sol",
        "provider_configured": True,
    })
    assert "Working in:" in text
    assert r"C:\Users\Violet\My Project" in text
    assert "Fix the failing tests" in text
    assert "/help" in text and "Ctrl+K" in text and "@" in text


def test_no_provider_empty_state_has_one_actionable_path():
    text = build_empty_state({
        "workspace": "/repo", "model": "-", "provider_configured": False,
    })
    assert "No model provider configured" in text
    assert "/connect" in text


def test_header_has_location_identity_and_text_state():
    text = build_header({
        "workspace": "/repo", "model": "deepseek-v4", "mode": "default",
        "session": "session-123", "location": "REMOTE · localhost:8765",
        "run_state": "waiting",
    }, width=120)
    assert "NZ-Coder" in text
    assert "REMOTE · localhost:8765" in text
    assert "WAITING" in text
    assert "deepseek-v4" in text and "123" in text
    assert "session-123" not in text


def test_responsive_status_hides_secondary_metadata_under_80_columns():
    state = {
        "model": "gpt-5.6-sol", "context": "12k/128k", "branch": "feature/windows",
        "changed": "3", "processes": "2",
    }
    assert responsive_band(70) == "narrow"
    assert "branch" not in compact_status(state, width=70).lower()
    assert "12k/128k" in compact_status(state, width=70)
    assert "feature/windows" in compact_status(state, width=110)


def test_activity_is_semantic_and_does_not_expose_tool_middleware():
    assert activity_label("grep_search", "query") == "Searching codebase..."
    assert activity_label("read_file", "src/main.py") == "Reading · src/main.py"
    assert activity_label("edit_file", "src/main.py") == "Editing · src/main.py"
    assert activity_label("bash", "pytest tests") == "Running tests · pytest tests"
    assert activity_label("process_read", "proc_1") == "Waiting for process · proc_1"
    assert activity_label("verify_changed_files", "static") == "Verifying · static"


def test_attachment_chips_are_bounded_and_cjk_safe():
    from nz_coder.interface.presentation_tokens import terminal_text_width

    value = attachment_chips(["src/main.py", "设计图.png", "very/long/path/file.pdf"], width=42)
    assert "[src/main.py]" in value
    assert "[设计图.png]" in value
    assert terminal_text_width(value) <= 42


def test_terminal_clip_uses_columns_and_keeps_combining_cluster_intact():
    from nz_coder.interface import presentation_tokens

    clip_terminal_text = presentation_tokens.clip_terminal_text
    terminal_text_width = presentation_tokens.terminal_text_width
    assert terminal_text_width("A界🚀e\u0301") == 6
    assert clip_terminal_text("A界B", 3) == "A…"
    assert clip_terminal_text("e\u0301x", 1) == "…"
    assert clip_terminal_text("e\u0301x", 2) == "e\u0301x"
    assert not clip_terminal_text("👨\u200d💻-developer", 4).endswith("\u200d")


def test_header_prioritizes_title_short_id_workspace_and_location():
    text = build_header({
        "workspace": r"C:\Users\Violet\very\long\repo",
        "model": "gpt-5.6-sol",
        "mode": "default",
        "session": "session-20260813-abcdef",
        "session_title": "Fix auth retry",
        "location": "REMOTE",
        "run_state": "running",
    }, width=120)

    assert "RUNNING" in text and "REMOTE" in text
    assert "repo" in text
    assert "Fix auth retry · cdef" in text
    assert "session-20260813-abcdef" not in text


def test_narrow_header_never_hides_status_or_location():
    from nz_coder.interface.presentation_tokens import terminal_text_width

    text = build_header({
        "workspace": "/very/long/workspace",
        "model": "very-long-model-name",
        "mode": "default",
        "session": "session-long-id",
        "location": "LOCAL",
        "run_state": "waiting",
    }, width=38)

    assert "WAITING" in text and "LOCAL" in text
    assert terminal_text_width(text) <= 38


def test_attachment_chips_hide_internal_clipboard_cache_name():
    value = attachment_chips(
        ["设计图.png", "src/auth.py", ".nz-coder/attachments/clipboard-0a12feed.png"],
        width=80,
    )

    assert "[设计图.png]" in value
    assert "[src/auth.py]" in value
    assert "[clipboard image]" in value
    assert ".nz-coder" not in value and "0a12feed" not in value


def test_terminal_status_exposes_product_session_title(monkeypatch, tmp_path):
    from nz_coder.interface import cli

    monkeypatch.setattr(cli, "current_workdir", lambda: tmp_path)
    status = cli._terminal_status(
        {
            "id": "session-abcdef",
            "session_title": "Fix Unicode output",
            "agent": object(),
        },
        [],
    )

    assert status["session_title"] == "Fix Unicode output"
