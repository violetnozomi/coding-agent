"""Tests for SessionRuntime and production RunContext ownership."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest

from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.result import TokenUsage
from nz_coder.runtime.session.model import Session, SessionStatus
from nz_coder.runtime.session.runtime import SessionRuntime


class MemorySessionStore:
    """Minimal real-behavior store for SessionRuntime contract tests."""

    def __init__(self, existing: Session | None = None) -> None:
        self.existing = existing
        self.saved = []

    async def load(self, identity, workspace):
        if self.existing is None or self.existing.identity.session_id != identity.session_id:
            return None
        snapshot = self.existing.snapshot()
        session = Session(
            identity=snapshot.identity,
            workspace=snapshot.workspace,
            transcript=list(snapshot.transcript),
            status=snapshot.status,
            metadata=snapshot.metadata,
            usage=snapshot.usage,
        )
        session.mark_persisted()
        return session

    async def save(self, session):
        self.saved.append(session.snapshot())
        session.mark_persisted()


def _request(tmp_path, messages, *, session_id="s1", metadata=None):
    return RunRequest(
        agent=AgentDefinition("coder", "Fix the repository."),
        profile=MAIN_PROFILE,
        messages=messages,
        workspace=tmp_path,
        session_id=session_id,
        metadata=metadata or {},
    )


def test_open_prefers_durable_session_and_appends_only_new_request_tail(tmp_path):
    """Resume never duplicates the already durable common prefix."""
    existing = Session.create(
        "s1",
        [{"role": "user", "content": "old"}],
        workspace=tmp_path,
    )
    existing.mark_persisted()
    store = MemorySessionStore(existing)
    request = _request(tmp_path, [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "new"},
    ])

    context = asyncio.run(SessionRuntime(store).open(request))

    assert [item["content"] for item in context.transcript] == ["old", "new"]
    assert context.session.dirty is True
    assert context.active_agent == "coder"


def test_open_creates_parent_linked_session_from_request_metadata(tmp_path):
    """Child identity is Session state rather than a side JSON convention."""
    request = _request(
        tmp_path,
        [{"role": "user", "content": "child work"}],
        session_id="child-1",
        metadata={"parent_session_id": "parent-1"},
    )

    context = asyncio.run(SessionRuntime(MemorySessionStore()).open(request))

    assert context.session.parent_session_id == "parent-1"


def test_open_appends_a_new_activation_to_completed_durable_history(tmp_path):
    """Legacy AgentLoop callers may pass only the next user activation."""
    existing = Session.create(
        "s1",
        [
            {"role": "user", "content": "old"},
            {"role": "assistant", "content": "done"},
        ],
        workspace=tmp_path,
    )
    existing.finish(SessionStatus.COMPLETED)
    existing.mark_persisted()
    request = _request(tmp_path, [{"role": "user", "content": "again"}])

    context = asyncio.run(SessionRuntime(MemorySessionStore(existing)).open(request))

    assert [item["content"] for item in context.transcript] == [
        "old",
        "done",
        "again",
    ]


def test_each_user_submission_gets_new_interaction_run_id(tmp_path):
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    first = asyncio.run(runtime.open(_request(
        tmp_path,
        [{"role": "user", "content": "first"}],
    )))
    first.session.finish(SessionStatus.COMPLETED)
    first.session.mark_persisted()
    store.existing = first.session

    second = asyncio.run(runtime.open(_request(
        tmp_path,
        [{"role": "user", "content": "second"}],
    )))

    assert first.interaction_run_id != second.interaction_run_id
    assert first.interaction_run_id.startswith("interaction-")
    assert second.transcript[-1]["_nz_interaction_run_id"] == (
        second.interaction_run_id
    )


def _legacy_active_session(tmp_path):
    return Session.create(
        "legacy-session",
        [
            {"role": "user", "content": "continue legacy work"},
            {
                "role": "assistant",
                "content": "partial",
                "_nz_message_id": "msg-legacy",
                "_nz_parts": [
                    {
                        "id": "part-legacy",
                        "message_id": "msg-legacy",
                        "type": "text",
                        "text": "partial",
                    }
                ],
            },
        ],
        workspace=tmp_path,
    )


def test_legacy_session_resume_creates_persisted_interaction_identity(tmp_path):
    existing = _legacy_active_session(tmp_path)
    existing.mark_persisted()
    store = MemorySessionStore(existing)
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(
        tmp_path,
        copy.deepcopy(existing.transcript),
        session_id="legacy-session",
    )))

    asyncio.run(runtime.checkpoint(context))

    assert context.interaction_run_id.startswith("interaction-")
    assert context.transcript[-1]["_nz_interaction_run_id"] == (
        context.interaction_run_id
    )
    assert context.transcript[-1]["_nz_parts"][0]["interaction_run_id"] == (
        context.interaction_run_id
    )
    assert store.saved[-1].metadata["active_interaction_run_id"] == (
        context.interaction_run_id
    )


def test_legacy_session_live_events_use_migrated_identity(tmp_path):
    from nz_coder.protocol.session_events import SessionEventBus

    existing = _legacy_active_session(tmp_path)
    existing.mark_persisted()
    context = asyncio.run(SessionRuntime(MemorySessionStore(existing)).open(
        _request(
            tmp_path,
            copy.deepcopy(existing.transcript),
            session_id="legacy-session",
        )
    ))
    bus = SessionEventBus(session_id="legacy-session")
    try:
        publisher = bus.for_interaction(context.interaction_run_id)
        event = publisher.publish("message.part.delta", {
            "part_id": "part-legacy",
            "delta": " resumed",
        })
    finally:
        bus.close()

    assert event.run_id == context.interaction_run_id


def test_legacy_snapshot_then_live_delta_updates_reducer(tmp_path):
    from nz_coder.protocol.message_schema import message_records
    from nz_coder.protocol.run_view_reducer import RunViewReducer

    existing = _legacy_active_session(tmp_path)
    existing.mark_persisted()
    context = asyncio.run(SessionRuntime(MemorySessionStore(existing)).open(
        _request(
            tmp_path,
            copy.deepcopy(existing.transcript),
            session_id="legacy-session",
        )
    ))
    reducer = RunViewReducer()
    records = message_records(context.transcript, "legacy-session")
    assert records[-1]["parts"][0]["interaction_run_id"] == (
        context.interaction_run_id
    )
    message_id = records[-1]["info"]["id"]
    part_id = records[-1]["parts"][0]["id"]
    reducer.replace_snapshot({
        "interaction_run_id": context.interaction_run_id,
        "messages": records,
    })

    accepted = reducer.apply_event({
        "type": "message.part.delta",
        "properties": {
            "message_id": message_id,
            "part_id": part_id,
            "field": "text",
            "delta": " resumed",
            "interaction_run_id": context.interaction_run_id,
        },
        "meta": {
            "event_id": "event-legacy-live",
            "sequence": 1,
            "interaction_run_id": context.interaction_run_id,
        },
    })

    assert accepted is True
    assert reducer.state.interaction_run_id == context.interaction_run_id


def test_legacy_migration_identity_survives_reconnect(tmp_path):
    existing = _legacy_active_session(tmp_path)
    existing.mark_persisted()
    first_store = MemorySessionStore(existing)
    runtime = SessionRuntime(first_store)
    first = asyncio.run(runtime.open(_request(
        tmp_path,
        copy.deepcopy(existing.transcript),
        session_id="legacy-session",
    )))
    asyncio.run(runtime.checkpoint(first))
    persisted = Session(
        identity=first_store.saved[-1].identity,
        workspace=first_store.saved[-1].workspace,
        transcript=list(first_store.saved[-1].transcript),
        status=first_store.saved[-1].status,
        metadata=first_store.saved[-1].metadata,
    )
    persisted.mark_persisted()
    reconnect_request = _request(
        tmp_path,
        copy.deepcopy(persisted.transcript),
        session_id="legacy-session",
    )
    reconnect_request = replace(
        reconnect_request,
        interaction_run_id=first.interaction_run_id,
    )

    second = asyncio.run(SessionRuntime(MemorySessionStore(persisted)).open(
        reconnect_request
    ))

    assert second.interaction_run_id == first.interaction_run_id
    assert second.transcript[-1]["_nz_parts"][0]["interaction_run_id"] == (
        first.interaction_run_id
    )


def test_cancelled_run_cannot_transition_back_to_completed(tmp_path):
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(
        tmp_path,
        [{"role": "user", "content": "cancel me"}],
    )))
    asyncio.run(runtime.finalize(context, RunStatus.CANCELLED))
    saved = len(store.saved)

    asyncio.run(runtime.checkpoint(context, SessionStatus.COMPLETED))

    assert context.terminal_status is RunStatus.CANCELLED
    assert context.session.status is SessionStatus.CANCELLED
    assert len(store.saved) == saved


def test_checkpoint_and_finalize_persist_owned_session_once(tmp_path):
    """The runtime owns non-terminal and exactly-once terminal persistence."""
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(
        tmp_path,
        [{"role": "user", "content": "work"}],
    )))

    asyncio.run(runtime.checkpoint(context, SessionStatus.RUNNING))
    asyncio.run(runtime.finalize(context, RunStatus.COMPLETED))

    assert [snapshot.status for snapshot in store.saved] == [
        SessionStatus.RUNNING,
        SessionStatus.COMPLETED,
    ]
    with pytest.raises(RuntimeError, match="terminal"):
        asyncio.run(runtime.finalize(context, RunStatus.ERROR))


def test_finalize_persists_blocked_as_distinct_terminal_status(tmp_path):
    """Policy denial is durable state, not a generic execution error."""
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(
        tmp_path,
        [{"role": "user", "content": "unsafe request"}],
    )))

    asyncio.run(runtime.finalize(context, RunStatus.BLOCKED))

    assert context.terminal_status is RunStatus.BLOCKED
    assert store.saved[-1].status is SessionStatus.BLOCKED


def test_error_checkpoint_does_not_finalize_run_context(tmp_path):
    """Diagnostic persistence before lifecycle finalization remains resumable."""
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(tmp_path, [])))

    asyncio.run(runtime.checkpoint(context, SessionStatus.ERROR))

    assert context.finalized is False
    asyncio.run(runtime.finalize(context, RunStatus.ERROR))
    assert context.finalized is True


def test_error_checkpoint_cleans_incomplete_tool_history_before_persistence(
    tmp_path,
):
    """Catch checkpoints must never persist Provider-invalid tool pairing."""
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(tmp_path, [
        {"role": "user", "content": "inspect both files"},
        {
            "role": "assistant",
            "content": "I will inspect them.",
            "tool_calls": [
                {
                    "id": "call-settled",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "call-orphan",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-settled",
            "content": "first.py",
        },
        {
            "role": "tool",
            "tool_call_id": "unknown-result",
            "content": "must be removed",
        },
    ])))

    asyncio.run(runtime.checkpoint(context, SessionStatus.ERROR))

    persisted = list(store.saved[-1].transcript)
    assert [call["id"] for call in persisted[1]["tool_calls"]] == [
        "call-settled",
    ]
    assert [
        message["tool_call_id"]
        for message in persisted
        if message["role"] == "tool"
    ] == ["call-settled"]


def test_terminal_finalize_cleans_tail_orphan_without_losing_assistant_text(
    tmp_path,
):
    """A failed tail call is stripped while honest visible text is retained."""
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(tmp_path, [
        {"role": "user", "content": "inspect app.py"},
        {
            "role": "assistant",
            "content": "Starting the requested inspection.",
            "tool_calls": [{
                "id": "call-never-settled",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        },
    ])))

    asyncio.run(runtime.finalize(context, RunStatus.ERROR))

    assistant = store.saved[-1].transcript[-1]
    assert assistant["content"] == "Starting the requested inspection."
    assert "tool_calls" not in assistant


def test_terminal_save_failure_keeps_context_retryable_without_double_usage(
    tmp_path,
):
    """A transient store failure cannot commit an in-memory false terminal."""

    class FailingOnceStore(MemorySessionStore):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def save(self, session):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary storage failure")
            await super().save(session)

    store = FailingOnceStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(
        tmp_path,
        [{"role": "user", "content": "finish safely"}],
    )))
    context.add_usage(TokenUsage(input_tokens=7, output_tokens=3))

    with pytest.raises(OSError, match="temporary storage failure"):
        asyncio.run(runtime.finalize(context, RunStatus.ERROR))

    assert context.finalized is False
    assert context.terminal_status is None
    assert context.session.status is SessionStatus.RUNNING
    assert context.session.usage == TokenUsage()

    asyncio.run(runtime.finalize(context, RunStatus.ERROR))

    assert context.finalized is True
    assert context.session.usage == TokenUsage(input_tokens=7, output_tokens=3)
    assert store.saved[-1].usage == context.session.usage


def test_run_context_snapshots_request_metadata(tmp_path):
    """Run-scoped metadata cannot be mutated through the immutable request."""
    request = _request(
        tmp_path,
        [],
        metadata={"verification": {"enabled": True}},
    )
    context = asyncio.run(SessionRuntime(MemorySessionStore()).open(request))
    context.metadata["verification"]["enabled"] = False

    assert copy.deepcopy(request.metadata)["verification"]["enabled"] is True
