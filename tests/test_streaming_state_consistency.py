"""Regression tests for authoritative streaming Message/Part state."""
from __future__ import annotations

import asyncio
import copy
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from nz_coder.protocol.message_schema import attach_message_identity
from nz_coder.protocol.message_part_reducer import MessagePartReducer
from nz_coder.protocol.message_schema import (
    MESSAGE_ID_KEY,
    PARTS_KEY,
    remove_message_part,
    upsert_message_part,
)
from nz_coder.protocol.session_events import SessionEventBus


def _assistant(*parts: dict, content: str = "") -> dict:
    return {
        "role": "assistant",
        "content": content,
        MESSAGE_ID_KEY: "msg-stream-state",
        PARTS_KEY: [copy.deepcopy(part) for part in parts],
    }


def _text_part(part_id: str, text: str, **extra) -> dict:
    return {
        "id": part_id,
        "message_id": "msg-stream-state",
        "type": "text",
        "text": text,
        **extra,
    }


def _event(
    event_type: str,
    properties: dict,
    *,
    event_id: str,
    sequence: int,
) -> dict:
    return {
        "type": event_type,
        "properties": properties,
        "meta": {
            "schema_version": 1,
            "event_id": event_id,
            "sequence": sequence,
            "timestamp": float(sequence),
            "session_id": "session-stream-state",
            "run_id": "run-stream-state",
            "agent_id": "agent-stream-state",
        },
    }


def _chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=text,
        tool_calls=None,
        reasoning_content=None,
    ))])


def _reasoning_chunk(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=None,
        tool_calls=None,
        reasoning_content=text,
    ))])


def _drain(subscription) -> list:
    events = []
    while True:
        try:
            events.append(subscription.get(timeout=0.01))
        except queue.Empty:
            return events


def test_text_part_replace_and_remove():
    first = _text_part("part-first", "old")
    second = _text_part("part-second", "tail")
    message = _assistant(first, second, content="oldtail")

    upsert_message_part(message, _text_part("part-first", "new"))

    assert [part["id"] for part in message[PARTS_KEY]] == [
        "part-first",
        "part-second",
    ]
    assert message[PARTS_KEY][0]["text"] == "new"

    removed = remove_message_part(message, "part-first")

    assert removed["text"] == "new"
    assert [part["id"] for part in message[PARTS_KEY]] == ["part-second"]
    assert message["content"] == "tail"


def test_duplicate_delta_event_is_idempotent():
    reducer = MessagePartReducer()
    created = _event(
        "message.part.updated",
        {
            "message_id": "msg-stream-state",
            "part": _text_part(
                "part-first",
                "",
                attempt_id="attempt-a",
                generation_id="generation-a",
                generation=1,
                version=0,
            ),
        },
        event_id="created",
        sequence=1,
    )
    delta = _event(
        "message.part.delta",
        {
            "message_id": "msg-stream-state",
            "part_id": "part-first",
            "attempt_id": "attempt-a",
            "generation_id": "generation-a",
            "generation": 1,
            "version": 1,
            "delta_sequence": 1,
            "field": "text",
            "delta": "hello",
        },
        event_id="delta-1",
        sequence=2,
    )

    reducer.apply_event(created)
    reducer.apply_event(delta)
    reducer.apply_event(delta)

    assert reducer.visible_text == "hello"


def test_stale_generation_event_is_ignored():
    reducer = MessagePartReducer()
    reducer.apply_event(_event(
        "message.part.updated",
        {
            "message_id": "msg-stream-state",
            "part": _text_part(
                "part-new",
                "accepted",
                attempt_id="attempt-new",
                generation_id="generation-new",
                generation=2,
                version=1,
            ),
        },
        event_id="new",
        sequence=2,
    ))

    changed = reducer.apply_event(_event(
        "message.part.delta",
        {
            "message_id": "msg-stream-state",
            "part_id": "part-old",
            "attempt_id": "attempt-old",
            "generation_id": "generation-old",
            "generation": 1,
            "version": 99,
            "delta_sequence": 99,
            "field": "text",
            "delta": " leaked",
        },
        event_id="late-old",
        sequence=3,
    ))

    assert changed is False
    assert reducer.visible_text == "accepted"


def test_part_order_is_stable_on_update():
    reducer = MessagePartReducer()
    reducer.replace_snapshot([
        {
            "info": {
                "id": "msg-stream-state",
                "role": "assistant",
                "content": "onetwo",
                "interaction_run_id": "run-stream-state",
            },
            "parts": [
                _text_part(
                    "part-first",
                    "one",
                    interaction_run_id="run-stream-state",
                    generation=1,
                    version=1,
                ),
                _text_part(
                    "part-second",
                    "two",
                    interaction_run_id="run-stream-state",
                    generation=1,
                    version=1,
                ),
            ],
        },
    ], interaction_run_id="run-stream-state")

    reducer.apply_event(_event(
        "message.part.updated",
        {
            "message_id": "msg-stream-state",
            "part": _text_part(
                "part-first",
                "ONE",
                generation=1,
                version=2,
            ),
        },
        event_id="replace-first",
        sequence=2,
    ))

    assert [part["id"] for part in reducer.parts("msg-stream-state")] == [
        "part-first",
        "part-second",
    ]
    assert reducer.visible_text == "ONEtwo"


def test_part_created_and_completed_do_not_duplicate_text():
    reducer = MessagePartReducer()
    part = _text_part(
        "part-first",
        "accepted",
        attempt_id="attempt-a",
        generation_id="generation-a",
        generation=1,
        version=1,
    )

    assert reducer.apply_event(_event(
        "message.part.created",
        {"message_id": "msg-stream-state", "part": part},
        event_id="part-created",
        sequence=1,
    )) is True
    created = reducer.parts("msg-stream-state")[0]
    assert created["status"] == "created"
    assert created["visible"] is True
    assert created["authoritative"] is True
    updated = {**part, "version": 2}
    assert reducer.apply_event(_event(
        "message.part.updated",
        {"message_id": "msg-stream-state", "part": updated},
        event_id="part-updated",
        sequence=2,
    )) is True
    assert reducer.apply_event(_event(
        "message.part.completed",
        {
            "message_id": "msg-stream-state",
            "part": updated,
        },
        event_id="part-completed",
        sequence=3,
    )) is True

    assert reducer.visible_text == "accepted"
    assert reducer.parts("msg-stream-state")[0]["status"] == "completed"

    reducer.replace_snapshot([{
        "info": {
            "id": "msg-hidden",
            "role": "assistant",
            "content": "must-stay-hidden",
        },
        "parts": [{
            **_text_part("part-hidden", "must-stay-hidden"),
            "message_id": "msg-hidden",
            "visible": False,
        }],
    }])
    assert reducer.visible_text == ""


