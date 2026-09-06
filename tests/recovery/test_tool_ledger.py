"""Durable ownership, ordering and raw-byte checkpoint contracts."""
from __future__ import annotations

import os

import pytest


def register(ledger, call="call-z", step="msg-z", session="session-a"):
    return ledger.register(
        session_id=session, interaction_id="interaction-a", assistant_step_id=step,
        agent_id="agent-invocation-a", call_id=call, tool="write_file",
        tool_input={"path": "a.bin"},
    )


def test_registration_survives_restart_and_never_sorts_random_ids(tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    first = register(ledger)
    second = register(ledger, "call-a", "msg-a")
    reopened = ToolLedger(tmp_path)
    assert register(reopened) == first
    assert [r["call_id"] for r in reopened.executions("session-a")] == ["call-z", "call-a"]
    assert first["sequence"] < second["sequence"]
    assert first["attempt_id"] != second["attempt_id"]
    assert not ledger.path.is_relative_to(tmp_path)


def test_empty_absent_and_unknown_are_distinct_and_bytes_preserved(tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    absent = ledger.save_state(None)
    empty = ledger.save_state(b"", mode=0o640)
    binary = ledger.save_state(b"\xff\x00\r\n", mode=0o755)
    assert absent != empty
    assert ledger.state_bytes(absent) is None
    assert ledger.state_bytes(empty) == b""
    assert ledger.state_bytes(binary) == b"\xff\x00\r\n"
    assert ledger.state(binary)["mode"] == (0o666 if os.name == "nt" else 0o755)
    with pytest.raises(Exception, match="state"):
        ledger.state("unknown-state")


def test_multifile_and_repeated_after_do_not_duplicate_mutations(tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    run = register(ledger)
    before, after = ledger.save_state(None), ledger.save_state(b"one")
    a = ledger.prepare_mutation(run["execution_id"], "a.bin", before, after, ordinal=0)
    b = ledger.prepare_mutation(run["execution_id"], "b.bin", before, after, ordinal=1)
    assert ledger.prepare_mutation(run["execution_id"], "a.bin", before, after, ordinal=0) == a
    ledger.confirm_mutation(a["operation_id"], after)
    ledger.confirm_mutation(a["operation_id"], after)
    ledger.confirm_mutation(b["operation_id"], after)
    assert len(ledger.mutations("session-a")) == 2
    assert {m["status"] for m in ledger.mutations("session-a")} == {"confirmed"}


def test_same_file_base_is_session_scoped_and_changes_keep_own_before(tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    before = ledger.save_state(b"base")
    after = ledger.save_state(b"first")
    user = ledger.save_state(b"user edit")
    final = ledger.save_state(b"second")
    first = register(ledger)
    second = register(ledger, "call-a", "msg-a")
    other = register(ledger, "call-other", session="session-b")
    for run, a, b in [(first, before, after), (second, user, final), (other, final, user)]:
        mutation = ledger.prepare_mutation(run["execution_id"], "a.bin", a, b, ordinal=0)
        ledger.confirm_mutation(mutation["operation_id"], b)
    assert ledger.base("session-a", "a.bin") == before
    assert ledger.base("session-b", "a.bin") == final
    assert [m["before_ref"] for m in ledger.mutations("session-a")] == [before, user]


@pytest.mark.parametrize("disposition", ["reverted", "compensated"])
def test_duplicate_confirmation_does_not_resurrect_recovered_mutation(tmp_path, disposition):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    run = register(ledger)
    after = ledger.save_state(b"written")
    mutation = ledger.prepare_mutation(run["execution_id"], "a.bin", ledger.save_state(None), after, ordinal=0)
    ledger.confirm_mutation(mutation["operation_id"], after)
    with ledger.connection() as db:
        db.execute("UPDATE mutations SET disposition=?", (disposition,))
    ledger.confirm_mutation(mutation["operation_id"], after)
    assert ledger.mutations("session-a")[0]["disposition"] == disposition


def test_missing_or_corrupt_blob_is_not_an_empty_file(tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    reference = ledger.save_state(b"original")
    blob = ledger.blobs._blob_path(ledger.state(reference)["blob"])
    blob.write_bytes(b"corrupt")
    with pytest.raises(Exception, match="corrupt"):
        ledger.state_bytes(reference)
    blob.unlink()
    with pytest.raises(Exception):
        ledger.state_bytes(reference)


@pytest.mark.parametrize("path", ["../outside", "/outside", ".nz-coder/state.json"])
def test_mutation_paths_must_pass_model_write_boundary(tmp_path, path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path)
    run = register(ledger)
    reference = ledger.save_state(None)
    with pytest.raises((ValueError, OSError)):
        ledger.prepare_mutation(run["execution_id"], path, reference, reference, ordinal=0)
    assert ledger.mutations("session-a") == []


def test_payload_size_and_corrupt_schema_fail_closed(tmp_path):
    from nz_coder.state.tool_ledger import ToolLedger

    ledger = ToolLedger(tmp_path, max_file_bytes=4)
    with pytest.raises(ValueError, match="limit"):
        ledger.save_state(b"12345")
    with ledger.connection() as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError, match="version"):
        ToolLedger(tmp_path)


@pytest.mark.parametrize("interrupted", [False, True])
def test_v3_migration_discards_previews_without_public_admission(monkeypatch, tmp_path, interrupted):
    from nz_coder.state.tool_ledger import ToolLedger
    import sqlite3

    ledger = ToolLedger(tmp_path)
    row = register(ledger)
    with ledger.connection() as db:
        db.execute("ALTER TABLE executions DROP COLUMN preview_admitted")
        db.execute("UPDATE executions SET result_preview='PRIVATE_OLD_OUTPUT'")
        db.execute("PRAGMA user_version=3")
    if interrupted:
        connect = sqlite3.connect
        class InterruptedMigration(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("UPDATE executions SET result_preview=''"):
                    raise OSError("injected interruption after ALTER")
                return super().execute(sql, *args)
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", lambda *a, **k: connect(*a, **k, factory=InterruptedMigration))
            with pytest.raises(OSError, match="after ALTER"):
                ToolLedger(tmp_path)
    reopened = ToolLedger(tmp_path)
    migrated = reopened.executions("session-a")[0]
    assert migrated["execution_id"] == row["execution_id"]
    assert migrated["preview_admitted"] == 0
    assert migrated["result_preview"] == ""
    assert ToolLedger(tmp_path).executions("session-a") == [migrated]


def test_atomic_blob_failure_does_not_double_close_transferred_descriptor(monkeypatch, tmp_path):
    from nz_coder.foundation import content_objects

    closed = []
    original_close = content_objects.os.close
    def close(fd):
        closed.append(fd)
        original_close(fd)
    monkeypatch.setattr(content_objects.os, "close", close)
    def fail(_fd):
        raise OSError("injected flush failure")
    monkeypatch.setattr(content_objects.os, "fsync", fail)
    with pytest.raises(OSError, match="injected"):
        content_objects.ContentObjectStore._atomic_bytes(tmp_path / "blob", b"data", 0o600)
    # fdopen owns and closes its descriptor, including when flush fails. A
    # second raw close could close another thread's newly reused descriptor.
    assert closed == []
    assert list(tmp_path.iterdir()) == []


def test_atomic_blob_fdopen_failure_closes_untransferred_descriptor(monkeypatch, tmp_path):
    from nz_coder.foundation import content_objects

    closed = []
    original_close = content_objects.os.close
    def close(fd):
        closed.append(fd)
        original_close(fd)
    monkeypatch.setattr(content_objects.os, "close", close)
    def fail(*_args):
        raise OSError("injected fdopen failure")
    monkeypatch.setattr(content_objects.os, "fdopen", fail)
    with pytest.raises(OSError, match="injected"):
        content_objects.ContentObjectStore._atomic_bytes(tmp_path / "blob", b"data", 0o600)
    assert len(closed) == 1
    assert list(tmp_path.iterdir()) == []
