"""当前 Run 的终态正文以生产生命周期返回值为准。"""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.http_service import InteractionBroker
from nz_coder.protocol.message_schema import COMPACTION_KEY, INTERACTION_RUN_ID_KEY
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.core.lifecycle_context import LifecycleRunState
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
from nz_coder.runtime.execution.runner import _typed_result as _read_typed_result
from nz_coder.runtime.core.result import freeze_terminal_result, normalize_run_status

from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.session.runtime import SessionRuntime
from nz_coder.runtime.session.store import EphemeralSessionStore
from nz_coder.sdk import AgentClient


@pytest.mark.parametrize("note", ["", "Current model note."])
def test_settlement_keeps_previous_interactions_reply_on_resume(tmp_path, note):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.protocol.message_schema import ASSISTANT_FINISH_KEY, ensure_message_identities

    messages = [
        {"role": "user", "content": "Check this change.", INTERACTION_RUN_ID_KEY: "first"},
        {"role": "assistant", "content": "Earlier result.", ASSISTANT_FINISH_KEY: "stop",
         INTERACTION_RUN_ID_KEY: "first"},
    ]
    ensure_message_identities(messages, "session-resume")
    host = SimpleNamespace(
        session_id="session-resume", event_publisher=SimpleNamespace(interaction_run_id="resumed"),
        runtime_state=VerificationScenario(workspace=tmp_path).state,
        _emit_session_event=lambda *_: None, _checkpoint_messages=lambda *_: None,
    )
    note_argument = {"model_note": note} if note else {}
    AgentLoop._persist_assistant_end_state(host, messages, "completed", "Current receipts.", **note_argument)
    assert messages[1]["content"] == "Earlier result." and "_nz_model_note" not in messages[1]
    assert messages[-1][INTERACTION_RUN_ID_KEY] == "resumed"
    assert messages[-1]["content"] == "Current receipts."
    assert messages[-1].get("_nz_model_note", "") == note


def _typed_result(request, context, raw):
    context.freeze_execution(context.terminal_status or normalize_run_status((raw or {}).get("status") if isinstance(raw, dict) else None))
    freeze_terminal_result(context, raw if isinstance(raw, dict) else {})
    return _read_typed_result(request, context, raw)

def _request(workspace, messages, **kwargs):
    return RunRequest(
        agent=AgentDefinition(
            name="worker", instructions="Follow the user.",
            allowed_tools=("read_file", "write_file"),
        ),
        profile=MAIN_PROFILE,
        messages=tuple(messages),
        workspace=workspace,
        session_id="run-result-text",
        **kwargs,
    )


def _lifecycle_context(publish=None):
    from tests.verification_scenarios import VerificationScenario
    scenario = VerificationScenario(plan={"stages": []})
    host = SimpleNamespace(
        session_id="run-result-text", runtime_state=scenario.state, run_evidence=None,
        _emit_session_event=publish or (lambda *_args, **_kwargs: None),
        _checkpoint_messages=lambda *_args, **_kwargs: None,
    )
    return SimpleNamespace(
        run_state=LifecycleRunState(),
        recovery=RecoveryState(),
        _scenario=scenario, verification_runtime=scenario.runtime, output_schema_requested=lambda: False,
        assert_terminal=lambda status: status,
        finish_lineage=lambda *_args: None,
        persist_assistant_end=lambda messages, status, content: (
            AgentLoop._persist_assistant_end_state(host, messages, status, content)
        ),
        runtime_summary=lambda: {}, current_agent_name=lambda: "worker",
        structured_outputs=lambda: {}, commit=lambda: None,
        run_evidence=lambda: SimpleNamespace(is_empty=lambda: True),
        trace_evidence_summary=lambda: {}, trace=lambda *_args, **_kwargs: None,
        publish_event=lambda *_args, **_kwargs: None,
        persist_runtime_state=lambda **_kwargs: None,
        save_learnings=lambda _messages: None,
    )


