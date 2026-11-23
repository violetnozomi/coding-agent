"""延迟交付的公开完成事件必须能单独还原最终正文。"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.protocol.message_schema import (
    ASSISTANT_FINISH_KEY, DEFERRED_MODEL_TEXT_KEY, INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY, PARTS_KEY, ensure_message_identities, message_records,
)
from nz_coder.protocol.session_events import SessionEventBus, SessionEventPublisher
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.core.result import TERMINAL_RESULT_KEY
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import load_session
from tests.runtime.test_denied_command_delivery import FALSE_CLAIM, _denied_run, _review_draft
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _offline_accept_review, _request
from tests.test_session_events import _drain
from tests.verification_scenarios import VerificationScenario


DRAFT = "我刚刚运行了 pytest，每条用例都已通过。"


def test_completed_event_can_restore_the_public_text_without_part_events(tmp_path, monkeypatch):
    responses = iter([_completion(tool="list_directory"), _completion(DRAFT)])
    provider = SimpleNamespace(name="offline", create_completion=lambda *_args, **_kwargs: next(responses))
    _install_runtime(monkeypatch, provider)
    request = _request(tmp_path)
    bus = SessionEventBus(session_id=request.session_id)
    subscription = bus.subscribe(["session.message.completed"])
    try:
        options = RunOptions(event_bus=bus, permission_asker=lambda *_: True)
        result = asyncio.run(native_sdk.NativeSDKRunner().run_result(request, options))
        events = [(e.type, e.properties) for e in bus.recent(200)]
        tool, final = [event.properties for event in _drain(subscription)]
    finally:
        bus.close()

    assert result.metadata["authored_content"] == DRAFT
    assert result.final_text and DRAFT not in result.final_text and DRAFT not in repr(events)
    end = events.index(("session.message.completed", final))
    assert (tool["turn"], tool["tool_calls"], tool["content"]) == (1, 1, "")
    assert (final["turn"], final["tool_calls"], final["content"]) == (2, 0, result.final_text)
    public_message = result.messages[-1]
    part, = [p for p in public_message[PARTS_KEY] if p["type"] == "text"]
    assert (final["message_id"], final["part_id"]) == (public_message[MESSAGE_ID_KEY], part["id"])
    assert part["text"] == result.final_text
    assert any(kind == "message.part.updated" and data.get("part", {}).get("id") == part["id"]
               and data["part"]["text"] == final["content"] for kind, data in events[:end])


@pytest.mark.parametrize("callback", ["on_text", "on_tool"])
def test_disconnected_callback_completes_the_answer_reopened_from_disk(tmp_path, monkeypatch, callback):
    def submit(_client, **kwargs):
        if review := _offline_accept_review(kwargs, report_kind="ordinary"):
            return review
        return _completion("只读工具可以列出目录。", tool="list_directory" if callback == "on_tool" else None)

    premature = []

    def disconnect(*_args):
        premature.extend(e for e in bus.recent(200) if e.type == "session.message.completed" and not e.properties["tool_calls"])
        raise RuntimeError("subscriber disconnected")

    _install_runtime(monkeypatch, SimpleNamespace(name="offline", create_completion=submit), with_delivery_review=True)
    request = replace(_request(tmp_path), metadata={"permission_mode": "auto", "persist_session": True})
    bus = SessionEventBus(session_id=request.session_id)
    try:
        with pytest.raises(RuntimeError, match="subscriber disconnected"):
            asyncio.run(native_sdk.NativeSDKRunner().run_result(
                request, RunOptions(event_bus=bus, permission_asker=lambda *_: True, **{callback: disconnect})))
        events = [(event.type, event.properties) for event in bus.recent(200)]
    finally:
        bus.close()

    with scoped_workdir(tmp_path):
        saved = load_session(request.session_id)
    answer = saved["metadata"][TERMINAL_RESULT_KEY]["last_result"]["answer"]
    message = saved["messages"][-1]
    part, = [p for p in message[PARTS_KEY] if p["type"] == "text"]
    assert answer and answer == message["content"] == part["text"]
    assert not premature
    final, = [data for kind, data in events if kind == "session.message.completed" and not data["tool_calls"]]
    assert (final["message_id"], final["part_id"], final["content"]) == (message[MESSAGE_ID_KEY], part["id"], answer)
    assert any(kind == "message.part.updated" and data.get("part", {}).get("id") == part["id"]
               and data["part"]["text"] == answer for kind, data in events)


def _terminal_host(tmp_path, events):
    state = VerificationScenario(workspace=tmp_path).state
    state.turn_count = 3
    return SimpleNamespace(session_id="terminal-events", runtime_state=state,
                           event_publisher=SimpleNamespace(interaction_run_id="interaction"),
                           _emit_session_event=lambda kind, data: events.append((kind, data)),
                           _checkpoint_messages=lambda *_: None)


def test_withdrawn_managed_answer_completes_only_the_new_host_message(tmp_path, monkeypatch):
    events = []
    publish = SessionEventPublisher.publish

    def observe(publisher, kind, data=None):
        events.append((kind, data))
        return publish(publisher, kind, data)

    def block(index, request, environment):
        assessment, finish = _review_draft(index, request, environment)
        assessment.update(verdict="blocked", reason="Release approval is missing.")
        return assessment, finish

    monkeypatch.setattr(SessionEventPublisher, "publish", observe)
    result, _, _, _ = _denied_run(monkeypatch, tmp_path, block, persist=True)
    withdrawn, = [m for m in result.messages if m.get("_nz_visible") is False and FALSE_CLAIM in (m.get("content") or "")]
    assert result.status.value == "blocked" and FALSE_CLAIM not in repr(events)
    final, = [data for kind, data in events if kind == "session.message.completed" and not data["tool_calls"]]
    message = result.messages[-1]
    part, = [p for p in message[PARTS_KEY] if p["type"] == "text"]
    assert final["message_id"] == message[MESSAGE_ID_KEY] != withdrawn[MESSAGE_ID_KEY]
    assert (final["part_id"], final["content"]) == (part["id"], result.final_text)


def test_empty_cancelled_delivery_removes_the_managed_draft_from_reopened_history(tmp_path):
    events = []
    host = _terminal_host(tmp_path, events)
    messages = [{"role": "user", "content": "检查项目。"},
                {"role": "assistant", "content": DRAFT, ASSISTANT_FINISH_KEY: "stop",
                 DEFERRED_MODEL_TEXT_KEY: True, INTERACTION_RUN_ID_KEY: "interaction"}]
    ensure_message_identities(messages, host.session_id)
    message_id = messages[-1][MESSAGE_ID_KEY]

    AgentLoop._persist_assistant_end_state(host, messages, "cancelled", "", replace_partial=True)

    assert messages[-1][MESSAGE_ID_KEY] == message_id
    assert messages[-1]["content"] == "" and DEFERRED_MODEL_TEXT_KEY not in messages[-1]
    assert DRAFT not in repr(message_records(messages, host.session_id))
    assert DRAFT not in repr(events)
    assert all(kind != "session.message.completed" for kind, _ in events)
