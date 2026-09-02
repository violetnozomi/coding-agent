"""Tests for the native instance-local Session event protocol."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from nz_coder.protocol.session_events import (
    EventCursorExpiredError,
    EventSubscriptionGapError,
    SessionEventBus,
    current_session_event_bus,
    encode_sse,
    iter_sse,
    publish_session_event,
    scoped_session_event_bus,
)


def _drain(subscription) -> list:
    events = []
    while True:
        try:
            events.append(subscription.get(timeout=0.01))
        except queue.Empty:
            return events


def test_session_event_envelope_sequence_identity_and_copy():
    bus = SessionEventBus(
        session_id="session-a",
        run_id="run-a",
        agent_id="agent-a",
    )
    subscription = bus.subscribe()
    properties = {"nested": {"value": 1}}

    first = bus.publish("session.run.started", properties)
    properties["nested"]["value"] = 2
    second = bus.publish("session.run.completed", {"status": "completed"})

    assert first.sequence == 1
    assert second.sequence == 2
    assert first.properties == {"nested": {"value": 1}}
    assert first.to_dict()["meta"] == {
        "schema_version": 1,
        "event_id": first.event_id,
        "sequence": 1,
        "timestamp": first.timestamp,
        "session_id": "session-a",
        "run_id": "run-a",
        "interaction_run_id": "run-a",
        "agent_id": "agent-a",
    }
    assert subscription.get(timeout=0.1) == first
    assert subscription.get(timeout=0.1) == second


def test_session_event_filter_replay_and_bounded_gap():
    bus = SessionEventBus(replay_capacity=5)
    bus.publish("session.message.completed", {"index": 1})
    bus.publish("session.tool.completed", {"index": 2})
    filtered = bus.subscribe(
        {"session.tool.completed"},
        max_queue=1,
        replay=5,
    )

    bus.publish("session.message.completed", {"index": 3})
    bus.publish("session.tool.completed", {"index": 4})

    with pytest.raises(EventSubscriptionGapError) as error:
        filtered.get(timeout=0.1)
    assert error.value.to_dict() == {
        "reason": "subscriber_queue_overflow",
        "dropped_events": 2,
        "latest_sequence": 4,
        "resume_required": True,
    }
    assert filtered.dropped_events == 2


def test_session_event_cursor_replays_strictly_after_and_expires():
    bus = SessionEventBus(replay_capacity=2)
    expired = bus.publish("session.message.completed", {"index": 1})
    first = bus.publish("session.message.completed", {"index": 2})
    second = bus.publish("session.message.completed", {"index": 3})

    resumed = bus.subscribe(after_event_id=first.event_id)
    assert resumed.get(timeout=0.1) == second
    with pytest.raises(queue.Empty):
        resumed.get(timeout=0.01)
    with pytest.raises(EventCursorExpiredError):
        bus.subscribe(after_event_id=expired.event_id)
    with pytest.raises(ValueError, match="cannot be combined"):
        bus.subscribe(replay=1, after_event_id=second.event_id)


def test_session_event_recent_zero_does_not_expand_to_full_history():
    """Python's ``[-0:]`` edge must not turn a zero limit into unbounded replay."""
    bus = SessionEventBus(replay_capacity=4)
    bus.publish("session.worker.event", {"index": 1})
    bus.publish("session.worker.event", {"index": 2})

    assert bus.recent(0) == []
    assert bus.recent(-1) == []
    assert len(bus.recent(1)) == 1


@pytest.mark.parametrize("capacity", [-1, True, 1.5, float("inf")])
def test_session_event_bus_rejects_invalid_replay_capacity(capacity):
    """A malformed capacity must fail before deque/journal allocation."""
    with pytest.raises(ValueError, match="non-negative integer"):
        SessionEventBus(replay_capacity=capacity)


@pytest.mark.parametrize("max_queue", [0, -1, True, 1.5, float("inf")])
def test_session_subscription_rejects_invalid_queue_capacity(max_queue):
    """Subscriber queues stay explicitly bounded by a positive integer."""
    bus = SessionEventBus()

    with pytest.raises(ValueError, match="positive integer"):
        bus.subscribe(max_queue=max_queue)


@pytest.mark.parametrize("limit", [True, 1.5, float("inf")])
def test_session_event_recent_rejects_non_integer_limit(limit):
    """Public inspection cannot leak raw int conversion exceptions."""
    bus = SessionEventBus()

    with pytest.raises(ValueError, match="integer"):
        bus.recent(limit)


@pytest.mark.parametrize("heartbeat", [0, -1, float("inf"), float("nan")])
def test_session_event_sse_rejects_invalid_heartbeat(heartbeat):
    """Invalid heartbeat values must not turn the stream into a busy loop."""
    bus = SessionEventBus()
    stream = iter_sse(bus.subscribe(), heartbeat_seconds=heartbeat)

    assert "server.connected" in next(stream)
    with pytest.raises(ValueError, match="positive finite"):
        next(stream)