@pytest.mark.parametrize("status", [
    "cancelled", "interrupted", "completed", "error", "max_turns", "blocked",
])
@pytest.mark.parametrize(("current", "expected"), [
    ([{"role": "assistant", "content": ""}], ""),
    ([{"role": "assistant", "content": "HIDDEN_CHILD", "_nz_internal": True}], ""),
    ([{"role": "assistant", "content": "HIDDEN_CHILD", "_nz_visible": False}], ""),
    ([{"role": "assistant", "content": "CURRENT_PARTIAL"}], "CURRENT_PARTIAL"),
    ([
        {"role": "assistant", "content": "CURRENT_PARTIAL"},
        {"role": "assistant", "content": "HIDDEN_CHILD", "_nz_internal": True},
    ], "CURRENT_PARTIAL"),
], ids=["empty", "internal", "invisible", "partial", "partial-before-hidden"])
def test_terminal_text_respects_current_user_visibility(tmp_path, status, current, expected):
    messages = [
        {"role": "user", "content": "previous request"},
        {"role": "assistant", "content": "PREVIOUS_RUN_ANSWER"},
        {"role": "user", "content": "current request"},
        *copy.deepcopy(current),
    ]
    raw = ProductionRunLifecycle().finalize_sync(
        _lifecycle_context(), messages, status, stream=False,
    )
    request = _request(tmp_path, messages)
    context = RunContext(
        request, Session.create(request.session_id, messages, workspace=tmp_path), "worker",
    )

    result = _typed_result(request, context, raw)

    if status == "completed":
        assert raw["content"].endswith(expected)
        assert "PREVIOUS_RUN_ANSWER" not in raw["content"] and "HIDDEN_CHILD" not in raw["content"]
    else:
        assert raw["content"] == ("An internal error occurred." if status == "error" and not expected else expected)
    assert result.final_text == raw["content"]
    assert result.status.value == ("completed_unverified" if status == "completed" else status)


@pytest.mark.parametrize("raw", [
    {"status": "cancelled", "content": ""},
    {"status": "cancelled", "content": None},
    {"status": "cancelled"},
    None,
    ["not a terminal payload"],
])
def test_empty_payload_never_invents_text_from_visible_transcript(tmp_path, raw):
    messages = [
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "VISIBLE_BUT_NOT_RETURNED"},
    ]
    request = _request(tmp_path, messages)
    context = RunContext(
        request, Session.create(request.session_id, messages, workspace=tmp_path), "worker",
    )
    context.finish(RunStatus.CANCELLED)

    assert _typed_result(request, context, raw).final_text == ""


@pytest.mark.parametrize(("status", "boundary"), [
    ("error", "CURRENT_ERROR_DETAILS"),
    ("max_turns", "Stopped at the configured work limit; verification remains pending."),
    ("blocked", "Completion guidance budget exhausted; unresolved requirements: R1."),
])
def test_explicit_terminal_boundary_text_survives_projection(tmp_path, status, boundary):
    messages = [
        {"role": "user", "content": "previous request"},
        {"role": "assistant", "content": "PREVIOUS_RUN_ANSWER"},
        {"role": "user", "content": "current request"},
        {"role": "assistant", "content": "CURRENT_PARTIAL"},
    ]
    raw = ProductionRunLifecycle().finalize_sync(
        _lifecycle_context(), messages, status, stream=False, content_text=boundary,
    )
    request = _request(tmp_path, messages)
    context = RunContext(
        request, Session.create(request.session_id, messages, workspace=tmp_path), "worker",
    )

    assert _typed_result(request, context, raw).final_text == boundary


@pytest.mark.parametrize("status", ["blocked", "max_turns"])
@pytest.mark.parametrize("stream", [False, True])
def test_incomplete_terminal_replaces_success_in_output_history_and_live_parts(status, stream):
    from nz_coder.protocol.message_part_reducer import MessagePartReducer
    from nz_coder.protocol.message_schema import (
        ASSISTANT_END_STATE_KEY, ASSISTANT_FINISH_KEY, MESSAGE_ID_KEY, PARTS_KEY,
        ensure_message_identities, message_records,
    )

    summary = "Completion guidance budget exhausted. Unresolved requirements: R1."
    messages = [
        {"role": "user", "content": "Update README.md"},
        {"role": "assistant", "content": "All work is complete.", ASSISTANT_FINISH_KEY: "stop"},
    ]
    ensure_message_identities(messages, "run-result-text")
    reasoning = {
        "id": "part-investigation", "message_id": messages[-1][MESSAGE_ID_KEY],
        "type": "reasoning", "text": "Investigated the requirements.",
    }
    messages[-1][PARTS_KEY].append(reasoning)
    initial_parts = message_records(messages, "run-result-text")[-1]["parts"]
    reducer = MessagePartReducer()
    initial_events = [{
        "type": "message.part.completed",
        "properties": {"message_id": messages[-1][MESSAGE_ID_KEY], "part": part},
    } for part in initial_parts]
    for event in initial_events:
        reducer.apply_event(event)
    assert reducer.visible_text == "All work is complete."

    visible = []
    context = _lifecycle_context(
        publish=lambda kind, properties: reducer.apply_event({"type": kind, "properties": properties}),
    )
    persisted = []
    context.persist_runtime_state = lambda **kwargs: persisted.append(kwargs)
    result = ProductionRunLifecycle().finalize_sync(
        context, messages, status, stream=stream, on_text=visible.append,
        content_text=summary,
    )

    assert result["status"] == status
    assert result["content"] == summary
    assert visible == [summary]
    assert messages[-1]["content"] == reducer.visible_text == summary
    expected_reason = "errored" if status == "blocked" else "interrupted"
    assert messages[-1][ASSISTANT_END_STATE_KEY]["reason"] == expected_reason
    assert messages[-1]["_nz_continuation"]["status"] == status
    assert persisted[-1] == {"active": True}
    assert reasoning in messages[-1][PARTS_KEY]
    # 终态新代文本发布后，迟到的旧成功文本不能重新覆盖界面。
    for event in initial_events:
        reducer.apply_event(event)
    assert reducer.visible_text == summary


