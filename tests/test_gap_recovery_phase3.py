"""Lossless gap recovery, reducer fencing, and pending interaction tests."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

from nz_coder.interface.run_renderer import TerminalRunRenderer
from nz_coder.protocol.message_part_reducer import MessagePartReducer
from nz_coder.protocol.run_view_reducer import RunViewReducer
from nz_coder.protocol.session_events import SessionEventBus


_IDENTITY = {
    "interaction_run_id": "interaction-1",
    "attempt_id": "attempt-1",
    "generation_id": "generation-1",
    "generation": 1,
}


def _part(*, status: str = "streaming") -> dict:
    return {
        "id": "part-1",
        "message_id": "message-1",
        "type": "text",
        "text": "base",
        "version": 1,
        "status": status,
        **_IDENTITY,
    }


def _snapshot(*, status: str = "streaming") -> list[dict]:
    return [{
        "info": {
            "id": "message-1",
            "role": "assistant",
            "interaction_run_id": "interaction-1",
        },
        "parts": [_part(status=status)],
    }]


def _event(event_type: str, properties: dict, *, sequence: int = 2) -> dict:
    return {
        "type": event_type,
        "properties": properties,
        "meta": {
            "event_id": f"event-{sequence}",
            "sequence": sequence,
            "interaction_run_id": properties.get("interaction_run_id", ""),
        },
    }


class _Streaming:
    def replace_text(self, _text):
        return None

    def set_status(self, _status):
        return None


class _Console:
    def print(self, *_args, **_kwargs):
        return None


def _local_view(bus: SessionEventBus) -> tuple[TerminalRunRenderer, object]:
    view = TerminalRunRenderer(_Console(), _Streaming())
    agent = SimpleNamespace(
        event_bus=bus,
        event_publisher=bus.for_interaction("interaction-1"),
        session_id="session-gap",
        model_id="test",
        active_run_context=SimpleNamespace(
            interaction_run_id="interaction-1",
            transcript=[],
        ),
    )
    view.begin(agent)
    assert view._subscription is not None
    return view, agent


def test_local_gap_snapshot_sequence_matches_snapshot_state():
    bus = SessionEventBus(session_id="session-gap")
    publisher = bus.for_interaction("interaction-1")
    publisher.publish("session.worker.event", {"index": 1})
    view, _agent = _local_view(bus)
    old = view._subscription

    assert view._rebase_local_after_gap(old) is True

    cursor = next(
        event for event in reversed(bus.recent())
        if event.type == "server.snapshot"
    )
    assert view._run_reducer.state.last_sequence == cursor.sequence - 1


def test_event_between_snapshot_and_cursor_is_replayed():
    bus = SessionEventBus(session_id="session-gap-race")
    publisher = bus.for_interaction("interaction-1")
    subscription = bus.subscribe(max_queue=8)
    capture_started = threading.Event()
    published = threading.Event()

    def publish_boundary() -> None:
        capture_started.wait(1)
        publisher.publish("session.worker.event", {"boundary": 11})
        published.set()

    worker = threading.Thread(target=publish_boundary)
    worker.start()

    def capture():
        capture_started.set()
        return {"value": "snapshot"}

    _snapshot_value, cursor, _replay = bus.checkpoint_with_replay(
        capture,
        event_type="server.snapshot",
        publisher=publisher,
    )
    worker.join(1)
    assert published.is_set()
    queued = []
    while True:
        try:
            queued.append(subscription.get(timeout=0.01))
        except queue.Empty:
            break

    boundary = next(event for event in queued if event.properties.get("boundary") == 11)
    assert boundary.sequence > cursor.sequence


def test_local_gap_rebase_does_not_drop_boundary_event():
    bus = SessionEventBus(session_id="session-gap-boundary")
    publisher = bus.for_interaction("interaction-1")
    view, agent = _local_view(bus)
    old = view._subscription
    original = bus.checkpoint_with_replay

    def checkpoint(*args, **kwargs):
        result = original(*args, **kwargs)
        publisher.publish("session.run.completed", {"status": "completed"})
        return result

    bus.checkpoint_with_replay = checkpoint

    assert view._rebase_local_after_gap(old) is True
    view.drain()
    assert view._run_reducer.state.status == "completed"
    assert agent.active_run_context.interaction_run_id == "interaction-1"


def test_failed_local_rebase_does_not_keep_permanent_gapped_subscription():
    bus = SessionEventBus(session_id="session-gap-failure")
    view, _agent = _local_view(bus)
    old = view._subscription
    bus.checkpoint_with_replay = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("capture failed")
    )

    assert view._rebase_local_after_gap(old) is False
    assert view._subscription is None
    assert old._closed is True


def test_local_rebase_switches_subscription_only_after_success():
    bus = SessionEventBus(session_id="session-gap-switch")
    view, _agent = _local_view(bus)
    old = view._subscription
    original = bus.checkpoint_with_replay
    observed = []

    def checkpoint(*args, **kwargs):
        observed.append(view._subscription is old)
        return original(*args, **kwargs)

    bus.checkpoint_with_replay = checkpoint

    assert view._rebase_local_after_gap(old) is True
    assert observed == [True]
    assert view._subscription is not old
    assert old._closed is True


def test_local_gap_rebase_matches_remote_snapshot_semantics():
    bus = SessionEventBus(session_id="session-gap-parity")
    view, agent = _local_view(bus)
    agent.active_run_context.transcript = [{
        "role": "assistant",
        "content": "base",
        "_nz_message_id": "message-1",
        "_nz_interaction_run_id": "interaction-1",
        "_nz_parts": [_part(status="completed")],
    }]
    old = view._subscription

    assert view._rebase_local_after_gap(old) is True
    local_state = view._run_reducer.state
    remote = RunViewReducer()
    remote.replace_snapshot({
        "interaction_run_id": "interaction-1",
        "status": "running",
        "messages": _snapshot(status="completed"),
        "snapshot_sequence": local_state.last_sequence,
    })

    assert view.logical_text == remote.visible_text == "base"
    assert local_state.interaction_run_id == remote.state.interaction_run_id
    assert local_state.last_sequence == remote.state.last_sequence


def test_bound_interaction_rejects_event_without_interaction_id():
    reducer = MessagePartReducer()
    reducer.replace_snapshot(_snapshot(), interaction_run_id="interaction-1")
    properties = {
        "message_id": "message-1",
        "part_id": "part-1",
        "delta": "-leak",
        "attempt_id": "attempt-1",
        "generation_id": "generation-1",
        "generation": 1,
        "version": 2,
    }

    assert reducer.apply_event(_event("message.part.delta", properties)) is False
    assert reducer.visible_text == "base"


def test_bound_attempt_rejects_event_without_attempt_id():
    reducer = MessagePartReducer()
    reducer.replace_snapshot(_snapshot(), interaction_run_id="interaction-1")
    properties = {
        "message_id": "message-1",
        "part_id": "part-1",
        "delta": "-leak",
        "interaction_run_id": "interaction-1",
        "generation_id": "generation-1",
        "generation": 1,
        "version": 2,
    }

    assert reducer.apply_event(_event("message.part.delta", properties)) is False


def test_bound_generation_rejects_event_without_generation_id():
    reducer = MessagePartReducer()
    reducer.replace_snapshot(_snapshot(), interaction_run_id="interaction-1")
    properties = {
        "message_id": "message-1",
        "part_id": "part-1",
        "delta": "-leak",
        "interaction_run_id": "interaction-1",
        "attempt_id": "attempt-1",
        "generation": 1,
        "version": 2,
    }

    assert reducer.apply_event(_event("message.part.delta", properties)) is False


def test_legacy_event_only_allowed_in_legacy_mode():
    legacy = MessagePartReducer()
    properties = {"delta": "legacy"}
    assert legacy.apply_event(_event("message.part.delta", properties)) is True

    bound = MessagePartReducer()
    bound.replace_snapshot(_snapshot(), interaction_run_id="interaction-1")
    assert bound.apply_event(_event("message.part.delta", properties)) is False


def test_missing_identity_cannot_update_completed_part():
    reducer = MessagePartReducer()
    reducer.replace_snapshot(
        _snapshot(status="completed"),
        interaction_run_id="interaction-1",
    )
    changed = reducer.apply_event(_event("message.part.updated", {
        "message_id": "message-1",
        "part": {
            "id": "part-1",
            "message_id": "message-1",
            "type": "text",
            "text": "reopened",
            "version": 2,
        },
    }))

    assert changed is False
    assert reducer.visible_text == "base"


def test_missing_identity_cannot_remove_current_part():
    reducer = MessagePartReducer()
    reducer.replace_snapshot(_snapshot(), interaction_run_id="interaction-1")

    assert reducer.apply_event(_event("message.part.removed", {
        "message_id": "message-1",
        "part_id": "part-1",
    })) is False
    assert reducer.visible_text == "base"


def test_snapshot_migration_produces_stable_identity():
    old = [{
        "info": {"id": "legacy-message", "role": "assistant"},
        "parts": [{
            "id": "legacy-part",
            "message_id": "legacy-message",
            "type": "text",
            "text": "legacy",
        }],
    }]
    first = MessagePartReducer()
    second = MessagePartReducer()

    first.replace_snapshot(old)
    second.replace_snapshot(old)

    assert first.interaction_run_id.startswith("legacy-")
    assert first.interaction_run_id == second.interaction_run_id


def test_run_view_restores_multiple_pending_permissions():
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-1",
        "messages": [],
        "pending": {"permissions": [{"id": "p1"}, {"id": "p2"}]},
    })
    assert set(reducer.state.pending_permissions) == {"p1", "p2"}


def test_run_view_restores_multiple_pending_questions():
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-1",
        "messages": [],
        "pending": {"questions": [{"id": "q1"}, {"id": "q2"}]},
    })
    assert set(reducer.state.pending_questions) == {"q1", "q2"}


def test_pending_interaction_resolves_by_request_id():
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-1",
        "messages": [],
        "pending": {"permissions": [{"id": "p1"}, {"id": "p2"}]},
    })
    assert reducer.apply_event(_event("permission.replied", {
        "interaction_run_id": "interaction-1",
        "request_id": "p1",
    })) is True
    assert set(reducer.state.pending_permissions) == {"p2"}


def test_duplicate_pending_event_is_idempotent():
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-1",
        "messages": [],
    })
    asked = _event("question.asked", {
        "interaction_run_id": "interaction-1",
        "id": "q1",
        "questions": [],
    })
    assert reducer.apply_event(asked) is True
    assert reducer.apply_event({**asked, "meta": {**asked["meta"], "event_id": "other", "sequence": 3}}) is False
    assert list(reducer.state.pending_questions) == ["q1"]


def test_terminal_run_clears_pending_interactions():
    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-1",
        "messages": [],
        "pending": {
            "permissions": [{"id": "p1"}],
            "questions": [{"id": "q1"}],
        },
    })
    assert reducer.apply_event(_event("session.run.cancelled", {
        "interaction_run_id": "interaction-1",
    })) is True
    assert reducer.state.pending_permissions == {}
    assert reducer.state.pending_questions == {}
