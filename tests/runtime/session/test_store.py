"""Contract tests for the legacy JSON-backed SessionStore adapter."""
from __future__ import annotations

import asyncio

from nz_coder import config
from nz_coder.message_schema import MESSAGE_ID_KEY, PARTS_KEY
from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.runtime.session.store import (
    EphemeralSessionStore,
    LegacyJsonSessionStore,
    SessionStore,
)


def test_legacy_store_satisfies_session_store_protocol():
    """The production adapter exposes the storage-neutral Session contract."""
    assert isinstance(LegacyJsonSessionStore(), SessionStore)


def test_store_round_trip_preserves_parent_metadata_status_and_parts(
    tmp_path,
    monkeypatch,
):
    """The adapter adds parent identity without losing the legacy wire shape."""
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / ".nz-coder" / "sessions")
    session = Session.create(
        "child-1",
        [{
            "role": "assistant",
            "content": "done",
            MESSAGE_ID_KEY: "msg-1",
            PARTS_KEY: [{"id": "part-1", "type": "text", "text": "done"}],
        }],
        workspace=tmp_path,
        parent_session_id="parent-1",
        metadata={"permission_mode": "default", "title": "Child task"},
    )
    session.finish(SessionStatus.COMPLETED)
    store = LegacyJsonSessionStore()

    asyncio.run(store.save(session))
    restored = asyncio.run(store.load(session.identity, tmp_path))

    assert restored is not None
    assert restored.identity == session.identity
    assert restored.transcript == session.transcript
    assert restored.status is SessionStatus.COMPLETED
    assert restored.metadata["permission_mode"] == "default"
    assert restored.metadata["title"] == "Child task"
    assert restored.dirty is False


def test_store_returns_none_for_missing_session(tmp_path, monkeypatch):
    """Missing durable state is distinct from an empty existing Session."""
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / ".nz-coder" / "sessions")

    restored = asyncio.run(
        LegacyJsonSessionStore().load(SessionIdentity("missing"), tmp_path)
    )

    assert restored is None


def test_ephemeral_store_round_trip_never_creates_workspace_files(tmp_path):
    store = EphemeralSessionStore()
    session = Session.create(
        "ephemeral-1",
        [{"role": "user", "content": "temporary"}],
        workspace=tmp_path,
    )

    asyncio.run(store.save(session))
    restored = asyncio.run(store.load(session.identity, tmp_path))

    assert isinstance(store, SessionStore)
    assert restored is not None
    assert restored is not session
    assert restored.transcript == session.transcript
    assert not (tmp_path / ".nz-coder").exists()