def test_restored_compacted_tail_with_new_run_identity_is_not_new_answer(tmp_path):
    messages = [
        {"role": "user", "content": "original task"},
        {"role": "assistant", "content": "PREVIOUS_RUN_ANSWER"},
        {
            "role": "user", "content": "Derived compaction summary",
            COMPACTION_KEY: {"version": 1},
            "_nz_synthetic": True,
        },
    ]
    request = _request(tmp_path, messages, interaction_run_id="resumed-interaction")
    context = asyncio.run(SessionRuntime(EphemeralSessionStore()).open(request))
    context.finish(RunStatus.CANCELLED)
    restored_answer = next(
        message for message in context.transcript if message.get("role") == "assistant"
    )
    assert restored_answer[INTERACTION_RUN_ID_KEY] == "resumed-interaction"

    result = _typed_result(request, context, {"status": "cancelled", "content": ""})

    assert result.final_text == ""
    assert restored_answer["content"] == "PREVIOUS_RUN_ANSWER"


class _AnswerThenApprovalProvider:
    name = "offline-result-text"

    def __init__(self, partial):
        self.partial = partial
        self.calls = 0

    def create_completion(self, _client, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            content, tool_calls, finish = "PREVIOUS_RUN_ANSWER", [], "stop"
        elif self.partial and self.calls == 2:
            content, tool_calls, finish = "CURRENT_RUN_PARTIAL", [], "stop"
        else:
            content, finish = "", "tool_calls"
            tool_calls = [SimpleNamespace(
                id=f"write-{self.calls}", type="function",
                function=SimpleNamespace(
                    name="write_file",
                    arguments=json.dumps({"path": "never-created.txt", "content": "CANCEL_ME"}),
                ),
            )]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=content, tool_calls=tool_calls),
            finish_reason=finish,
        )], usage=None)