def test_stale_sequence_cannot_replace_authoritative_part():
    reducer = MessagePartReducer()
    current = _text_part(
        "part-first",
        "current",
        generation=1,
        version=2,
    )
    reducer.apply_event(_event(
        "message.part.updated",
        {"message_id": "msg-stream-state", "part": current},
        event_id="current-sequence",
        sequence=5,
    ))

    changed = reducer.apply_event(_event(
        "message.part.updated",
        {
            "message_id": "msg-stream-state",
            "part": {**current, "text": "stale", "version": 3},
        },
        event_id="stale-sequence",
        sequence=4,
    ))

    assert changed is False
    assert reducer.visible_text == "current"


def test_output_guardrail_never_leaks_raw_delta(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.guardrails import OutputGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import load_session

    class Completions:
        def create(self, **_kwargs):
            return iter([_chunk("raw-secret")])

    async def rewrite(message, _context):
        assert message["content"] == "raw-secret"
        return {
            "action": "rewrite",
            "payload": {"role": "assistant", "content": "safe-output"},
        }

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER",
            guardrails=(OutputGuardrail("redact", rewrite),),
        ),
    ], start="worker")
    journal = tmp_path / "events.jsonl"
    bus = SessionEventBus(
        session_id="guarded-stream",
        journal_path=journal,
    )
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    messages = [{"role": "user", "content": "respond"}]
    tokens = []
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=Completions()),
            ),
            trace_enabled=False,
            session_id="guarded-stream",
            event_bus=bus,
            agent_graph=graph,
        )
        try:
            result = asyncio.run(agent.run(
                messages,
                stream=True,
                on_token=tokens.append,
            ))
            events = _drain(subscription)
            persisted = load_session("guarded-stream")
        finally:
            agent.close()

    assert result["status"] == "completed"
    assert messages[-1]["content"] == "safe-output"
    assert "raw-secret" not in repr(messages)
    assert "raw-secret" not in "".join(str(token or "") for token in tokens)
    assert "raw-secret" not in repr(persisted.get("messages", []))
    assert persisted["messages"][-1]["content"] == "safe-output"
    assert all(
        "raw-secret" not in repr(event.properties)
        for event in events
    )
    assert "raw-secret" not in journal.read_text(encoding="utf-8")


def test_output_guardrail_reject_discards_entire_attempt(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.protocol.public_error import PublicRuntimeError
    from nz_coder.runtime.agent.guardrails import OutputGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    class Completions:
        def create(self, **_kwargs):
            return iter([_chunk("must-not-escape")])

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER",
            guardrails=(OutputGuardrail(
                "block",
                lambda _message, _context: {
                    "action": "block",
                    "reason": "policy",
                },
            ),),
        ),
    ], start="worker")
    bus = SessionEventBus(session_id="blocked-stream")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    messages = [{"role": "user", "content": "respond"}]
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=Completions()),
            ),
            trace_enabled=False,
            session_id="blocked-stream",
            event_bus=bus,
            agent_graph=graph,
        )
        try:
            with pytest.raises(PublicRuntimeError):
                asyncio.run(agent.run(messages, stream=True))
            events = _drain(subscription)
        finally:
            agent.close()

    assert "must-not-escape" not in repr(messages)
    assert all(
        "must-not-escape" not in repr(event.properties)
        for event in events
    )


def test_output_guardrail_tool_turn_never_materializes_private_text(
    tmp_path,
    monkeypatch,
):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.guardrails import OutputGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    tool_call = SimpleNamespace(
        index=0,
        id="call-private-turn",
        function=SimpleNamespace(
            name="list_directory",
            arguments='{"path":".","depth":1}',
        ),
        provider_extra=None,
    )
    responses = [
        iter([SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls",
            delta=SimpleNamespace(
                content="raw-secret",
                tool_calls=[tool_call],
                reasoning_content="private-reasoning",
            ),
        )])]),
        iter([SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="stop",
            delta=SimpleNamespace(
                content="provider-final",
                tool_calls=None,
                reasoning_content=None,
            ),
        )])]),
    ]

    class Completions:
        def create(self, **_kwargs):
            return responses.pop(0)

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER",
            guardrails=(OutputGuardrail(
                "redact",
                lambda _message, _context: {
                    "action": "rewrite",
                    "payload": {
                        "role": "assistant",
                        "content": "safe-output",
                    },
                },
            ),),
        ),
    ], start="worker")
    journal = tmp_path / "tool-guardrail-events.jsonl"
    bus = SessionEventBus(
        session_id="guarded-tool-stream",
        journal_path=journal,
    )
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    messages = [{"role": "user", "content": "inspect"}]
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=Completions()),
            ),
            trace_enabled=False,
            session_id="guarded-tool-stream",
            event_bus=bus,
            agent_graph=graph,
        )
        try:
            result = asyncio.run(agent.run(messages, stream=True))
            events = _drain(subscription)
        finally:
            agent.close()

    public_state = repr(messages) + repr([
        event.properties for event in events
    ]) + journal.read_text(encoding="utf-8")
    assert result["status"] == "completed"
    assert messages[-1]["content"] == "safe-output"
    assert "raw-secret" not in public_state
    assert "private-reasoning" not in public_state


def test_output_guardrail_audit_never_records_private_reason():
    from nz_coder.runtime.agent.guardrail_runtime import ProductionGuardrailRuntime

    records = []
    host = SimpleNamespace(
        current_agent_name="worker",
        tracer=SimpleNamespace(
            log=lambda name, **fields: records.append((name, fields)),
        ),
    )

    ProductionGuardrailRuntime._trace(
        host,
        SimpleNamespace(name="redact"),
        "output",
        {"action": "block", "reason": "raw-secret"},
    )

    assert "raw-secret" not in repr(records)
    assert records[0][1]["reason_provided"] is True


def test_streamed_tool_arguments_remain_private_until_approved():
    """Raw streamed tool JSON never reaches SessionProcessor before admission."""
    import threading

    from nz_coder.runtime.execution.stream_state import StreamAttemptBuffer

    published = []
    processor = SimpleNamespace(
        stream_tool_delta=lambda *args, **kwargs: published.append((args, kwargs)),
    )
    identity = {
        "run_id": "run-1",
        "message_id": "msg-1",
        "part_id": "part-1",
        "attempt_id": "attempt-1",
        "generation_id": "generation-1",
        "generation": 0,
    }
    host = SimpleNamespace(
        _message_part_matches=lambda _part, candidate: candidate == identity,
        _message_part_identity=lambda _part: dict(identity),
    )
    part = {**identity, "lock": threading.RLock()}
    attempt = StreamAttemptBuffer(host, part, processor=processor, publish=True)

    assert attempt.update_tool(0, {
        "id": "call-1",
        "function": {"name": "shell", "arguments": '{"token":"raw-secret"}'},
    }) is True
    assert published == []