@pytest.mark.parametrize("timeout", [-1, True, float("inf"), float("nan")])
def test_session_subscription_rejects_invalid_wait_timeout(timeout):
    subscription = SessionEventBus().subscribe()

    with pytest.raises(ValueError, match="timeout"):
        subscription.get(timeout=timeout)


def test_unserializable_journal_properties_do_not_break_live_event_delivery(tmp_path):
    """Best-effort replay persistence must not crash the running Agent."""
    bus = SessionEventBus(
        session_id="session",
        replay_capacity=4,
        journal_path=tmp_path / "events.jsonl",
    )
    cyclic = {}
    cyclic["self"] = cyclic

    event = bus.publish("session.test", {"cyclic": cyclic})

    assert event.sequence == 1
    assert bus.recent(1)[0].event_id == event.event_id
    bus.close()


def test_uncopyable_event_property_isolated_from_live_agent():
    """Extension metadata cannot abort fan-out merely by rejecting deepcopy."""
    bus = SessionEventBus(replay_capacity=4)

    class Uncopyable:
        def __deepcopy__(self, _memo):
            raise RuntimeError("cannot copy")

        def __str__(self):
            return "uncopyable-value"

    event = bus.publish("session.test", {"value": Uncopyable()})

    assert event.properties == {"value": "uncopyable-value"}


def test_event_properties_are_strict_json_at_live_journal_and_sse_boundaries(
    tmp_path,
):
    """Provider/tool NaN values must not poison attach replay or SSE clients."""
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="strict-json",
        replay_capacity=4,
        journal_path=journal,
    )

    event = bus.publish("session.test", {
        "usage": [float("nan"), float("inf"), -float("inf")],
    })
    bus.close()

    assert event.properties == {"usage": [None, None, None]}
    json.dumps(event.to_dict(), allow_nan=False)
    payload = encode_sse(event).split("data: ", 1)[1].strip()
    json.loads(payload, parse_constant=lambda value: (_ for _ in ()).throw(
        ValueError(value)
    ))
    for line in journal.read_text(encoding="utf-8").splitlines():
        json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(value)
        ))


@pytest.mark.parametrize("replay", [-1, True, 1.5])
def test_session_subscription_rejects_invalid_replay_limit(replay):
    """Invalid replay values must not become an accidental full-history slice."""
    bus = SessionEventBus(replay_capacity=4)
    bus.publish("session.worker.event", {"index": 1})

    with pytest.raises(ValueError, match="non-negative integer"):
        bus.subscribe(replay=replay)


def test_session_event_journal_restores_sequence_and_cursor(tmp_path):
    journal = tmp_path / "events.jsonl"
    first_bus = SessionEventBus(
        session_id="persistent-session",
        replay_capacity=4,
        journal_path=journal,
    )
    first = first_bus.publish("session.run.started", {})
    second = first_bus.publish("session.run.completed", {"status": "completed"})
    first_bus.close()

    restored = SessionEventBus(
        session_id="persistent-session",
        replay_capacity=4,
        journal_path=journal,
    )
    replay = restored.subscribe(after_event_id=first.event_id)
    assert replay.get(timeout=0.1) == second
    assert replay.get(timeout=0.1).type == "session.disposed"
    live = restored.publish("session.run.started", {"restored": True})
    assert live.sequence == 4
    assert live.event_id != first.event_id
    restored.close()


def test_session_event_journal_compacts_to_bounded_tail(tmp_path):
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="compact-session",
        replay_capacity=4,
        journal_path=journal,
    )
    expired = None
    for index in range(260):
        event = bus.publish("session.worker.event", {"index": index})
        if index == 0:
            expired = event
    bus.close()

    assert len(journal.read_text(encoding="utf-8").splitlines()) < 20
    restored = SessionEventBus(
        session_id="compact-session",
        replay_capacity=4,
        journal_path=journal,
    )
    assert [event.sequence for event in restored.recent(4)] == [258, 259, 260, 261]
    with pytest.raises(EventCursorExpiredError):
        restored.subscribe(after_event_id=expired.event_id)
    assert restored.publish("session.worker.event", {}).sequence == 262
    restored.close()


def test_session_event_journal_replays_only_contiguous_suffix_after_gap(tmp_path):
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="gap-session",
        replay_capacity=8,
        journal_path=journal,
    )
    first = bus.publish("session.worker.event", {"index": 1})
    bus.publish("session.worker.event", {"index": 2})
    third = bus.publish("session.worker.event", {"index": 3})
    bus.close()

    records = [
        json.loads(line)
        for line in journal.read_text(encoding="utf-8").splitlines()
    ]
    journal.write_text(
        "\n".join(
            json.dumps(record)
            for record in records
            if record["meta"]["sequence"] != 2
        )
        + "\n",
        encoding="utf-8",
    )

    restored = SessionEventBus(
        session_id="gap-session",
        replay_capacity=8,
        journal_path=journal,
    )
    assert [event.sequence for event in restored.recent(8)] == [3, 4]
    with pytest.raises(EventCursorExpiredError):
        restored.subscribe(after_event_id=first.event_id)
    replay = restored.subscribe(after_event_id=third.event_id)
    assert replay.get(timeout=0.1).sequence == 4
    assert restored.publish("session.worker.event", {}).sequence == 5
    restored.close()


