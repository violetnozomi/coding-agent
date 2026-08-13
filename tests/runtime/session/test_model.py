"""Unit tests for Session transcript and lifecycle ownership."""
from __future__ import annotations

from pathlib import Path

import pytest

from nz_coder.runtime.session.model import Session, SessionStatus


def test_session_copies_initial_messages_but_owns_live_transcript(tmp_path):
    """Caller mutations cannot alter the Session-owned initial transcript."""
    source = [{"role": "user", "content": "inspect"}]
    session = Session.create("session-1", source, workspace=tmp_path)

    source[0]["content"] = "mutated"
    session.append({"role": "assistant", "content": "done"})

    assert session.transcript[0]["content"] == "inspect"
    assert session.transcript[-1]["content"] == "done"
    assert session.workspace == Path(tmp_path).resolve()


def test_session_rejects_append_after_explicit_close(tmp_path):
    """Only explicit Session closure prevents future user turns."""
    session = Session.create("session-1", [], workspace=tmp_path)
    session.close()

    with pytest.raises(RuntimeError, match="closed"):
        session.append({"role": "user", "content": "late"})


def test_completed_session_can_begin_a_new_run(tmp_path):
    """A completed Run does not permanently close its conversation Session."""
    session = Session.create("session-1", [], workspace=tmp_path)
    session.finish(SessionStatus.COMPLETED)

    session.begin_run()
    session.append({"role": "user", "content": "follow up"})

    assert session.status is SessionStatus.RUNNING
    assert session.transcript[-1]["content"] == "follow up"


def test_session_snapshot_is_deeply_isolated(tmp_path):
    """Persistence snapshots cannot mutate the live Session."""
    session = Session.create(
        "child-1",
        [{"role": "user", "content": "work", "metadata": {"depth": 1}}],
        workspace=tmp_path,
        parent_session_id="parent-1",
        metadata={"permission_mode": "default"},
    )

    snapshot = session.snapshot()
    snapshot.transcript[0]["metadata"]["depth"] = 2
    snapshot.metadata["permission_mode"] = "plan"

    assert session.identity.parent_session_id == "parent-1"
    assert session.transcript[0]["metadata"]["depth"] == 1
    assert session.metadata["permission_mode"] == "default"


def test_session_can_mark_in_place_message_mutation_dirty(tmp_path):
    """Processor mutations on owned dictionaries become persistable state."""
    session = Session.create("session-1", [], workspace=tmp_path)
    session.mark_persisted()

    session.mark_dirty()

    assert session.dirty is True


@pytest.mark.parametrize("session_id", ["", " ", "../escape", "a/b"])
def test_session_rejects_unsafe_identity(tmp_path, session_id):
    """Session IDs are storage keys and cannot contain path syntax."""
    with pytest.raises(ValueError, match="session_id"):
        Session.create(session_id, [], workspace=tmp_path)
