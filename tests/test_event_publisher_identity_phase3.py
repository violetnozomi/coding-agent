"""Regression coverage for immutable interaction-scoped event publishers."""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from nz_coder.protocol.run_view_reducer import RunViewReducer
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager


def test_each_interaction_gets_immutable_event_publisher():
    bus = SessionEventBus(session_id="session-publisher")

    first = bus.for_interaction("interaction-a", agent_invocation_id="agent-a")
    second = bus.for_interaction("interaction-b", agent_invocation_id="agent-b")

    assert first is not second
    assert first.interaction_run_id == "interaction-a"
    assert second.interaction_run_id == "interaction-b"
    with pytest.raises(FrozenInstanceError):
        first.interaction_run_id = "interaction-b"


def test_background_event_keeps_origin_interaction_after_next_run_starts(tmp_path):
    bus = SessionEventBus(session_id="session-background")
    origin = bus.for_interaction("interaction-a", agent_invocation_id="parent-a")
    next_run = bus.for_interaction("interaction-b", agent_invocation_id="parent-b")
    manager = BackgroundAgentManager(tmp_path, "session-background")
    manager.bind_event_bus(bus)
    manager.bind_event_publisher(origin)
    manager._remember_task_publisher("task-a", origin.for_child("child-a"))

    manager.bind_event_publisher(next_run)
    manager._bridge_workflow_event(
        {"type": "task_terminal", "task_id": "task-a"},
        {"revision": 2, "items": []},
    )

    child = next(event for event in bus.recent() if event.type == "session.child.finished")
    assert child.run_id == "interaction-a"
    assert child.agent_id == "child-a"


def test_background_child_finish_does_not_enter_next_run_view(tmp_path):
    bus = SessionEventBus(session_id="session-background-view")
    origin = bus.for_interaction("interaction-a", agent_invocation_id="parent-a")
    manager = BackgroundAgentManager(tmp_path, "session-background-view")
    manager.bind_event_bus(bus)
    manager.bind_event_publisher(origin)
    manager._remember_task_publisher("task-a", origin.for_child("child-a"))
    manager.bind_event_publisher(
        bus.for_interaction("interaction-b", agent_invocation_id="parent-b")
    )
    manager._bridge_workflow_event(
        {"type": "task_terminal", "task_id": "task-a"},
        {"revision": 2, "items": []},
    )
    child = next(event for event in bus.recent() if event.type == "session.child.finished")
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-b",
        "status": "running",
        "messages": [],
    })

    assert reducer.apply_event(child) is False
    assert reducer.state.interaction_run_id == "interaction-b"


def test_session_bus_cannot_relabel_existing_publisher():
    bus = SessionEventBus(session_id="session-fixed")
    first = bus.for_interaction("interaction-a", agent_invocation_id="agent-a")

    replacement = bus.bind_identity(run_id="interaction-b", agent_id="agent-b")
    event = first.publish("session.worker.event", {"value": 1})

    assert replacement.interaction_run_id == "interaction-b"
    assert event.run_id == "interaction-a"
    assert event.agent_id == "agent-a"


def test_parent_and_child_agent_invocation_ids_are_preserved():
    bus = SessionEventBus(session_id="session-lineage")
    parent = bus.for_interaction("interaction-a", agent_invocation_id="agent-parent")
    child = parent.for_child("agent-child")

    event = child.publish("session.child.finished", {})
    meta = event.to_dict()["meta"]

    assert meta["interaction_run_id"] == "interaction-a"
    assert meta["agent_invocation_id"] == "agent-child"
    assert meta["parent_agent_invocation_id"] == "agent-parent"


def test_event_journal_preserves_origin_interaction_identity(tmp_path):
    path = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="session-journal-origin",
        replay_capacity=8,
        journal_path=path,
    )
    publisher = bus.for_interaction(
        "interaction-a",
        agent_invocation_id="agent-child",
        parent_interaction_run_id="interaction-parent",
        parent_agent_invocation_id="agent-parent",
    )
    publisher.publish("session.child.finished", {"status": "completed"})
    bus.close()

    restored = SessionEventBus(
        session_id="session-journal-origin",
        replay_capacity=8,
        journal_path=path,
    ).recent()
    event = next(item for item in restored if item.type == "session.child.finished")

    assert event.run_id == "interaction-a"
    assert event.agent_id == "agent-child"
    assert event.parent_interaction_run_id == "interaction-parent"
    assert event.parent_agent_invocation_id == "agent-parent"


def test_snapshot_filters_background_event_by_origin_identity():
    bus = SessionEventBus(session_id="session-filter")
    old = bus.for_interaction("interaction-a", agent_invocation_id="child-a")
    event = old.publish("session.child.finished", {"status": "completed"})
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-b",
        "status": "running",
        "messages": [],
    })

    assert reducer.apply_event(event) is False
    assert reducer.state.terminal is None