def test_session_event_journal_corruption_invalidates_earlier_cursor(tmp_path):
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="corrupt-session",
        replay_capacity=8,
        journal_path=journal,
    )
    first = bus.publish("session.worker.event", {"index": 1})
    second = bus.publish("session.worker.event", {"index": 2})
    bus.close()

    lines = journal.read_text(encoding="utf-8").splitlines()
    journal.write_text(
        "\n".join([lines[0], "not-json", *lines[1:]]) + "\n",
        encoding="utf-8",
    )

    restored = SessionEventBus(
        session_id="corrupt-session",
        replay_capacity=8,
        journal_path=journal,
    )
    with pytest.raises(EventCursorExpiredError):
        restored.subscribe(after_event_id=first.event_id)
    replay = restored.subscribe(after_event_id=second.event_id)
    assert replay.get(timeout=0.1).type == "session.disposed"
    assert restored.publish("session.worker.event", {}).sequence == 4
    restored.close()


def test_session_event_checkpoint_is_atomic_with_later_publish():
    bus = SessionEventBus(session_id="checkpoint-session")
    capture_entered = threading.Event()
    capture_release = threading.Event()

    def capture():
        capture_entered.set()
        assert capture_release.wait(timeout=2)
        return {"messages": ["stable"]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        checkpoint = pool.submit(
            bus.checkpoint,
            capture,
            event_type="session.snapshot.created",
            properties={"snapshot_id": "snap-test"},
        )
        assert capture_entered.wait(timeout=1)
        published = pool.submit(
            bus.publish,
            "session.run.started",
            {"after": True},
        )
        assert not published.done()
        capture_release.set()
        snapshot, cursor = checkpoint.result(timeout=2)
        later = published.result(timeout=2)

    assert snapshot == {"messages": ["stable"]}
    assert cursor.type == "session.snapshot.created"
    assert later.sequence == cursor.sequence + 1
    resumed = bus.subscribe(after_event_id=cursor.event_id)
    assert resumed.get(timeout=0.1) == later
    bus.close()


def test_checkpoint_with_replay_publishes_one_cursor_event():
    """Remote attach must not emit the same snapshot boundary twice."""
    bus = SessionEventBus(session_id="attach-checkpoint")
    subscription = bus.subscribe()
    try:
        snapshot, cursor, replay = bus.checkpoint_with_replay(
            lambda: {"messages": ["stable"]},
            event_type="session.snapshot.created",
            replay=10,
        )

        assert snapshot == {"messages": ["stable"]}
        assert replay == []
        assert subscription.get(timeout=0) == cursor
        with pytest.raises(queue.Empty):
            subscription.get(timeout=0)
    finally:
        bus.close()


def test_session_event_context_scope_restores_parent_bus():
    parent = SessionEventBus(session_id="parent")
    child = SessionEventBus(session_id="child")
    parent_sub = parent.subscribe()
    child_sub = child.subscribe()

    assert current_session_event_bus() is None
    with scoped_session_event_bus(parent):
        publish_session_event("session.message.completed", {"scope": "parent"})
        with scoped_session_event_bus(child):
            publish_session_event("session.message.completed", {"scope": "child"})
        assert current_session_event_bus() is parent
    assert current_session_event_bus() is None

    assert parent_sub.get(timeout=0.1).properties["scope"] == "parent"
    assert child_sub.get(timeout=0.1).properties["scope"] == "child"


def test_tool_started_event_classifies_serial_readonly_extension_as_read(
    monkeypatch,
):
    """Public event categories describe effects, not scheduler parallelism."""
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution import tool_executor
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )

    name = "_test_serial_read_event"
    bus = SessionEventBus(session_id="serial-read-event")
    subscription = bus.subscribe({"session.tool.started"})
    try:
        register(
            name,
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="serial",
            side_effect="readonly",
        )
        monkeypatch.setattr(tool_executor, "dispatch", lambda _name, _input: "ok")
        with scoped_session_event_bus(bus):
            tool_executor.ToolExecutor(PermissionManager("auto")).execute_one({
                "id": "serial-read",
                "function": {"name": name, "arguments": "{}"},
            }, 0)

        assert subscription.get(timeout=0).properties["category"] == "read"
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]
        bus.close()


