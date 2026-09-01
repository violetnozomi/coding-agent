"""Phase-3 contracts for remote interaction and transport backpressure."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace


def _event(event_type: str, *, request_id: str = "", sequence: int = 1, **props):
    properties = dict(props)
    if request_id:
        properties["id"] = request_id
    return {
        "type": event_type,
        "properties": properties,
        "meta": {
            "interaction_run_id": "interaction-1",
            "sequence": sequence,
        },
    }


def _delta(sequence: int, text: str = "x", *, part_id: str = "part-1"):
    return _event(
        "message.part.delta",
        sequence=sequence,
        message_id="message-1",
        part_id=part_id,
        attempt_id="attempt-1",
        generation_id="generation-1",
        delta_sequence=sequence,
        field="text",
        delta=text,
    )


def test_remote_overflow_never_evicts_pending_permission():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=1, critical_reserve=2)
    permission = _event("permission.asked", request_id="permission-1")
    assert mailbox.offer(permission)
    for sequence in range(1, 101):
        mailbox.offer(_delta(sequence))

    assert mailbox.pop() == permission


def test_remote_overflow_never_evicts_pending_question():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=1, critical_reserve=2)
    question = _event("question.asked", request_id="question-1")
    assert mailbox.offer(question)
    for sequence in range(1, 101):
        mailbox.offer(_delta(sequence))

    assert mailbox.pop() == question


def test_remote_overflow_never_evicts_terminal_event():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=1, critical_reserve=2)
    terminal = _event("session.run.completed", sequence=2, status="completed")
    mailbox.offer(_delta(1))
    assert mailbox.offer(terminal)
    for sequence in range(3, 101):
        mailbox.offer(_delta(sequence))

    drained = [mailbox.pop(), mailbox.pop(), mailbox.pop()]
    assert terminal in drained


def test_remote_delta_is_coalesced_by_part_identity():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=1)
    assert mailbox.offer(_delta(1, "hello "))
    assert mailbox.offer(_delta(2, "world"))

    payload = mailbox.pop()
    assert payload["properties"]["delta"] == "hello world"
    assert payload["meta"]["sequence"] == 2
    assert mailbox.pop() is None


def test_remote_overflow_emits_single_gap_marker():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=1)
    for sequence in range(1, 101):
        mailbox.offer(_delta(sequence, part_id=f"part-{sequence}"))

    drained = []
    while (payload := mailbox.pop()) is not None:
        drained.append(payload)
    gaps = [item for item in drained if item["type"] == "server.event_gap"]
    assert len(gaps) == 1
    assert gaps[0]["properties"] == {
        "interaction_run_id": "interaction-1",
        "last_applied_sequence": 0,
        "overflow_reason": "remote_mailbox_overflow",
        "resume_required": True,
    }


def test_remote_transport_bridge_has_bounded_memory():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    loop = SimpleNamespace(call_soon_threadsafe=lambda *_args: None)
    bridge = RemoteTransportBridge(loop, capacity=4, critical_reserve=2)
    for sequence in range(1, 10_001):
        bridge.offer(_delta(sequence, part_id=f"part-{sequence}"))

    assert bridge.buffered_count <= 7  # ordinary + reserve + one gap marker


def test_sse_reader_does_not_schedule_callback_per_delta():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    callbacks = []
    loop = SimpleNamespace(
        call_soon_threadsafe=lambda callback, *args: callbacks.append((callback, args)),
    )
    bridge = RemoteTransportBridge(loop, capacity=4)
    for sequence in range(1, 1_001):
        bridge.offer(_delta(sequence))

    assert len(callbacks) == 1


def test_remote_transport_coalesces_wakeup():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    callbacks = []
    loop = SimpleNamespace(
        call_soon_threadsafe=lambda callback, *args: callbacks.append((callback, args)),
    )
    bridge = RemoteTransportBridge(loop, capacity=4)
    bridge.offer(_delta(1))
    bridge.offer(_event("permission.asked", request_id="permission-1", sequence=2))
    bridge.offer(_event("session.run.completed", sequence=3))

    assert len(callbacks) == 1


def test_remote_transport_preserves_critical_reserve():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    loop = SimpleNamespace(call_soon_threadsafe=lambda *_args: None)
    bridge = RemoteTransportBridge(loop, capacity=1, critical_reserve=2)
    bridge.offer(_delta(1, part_id="ordinary"))
    permission = _event("permission.asked", request_id="permission-1", sequence=2)
    terminal = _event("session.run.completed", sequence=3)

    assert bridge.offer(permission)
    assert bridge.offer(terminal)
    assert permission in bridge.snapshot()
    assert terminal in bridge.snapshot()


def test_remote_transport_survives_slow_renderer():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    loop = SimpleNamespace(call_soon_threadsafe=lambda *_args: None)
    bridge = RemoteTransportBridge(loop, capacity=2, critical_reserve=2)
    for sequence in range(1, 5_001):
        bridge.offer(_delta(sequence, part_id=f"part-{sequence}"))
    terminal = _event("session.run.settled", sequence=5_001, status="completed")

    assert bridge.offer(terminal)
    assert terminal in bridge.snapshot()
    assert bridge.buffered_count <= 5


def test_remote_transport_survives_pending_user_input():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    loop = SimpleNamespace(call_soon_threadsafe=lambda *_args: None)
    bridge = RemoteTransportBridge(loop, capacity=1, critical_reserve=2)
    permission = _event("permission.asked", request_id="permission-1")
    bridge.offer(permission)
    for sequence in range(2, 1_002):
        bridge.offer(_delta(sequence))

    assert permission in bridge.snapshot()


def test_remote_terminal_event_not_starved_by_delta_burst():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    loop = SimpleNamespace(call_soon_threadsafe=lambda *_args: None)
    bridge = RemoteTransportBridge(loop, capacity=1, critical_reserve=2)
    for sequence in range(1, 500):
        bridge.offer(_delta(sequence))
    terminal = _event("session.run.settled", sequence=500, status="completed")
    bridge.offer(terminal)
    for sequence in range(501, 1_000):
        bridge.offer(_delta(sequence))

    assert terminal in bridge.snapshot()


async def _exercise_pending(kind: str):
    from nz_coder.interface.remote import (
        _InteractionTaskRegistry,
        _register_pending_interactions,
    )

    calls = []

    class Backend:
        def reply_permission(self, request_id, reply):
            calls.append(("permission", request_id, reply))
            return True

        def reply_question(self, request_id, reply):
            calls.append(("question", request_id, reply))
            return True

        def reject_question(self, request_id):
            calls.append(("question-rejected", request_id))
            return True

    class Bridge:
        async def _ask_permission(self, *_args):
            return "allow"

        async def _ask_questions(self, _questions):
            return [["answer"]]

    pending = {
        "permissions": [{"id": "permission-1", "permission": "write"}],
        "questions": [{"id": "question-1", "questions": [{"question": "Continue?"}]}],
    }
    registry = _InteractionTaskRegistry()
    _register_pending_interactions(Backend(), pending, Bridge(), registry)
    _register_pending_interactions(Backend(), pending, Bridge(), registry)
    await registry.wait()
    return calls, registry


def test_remote_overflow_snapshot_resumes_pending_permission():
    calls, _registry = asyncio.run(_exercise_pending("permission"))
    assert [item for item in calls if item[0] == "permission"] == [
        ("permission", "permission-1", "allow")
    ]


def test_remote_overflow_snapshot_resumes_pending_question():
    calls, _registry = asyncio.run(_exercise_pending("question"))
    assert [item for item in calls if item[0] == "question"] == [
        ("question", "question-1", [["answer"]])
    ]


def test_same_permission_request_is_prompted_once():
    calls, _registry = asyncio.run(_exercise_pending("permission"))
    assert sum(item[0] == "permission" for item in calls) == 1


def test_same_question_request_is_prompted_once():
    calls, _registry = asyncio.run(_exercise_pending("question"))
    assert sum(item[0] == "question" for item in calls) == 1
