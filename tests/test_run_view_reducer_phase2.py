"""Regression coverage for the interaction-scoped terminal projection."""
from __future__ import annotations

from nz_coder.protocol.run_view_reducer import RunViewReducer


def _snapshot(*parts, pending=None):
    return {
        "interaction_run_id": "interaction-1",
        "status": "running",
        "messages": [{
            "info": {
                "id": "msg-1",
                "role": "assistant",
                "interaction_run_id": "interaction-1",
            },
            "parts": list(parts),
        }],
        "pending": pending or {},
    }


def test_snapshot_restores_running_tool_state():
    reducer = RunViewReducer()
    reducer.replace_snapshot(_snapshot({
        "id": "part-tool",
        "message_id": "msg-1",
        "type": "tool",
        "tool": "bash",
        "call_id": "call-1",
        "state": {
            "status": "running",
            "input": {"command": "pytest -q"},
            "time": {"start": 1.0},
        },
        "interaction_run_id": "interaction-1",
    }))

    assert reducer.state.tool_parts["part-tool"]["state"]["status"] == "running"


def test_snapshot_restores_pending_question_and_permission():
    reducer = RunViewReducer()
    reducer.replace_snapshot(_snapshot(pending={
        "permissions": [{"id": "permission-1", "permission": "write"}],
        "questions": [{"id": "question-1", "questions": []}],
    }))

    assert reducer.state.pending_permission["id"] == "permission-1"
    assert reducer.state.pending_question["id"] == "question-1"


def test_stale_event_from_previous_interaction_is_rejected():
    reducer = RunViewReducer()
    reducer.replace_snapshot(_snapshot())

    assert reducer.apply_event({
        "type": "session.run.completed",
        "properties": {"status": "completed"},
        "meta": {
            "interaction_run_id": "interaction-old",
            "sequence": 2,
        },
    }) is False
    assert reducer.state.status == "running"


def test_local_remote_run_view_reducer_parity():
    events = [{
        "type": "message.part.updated",
        "properties": {
            "message_id": "msg-1",
            "part": {
                "id": "part-text",
                "message_id": "msg-1",
                "type": "text",
                "text": "answer",
                "interaction_run_id": "interaction-1",
                "generation": 1,
                "version": 1,
                "status": "streaming",
            },
        },
        "meta": {
            "interaction_run_id": "interaction-1",
            "event_id": "event-1",
            "sequence": 1,
        },
    }, {
        "type": "session.run.completed",
        "properties": {"status": "completed"},
        "meta": {
            "interaction_run_id": "interaction-1",
            "event_id": "event-2",
            "sequence": 2,
        },
    }]
    local = RunViewReducer()
    remote = RunViewReducer()
    local.replace_snapshot(_snapshot())
    remote.replace_snapshot(_snapshot())
    for event in events:
        local.apply_event(event)
        remote.apply_event(event)

    assert local.visible_text == remote.visible_text == "answer"
    assert local.state == remote.state