def test_tool_completed_event_classifies_serial_readonly_extension_as_read():
    """Completed and started event projections must use the same semantics."""
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.tools import (
        TOOL_EXECUTION_MODES,
        TOOL_HANDLERS,
        TOOL_PLAN_MODE_ALLOWED,
        TOOL_SIDE_EFFECTS,
        TOOL_SPECS,
        register,
    )

    name = "_test_serial_read_completed"
    events = []
    try:
        register(
            name,
            "test",
            {"type": "object", "properties": {}},
            lambda: "ok",
            execution="serial",
            side_effect="readonly",
        )
        agent = AgentLoop.__new__(AgentLoop)
        agent.tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)
        agent._emit_session_event = lambda event, properties: events.append(
            (event, properties)
        )
        result = SimpleNamespace(
            name=name,
            executed=True,
            dispatch_failed=False,
            command_failed=False,
            is_write=False,
            tool_input={},
            duration_ms=1.0,
            queue_wait_ms=0.0,
        )

        agent._trace_tool_result(result, "ok")

        assert events[0][0] == "session.tool.completed"
        assert events[0][1]["category"] == "read"
    finally:
        TOOL_HANDLERS.pop(name, None)
        TOOL_EXECUTION_MODES.pop(name, None)
        TOOL_SIDE_EFFECTS.pop(name, None)
        TOOL_PLAN_MODE_ALLOWED.pop(name, None)
        TOOL_SPECS[:] = [
            spec for spec in TOOL_SPECS
            if spec["function"]["name"] != name
        ]


@pytest.mark.parametrize(
    "name",
    ["agent_manager", "workflow_run", "send_message", "emit_handoff"],
)
def test_tool_event_category_marks_agent_control_tools_as_agent(name):
    """Child orchestration and communication must render as Agent activity."""
    from nz_coder.runtime.execution.tool_executor import tool_category

    assert tool_category(name) == "agent"


def test_completed_event_keeps_authorized_dynamic_tool_generation():
    """A live MCP refresh must not relabel a call after it has executed."""
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.tools import scoped_dynamic_tool_provider

    generation = {"effect": "reads-network", "execution": "read"}

    def provider():
        return [{
            "name": "mcp_event_generation",
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: "ok",
            "execution": generation["execution"],
            "side_effect": generation["effect"],
        }]

    events = []
    with scoped_dynamic_tool_provider(provider):
        result = ToolExecutor(PermissionManager("default")).execute_one({
            "id": "generation-event",
            "function": {"name": "mcp_event_generation", "arguments": "{}"},
        }, 0)
        generation.update(effect="mutates-network", execution="write")

        agent = AgentLoop.__new__(AgentLoop)
        agent.tracer = SimpleNamespace(log=lambda *_args, **_kwargs: None)
        agent._emit_session_event = lambda event, properties: events.append(
            (event, properties)
        )
        agent._trace_tool_result(result, result.output)

    assert events[0][1]["category"] == "read"


def test_session_event_concurrent_publish_has_unique_order():
    bus = SessionEventBus(replay_capacity=200)
    subscription = bus.subscribe(max_queue=200)

    def publish_batch(worker: int) -> None:
        for index in range(25):
            bus.publish("session.worker.event", {"worker": worker, "index": index})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(publish_batch, range(4)))

    sequences = [event.sequence for event in bus.recent(200)]
    assert sequences == list(range(1, 101))
    delivered = [subscription.get(timeout=0.1).sequence for _ in range(100)]
    assert delivered == list(range(1, 101))


def test_session_event_sse_connected_event_and_heartbeat():
    bus = SessionEventBus(session_id="session-a")
    subscription = bus.subscribe()
    stream = iter_sse(subscription, heartbeat_seconds=0.05)

    connected = json.loads(next(stream).removeprefix("data: "))
    event = bus.publish("session.run.started", {"model": "test"})
    frame = next(stream)
    heartbeat = json.loads(next(stream).removeprefix("data: "))

    assert connected == {"type": "server.connected", "properties": {}}
    assert frame == encode_sse(event)
    assert frame.startswith(f"id: {event.event_id}\n")
    assert heartbeat == {"type": "server.heartbeat", "properties": {}}
    subscription.close()


def test_session_event_sse_reports_queue_gap_without_advancing_cursor():
    bus = SessionEventBus(session_id="session-a")
    subscription = bus.subscribe(max_queue=1)
    stream = iter_sse(subscription)
    assert json.loads(next(stream).removeprefix("data: "))["type"] == (
        "server.connected"
    )

    bus.publish("session.message.completed", {"index": 1})
    bus.publish("session.message.completed", {"index": 2})
    gap_frame = next(stream)
    gap = json.loads(gap_frame.removeprefix("data: "))

    assert not gap_frame.startswith("id:")
    assert gap["type"] == "server.event_gap"
    assert gap["properties"]["resume_required"] is True
    with pytest.raises(StopIteration):
        next(stream)


