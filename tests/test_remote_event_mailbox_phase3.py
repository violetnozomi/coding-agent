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


def test_mailbox_preserves_global_sequence_across_critical_and_delta():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=4, critical_reserve=2)
    delta = _delta(10, "FINAL ANSWER")
    terminal = _event("session.run.settled", sequence=11, status="completed")

    assert mailbox.offer(delta)
    assert mailbox.offer(terminal)

    assert mailbox.pop() == delta
    assert mailbox.pop() == terminal


def test_terminal_does_not_overtake_prior_text_delta():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=2, critical_reserve=1)
    assert mailbox.offer(_delta(1, "answer"))
    assert mailbox.offer(_event("session.run.completed", sequence=2))

    assert [mailbox.pop()["type"], mailbox.pop()["type"]] == [
        "message.part.delta",
        "session.run.completed",
    ]


def test_status_event_does_not_overtake_prior_delta():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=3)
    assert mailbox.offer(_delta(1, "A"))
    assert mailbox.offer(_event(
        "message.updated",
        sequence=2,
        info={"id": "message-1", "content": "A"},
    ))

    assert [mailbox.pop()["type"], mailbox.pop()["type"]] == [
        "message.part.delta",
        "message.updated",
    ]


def test_delta_coalescing_stops_at_authoritative_part_barrier():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=4)
    assert mailbox.offer(_delta(1, "A"))
    assert mailbox.offer(_event(
        "message.part.updated",
        sequence=2,
        part={
            "id": "part-1",
            "message_id": "message-1",
            "type": "text",
            "text": "A",
        },
    ))
    assert mailbox.offer(_delta(3, "B"))

    first, barrier, last = mailbox.pop(), mailbox.pop(), mailbox.pop()

    assert first["properties"]["delta"] == "A"
    assert barrier["type"] == "message.part.updated"
    assert last["properties"]["delta"] == "B"


def test_contiguous_delta_merge_records_sequence_window():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=2)
    assert mailbox.offer(_delta(7, "A"))
    assert mailbox.offer(_delta(8, "B"))

    merged = mailbox.pop()

    assert merged["properties"]["delta"] == "AB"
    assert merged["properties"]["from_sequence"] == 7
    assert merged["properties"]["to_sequence"] == 8


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


def test_critical_reserve_exhaustion_is_not_silent():
    from nz_coder.interface.remote_mailbox import RemoteEventMailbox

    mailbox = RemoteEventMailbox(capacity=1, critical_reserve=1)
    assert mailbox.offer(_event("permission.asked", request_id="permission-1"))
    assert mailbox.offer(_event("permission.asked", request_id="permission-2"))

    assert not mailbox.offer(_event("permission.asked", request_id="permission-3"))


def test_terminal_offer_failure_forces_reconnect():
    from nz_coder.interface.remote import _pump_remote_stream
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    loop = SimpleNamespace(call_soon_threadsafe=lambda callback, *_args: callback())
    bridge = RemoteTransportBridge(
        loop,
        capacity=1,
        critical_reserve=1,
        critical_offer_timeout=0,
    )
    bridge.mailbox.offer(_event("permission.asked", request_id="permission-1"))
    bridge.mailbox.offer(_event("permission.asked", request_id="permission-2"))

    _pump_remote_stream(iter([_event("session.run.settled")]), bridge)

    assert bridge.state.closed is True
    assert bridge.state.reconnect_required is True
    assert bridge.state.fatal_error.code == "remote_transport_overflow"


def test_transport_done_uses_out_of_band_control_state():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    async def exercise():
        bridge = RemoteTransportBridge(asyncio.get_running_loop(), capacity=1)
        bridge.close_reader()
        return await bridge.get(timeout=0.1), bridge.snapshot()

    payload, queued = asyncio.run(exercise())
    assert payload == {"_transport_done": True}
    assert queued == []


def test_transport_error_uses_out_of_band_control_state():
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge
    from nz_coder.protocol.public_error import PublicError

    async def exercise():
        bridge = RemoteTransportBridge(asyncio.get_running_loop(), capacity=1)
        bridge.fail_closed(
            PublicError(
                "remote_transport_overflow",
                "The remote event stream requires resynchronization.",
                retryable=True,
            ),
            reconnect_required=True,
        )
        return await bridge.get(timeout=0.1), bridge.snapshot()

    payload, queued = asyncio.run(exercise())
    assert payload["_error"]["code"] == "remote_transport_overflow"
    assert payload["_reconnect_required"] is True
    assert queued == []


def test_critical_offer_failure_stops_sse_reader():
    from nz_coder.interface.remote import _pump_remote_stream
    from nz_coder.interface.remote_mailbox import RemoteTransportBridge

    observed = []

    def stream():
        for item in (
            _event("permission.asked", request_id="permission-3"),
            _event("message.part.delta", sequence=4, delta="must-not-read"),
        ):
            observed.append(item["type"])
            yield item

    loop = SimpleNamespace(call_soon_threadsafe=lambda callback, *_args: callback())
    bridge = RemoteTransportBridge(
        loop,
        capacity=1,
        critical_reserve=1,
        critical_offer_timeout=0,
    )
    bridge.mailbox.offer(_event("permission.asked", request_id="permission-1"))
    bridge.mailbox.offer(_event("permission.asked", request_id="permission-2"))

    _pump_remote_stream(stream(), bridge)

    assert observed == ["permission.asked"]
    assert bridge.state.reader_done is True


def test_permission_burst_beyond_reserve_recovers_from_snapshot():
    calls, _registry = asyncio.run(_exercise_pending_burst("permissions"))
    assert sorted(calls) == [
        ("permission", "permission-1", "allow"),
        ("permission", "permission-2", "allow"),
        ("permission", "permission-3", "allow"),
    ]


def test_question_burst_beyond_reserve_recovers_from_snapshot():
    calls, _registry = asyncio.run(_exercise_pending_burst("questions"))
    assert sorted(calls) == [
        ("question", "question-1", [["answer"]]),
        ("question", "question-2", [["answer"]]),
        ("question", "question-3", [["answer"]]),
    ]


def test_reconnected_snapshot_restores_all_pending_interactions():
    permission_calls, _ = asyncio.run(_exercise_pending_burst("permissions"))
    question_calls, _ = asyncio.run(_exercise_pending_burst("questions"))
    assert len(permission_calls) + len(question_calls) == 6


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


async def _exercise_pending_burst(kind: str):
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
        kind: [
            {
                "id": f"{kind[:-1]}-{number}",
                "permission": "write",
                "questions": [{"question": "Continue?"}],
            }
            for number in range(1, 4)
        ]
    }
    registry = _InteractionTaskRegistry()
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