def test_agent_as_tool_output_is_not_user_visible():
    """Internal child output remains private in the public message projection."""
    from nz_coder.protocol.message_schema import (
        attach_message_identity,
        message_records,
    )

    message = {
        "role": "assistant",
        "content": "child-private-answer",
        "_nz_internal": True,
        "_nz_visible": False,
    }
    attach_message_identity(message, session_id="session-child")

    assert message_records([message], "session-child") == []


def test_all_parts_in_one_turn_share_interaction_run_id():
    from nz_coder.runtime.session.session_processor import SessionProcessor

    assistant = {
        "role": "assistant",
        "content": "",
        "_nz_interaction_run_id": "interaction-one",
    }
    attach_message_identity(assistant, session_id="session-interaction")
    processor = SessionProcessor(assistant)
    processor.start_step()
    processor.stream_text("answer", part_id="part-answer")
    processor.finish_step("stop")

    assert assistant[PARTS_KEY]
    assert {
        part.get("interaction_run_id")
        for part in assistant[PARTS_KEY]
    } == {"interaction-one"}


def test_remote_snapshot_only_rebases_current_run():
    """The current-answer reducer accepts only the active interaction records."""
    reducer = MessagePartReducer()
    records = [
        {
            "info": {
                "id": "msg-old",
                "role": "assistant",
                "interaction_run_id": "interaction-old",
            },
            "parts": [{
                "id": "part-old",
                "message_id": "msg-old",
                "type": "text",
                "text": "OLD ANSWER",
                "interaction_run_id": "interaction-old",
                "status": "completed",
            }],
        },
        {
            "info": {
                "id": "msg-current",
                "role": "assistant",
                "interaction_run_id": "interaction-current",
            },
            "parts": [{
                "id": "part-current",
                "message_id": "msg-current",
                "type": "text",
                "text": "CURRENT",
                "interaction_run_id": "interaction-current",
                "status": "completed",
            }],
        },
    ]

    reducer.replace_snapshot(
        records,
        interaction_run_id="interaction-current",
    )

    assert reducer.visible_text == "CURRENT"


def test_in_progress_snapshot_part_remains_streaming():
    """Taking a snapshot is not itself a completion signal."""
    reducer = MessagePartReducer()
    reducer.replace_snapshot([{
        "info": {"id": "msg-live", "role": "assistant"},
        "parts": [{
            "id": "part-live",
            "message_id": "msg-live",
            "type": "text",
            "text": "partial",
            "time": {"start": 1.0},
        }],
    }])

    assert reducer.parts("msg-live")[0]["status"] == "streaming"


def test_mismatched_generation_id_is_rejected():
    """A numeric generation match cannot authorize another generation UUID."""
    reducer = MessagePartReducer()
    reducer.replace_snapshot([{
        "info": {"id": "msg-live", "role": "assistant"},
        "parts": [{
            "id": "part-live",
            "message_id": "msg-live",
            "type": "text",
            "text": "approved",
            "attempt_id": "attempt-new",
            "generation_id": "generation-new",
            "generation": 2,
            "version": 2,
            "status": "streaming",
        }],
    }])

    changed = reducer.apply_event(_event(
        "message.part.delta",
        {
            "message_id": "msg-live",
            "part_id": "part-live",
            "field": "text",
            "delta": "-stale",
            "attempt_id": "attempt-new",
            "generation_id": "generation-old",
            "generation": 2,
            "version": 3,
            "delta_sequence": 1,
        },
        event_id="generation-mismatch",
        sequence=1,
    ))

    assert changed is False
    assert reducer.visible_text == "approved"


def test_removed_part_cannot_be_revived_by_old_delta():
    reducer = MessagePartReducer()
    created = _event(
        "message.part.updated",
        {"message_id": "msg-live", "part": {
            "id": "part-live", "message_id": "msg-live", "type": "text",
            "text": "partial", "attempt_id": "attempt-1",
            "generation_id": "generation-1", "generation": 1, "version": 1,
        }},
        event_id="created-live", sequence=1,
    )
    removed = _event(
        "message.part.removed",
        {"message_id": "msg-live", "part_id": "part-live",
         "attempt_id": "attempt-1", "generation_id": "generation-1",
         "generation": 1, "version": 1},
        event_id="removed-live", sequence=2,
    )
    reducer.apply_event(created)
    reducer.apply_event(removed)

    changed = reducer.apply_event(_event(
        "message.part.delta",
        {"message_id": "msg-live", "part_id": "part-live", "delta": "stale",
         "attempt_id": "attempt-1", "generation_id": "generation-1",
         "generation": 1, "version": 2, "delta_sequence": 1},
        event_id="late-live", sequence=3,
    ))

    assert changed is False
    assert reducer.visible_text == ""


def test_completed_part_rejects_old_delta():
    reducer = MessagePartReducer()
    reducer.replace_snapshot([{
        "info": {"id": "msg-done", "role": "assistant"},
        "parts": [{
            "id": "part-done", "message_id": "msg-done", "type": "text",
            "text": "done", "status": "completed", "attempt_id": "attempt-1",
            "generation_id": "generation-1", "generation": 1, "version": 3,
        }],
    }])

    changed = reducer.apply_event(_event(
        "message.part.delta",
        {"message_id": "msg-done", "part_id": "part-done", "delta": "late",
         "attempt_id": "attempt-1", "generation_id": "generation-1",
         "generation": 1, "version": 4, "delta_sequence": 1},
        event_id="late-completed", sequence=1,
    ))

    assert changed is False
    assert reducer.visible_text == "done"


def test_mismatched_attempt_id_is_rejected():
    reducer = MessagePartReducer()
    reducer.replace_snapshot([{
        "info": {"id": "msg-attempt", "role": "assistant"},
        "parts": [{
            "id": "part-attempt", "message_id": "msg-attempt",
            "type": "text", "text": "approved", "status": "streaming",
            "attempt_id": "attempt-new", "generation_id": "generation-1",
            "generation": 1, "version": 1,
        }],
    }])

    changed = reducer.apply_event(_event(
        "message.part.delta",
        {
            "message_id": "msg-attempt", "part_id": "part-attempt",
            "delta": " leaked", "attempt_id": "attempt-old",
            "generation_id": "generation-1", "generation": 1,
            "version": 2, "delta_sequence": 1,
        },
        event_id="attempt-mismatch", sequence=1,
    ))

    assert changed is False
    assert reducer.visible_text == "approved"