def test_filtered_gap_survives_bus_close_before_transport_reads_it():
    bus = SessionEventBus(session_id="session-a")
    subscription = bus.subscribe(
        {"session.worker.event"},
        max_queue=1,
    )
    stream = iter_sse(subscription)
    next(stream)
    bus.publish("session.worker.event", {"index": 1})
    bus.publish("session.worker.event", {"index": 2})

    # session.disposed does not match the filter, but shutdown must not replace
    # the already pending continuity failure with a silent close sentinel.
    bus.close()
    gap = json.loads(next(stream).removeprefix("data: "))

    assert gap["type"] == "server.event_gap"
    assert gap["properties"]["latest_sequence"] == 2
    with pytest.raises(StopIteration):
        next(stream)


def test_session_event_bus_close_disposes_subscribers():
    bus = SessionEventBus()
    subscription = bus.subscribe(max_queue=1)

    bus.close()

    assert subscription.get(timeout=0.1).type == "session.disposed"
    with pytest.raises(StopIteration):
        subscription.get(timeout=0.1)
    with pytest.raises(RuntimeError, match="closed"):
        bus.publish("session.run.started", {})


def test_agent_loop_publishes_native_session_lifecycle(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    class Message:
        content = "done"
        tool_calls = []
        reasoning_content = None

    class Completions:
        def create(self, **kwargs):
            return type(
                "Response",
                (),
                {"choices": [type("Choice", (), {"message": Message()})()]},
            )()

    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": Completions()})()},
    )()
    bus = SessionEventBus(session_id="event-session")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=client,
        trace_enabled=False,
        session_id="event-session",
        event_bus=bus,
    )

    messages = [{"role": "user", "content": "finish"}]
    result = asyncio.run(agent.run(messages, stream=False))
    events = _drain(subscription)

    assert result["status"] == "completed"
    assert [event.type for event in events] == [
        "session.run.started",
        "message.part.updated",
        "message.part.updated",
        "message.part.updated",
        "session.message.completed",
        "message.part.updated",
        "message.updated",
        "message.updated",
        "session.run.completed",
    ]
    assert events[1].properties["part"]["type"] == "step-start"
    assert events[2].properties["part"]["type"] == "step-start"
    assert events[2].properties["part"]["snapshot"].startswith("snap-")
    part = events[3].properties["part"]
    assert part["type"] == "text"
    assert part["text"] == "done"
    assert part["id"] == events[4].properties["part_id"]
    assert part["message_id"] == events[4].properties["message_id"]
    assert messages[-1]["_nz_message_id"] == part["message_id"]
    assert events[-2].properties["info"]["finish"] == "stop"
    assert events[-2].properties["message_id"] == part["message_id"]
    assert [item["type"] for item in messages[-1]["_nz_parts"]] == [
        "text", "step-start", "step-finish",
    ]
    step_start = next(
        item for item in messages[-1]["_nz_parts"] if item["type"] == "step-start"
    )
    step_finish = next(
        item for item in messages[-1]["_nz_parts"] if item["type"] == "step-finish"
    )
    assert step_start["snapshot"].startswith("snap-")
    assert step_finish["snapshot"] == step_start["snapshot"]
    assert events[-1].properties["status"] == "completed"
    assert all(event.session_id == "event-session" for event in events)

    agent.close()
    assert subscription.get(timeout=0.1).type == "session.disposed"
    with pytest.raises(StopIteration):
        subscription.get(timeout=0.1)


def test_agent_stream_publishes_incremental_text_part_lifecycle(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content="hello",
                tool_calls=None,
                reasoning_content=None,
            ))],
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=" world",
                tool_calls=None,
                reasoning_content=None,
            ))],
        ),
    ]

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return iter(chunks)

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )
    bus = SessionEventBus(session_id="stream-session")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=client,
        trace_enabled=False,
        session_id="stream-session",
        event_bus=bus,
    )

    result = asyncio.run(
        agent.run([{"role": "user", "content": "finish"}], stream=True)
    )
    events = _drain(subscription)

    assert result["status"] == "completed"
    assert [event.type for event in events] == [
        "session.run.started",
        "message.part.updated",
        "message.part.updated",
        "message.part.delta",
        "message.part.delta",
        "message.part.updated",
        "message.part.updated",
        "session.message.completed",
        "message.part.updated",
        "message.updated",
        "message.updated",
        "session.run.completed",
    ]
    assert events[1].properties["part"]["type"] == "step-start"
    initial, first_delta, second_delta = events[2:5]
    snapshot_update = events[5]
    completed = events[6]
    assert initial.properties["part"]["text"] == ""
    assert first_delta.properties["delta"] == "hello"
    assert second_delta.properties["delta"] == " world"
    assert completed.properties["part"]["text"] == "hello world"
    assert snapshot_update.properties["part"]["type"] == "step-start"
    assert snapshot_update.properties["part"]["snapshot"].startswith("snap-")
    assert events[-2].properties["info"]["finish"] == "stop"
    assert (
        initial.properties["part"]["id"]
        == first_delta.properties["part_id"]
        == second_delta.properties["part_id"]
        == completed.properties["part"]["id"]
    )
    agent.close()