@pytest.mark.parametrize("stream", [False, True], ids=["nonstream-entry", "stream-entry"])
@pytest.mark.parametrize("partial", [False, True], ids=["empty-cancel", "provisional-partial-cancel"])
def test_native_two_runs_cancel_never_republishes_an_old_answer_or_unreviewed_draft(
    tmp_path, monkeypatch, stream, partial,
):
    """真实 Native/审批/生命周期；入口 stream=True 仍使用非流式离线模型。"""
    from nz_coder.protocol.message_schema import legacy_messages
    provider = _AnswerThenApprovalProvider(partial)
    runtime = ResolvedModelRuntime(
        provider_id=provider.name, model_id="offline-model", request_model_id="offline-model",
        variant=None, provider=provider, client=object(), owns_client=False,
        capabilities=ModelCapabilities(
            provider=provider.name, model_id="offline-model", supports_streaming=False,
        ),
    )
    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda _request: replace(runtime))
    monkeypatch.setattr("nz_coder.runtime.execution.loop.resolve_model_runtime", lambda _: replace(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path.parent / f"user-config-{tmp_path.name}"))
    for setting in ("PLANNING_ENABLED",):
        monkeypatch.setattr(config, setting, False)

    async def scenario():
        sdk = AgentClient()
        request = _request(
            tmp_path, [{"role": "user", "content": "Reply with the previous marker."}],
            stream=stream, provider=provider.name, model="offline-model",
            metadata={"permission_mode": "default", "persist_session": False, "max_turns": 4},
        )
        bus = SessionEventBus(session_id=request.session_id)
        broker = InteractionBroker(session_id=request.session_id, event_bus=bus, timeout_seconds=10)
        permission_requested = asyncio.Event()
        loop = asyncio.get_running_loop()

        def publish(event_type, properties):
            bus.publish(event_type, properties)
            if event_type == "permission.asked":
                loop.call_soon_threadsafe(permission_requested.set)

        broker.begin_run(publisher=SimpleNamespace(publish=publish))
        task = None
        try:
            first = await asyncio.wait_for(sdk.run(
                request, event_bus=bus, permission_asker=broker.ask_permission,
            ), timeout=15)
            assert first.status is RunStatus.COMPLETED_UNVERIFIED
            assert first.metadata["authored_content"] == "PREVIOUS_RUN_ANSWER"
            assert "PREVIOUS_RUN_ANSWER" not in first.final_text
            assert "未评估" in first.final_text and "/message" in first.final_text
            second_request = replace(request, messages=(
                *copy.deepcopy(first.messages),
                {"role": "user", "content": "Write never-created.txt containing CANCEL_ME."},
            ))
            broker.begin_run()
            task = asyncio.create_task(sdk.run(
                second_request, event_bus=bus, permission_asker=broker.ask_permission,
            ))
            await asyncio.wait_for(permission_requested.wait(), timeout=10)
            assert broker.list("permission")[0]["tool_input"]["path"] == "never-created.txt"
            task.cancel()
            broker.cancel_all("cancelled", block_new=True)
            result = await asyncio.wait_for(task, timeout=10)

            assert result.status is RunStatus.CANCELLED
            assert result.final_text == "" and not result.metadata["authored_content"]
            assert provider.calls >= (3 if partial else 2)
            assert not (tmp_path / "never-created.txt").exists()
            assert broker.list("permission") == []
            assert any(message.get("content") == first.final_text for message in result.messages)
            current_user = max(
                index for index, message in enumerate(result.messages)
                if message.get("role") == "user" and not message.get("_nz_synthetic")
            )
            current = legacy_messages(list(result.messages[current_user + 1:]))
            assert all(not m["content"] for m in current if m["role"] == "assistant")
        finally:
            broker.close()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            bus.close()

    asyncio.run(scenario())


@pytest.mark.parametrize('stream', [False, True])
def test_frozen_verification_appendix_is_same_in_result_message_and_terminal_part(stream):
    events=[]
    context=_lifecycle_context(publish=lambda name, payload:events.append((name,payload)))
    context._scenario.command_result({"command": "pytest tests/test_a.py | tail -8"}, "1 passed", False, False, exit_code=0)
    context.output_schema_requested=lambda:False
    messages=[{'role':'user','content':'test'}, {'role':'assistant','content':'candidate', '_nz_finish':'stop'}]
    tokens=[]
    raw=ProductionRunLifecycle().finalize_sync(context,messages,'completed_unverified',
        stream=stream, on_token=tokens.append,content_text='candidate')
    assert 'pytest tests/test_a.py | tail -8' in raw['content']
    assert '未采信' in raw['content']
    assert messages[-1]['content']==raw['content']
    parts=[p for p in messages[-1]['_nz_parts'] if p['type']=='text']
    assert parts[-1]['text']==raw['content']
    assert raw['verification']['verification_needed'] is False
    assert raw['verification']['tests_passed'] is None
    assert raw['verification']['ignored_verifications']
    assert any(name=='message.part.updated' and payload['part'].get('text')==raw['content'] for name,payload in events)
    if stream:
        assert tokens==[raw['content'],None]


def test_schema_output_keeps_verification_only_in_metadata():
    context=_lifecycle_context()
    context._scenario.command_result({"command": "pytest tests/test_a.py"}, "no tests ran", False, False, exit_code=0)
    context.output_schema_requested=lambda:True
    messages=[{'role':'user','content':'JSON'}, {'role':'assistant','content':'{"ok":true}', '_nz_finish':'stop'}]
    raw=ProductionRunLifecycle().finalize_sync(context,messages,'completed_unverified',stream=False)
    assert json.loads(raw['content'])=={'ok':True}
    assert raw['verification']['verification_needed'] is False
    assert raw['verification']['tests_passed'] is None
    assert raw['verification']['ignored_verifications']