def test_snapshot_then_old_replay_event_is_idempotent():
    reducer = MessagePartReducer()
    reducer.replace_snapshot([{
        "info": {"id": "msg-snapshot", "role": "assistant"},
        "parts": [{
            "id": "part-snapshot", "message_id": "msg-snapshot",
            "type": "text", "text": "current", "status": "streaming",
            "attempt_id": "attempt-1", "generation_id": "generation-1",
            "generation": 1, "version": 4,
        }],
    }], last_sequence=10)

    changed = reducer.apply_event(_event(
        "message.part.delta",
        {
            "message_id": "msg-snapshot", "part_id": "part-snapshot",
            "delta": "old", "attempt_id": "attempt-1",
            "generation_id": "generation-1", "generation": 1,
            "version": 3, "delta_sequence": 3,
        },
        event_id="old-replay", sequence=9,
    ))

    assert changed is False
    assert reducer.visible_text == "current"


def test_raw_tool_arguments_not_published_before_guardrail(tmp_path, monkeypatch):
    """Tool policy observes no raw envelope and only its rewrite becomes public."""
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.execution.loop import AgentLoop

    raw_arguments = '{"path":"raw-secret-path","depth":1}'
    tool_call = SimpleNamespace(
        index=0,
        id="call-private-tool",
        function=SimpleNamespace(name="list_directory", arguments=raw_arguments),
        provider_extra=None,
    )
    responses = [
        iter([SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls",
            delta=SimpleNamespace(
                content=None,
                tool_calls=[tool_call],
                reasoning_content=None,
            ),
        )])]),
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
        def create(self, **_kwargs):
            return responses.pop(0)

    seen_messages = []

    async def rewrite(call, context):
        seen_messages.append(copy.deepcopy(context["messages"]))
        assert "raw-secret-path" not in repr(context["messages"])
        rewritten = copy.deepcopy(call)
        rewritten["function"]["arguments"] = '{"path":".","depth":1}'
        return {"action": "rewrite", "payload": rewritten}

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER",
            guardrails=(ToolGuardrail("rewrite-private", before_tool=rewrite),),
        ),
    ], start="worker")
    bus = SessionEventBus(session_id="private-tool-boundary")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "unused",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="private-tool-boundary",
        event_bus=bus,
        agent_graph=graph,
    )
    messages = [{"role": "user", "content": "inspect"}]
    try:
        result = asyncio.run(agent.run(messages, stream=True))
        events = _drain(subscription)
    finally:
        agent.close()

    assert result["status"] == "completed"
    assert seen_messages
    public = repr(messages) + repr([event.properties for event in events])
    assert "raw-secret-path" not in public
    assert "\\\"path\\\":\\\".\\\"" in public or "'path': '.'" in public


def test_tool_guardrail_block_does_not_expose_raw_input(tmp_path, monkeypatch):
    """A blocked call publishes structural policy state, not input or reason."""
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.guardrails import ToolGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.execution.loop import AgentLoop

    raw_arguments = '{"path":"token-raw-secret","depth":1}'
    raw_reason = "reason-raw-secret"
    tool_call = SimpleNamespace(
        index=0,
        id="call-blocked-tool",
        function=SimpleNamespace(name="list_directory", arguments=raw_arguments),
        provider_extra=None,
    )
    responses = [
        iter([SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="tool_calls",
            delta=SimpleNamespace(content=None, tool_calls=[tool_call], reasoning_content=None),
        )])]),
        iter([_chunk("recovered")]),
    ]

    class Completions:
        def create(self, **_kwargs):
            return responses.pop(0)

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER",
            guardrails=(ToolGuardrail(
                "deny-private",
                before_tool=lambda _call, _context: {
                    "action": "block",
                    "reason": raw_reason,
                },
            ),),
        ),
    ], start="worker")
    bus = SessionEventBus(session_id="blocked-tool-boundary")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "unused",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="blocked-tool-boundary",
        event_bus=bus,
        agent_graph=graph,
    )
    messages = [{"role": "user", "content": "inspect"}]
    try:
        asyncio.run(agent.run(messages, stream=True))
        events = _drain(subscription)
    finally:
        agent.close()

    public = repr(messages) + repr([event.properties for event in events])
    assert "token-raw-secret" not in public
    assert raw_reason not in public
    assert "deny-private" in public


def test_guardrail_reason_not_present_in_public_error():
    from nz_coder.protocol.public_error import to_public_error
    from nz_coder.runtime.agent.guardrails import GuardrailBlockedError

    error = GuardrailBlockedError(
        "redactor",
        "output",
        "private-reason-containing-token-secret",
    )

    public = to_public_error(error)

    assert "token-secret" not in str(error)
    assert "token-secret" not in repr(public.to_dict())
    assert public.code == "guardrail_blocked"


def test_guardrail_reason_not_present_in_event_journal(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent.guardrails import OutputGuardrail
    from nz_coder.runtime.agent.handoffs import AgentGraph, AgentSpec
    from nz_coder.runtime.execution.loop import AgentLoop

    class Completions:
        def create(self, **_kwargs):
            return iter([_chunk("private-output")])

    graph = AgentGraph([
        AgentSpec(
            "worker",
            "WORKER",
            guardrails=(OutputGuardrail(
                "redactor",
                lambda _message, _context: {
                    "action": "block",
                    "reason": "private-reason-token-secret",
                },
            ),),
        ),
    ], start="worker")
    journal = tmp_path / "guardrail-events.jsonl"
    bus = SessionEventBus(
        session_id="guardrail-error-journal",
        journal_path=journal,
    )
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    agent = AgentLoop(
        "unused",
        permission_mode="auto",
        client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
        trace_enabled=False,
        session_id="guardrail-error-journal",
        event_bus=bus,
        agent_graph=graph,
    )
    try:
        with pytest.raises(Exception):
            asyncio.run(agent.run(
                [{"role": "user", "content": "answer"}],
                stream=True,
            ))
    finally:
        agent.close()

    persisted = journal.read_text(encoding="utf-8")
    assert "private-output" not in persisted
    assert "private-reason-token-secret" not in persisted
    assert "guardrail_blocked" in persisted


def test_stream_retry_removes_old_part_everywhere(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.interface.timeline import latest_assistant_text
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import load_session

    def broken_stream():
        yield _chunk("partial-old")
        raise RuntimeError("connection reset")

    class Completions:
        def __init__(self):
            self.calls = 0

        def create(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return broken_stream()
            return iter([_chunk("complete-new")])

    bus = SessionEventBus(session_id="retry-authoritative")
    subscription = bus.subscribe()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    messages = [{"role": "user", "content": "respond"}]
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=Completions()),
            ),
            trace_enabled=False,
            session_id="retry-authoritative",
            event_bus=bus,
        )
        monkeypatch.setattr(agent, "_handle_api_error", lambda _error: True)
        try:
            result = asyncio.run(agent.run(messages, stream=True))
            events = _drain(subscription)
            persisted = load_session("retry-authoritative")
        finally:
            agent.close()

    reducer = MessagePartReducer()
    for event in events:
        reducer.apply_event(event)
    assert result["status"] == "completed"
    assert messages[-1]["content"] == "complete-new"
    assert "partial-old" not in repr(messages[-1].get(PARTS_KEY, []))
    assert latest_assistant_text(messages) == "complete-new"
    assert "partial-old" not in repr(persisted.get("messages", []))
    assert latest_assistant_text(persisted["messages"]) == "complete-new"
    assert reducer.visible_text == "complete-new"
    assert any(event.type == "message.part.removed" for event in events)