def test_stream_tool_input_is_durable_before_tool_dispatch(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    tool_start = SimpleNamespace(
        index=0,
        id="call-live",
        function=SimpleNamespace(name="list_directory", arguments=""),
        provider_extra={"thoughtSignature": "stream-signature"},
    )
    tool_arguments = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(
            name=None,
            arguments='{"path":".","depth":1}',
        ),
        provider_extra=None,
    )
    responses = [
        [
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[tool_start],
                reasoning_content=None,
            ))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content=None,
                tool_calls=[tool_arguments],
                reasoning_content=None,
            ))]),
        ],
        [SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content="done",
            tool_calls=None,
            reasoning_content=None,
        ))])],
    ]

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return iter(responses.pop(0))

    bus = SessionEventBus(session_id="tool-stream-session")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="tool-stream-session",
        event_bus=bus,
    )
    messages = [{"role": "user", "content": "inspect"}]

    result = asyncio.run(agent.run(messages, stream=True))
    events = _drain(subscription)

    assert result["status"] == "completed"
    pending_indexes = [
        index for index, event in enumerate(events)
        if event.type == "message.part.updated"
        and event.properties.get("part", {}).get("type") == "tool"
        and event.properties["part"].get("state", {}).get("status") == "pending"
    ]
    completed_message_index = next(
        index for index, event in enumerate(events)
        if event.type == "session.message.completed"
        and event.properties.get("tool_calls") == 1
    )
    dispatch_index = next(
        index for index, event in enumerate(events)
        if event.type == "session.tool.started"
    )
    assert pending_indexes
    assert pending_indexes[0] < dispatch_index < completed_message_index
    assert events[pending_indexes[-1]].properties["part"]["state"]["input"] == {
        "path": ".",
        "depth": 1,
    }
    assert "metadata" not in events[pending_indexes[-1]].properties["part"]
    first_assistant = next(
        message for message in messages if message.get("role") == "assistant"
    )
    private_tool_state = first_assistant["tool_calls"][0]["provider_extra"]
    assert private_tool_state["schema"] == "nz.provider_private_state.v1"
    assert private_tool_state["provider_id"] == "openai-compatible"
    assert private_tool_state["payload"] == {
        "thoughtSignature": "stream-signature",
    }
    first_assistant = next(message for message in messages if message.get("role") == "assistant")
    tool = next(part for part in first_assistant["_nz_parts"] if part["type"] == "tool")
    assert tool["state"]["status"] == "completed"
    assert tool["call_id"] == "call-live"
    assert tool["_nz_provider_metadata"] == {
        "thoughtSignature": "stream-signature",
    }

    agent.close()


def test_stream_executes_tool_before_consuming_trailing_usage(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    order = []
    tool_call = SimpleNamespace(
        index=0,
        id="call-stream-order",
        function=SimpleNamespace(
            name="list_directory",
            arguments='{"path":".","depth":1}',
        ),
        provider_extra=None,
    )

    def first_stream():
        yield SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls",
            delta=SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                reasoning_content=None,
            ),
        )])
        order.append("stream-tail")
        yield SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
            ),
        )

    responses = [
        first_stream(),
        iter([SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            delta=SimpleNamespace(
                content="done",
                tool_calls=None,
                reasoning_content=None,
            ),
        )])]),
    ]

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return responses.pop(0)

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="stream-order-session",
    )
    messages = [{"role": "user", "content": "inspect"}]

    result = asyncio.run(agent.run(
        messages,
        stream=True,
        on_tool=lambda _name, _output: order.append("tool-result"),
    ))

    first_assistant = next(
        message for message in messages
        if message.get("role") == "assistant" and message.get("tool_calls")
    )
    finish = next(
        part for part in first_assistant["_nz_parts"]
        if part["type"] == "step-finish"
    )
    assert result["status"] == "completed"
    assert order.index("tool-result") < order.index("stream-tail")
    assert finish["tokens"] == {"input": 3, "output": 2, "total": 5}
    assert first_assistant["_nz_usage"] == {"input": 3, "output": 2, "total": 5}
    assert len([
        part for part in first_assistant["_nz_parts"]
        if part["type"] == "tool" and part["state"]["status"] == "completed"
    ]) == 1
    agent.close()


