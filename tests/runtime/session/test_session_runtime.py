"""Tests for SessionRuntime and production RunContext ownership."""
from __future__ import annotations

import asyncio
import copy

import pytest

from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
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


def test_error_checkpoint_does_not_finalize_run_context(tmp_path):
    """Diagnostic persistence before lifecycle finalization remains resumable."""
    store = MemorySessionStore()
    runtime = SessionRuntime(store)
    context = asyncio.run(runtime.open(_request(tmp_path, [])))

    asyncio.run(runtime.checkpoint(context, SessionStatus.ERROR))

    assert context.finalized is False
    asyncio.run(runtime.finalize(context, RunStatus.ERROR))
    assert context.finalized is True


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