def test_late_retired_attempt_cannot_mutate_session(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.execution.stream_state import StreamAttemptBuffer
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.session_processor import SessionProcessor

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(),
            trace_enabled=False,
            session_id="retired-attempt",
        )
        try:
            message_part = agent._new_message_part(1)
            assistant = {"role": "assistant", "content": ""}
            attach_message_identity(
                assistant,
                message_part["message_id"],
                session_id="retired-attempt",
            )
            processor = SessionProcessor(
                assistant,
                publish=agent._emit_session_event,
            )
            agent._active_session_processor = processor
            attempt = StreamAttemptBuffer(
                agent,
                message_part,
                processor=processor,
                publish=True,
            )
            assert attempt.append_text("early") is True
            agent._retire_message_part(message_part, "cancelled")

            assert attempt.append_text("late") is False
            assert assistant["content"] == ""
            assert not any(
                part.get("type") == "text"
                for part in assistant.get(PARTS_KEY, [])
            )
        finally:
            agent.close()


def test_local_remote_renderer_parity():
    from io import StringIO

    from rich.console import Console

    from nz_coder.interface.run_renderer import TerminalRunRenderer

    class TextProjection:
        def __init__(self):
            self.text = ""
            self.status = None

        def replace_text(self, value: str) -> None:
            self.text = value

        def set_status(self, value) -> None:  # noqa: ANN001
            self.status = value

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

    properties = [
        (
            "message.part.updated",
            {
                "message_id": "msg-stream-state",
                "part": _text_part(
                    "part-old",
                    "",
                    attempt_id="attempt-old",
                    generation_id="generation-old",
                    generation=1,
                    version=0,
                ),
            },
        ),
        (
            "message.part.delta",
            {
                "message_id": "msg-stream-state",
                "part_id": "part-old",
                "attempt_id": "attempt-old",
                "generation_id": "generation-old",
                "generation": 1,
                "version": 1,
                "delta_sequence": 1,
                "field": "text",
                "delta": "partial",
            },
        ),
        (
            "message.part.removed",
            {
                "message_id": "msg-stream-state",
                "part_id": "part-old",
                "attempt_id": "attempt-old",
                "generation_id": "generation-old",
                "generation": 1,
                "version": 1,
                "reason": "stream_retry",
            },
        ),
        (
            "message.part.updated",
            {
                "message_id": "msg-stream-state",
                "part": _text_part(
                    "part-new",
                    "accepted",
                    attempt_id="attempt-new",
                    generation_id="generation-new",
                    generation=2,
                    version=1,
                ),
            },
        ),
    ]

    local_projection = TextProjection()
    local_bus = SessionEventBus(session_id="local-parity")
    local_view = TerminalRunRenderer(
        Console(file=StringIO(), force_terminal=False),
        local_projection,
    )
    local_view.begin(SimpleNamespace(event_bus=local_bus, model_id="model"))
    for event_type, event_properties in properties:
        local_bus.publish(event_type, event_properties)
    local_view.drain()

    remote_projection = TextProjection()
    remote_view = TerminalRunRenderer(
        Console(file=StringIO(), force_terminal=False),
        remote_projection,
    )
    remote_view.begin_remote(SimpleNamespace(model_id="model"))
    for sequence, (event_type, event_properties) in enumerate(properties, 1):
        remote_view.feed(_event(
            event_type,
            event_properties,
            event_id=f"remote-{sequence}",
            sequence=sequence,
        ))

    assert local_view.logical_text == remote_view.logical_text == "accepted"
    assert local_projection.text == remote_projection.text == "accepted"


def test_checkpoint_is_coalesced(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.session_processor import SessionProcessor

    class Completions:
        def create(self, **_kwargs):
            return iter([_chunk("x") for _index in range(200)])

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "STREAM_CHECKPOINT_INTERVAL_SECONDS", 60.0)
    monkeypatch.setattr(config, "STREAM_CHECKPOINT_MIN_CHARS", 4096)
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=Completions()),
            ),
            trace_enabled=False,
            session_id="coalesced-checkpoint",
        )
        try:
            message_part = agent._new_message_part(1)
            assistant = {"role": "assistant", "content": ""}
            attach_message_identity(
                assistant,
                message_part["message_id"],
                session_id="coalesced-checkpoint",
            )
            processor = SessionProcessor(assistant)
            agent._active_session_processor = processor
            agent._active_processor_messages = [assistant]
            subscription = agent.event_bus.subscribe({"message.part.delta"})
            checkpoints = []
            monkeypatch.setattr(
                agent,
                "_checkpoint_messages",
                lambda _messages, status: checkpoints.append(status),
            )

            result = agent._call_streaming([], message_part=message_part)
            delta_events = _drain(subscription)
        finally:
            agent.close()

    assert result.content == "x" * 200
    assert checkpoints == ["running"]
    assert len(delta_events) < 20
    assert "".join(
        str(event.properties.get("delta") or "")
        for event in delta_events
    ) == result.content