def test_stream_error_after_write_tool_does_not_retry_side_effect(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    tool_call = SimpleNamespace(
        index=0,
        id="call-stream-write",
        function=SimpleNamespace(
            name="write_file",
            arguments='{"path":"written.txt","content":"once"}',
        ),
        provider_extra=None,
    )

    def broken_after_tool():
        yield SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls",
            delta=SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                reasoning_content=None,
            ),
        )])
        raise RuntimeError("stream failed after tool result")

    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            return broken_after_tool()

    completions = Completions()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        trace_enabled=False,
        session_id="stream-error-after-tool",
    )
    messages = [{"role": "user", "content": "write once"}]

    result = asyncio.run(agent.run(messages, stream=True))

    assistant = next(message for message in messages if message.get("role") == "assistant")
    tools = [part for part in assistant["_nz_parts"] if part["type"] == "tool"]
    finish = next(part for part in assistant["_nz_parts"] if part["type"] == "step-finish")
    assert result["status"] == "error"
    assert completions.calls == 1
    assert (tmp_path / "written.txt").read_text(encoding="utf-8") == "once"
    assert len(tools) == 1
    assert tools[0]["state"]["status"] == "completed"
    assert finish["reason"] == "error"
    assert assistant["_nz_error"] == "An internal error occurred."
    assert "stream failed after tool result" not in str(assistant)
    agent.close()


def test_stream_tool_bridge_cancellation_settles_async_handler(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    tool_call = SimpleNamespace(
        index=0,
        id="call-stream-cancel",
        function=SimpleNamespace(
            name="list_directory",
            arguments='{"path":"."}',
        ),
        provider_extra=None,
    )

    closed = threading.Event()

    class CompletionStream:
        def __iter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(
                finish_reason="tool_calls",
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[tool_call],
                    reasoning_content=None,
                ),
            )])

        def close(self):
            closed.set()

    class Completions:
        def create(self, **kwargs):
            return CompletionStream()

    async def scenario():
        started = asyncio.Event()
        settled = asyncio.Event()

        async def handler(_result):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                settled.set()

        task = asyncio.create_task(agent._call_llm_async(
            [],
            True,
            stream_tool_handler=handler,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)
        assert settled.is_set()
        assert closed.is_set()

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="stream-bridge-cancel",
    )
    asyncio.run(scenario())
    agent.close()


def test_bash_running_metadata_reaches_durable_session_events(tmp_path):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.session.session_processor import SessionProcessor
    from nz_coder.runtime.process.workdir import scoped_workdir

    bus = SessionEventBus(session_id="bash-progress-session")
    subscription = bus.subscribe()
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": "call-bash-progress",
            "type": "function",
            "function": {
                "name": "bash",
                "arguments": json.dumps({"command": "printf 'hello\\n'"}),
            },
        }],
    }
    attach_message_identity(
        assistant,
        "msg-bash-progress",
        session_id="bash-progress-session",
    )
    processor = SessionProcessor(assistant, publish=bus.publish)
    processor.start_step()
    processor.register_tool_calls(assistant["tool_calls"])
    messages = [{"role": "user", "content": "run"}, assistant]

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test",
            permission_mode="auto",
            client=object(),
            trace_enabled=False,
            session_id="bash-progress-session",
            event_bus=bus,
        )
        agent._execute_tools(
            assistant["tool_calls"],
            messages,
            processor=processor,
        )

    events = _drain(subscription)
    agent.close()
    running = [
        event.properties["part"]
        for event in events
        if event.type == "message.part.updated"
        and event.properties.get("part", {}).get("type") == "tool"
        and event.properties["part"].get("state", {}).get("status") == "running"
        and event.properties["part"]["state"].get("metadata")
    ]
    assert running
    assert running[0]["state"]["metadata"]["workdir"] == str(tmp_path)
    assert any(
        "hello" in part["state"]["metadata"].get("output", "")
        for part in running
    )
    tool = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
    assert tool["state"]["status"] == "completed"
    assert tool["state"]["metadata"]["exit"] == 0
    assert tool["state"]["metadata"]["output"] == "hello"


def test_stream_retry_removes_partial_part_before_new_attempt(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    def chunk(text):
        return SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(
                content=text,
                tool_calls=None,
                reasoning_content=None,
            ))],
        )

    def broken_stream():
        yield chunk("partial")
        raise RuntimeError("connection reset")

    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return broken_stream()
            return iter([chunk("complete")])

    bus = SessionEventBus(session_id="retry-session")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=Completions()),
        ),
        trace_enabled=False,
        session_id="retry-session",
        event_bus=bus,
    )
    monkeypatch.setattr(
        agent,
        "_model_gateway_observer",
        lambda _name, _payload: (_ for _ in ()).throw(
            RuntimeError("trace sink failed")
        ),
    )
    monkeypatch.setattr(agent, "_handle_api_error", lambda _error: True)
    message_part = agent._new_message_part(1)

    result = agent._call_streaming([], message_part=message_part)
    agent._finish_message_part(message_part, result.content or "")
    events = _drain(subscription)

    assert result.content == "complete"
    assert [event.type for event in events] == [
        "message.part.updated",
        "message.part.delta",
        "message.part.removed",
        "message.part.updated",
        "message.part.delta",
        "message.part.updated",
    ]
    first_part = events[1].properties["part_id"]
    second_part = events[4].properties["part_id"]
    assert first_part != second_part
    assert events[2].properties["part_id"] == first_part
    assert events[5].properties["part"]["id"] == second_part
    assert events[1].properties["message_id"] == events[4].properties["message_id"]
    agent.close()


