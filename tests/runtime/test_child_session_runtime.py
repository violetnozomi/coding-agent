"""Native parent-linked Session tests for child Agent execution."""
from __future__ import annotations

import asyncio

from nz_coder.runtime.core.profiles import READ_CHILD_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.child_contracts import TaskStatus
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.subagent import (
    _bind_child_session_identity,
    _child_activation_messages,
    _new_subagent_state,
)


class _Agent:
    session_id = "child-1"


def test_new_child_task_record_has_no_conversation_copy() -> None:
    state = _new_subagent_state("parent-1", "explore", ["read_file"])

    assert "messages" not in state
    assert state["status"] == TaskStatus.RUNNING.value


def test_child_agent_receives_explicit_parent_session_identity() -> None:
    agent = _Agent()

    _bind_child_session_identity(agent, "parent-1")

    assert agent.parent_session_id == "parent-1"


def test_child_agent_rejects_self_parent_identity() -> None:
    agent = _Agent()

    try:
        _bind_child_session_identity(agent, "child-1")
    except ValueError as error:
        assert "parent" in str(error)
    else:
        raise AssertionError("self-parent child Session was accepted")


def test_native_child_session_wins_over_task_control_transcript(tmp_path) -> None:
    store = LegacyJsonSessionStore()
    session = Session.create(
        "child-1",
        [{"role": "user", "content": "durable"}],
        workspace=tmp_path,
        parent_session_id="parent-1",
    )
    session.begin_run()
    asyncio.run(store.save(session))
    task_state = {
        "session_id": "child-1",
        "parent_session_id": "parent-1",
        "messages": [{"role": "user", "content": "stale-control-copy"}],
    }

    messages, native = _child_activation_messages(
        task_state,
        "continue",
        workspace=tmp_path,
        store=store,
    )

    assert native is True
    assert messages == [{"role": "user", "content": "continue"}]
    assert "messages" not in task_state


def test_legacy_child_transcript_bootstraps_native_session(tmp_path) -> None:
    state = {
        "session_id": "child-new",
        "parent_session_id": "parent-1",
        "messages": [{"role": "user", "content": "legacy"}],
    }

    messages, native = _child_activation_messages(
        state,
        "continue",
        workspace=tmp_path,
        store=LegacyJsonSessionStore(),
    )

    assert native is False
    assert [item["content"] for item in messages] == ["legacy", "continue"]


def test_child_resume_after_runtime_restart_appends_only_new_activation(tmp_path) -> None:
    first_runtime = SessionRuntime(LegacyJsonSessionStore())
    first_request = RunRequest(
        agent=AgentDefinition(name="explore", instructions="inspect"),
        profile=READ_CHILD_PROFILE,
        messages=({"role": "user", "content": "prompt A"},),
        workspace=tmp_path,
        session_id="child-restart",
        metadata={"parent_session_id": "parent-1"},
    )
    first = asyncio.run(first_runtime.open(first_request))
    first.session.append({"role": "assistant", "content": "answer A"})
    asyncio.run(first_runtime.finalize(first, RunStatus.COMPLETED))

    second_runtime = SessionRuntime(LegacyJsonSessionStore())
    second_request = RunRequest(
        agent=first_request.agent,
        profile=READ_CHILD_PROFILE,
        messages=({"role": "user", "content": "prompt B"},),
        workspace=tmp_path,
        session_id="child-restart",
        metadata={"parent_session_id": "parent-1"},
    )
    resumed = asyncio.run(second_runtime.open(second_request))

    assert resumed.session.identity == SessionIdentity("child-restart", "parent-1")
    assert [item["content"] for item in resumed.transcript] == [
        "prompt A", "answer A", "prompt B",
    ]