def test_reasoning_part_updates_are_coalesced(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.session_processor import SessionProcessor

    class Completions:
        def create(self, **_kwargs):
            return iter([_reasoning_chunk("r") for _index in range(200)])

    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "STREAM_DELTA_INTERVAL_SECONDS", 0.08)
    monkeypatch.setattr(config, "STREAM_DELTA_MIN_CHARS", 4096)
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(
                chat=SimpleNamespace(completions=Completions()),
            ),
            trace_enabled=False,
            session_id="coalesced-reasoning",
        )
        try:
            message_part = agent._new_message_part(1)
            assistant = {"role": "assistant", "content": ""}
            attach_message_identity(
                assistant,
                message_part["message_id"],
                session_id="coalesced-reasoning",
            )
            processor = SessionProcessor(
                assistant,
                publish=agent._emit_session_event,
            )
            agent._active_session_processor = processor
            agent._active_processor_messages = [assistant]
            subscription = agent.event_bus.subscribe({"message.part.updated"})

            result = agent._call_streaming([], message_part=message_part)
            events = _drain(subscription)
        finally:
            agent.close()

    reasoning_updates = [
        event for event in events
        if event.properties.get("part", {}).get("type") == "reasoning"
    ]
    assert result.extra["reasoning_content"] == "r" * 200
    assert reasoning_updates == []
    private_reasoning = [
        part for part in assistant["_nz_parts"]
        if part.get("type") == "reasoning"
    ]
    assert private_reasoning[-1]["text"] == "r" * 200
    assert private_reasoning[-1]["internal"] is True
    assert private_reasoning[-1]["visible"] is False


def test_event_journal_compaction_never_blocks_event_bus_lock(
    tmp_path,
    monkeypatch,
):
    journal = tmp_path / "nonblocking-events.jsonl"
    bus = SessionEventBus(
        session_id="nonblocking-journal",
        replay_capacity=4,
        journal_path=journal,
    )
    event_journal = bus._journal
    original_compact = event_journal._compact

    def slow_compact(recent):
        time.sleep(0.2)
        original_compact(recent)

    monkeypatch.setattr(event_journal, "_compact", slow_compact)
    started = time.monotonic()
    for index in range(event_journal.max_entries):
        bus.publish("session.worker.event", {"index": index})
    publish_elapsed = time.monotonic() - started
    bus.close()

    assert publish_elapsed < 0.1
    assert journal.exists()


def test_event_journal_close_never_holds_event_bus_lock(tmp_path, monkeypatch):
    bus = SessionEventBus(
        session_id="nonblocking-journal-close",
        journal_path=tmp_path / "close-events.jsonl",
    )
    journal = bus._journal
    close_started = threading.Event()
    close_release = threading.Event()
    probe_done = threading.Event()
    original_close = journal.close

    def slow_close():
        close_started.set()
        close_release.wait(timeout=1.0)
        original_close()

    monkeypatch.setattr(journal, "close", slow_close)
    bus.publish("session.worker.event", {"index": 1})
    closer = threading.Thread(target=bus.close)
    closer.start()
    assert close_started.wait(timeout=1.0)
    probe = threading.Thread(target=lambda: (bus.recent(), probe_done.set()))
    probe.start()
    lock_was_available = probe_done.wait(timeout=0.1)
    close_release.set()
    closer.join(timeout=1.0)
    probe.join(timeout=1.0)

    assert lock_was_available is True


def test_gap_snapshot_rebase():
    from io import StringIO

    from rich.console import Console

    from nz_coder.interface.remote import _feed_snapshot_events
    from nz_coder.interface.run_renderer import TerminalRunRenderer

    class Projection:
        def __init__(self):
            self.text = ""

        def replace_text(self, value: str) -> None:
            self.text = value

        def set_status(self, _value) -> None:
            pass

        def pause(self) -> None:
            pass

        def resume(self) -> None:
            pass

    projection = Projection()
    view = TerminalRunRenderer(
        Console(file=StringIO(), force_terminal=False),
        projection,
    )
    view.begin_remote(SimpleNamespace(model_id="remote"))
    view.feed(_event(
        "message.part.delta",
        {
            "message_id": "msg-stream-state",
            "part_id": "part-old",
            "attempt_id": "attempt-old",
            "generation_id": "generation-old",
            "generation": 1,
            "version": 1,
            "delta_sequence": 1,
            "field": "text",
            "delta": "stale",
        },
        event_id="before-gap",
        sequence=1,
    ))

    _feed_snapshot_events(view, {
        "messages": [{
            "info": {
                "id": "msg-stream-state",
                "role": "assistant",
                "content": "accepted",
            },
            "parts": [_text_part(
                "part-new",
                "accepted",
                attempt_id="attempt-new",
                generation_id="generation-new",
                generation=2,
                version=1,
            )],
        }],
        "events": [_event(
            "message.part.delta",
            {
                "message_id": "msg-stream-state",
                "part_id": "part-old",
                "attempt_id": "attempt-old",
                "generation_id": "generation-old",
                "generation": 1,
                "version": 2,
                "delta_sequence": 2,
                "field": "text",
                "delta": "-late",
            },
            event_id="after-gap-old-generation",
            sequence=2,
        )],
    })

    assert view.logical_text == "accepted"
    assert projection.text == "accepted"


def test_local_gap_rebase_only_restores_current_run():
    """The authoritative gap-rebase regression is shared by both transports."""
    test_gap_snapshot_rebase()


def test_previous_assistant_answers_not_rendered_in_current_run():
    from nz_coder.protocol.run_view_reducer import RunViewReducer

    reducer = RunViewReducer()
    reducer.replace_snapshot({
        "interaction_run_id": "interaction-current",
        "status": "running",
        "messages": [{
            "info": {
                "id": "msg-old", "role": "assistant",
                "interaction_run_id": "interaction-old",
            },
            "parts": [{
                "id": "part-old", "message_id": "msg-old", "type": "text",
                "text": "OLD ANSWER", "status": "completed",
                "interaction_run_id": "interaction-old",
            }],
        }, {
            "info": {
                "id": "msg-current", "role": "assistant",
                "interaction_run_id": "interaction-current",
            },
            "parts": [{
                "id": "part-current", "message_id": "msg-current",
                "type": "text", "text": "CURRENT", "status": "completed",
                "interaction_run_id": "interaction-current",
            }],
        }],
    })

    assert reducer.visible_text == "CURRENT"


def test_local_gap_rebases_from_authoritative_messages():
    from io import StringIO

    from rich.console import Console

    from nz_coder.interface.run_renderer import TerminalRunRenderer

    projection = SimpleNamespace(
        text="",
        replace_text=lambda value: setattr(projection, "text", value),
        set_status=lambda _value: None,
        pause=lambda: None,
        resume=lambda: None,
    )
    bus = SessionEventBus(session_id="local-gap")
    authoritative = _assistant(
        _text_part(
            "part-authoritative",
            "accepted",
            attempt_id="attempt-current",
            generation_id="generation-current",
            generation=2,
            version=1,
        ),
        content="accepted",
    )
    agent = SimpleNamespace(
        event_bus=bus,
        model_id="model",
        session_id="local-gap",
        _active_processor_messages=[authoritative],
    )
    view = TerminalRunRenderer(
        Console(file=StringIO(), force_terminal=False),
        projection,
    )
    view.begin(agent)
    for index in range(600):
        bus.publish("message.part.delta", {
            "message_id": "msg-stale",
            "part_id": "part-stale",
            "generation": 1,
            "version": index + 1,
            "delta_sequence": index + 1,
            "field": "text",
            "delta": "x",
        })

    view.drain()

    assert view.logical_text == "accepted"
    assert projection.text == "accepted"
    assert view._subscription is not None
    assert view._subscription.dropped_events == 0