def test_stream_retry_settles_incomplete_tool_part(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop

    tool_start = SimpleNamespace(
        index=0,
        id="call-incomplete",
        function=SimpleNamespace(name="read_file", arguments='{"path":'),
        provider_extra=None,
    )

    def broken_stream():
        yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            tool_calls=[tool_start],
            reasoning_content=None,
        ))])
        raise RuntimeError("temporary connection reset")

    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return broken_stream()
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content="recovered",
                tool_calls=None,
                reasoning_content=None,
            ))])])

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "test",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="incomplete-tool-retry",
    )
    agent.recovery.backoff_base = 0
    messages = [{"role": "user", "content": "inspect"}]

    result = asyncio.run(agent.run(messages, stream=True))

    assert result["status"] == "completed"
    assistant = next(message for message in messages if message.get("role") == "assistant")
    retry = next(part for part in assistant["_nz_parts"] if part["type"] == "retry")
    assert all(part["type"] != "tool" for part in assistant["_nz_parts"])
    assert "call-incomplete" not in repr(messages)
    assert retry["attempt"] == 1
    assert assistant["content"] == "recovered"

    agent.close()


def test_journal_append_close_race_does_not_lose_accepted_event(tmp_path):
    """An accepted event must enter the queue before the close sentinel."""
    from nz_coder.protocol.session_events import (
        SessionEvent,
        _EventJournal,
        _JOURNAL_CLOSED,
    )

    entered = threading.Event()
    release = threading.Event()

    class OrderedQueue:
        def __init__(self) -> None:
            self.items = []

        def put_nowait(self, item) -> None:
            if item is not _JOURNAL_CLOSED:
                entered.set()
                assert release.wait(2)
            self.items.append(item)

        def put(self, item, timeout=None) -> None:
            self.put_nowait(item)

        def qsize(self) -> int:
            return len(self.items)

    class JoinedWorker:
        def join(self, timeout=None) -> None:
            return None

    journal = _EventJournal(tmp_path / "events.jsonl", 8, "session-race")
    ordered = OrderedQueue()
    journal._queue = ordered
    journal._worker = JoinedWorker()
    event = SessionEvent(
        type="session.run.completed",
        properties={"status": "completed"},
        sequence=1,
        timestamp=1.0,
        session_id="session-race",
        run_id="interaction-race",
        agent_id="agent-race",
        event_id="event-race",
    )

    append_thread = threading.Thread(target=journal.append, args=(event,))
    append_thread.start()
    assert entered.wait(1)
    close_thread = threading.Thread(target=journal.close)
    close_thread.start()
    time.sleep(0.02)
    release.set()
    append_thread.join(1)
    close_thread.join(1)

    assert ordered.items == [event, _JOURNAL_CLOSED]


def test_journal_close_is_idempotent(tmp_path):
    from nz_coder.protocol.session_events import _EventJournal

    journal = _EventJournal(tmp_path / "idempotent.jsonl", 4, "session")
    journal.close()
    journal.close()

    assert journal._closing is True


def test_journal_queue_is_bounded(tmp_path):
    from nz_coder.protocol.session_events import _EventJournal

    journal = _EventJournal(tmp_path / "bounded.jsonl", 4, "session")

    assert journal._queue.maxsize > 0
    assert journal._queue.maxsize <= 4096


def test_journal_terminal_event_is_not_dropped(tmp_path):
    from nz_coder.protocol.session_events import SessionEvent, _EventJournal

    journal = _EventJournal(tmp_path / "terminal.jsonl", 1, "session")
    journal._worker = SimpleNamespace(join=lambda timeout=None: None)
    for index in range(journal._ordinary_queue_limit):
        journal._queue.put_nowait(SessionEvent(
            type="message.part.delta",
            properties={"index": index},
            sequence=index + 1,
            timestamp=1.0,
            session_id="session",
            run_id="interaction",
            agent_id="agent",
            event_id=f"event-{index}",
        ))
    terminal = SessionEvent(
        type="session.run.completed",
        properties={"status": "completed"},
        sequence=journal._ordinary_queue_limit + 1,
        timestamp=2.0,
        session_id="session",
        run_id="interaction",
        agent_id="agent",
        event_id="event-terminal",
    )

    assert journal.append(terminal) is True
    assert terminal in list(journal._queue.queue)


def test_session_event_bus_exposes_journal_failure(tmp_path):
    from nz_coder.protocol.session_events import SessionEventBus

    bus = SessionEventBus(
        session_id="session",
        replay_capacity=4,
        journal_path=tmp_path / "events.jsonl",
    )
    expected = RuntimeError("writer unavailable")
    bus._journal._failure = expected

    assert bus.journal_failure is expected
