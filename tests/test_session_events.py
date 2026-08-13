"""Tests for the native instance-local Session event protocol."""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from nz_coder.session_events import (
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
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    assert events[pending_indexes[-1]].properties["part"]["metadata"] == {
        "thoughtSignature": "stream-signature",
    }
    first_assistant = next(message for message in messages if message.get("role") == "assistant")
    tool = next(part for part in first_assistant["_nz_parts"] if part["type"] == "tool")
    assert tool["state"]["status"] == "completed"
    assert tool["call_id"] == "call-live"
    assert tool["metadata"] == {"thoughtSignature": "stream-signature"}

    agent.close()


def test_stream_executes_tool_before_consuming_trailing_usage(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    assert "stream failed after tool result" in assistant["_nz_error"]
    agent.close()


def test_stream_tool_bridge_cancellation_settles_async_handler(tmp_path, monkeypatch):
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder.message_schema import attach_message_identity
    from nz_coder.runtime.loop import AgentLoop
    from nz_coder.runtime.session_processor import SessionProcessor
    from nz_coder.runtime.workdir import scoped_workdir

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
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    from nz_coder import config
    from nz_coder.runtime.loop import AgentLoop

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
    tool = next(part for part in assistant["_nz_parts"] if part["type"] == "tool")
    retry = next(part for part in assistant["_nz_parts"] if part["type"] == "retry")
    assert tool["state"]["status"] == "error"
    assert "temporary connection reset" in tool["state"]["error"]
    assert retry["attempt"] == 1
    assert assistant["content"] == "recovered"

    agent.close()
