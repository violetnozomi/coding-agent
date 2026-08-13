"""Characterization coverage for the legacy JSON Session boundary."""
from __future__ import annotations

from nz_coder import config
from nz_coder.message_schema import MESSAGE_ID_KEY, PARTS_KEY
from nz_coder.sessions import load_session, save_session


def test_legacy_session_round_trip_preserves_status_and_message_parts(
    tmp_path,
    monkeypatch,
):
    """Existing persistence keeps stable message parts and terminal status."""
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / ".nz-coder" / "sessions")
    messages = [{
        "role": "assistant",
        "content": "done",
        MESSAGE_ID_KEY: "msg-1",
        PARTS_KEY: [{"id": "part-1", "type": "text", "text": "done"}],
    }]

    save_session(
        messages,
        session_id="child-1",
        run_status="completed",
        require_aliases=False,
    )
    payload = load_session("child-1")

    assert payload["messages"] == messages
    assert payload["run_status"] == "completed"
