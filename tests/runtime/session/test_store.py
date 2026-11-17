"""Contract tests for the legacy JSON-backed SessionStore adapter."""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from nz_coder.foundation import config
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, PARTS_KEY
from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.runtime.session.store import (
    EphemeralSessionStore,
    LegacyJsonSessionStore,
    SessionStore,
)


def test_cancelled_checkpoint_settles_writer_before_terminal_save(tmp_path, monkeypatch):
    """取消检查点后，旧写入必须先结清，终态不能被覆盖回 running。"""
    from nz_coder.runtime.core.profiles import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.session.runtime import SessionRuntime
    from nz_coder.state import sessions

    entered = threading.Event()
    release = threading.Event()
    written = threading.Event()
    original = sessions._publish_session_json

    def delayed_write(prepared, path):
        payload = json.loads(prepared.read_text(encoding="utf-8"))
        if path.name == "cancel-save.json" and payload.get("run_status") == "running":
            entered.set()
            assert release.wait(3), "test writer was not released"
            try:
                return original(prepared, path)
            finally:
                written.set()
        return original(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", delayed_write)

    async def scenario():
        store = LegacyJsonSessionStore()
        runtime = SessionRuntime(store)
        context = await runtime.open(RunRequest(
            agent=AgentDefinition(name="worker", instructions="offline test"),
            profile=MAIN_PROFILE,
            messages=[{"role": "user", "content": "persist then cancel"}],
            workspace=tmp_path,
            session_id="cancel-save",
        ))
        checkpoint = asyncio.create_task(runtime.checkpoint(context))

        async def settle_run():
            with pytest.raises(asyncio.CancelledError):
                await checkpoint
            await runtime.finalize(context, RunStatus.CANCELLED)

        terminal = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            checkpoint.cancel()
            terminal = asyncio.create_task(settle_run())
            await asyncio.sleep(0.03)
            terminal_overtook_writer = terminal.done()
        finally:
            release.set()
            if terminal is not None:
                await asyncio.wait_for(terminal, timeout=3)
            elif not checkpoint.done():
                await checkpoint
        assert await asyncio.to_thread(written.wait, 2)
        restored = await store.load(context.session.identity, tmp_path)
        assert restored is not None
        assert restored.status is SessionStatus.CANCELLED
        assert terminal_overtook_writer is False

    asyncio.run(scenario())


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


def test_legacy_store_isolates_invalid_messages_and_parent_on_restore(
    tmp_path,
    monkeypatch,
):
    """One corrupt legacy record must not make the entire Session unusable."""
    payload = {
        "session_id": "resume-1",
        "parent_session_id": "../other-session",
        "run_status": "running",
        "messages": [
            {"role": "user", "content": "keep me"},
            {"role": "assistant"},
            "not-a-message",
        ],
    }
    monkeypatch.setattr(
        LegacyJsonSessionStore,
        "_load_sync",
        staticmethod(lambda _identity, _workspace: payload),
    )

    restored = asyncio.run(LegacyJsonSessionStore().load(
        SessionIdentity("resume-1"),
        tmp_path,
    ))

    assert restored is not None
    assert restored.parent_session_id is None
    assert restored.transcript == [{"role": "user", "content": "keep me"}]
    assert restored.metadata["session_recovery"] == {
        "invalid_messages_dropped": 2,
        "invalid_parent_session_id_ignored": True,
    }


def test_legacy_store_roundtrips_usage_and_closed(tmp_path, monkeypatch):
    from nz_coder.runtime.core.result import TokenUsage
    monkeypatch.setattr(config, "SESSIONS_DIR", tmp_path / "sessions", raising=False)
    usage = TokenUsage(input_tokens=1, output_tokens=2, cached_read_tokens=3,
                       cached_write_tokens=4, reasoning_tokens=5)
    session = Session(identity=SessionIdentity("usage-test"), workspace=tmp_path,
                      transcript=[{"role": "user", "content": "hello"}],
                      usage=usage, closed=True, metadata={"custom": "value"})
    store = LegacyJsonSessionStore()
    asyncio.run(store.save(session))
    restored = asyncio.run(store.load(session.identity, tmp_path))
    assert restored.usage == usage
    assert restored.closed is True
    assert restored.metadata["custom"] == "value"
    assert "_nz_session_snapshot" not in restored.metadata


@pytest.mark.parametrize("parts", [1, [1], [{"id": "part-bad", "message_id": "msg-other", "type": "text", "text": "x"}], [{"id": "part-bad", "message_id": "msg-bad", "type": "tool", "state": {"status": []}}]])
def test_legacy_store_rejects_invalid_persisted_parts(tmp_path, parts):
    from nz_coder.state.sessions import save_session
    from nz_coder.state.workdir import scoped_workdir
    message = {"role": "assistant", "content": "", "_nz_message_id": "msg-bad", "_nz_parts": parts}
    with scoped_workdir(tmp_path):
        path = save_session([message], session_id="bad-parts", activate=False, require_aliases=False)
    original = path.read_bytes()
    with pytest.raises(ValueError, match="parts"):
        asyncio.run(LegacyJsonSessionStore().load(SessionIdentity("bad-parts"), tmp_path))
    assert path.read_bytes() == original


def test_runtime_checkpoint_budget_error_is_public_and_preserves_files(tmp_path, monkeypatch):
    import hashlib
    from nz_coder.protocol.public_error import to_public_error
    from nz_coder.runtime.core.profiles import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest
    from nz_coder.runtime.session.runtime import SessionRuntime
    from nz_coder.state import sessions
    from nz_coder.state.workdir import scoped_workdir
    with scoped_workdir(tmp_path):
        path = sessions.save_session([{'role': 'user', 'content': 'old'}], session_id='budget-run', model='offline')
        root = sessions.session_dir()
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json')}
        limit = path.stat().st_size + 2048
    monkeypatch.setattr(sessions, '_session_byte_limit', lambda: limit)
    request = RunRequest(agent=AgentDefinition(name='test', instructions='offline'), profile=MAIN_PROFILE,
                         messages=[{'role': 'user', 'content': 'a' * (limit * 4)}], workspace=tmp_path,
                         session_id='budget-run')
    async def run():
        runtime = SessionRuntime(LegacyJsonSessionStore())
        context = await runtime.open(request)
        with pytest.raises(sessions.SessionReadError) as error:
            await runtime.checkpoint(context)
        public = to_public_error(error.value)
        assert public.code == 'resource_limit'
        assert str(path) not in str(public.to_dict())
    asyncio.run(run())
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*.json')} == before
