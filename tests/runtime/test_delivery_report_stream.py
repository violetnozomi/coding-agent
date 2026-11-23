"""流式输出、运行中快照和取消恢复使用同一份可公开的执行记录。"""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.interface.timeline import format_transcript
from nz_coder.protocol.message_schema import DEFERRED_MODEL_TEXT_KEY, INTERACTION_RUN_ID_KEY, legacy_messages, message_records
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.providers.normalized import chunk
from nz_coder.runtime.core.result import RunStatus, TERMINAL_RESULT_KEY, freeze_terminal_result
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.verification.sidecar_verifier import ResolvedVerifierProvider, create_sidecar_verifier_hook
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
from tests.test_streaming_state_consistency import _chunk, _drain, _reasoning_chunk


CLAIM = "我刚刚运行了 pytest，两个测试全部通过。"


def test_product_run_without_a_reviewer_keeps_the_model_claim_in_details(tmp_path, monkeypatch):
    calls = []

    def submit(_client, **request):
        calls.append(request)
        return _completion(CLAIM)

    _install_runtime(monkeypatch, SimpleNamespace(name="offline", create_completion=submit))
    request = _request(tmp_path)
    bus = SessionEventBus(session_id=request.session_id)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner().run_result(request, RunOptions(event_bus=bus)))
        public_events = [(event.type, event.properties) for event in bus.recent(200)]
    finally:
        bus.close()
    assert len(calls) == 1 and result.metadata["authored_content"] == CLAIM
    assert "本轮没有已采信的测试结果" in result.final_text and CLAIM not in result.final_text
    assert CLAIM not in str(public_events) and CLAIM not in str(legacy_messages(list(result.messages)))


@pytest.mark.parametrize("kind", [None, "test_report"])
def test_unsubstantiated_test_report_never_streams_before_review(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    session_id = "stream-review"
    thought = "还未核实的模型草稿"
    stream = iter([_reasoning_chunk(thought), _chunk(CLAIM), chunk(finish_reason="stop")])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: stream)))
    bus = SessionEventBus(session_id=session_id)
    subscription = bus.subscribe()
    callbacks, checkpoints, review_calls = [], [], []

    def review(_client, **kwargs):
        review_calls.append(kwargs)
        verdict = {"verdict": "accept", "reason": "OK", "delivery_assessment": {"status": "reviewed", "issues": []}}
        if kind is not None:
            verdict["test_report_kind"] = kind
        call = SimpleNamespace(function=SimpleNamespace(name="emit_sidecar_verdict", arguments=json.dumps(verdict)))
        choice = SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[call]), finish_reason="tool_calls")
        return SimpleNamespace(choices=[choice])

    with scoped_workdir(tmp_path):
        agent = AgentLoop("unused", client=client, permission_mode="auto", trace_enabled=False,
                          event_bus=bus, session_id=session_id, sidecar_verifier=False)
        provider = SimpleNamespace(name="offline", create_completion=review)
        agent._configured_sidecar_verifier = create_sidecar_verifier_hook(agent,
            ResolvedVerifierProvider(provider, object(), "offline-model", "offline", "inherit-main"),
            env={"KODAX_VERIFIER_ALWAYS": "1"})
        original_checkpoint = agent._checkpoint_messages

        def checkpoint(messages, status):
            if any(message.get("content") == CLAIM for message in messages):
                checkpoints.append((legacy_messages(messages), message_records(deepcopy(messages), session_id),
                                    format_transcript(session_id, messages)))
            original_checkpoint(messages, status)

        monkeypatch.setattr(agent, "_checkpoint_messages", checkpoint)
        try:
            result = asyncio.run(agent.run([{"role": "user", "content": "项目的测试结果如何？"}],
                                           stream=True, on_text=callbacks.append, on_token=callbacks.append))
            events = [(event.type, event.properties) for event in _drain(subscription)]
        finally:
            agent.close()
            bus.close()

    assert len(review_calls) == 1 and checkpoints
    assert result["authored_content"] == CLAIM
    assert "本轮没有已采信的测试结果" in result["content"] and result["content"] in callbacks
    for view in (repr(checkpoints), repr(callbacks), repr(events)):
        assert CLAIM not in view and thought not in view


def test_cancellation_before_review_does_not_republish_the_draft(tmp_path):
    request = _request(tmp_path)
    session = Session.create(request.session_id, [], workspace=tmp_path)
    run = RunContext(request, session, request.agent.name)
    run.uses_receipt_based_delivery = True
    session.transcript.append({"role": "assistant", "content": CLAIM,
                               INTERACTION_RUN_ID_KEY: run.interaction_run_id, DEFERRED_MODEL_TEXT_KEY: True})
    run.freeze_execution(RunStatus.CANCELLED)
    freeze_terminal_result(run)
    frozen = session.metadata[TERMINAL_RESULT_KEY]["last_result"]
    assert frozen["answer"] == "" and not frozen["metadata"]["authored_content"]
    assert CLAIM not in str(legacy_messages(session.transcript)) and session.transcript[-1]["content"] == frozen["answer"]