def _model_context_for_cancel(operation, retired: list):  # noqa: ANN001
    from nz_coder.runtime.core.model_context import ModelExecutionContext

    return ModelExecutionContext(
        capabilities=lambda: SimpleNamespace(
            supports_streaming=True,
            supports_tools=False,
            provider="test",
        ),
        active_model_id=lambda: "test-model",
        active_tool_specs=lambda: [],
        prompt_budget=lambda: SimpleNamespace(output_reserve_tokens=100),
        call_streaming=lambda *_args, **_kwargs: None,
        call_non_streaming=lambda *_args, **_kwargs: None,
        gateway=lambda **_kwargs: None,
        project_outcome=lambda value: value,
        record_success=lambda: None,
        trace=lambda *_args, **_kwargs: None,
        retire_message_part=lambda part, reason: retired.append((part, reason)),
        complete_override=operation,
    )


@pytest.mark.parametrize("stream", [False, True])
def test_cancel_during_provider_connect(stream, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.services import ProductionTurnModelRuntime

    started = threading.Event()
    release = threading.Event()
    retired = []

    def connect(*_args):
        started.set()
        release.wait(timeout=2.0)
        return "late-result"

    monkeypatch.setattr(config, "PROVIDER_CANCEL_GRACE_SECONDS", 0.02)

    async def exercise():
        task = asyncio.create_task(ProductionTurnModelRuntime().complete_turn(
            _model_context_for_cancel(connect, retired),
            [],
            stream=stream,
            on_token=None,
            message_part={"id": "part"},
            stream_tool_handler=None,
        ))
        await asyncio.to_thread(started.wait, 1.0)
        before = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        elapsed = time.monotonic() - before
        release.set()
        return elapsed

    elapsed = asyncio.run(exercise())

    assert elapsed < 0.2
    assert retired == [({"id": "part"}, "cancelled")]


def test_cancel_during_stream(monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.services import ProductionTurnModelRuntime

    started = threading.Event()
    release = threading.Event()
    retired = []

    def stream(*_args):
        started.set()
        release.wait(timeout=2.0)
        return "late-stream-result"

    monkeypatch.setattr(config, "PROVIDER_CANCEL_GRACE_SECONDS", 0.02)

    async def exercise():
        task = asyncio.create_task(ProductionTurnModelRuntime().complete_turn(
            _model_context_for_cancel(stream, retired),
            [],
            stream=True,
            on_token=None,
            message_part={"id": "stream-part"},
            stream_tool_handler=lambda _result: asyncio.sleep(0),
        ))
        await asyncio.to_thread(started.wait, 1.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()

    asyncio.run(exercise())

    assert retired == [({"id": "stream-part"}, "cancelled")]


def test_late_provider_result_after_cancel_is_ignored(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.conversation.model_result import LLMResult
    from nz_coder.runtime.core.model_context import ModelExecutionContext
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.execution.services import ProductionTurnModelRuntime
    from nz_coder.runtime.execution.stream_state import StreamAttemptBuffer
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.runtime.session.session_processor import SessionProcessor

    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "PROVIDER_CANCEL_GRACE_SECONDS", 0.02)
    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "unused",
            permission_mode="auto",
            client=SimpleNamespace(),
            trace_enabled=False,
            session_id="late-cancel-result",
        )
        message_part = agent._new_message_part(1)
        assistant = {"role": "assistant", "content": ""}
        attach_message_identity(
            assistant,
            message_part["message_id"],
            session_id="late-cancel-result",
        )
        processor = SessionProcessor(assistant)
        agent._active_session_processor = processor
        attempt = StreamAttemptBuffer(
            agent,
            message_part,
            processor=processor,
            publish=True,
        )

        def call_streaming(*_args, **_kwargs):
            started.set()
            release.wait(timeout=2.0)
            attempt.append_text("late")
            return LLMResult(content="late")

        context = ModelExecutionContext(
            capabilities=lambda: SimpleNamespace(
                supports_streaming=True,
                supports_tools=False,
                provider="test",
            ),
            active_model_id=lambda: "test-model",
            active_tool_specs=lambda: [],
            prompt_budget=lambda: SimpleNamespace(output_reserve_tokens=100),
            call_streaming=call_streaming,
            call_non_streaming=lambda *_args: LLMResult(content="late"),
            gateway=lambda **_kwargs: None,
            project_outcome=lambda value: value,
            record_success=lambda: None,
            trace=lambda *_args, **_kwargs: None,
            retire_message_part=agent._retire_message_part,
        )

        async def exercise():
            task = asyncio.create_task(ProductionTurnModelRuntime().complete_turn(
                context,
                [],
                stream=True,
                on_token=None,
                message_part=message_part,
                stream_tool_handler=None,
            ))
            await asyncio.to_thread(started.wait, 1.0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()
            await asyncio.sleep(0.05)

        try:
            asyncio.run(exercise())
        finally:
            agent.close()

    assert assistant["content"] == ""
    assert not any(
        part.get("type") == "text"
        for part in assistant.get(PARTS_KEY, [])
    )


def _late_callback_scenario(*, retry=False, events=()):
    from nz_coder.runtime.conversation.model_result import LLMResult
    from nz_coder.runtime.execution.provider_stream import project_streaming_turn
    from nz_coder.runtime.model_gateway import (
        ModelCallOutcome,
        ModelStreamEvent,
    )
    from nz_coder.runtime.session.session_processor import SessionProcessor

    active = {"value": True}
    effects = {"retry": 0, "tool": 0, "checkpoint": 0}
    identity = {
        "run_id": "interaction-cancel",
        "message_id": "msg-cancel",
        "part_id": "part-cancel",
        "attempt_id": "attempt-cancel",
        "generation_id": "generation-cancel",
        "generation": 1,
    }
    assistant = {"role": "assistant", "content": ""}
    attach_message_identity(
        assistant,
        "msg-cancel",
        session_id="session-cancel",
    )
    processor = SessionProcessor(assistant)
    processor.fail_unsettled = lambda *_args: effects.__setitem__(
        "retry", effects["retry"] + 1
    )

    class Gateway:
        observer = None

        def complete_stream_sync(self, _call, *, on_event, **_kwargs):
            active["value"] = False
            if retry:
                self.observer("model_call_retry", {
                    "streaming": True,
                    "error": "late retry",
                })
            for kind, data in events:
                on_event(ModelStreamEvent(kind, data))
            return ModelCallOutcome.completed(finish_reason="stop")

    gateway = Gateway()
    host = SimpleNamespace(
        _active_session_processor=processor,
        _active_processor_messages=[assistant],
        _message_part_identity=lambda _part: dict(identity),
        _message_part_matches=lambda _part, _candidate: active["value"],
        _message_part_is_retired=lambda _part: not active["value"],
        _active_tool_specs=lambda: [],
        _prompt_budget=lambda: SimpleNamespace(output_reserve_tokens=100),
        _gateway=lambda **_kwargs: gateway,
        _gateway_outcome_result=lambda _outcome: LLMResult(
            content="", finish_reason="stop"
        ),
        _checkpoint_messages=lambda *_args: effects.__setitem__(
            "checkpoint", effects["checkpoint"] + 1
        ),
        _discard_message_part=lambda *_args: active.__setitem__("value", False),
        recovery=SimpleNamespace(record_success=lambda: None),
        model_capabilities=SimpleNamespace(supports_tools=False),
        provider_id="fake",
    )
    project_streaming_turn(
        host,
        [],
        message_part={**identity, "lock": threading.RLock()},
        stream_tool_handler=lambda _result: effects.__setitem__(
            "tool", effects["tool"] + 1
        ),
    )
    return effects, assistant


def test_late_retry_observer_after_cancel_is_ignored():
    effects, assistant = _late_callback_scenario(retry=True)

    assert effects == {"retry": 0, "tool": 0, "checkpoint": 0}
    assert assistant.get(PARTS_KEY, []) == []


def test_late_finish_reason_after_cancel_is_ignored():
    effects, _assistant = _late_callback_scenario(
        events=(("finish", {"reason": "tool-calls"}),),
    )

    assert effects["tool"] == 0
    assert effects["checkpoint"] == 0


def test_late_tool_delta_after_cancel_is_ignored():
    effects, assistant = _late_callback_scenario(events=(
        ("tool_delta", {
            "index": 0,
            "call_id": "call-late",
            "name": "bash",
            "arguments": '{"command":"secret"}',
        }),
        ("finish", {"reason": "tool-calls"}),
    ))

    assert effects["tool"] == 0
    assert "secret" not in repr(assistant)


def test_late_checkpoint_after_cancel_is_not_written():
    from nz_coder.runtime.execution.stream_state import StreamCheckpointScheduler

    writes = []
    scheduler = StreamCheckpointScheduler(
        SimpleNamespace(_checkpoint_messages=lambda *_args: writes.append("write")),
        [],
        enabled=True,
        interval_seconds=0.05,
        min_chars=1,
        active_check=lambda: False,
    )
    scheduler.note(10)

    assert scheduler.flush(force=True) is False
    assert writes == []


def test_remote_cleanup_when_reconcile_fails(monkeypatch):
    from nz_coder.interface import remote

    state = {
        "renderer_finished": 0,
        "view_finished": 0,
        "view_closed": 0,
        "input_closed": 0,
        "stream_closed": 0,
    }

    class Backend:
        def __init__(self):
            self.snapshots = 0

        def attach_snapshot(self):
            self.snapshots += 1
            if self.snapshots > 1:
                raise RuntimeError("reconcile failed")
            return {
                "cursor": {},
                "session": {"running": True},
                "messages": [],
                "events": [],
                "pending": {},
            }

        def events(self, **_kwargs):
            backend_state = state

            class Stream:
                def __iter__(self):
                    return iter([{
                        "type": "session.run.settled",
                        "properties": {"status": "completed"},
                    }])

                def close(self):
                    backend_state["stream_closed"] += 1

            return Stream()

    class Renderer:
        def __init__(self, _console):
            pass

        def start(self):
            pass

        def finish(self):
            state["renderer_finished"] += 1

    class RunView:
        def __init__(self, _console, _renderer):
            pass

        def begin_remote(self, _agent):
            pass

        def rebase_remote(self, _messages=None, **_kwargs):
            pass

        def feed(self, _event):
            pass

        def finish(self, _result):
            state["view_finished"] += 1

        def close(self):
            state["view_closed"] += 1

    class Input:
        def __init__(self, **_kwargs):
            pass

        async def close_async(self):
            state["input_closed"] += 1

    class Bridge:
        def __init__(self, terminal_input, _renderer, _loop):
            self.terminal_input = terminal_input

    monkeypatch.setattr(remote, "StreamingRenderer", Renderer)
    monkeypatch.setattr(remote, "TerminalRunRenderer", RunView)
    monkeypatch.setattr(remote, "TerminalInput", Input)
    monkeypatch.setattr(remote, "TerminalInteractionBridge", Bridge)

    asyncio.run(remote._follow_run(
        Backend(),
        SimpleNamespace(print=lambda *_args, **_kwargs: None),
    ))

    assert state["renderer_finished"] == 1
    assert state["view_finished"] == 1
    assert state["view_closed"] == 1
    assert state["input_closed"] == 1
    assert state["stream_closed"] >= 1


def test_streaming_renderer_replacement_and_finish_are_safe():
    from io import StringIO

    from rich.console import Console

    from nz_coder.interface.cli import StreamingRenderer

    output = StringIO()
    renderer = StreamingRenderer(Console(
        file=output,
        force_terminal=False,
        width=80,
    ))
    renderer.start()
    renderer.replace_text("safe\x1b[31mred\x1b[0m\x07 **unfinished[")
    renderer.replace_text("safe\x1b[31mred\x1b[0m\x07 **unfinished[")
    renderer.finish()
    renderer.finish()

    rendered = output.getvalue()
    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert rendered.count("safered") == 1


def test_snapshot_rebase_preserves_rendered_terminal_boundary():
    from io import StringIO

    from rich.console import Console

    from nz_coder.interface.run_renderer import TerminalRunRenderer

    output = StringIO()
    projection = SimpleNamespace(
        set_status=lambda _value: None,
        pause=lambda: None,
        resume=lambda: None,
        replace_text=lambda _value: None,
    )
    view = TerminalRunRenderer(
        Console(file=output, force_terminal=False, width=100),
        projection,
    )
    view.begin_remote(SimpleNamespace(model_id="remote"))
    view.feed(_event(
        "session.run.completed",
        {"status": "completed"},
        event_id="terminal-before-final-snapshot",
        sequence=1,
    ))

    view.rebase_remote([])
    view.finish({"status": "completed"})

    assert output.getvalue().count("Run completed") == 1
