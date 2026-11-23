"""终端、消息历史和结算记录必须对同一次未执行的命令给出一致说法。"""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.interface.timeline import format_transcript
from nz_coder.protocol.message_schema import ASSISTANT_FINISH_KEY, attach_message_identity, message_records
from nz_coder.protocol.public_error import PublicError
from nz_coder.runtime.core.result import RunStatus, TERMINAL_RESULT_KEY, freeze_terminal_result
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.session.session_processor import SessionProcessor
from nz_coder.runtime.tool_runtime.result_projection import _settle_session_tool
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.verification.hooks import AgentHooks, StopHookContext
from nz_coder.state.sessions import load_session
from nz_coder.tool_platform.execution import ToolExecutionResult
from tests.runtime import test_delivery_assessment_runner as delivery_probes
from tests.runtime.test_delivery_assessment_runner import accepted, run_probe


@pytest.mark.parametrize("started", [True, False])
@pytest.mark.parametrize("nested_denial", [False, True])
def test_only_host_denial_before_launch_can_attest_that_command_did_not_run(started, nested_denial):
    args = {"command": "example --version"}
    call = {"id": "denial-id", "type": "function", "function": {"name": "bash", "arguments": args}}
    message = {"role": "assistant", "content": "", "tool_calls": [call]}
    attach_message_identity(message, "msg-denial", session_id="denial-test")
    processor = SessionProcessor(message)
    processor.start_step()
    processor.register_tool_calls([call])
    processor.start_tools([call])
    result = ToolExecutionResult("bash", args, "Denied: example", executed=started,
        dispatch_failed=True, command_failed=False, is_write=False, permission_denied=True,
        metadata={"permission_source": "policy"},
        public_error=PublicError("permission_stopped", "Permission denied inside the tool.") if nested_denial else None)
    _settle_session_tool(processor, call["id"], result, result.output, stop_on_denial=False)
    record, = message_records([message], "denial-test")
    state = next(part["state"] for part in record["parts"] if part["type"] == "tool")
    assert (state["error"]["code"] == "permission_stopped") is (not started)
    if started and nested_denial:
        assert state["error"]["message"] == "Permission denied inside the tool."


def _denied_run(monkeypatch, tmp_path, reports, *, persist=False, max_turns=None, structured=False, retry=False):
    root = tmp_path / "workspace"
    root.mkdir()
    if persist or max_turns is not None or structured:
        request = delivery_probes._request(root)
        metadata = {**request.metadata, "persist_session": persist}
        if max_turns is not None:
            metadata["max_turns"] = max_turns
        if structured:
            schema = {"type": "object", "properties": {"details": {"type": "string"}}, "required": ["details"]}
            request = replace(request, agent=replace(request.agent, output_schema=schema))
        request = replace(request, metadata=metadata)
        monkeypatch.setattr(delivery_probes, "_request", lambda _root: request)
    command = f".fresh-venv/bin/ledger --version && cat {tmp_path}/outside.txt"
    actions = [[("bash", {"command": command})]]
    if retry:
        (root / "alternate").mkdir()
        actions.append([("bash", {"command": "python --version", "workdir": "alternate"})])
    candidate = "Use ledger list. " + FALSE_CLAIM
    return run_probe(monkeypatch, root, reports=reports,
        task="Check the installed ledger version and explain how to use the program.",
        initial_actions=actions, candidate=json.dumps({"details": candidate}) if structured else candidate)


def test_missing_receipt_assessment_exposes_the_model_report_separately(monkeypatch, tmp_path):
    result, _, _, _ = _denied_run(monkeypatch, tmp_path, lambda *_: (accepted(), "tool_calls"))
    assert result.status.value == "completed_unverified"
    text = result.final_text
    assert "未执行" in text
    assert ".fresh-venv/bin/ledger --version" in text and "/message" in text
    assert "Use ledger list." not in text
    assert result.metadata["authored_content"] == "Use ledger list. " + FALSE_CLAIM
    final = result.messages[-1]
    assert final["content"] == text
    assert next(p["text"] for p in final["_nz_parts"] if p["type"] == "text") == text


FALSE_CLAIM = "I verified the installed ledger version."


def test_mistaken_review_cannot_publish_a_model_claim_as_host_evidence(monkeypatch, tmp_path):
    def mistaken(_index, request, env):
        return _review_draft(2, request, env)

    result, _, provider, _ = _denied_run(monkeypatch, tmp_path, mistaken, persist=True)
    assert (provider.mains, provider.reviews) == (2, 1)
    assert result.status.value == "completed"
    assert FALSE_CLAIM not in result.final_text and "Use ledger list." not in result.final_text
    assert "/message" in result.final_text and ".fresh-venv/bin/ledger --version" in result.final_text
    assert result.metadata["authored_content"] == "Use ledger list. " + FALSE_CLAIM
    refusals = result.metadata["verification"]["denied_commands"]
    receipt, = refusals["entries"]
    assert refusals["total"] == 1 and receipt["call_id"] == "probe-1-0"
    with scoped_workdir(tmp_path / "workspace"):
        saved = load_session(result.session_id)
    frozen = saved["metadata"][TERMINAL_RESULT_KEY]["last_result"]
    assert frozen["answer"] == result.final_text
    assert frozen["metadata"]["verification"]["denied_commands"] == refusals
    assert FALSE_CLAIM not in format_transcript(result.session_id, saved["messages"])


def test_structured_result_keeps_its_schema_and_the_host_receipt_is_outside_it(monkeypatch, tmp_path):
    result, _, _, _ = _denied_run(monkeypatch, tmp_path, lambda i, request, env: _review_draft(2, request, env),
                                   structured=True)
    assert result.status.value == "completed"
    assert json.loads(result.final_text) == result.metadata["structured"] == {"details": "Use ledger list. " + FALSE_CLAIM}
    verification = result.metadata["verification"]
    assert verification["structured_output_source"] == "model" and verification["denied_commands"]["total"] == 1


def test_rejected_structured_candidate_cannot_be_published_as_its_own_stop_reason():
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    from nz_coder.runtime.verification.domain.models import ReviewFact, candidate_fingerprint
    from tests.runtime.test_run_result_text import _lifecycle_context

    context = _lifecycle_context()
    runtime = context.verification_runtime
    run = runtime.run_context
    fixture = Path(__file__).parents[1] / "fixtures/delivery_followup/denied-venv-report.json"
    denied, tool = json.loads(fixture.read_text(encoding="utf-8"))["messages"]
    denied["_nz_session_id"] = tool["_nz_session_id"] = run.session.session_id
    denied["_nz_interaction_run_id"] = denied["_nz_parts"][0]["interaction_run_id"] = run.interaction_run_id
    payload = {"report": "REJECTED_CANDIDATE"}
    draft = json.dumps(payload)
    run.metadata["authored_content"] = draft
    run.transcript.extend([denied, tool, {"role": "assistant", "content": draft, "_nz_interaction_run_id": run.interaction_run_id}])
    context.output_schema_requested = lambda: True
    outputs = {"worker": payload}
    context.structured_outputs = lambda: outputs
    domain = runtime.domain
    fingerprint = candidate_fingerprint(draft, payload)
    domain.set_completion_candidate(fingerprint)
    domain.observe_review(ReviewFact("review", domain.scope.task, (), domain.snapshot().version, "immutable", False,
                                     basis_fingerprint=domain.review_basis(), candidate_fingerprint=fingerprint))
    raw = ProductionRunLifecycle().finalize_sync(context, run.transcript, "completed", stream=False, content_text=draft)
    assert raw["status"] == "blocked" and raw["verification"]["denied_commands"]["total"] == 1
    assert "structured_output_source" not in raw["verification"]
    assert "REJECTED_CANDIDATE" not in raw["content"] and raw["authored_content"] == "" and "structured" not in raw


def test_successful_check_in_another_directory_is_reported_with_its_own_scope(monkeypatch, tmp_path):
    result, _, _, _ = _denied_run(monkeypatch, tmp_path, lambda i, request, env: _review_draft(2, request, env),
                                   retry=True)
    assert result.status.value == "completed"
    assert ".fresh-venv/bin/ledger --version" in result.final_text and "未执行" in result.final_text
    assert "python --version" in result.final_text and str(tmp_path / "workspace/alternate") in result.final_text
    assert FALSE_CLAIM not in result.final_text


def _review_draft(index, request, _env):
    schema = request["tools"][0]["function"]["parameters"]
    receipts = schema["properties"]["denied_command_claims"]["required"]
    assessment = accepted()
    assessment["denied_command_claims"] = {
        receipt: {"status": "claimed_as_executed" if index == 1 else "not_claimed",
                  "candidate_excerpt": FALSE_CLAIM if index == 1 else ""}
        for receipt in receipts
    }
    return assessment, "tool_calls"


def test_corrected_report_retires_only_the_false_candidate_from_public_history(monkeypatch, tmp_path):
    result, _, provider, _ = _denied_run(monkeypatch, tmp_path, _review_draft, persist=True)
    assert result.status.value == "completed"
    assert provider.mains == 3
    assert "A revised answer." not in result.final_text
    assert result.metadata["authored_content"] == "A revised answer."
    assert FALSE_CLAIM not in result.final_text
    with scoped_workdir(tmp_path / "workspace"):
        saved = load_session(result.session_id)
    history = saved["messages"]
    revoked = next(m for m in history if m["role"] == "assistant" and (m.get("content") or "").endswith(FALSE_CLAIM))
    assert revoked["_nz_visible"] is False
    transcript = format_transcript(result.session_id, history)
    assert FALSE_CLAIM not in transcript
    assert "A revised answer." not in transcript and "/message" in transcript
    assert all(r["info"]["id"] != revoked["_nz_message_id"] for r in message_records(history, result.session_id))


def test_blocked_review_preserves_the_stop_reason_and_withdraws_a_proven_false_claim(monkeypatch, tmp_path):
    def blocked(index, request, env):
        assessment, finish = _review_draft(index, request, env)
        assessment.update(verdict="blocked", reason="Release approval is missing.")
        return assessment, finish

    result, _, provider, _ = _denied_run(monkeypatch, tmp_path, blocked, persist=True)
    assert result.status.value == "blocked"
    assert provider.mains == 2 and provider.reviews == 1
    assert "Release approval is missing." in result.final_text
    assert FALSE_CLAIM not in result.final_text
    assert FALSE_CLAIM not in result.metadata["authored_content"]
    with scoped_workdir(tmp_path / "workspace"):
        history = load_session(result.session_id)["messages"]
    assert any(m.get("_nz_visible") is False and FALSE_CLAIM in (m.get("content") or "") for m in history)
    assert FALSE_CLAIM not in format_transcript(result.session_id, history)


def test_cancel_before_revision_keeps_wrong_answer_out_of_restored_session(monkeypatch, tmp_path):
    retire_candidate = AgentHooks._retire_reviewed_candidate
    events = []

    def retire_then_cancel(loop, messages, context):
        original_emit = loop._emit_session_event

        def observe(event_type, properties):
            events.append((event_type, properties))
            original_emit(event_type, properties)

        monkeypatch.setattr(loop, "_emit_session_event", observe)
        retire_candidate(loop, messages, context)
        loop.verification_runtime.run_context.request_cancel()

    monkeypatch.setattr(AgentHooks, "_retire_reviewed_candidate", staticmethod(retire_then_cancel))
    result, _, provider, _ = _denied_run(monkeypatch, tmp_path, _review_draft, persist=True)
    assert result.status.value == "cancelled"
    assert provider.mains == 2
    assert FALSE_CLAIM not in result.final_text
    assert "未执行" in result.final_text and ".fresh-venv/bin/ledger --version" in result.final_text
    with scoped_workdir(tmp_path / "workspace"):
        saved = load_session(result.session_id)
    assert saved["run_status"] == "cancelled"
    assert saved["metadata"][TERMINAL_RESULT_KEY]["last_result"]["answer"] == result.final_text
    history = saved["messages"]
    draft = next(m for m in history if m.get("_nz_visible") is False and FALSE_CLAIM in (m.get("content") or ""))
    part_id = next(p["id"] for p in draft["_nz_parts"] if p["type"] == "text")
    assert any(kind == "message.part.removed" and data["part_id"] == part_id
               and data["message_id"] == draft["_nz_message_id"] for kind, data in events)
    assert FALSE_CLAIM not in format_transcript(result.session_id, history)


def test_frozen_stop_after_withdrawal_preserves_an_earlier_progress_message(tmp_path):
    from tests.verification_scenarios import VerificationScenario

    messages = [
        {"role": "user", "content": "Check the project."},
        {"role": "assistant", "content": "The database migrated.", ASSISTANT_FINISH_KEY: "stop"},
        {"role": "assistant", "content": FALSE_CLAIM, ASSISTANT_FINISH_KEY: "stop", "_nz_visible": False},
    ]
    for index, message in enumerate(messages):
        attach_message_identity(message, f"msg-{index}", session_id="test-retirement")
        message["_nz_interaction_run_id"] = "run-review"
    host = SimpleNamespace(session_id="test-retirement", event_publisher=SimpleNamespace(interaction_run_id="run-review"),
                           runtime_state=VerificationScenario(workspace=tmp_path).state,
                           _emit_session_event=lambda *_: None, _checkpoint_messages=lambda *_: None)
    AgentLoop._persist_assistant_end_state(host, messages, "blocked", "Release approval is missing.")
    assert messages[1]["content"] == "The database migrated."
    assert messages[2]["content"] == FALSE_CLAIM and messages[2]["_nz_visible"] is False
    assert len(messages) == 4
    assert messages[-1]["content"] == "Release approval is missing."
    assert messages[-1]["_nz_interaction_run_id"] == "run-review"


@pytest.mark.parametrize("newer", ["scheduled_check", "model_call", "user_turn", "user_turn_with_control_prefix"])
def test_retirement_binds_the_draft_across_host_scheduled_checks_only(tmp_path, newer):
    from tests.verification_scenarios import VerificationScenario

    runtime = VerificationScenario(workspace=tmp_path).runtime
    run = runtime.run_context
    candidate = {"role": "assistant", "content": FALSE_CLAIM, "_nz_interaction_run_id": run.interaction_run_id}
    attach_message_identity(candidate, "msg-draft", session_id=run.session.session_id)
    messages = [candidate]
    stop_context = StopHookContext(transcript=(deepcopy(candidate),), last_assistant_text=FALSE_CLAIM)
    if newer.startswith("user_turn"):
        text = "<reminder> Please pause this review." if newer.endswith("prefix") else "Start a different task."
        messages.append({"role": "user", "content": text})
    else:
        assistant = {"role": "assistant", "content": "", "tool_calls": [{"id": "check"}],
                     "_nz_interaction_run_id": run.interaction_run_id}
        if newer == "scheduled_check":
            assistant.update(_nz_synthetic=True, _nz_verification_stage="acceptance")
        messages.extend([assistant, {"role": "tool", "tool_call_id": "check", "content": "passed"}])
        stop_context = replace(stop_context, transcript=tuple(deepcopy(messages)))
    AgentHooks._retire_reviewed_candidate(SimpleNamespace(verification_runtime=runtime), messages, stop_context)
    assert (candidate.get("_nz_visible") is False) == (newer == "scheduled_check")
    assert messages[1].get("_nz_visible") is not False


def test_turn_limit_after_denial_still_explains_what_did_not_run(monkeypatch, tmp_path):
    result, _, provider, _ = _denied_run(monkeypatch, tmp_path, lambda *_: (accepted(), "tool_calls"), max_turns=1)
    assert result.status.value == "max_turns" and provider.mains == 1
    assert "未执行" in result.final_text and ".fresh-venv/bin/ledger --version" in result.final_text


def test_cancellation_during_review_restores_the_same_qualified_answer(monkeypatch, tmp_path):
    def cancel_during_review(_index, _request, environment):
        environment.verification_runtime.run_context.request_cancel()
        return accepted(), "tool_calls"

    result, _, provider, _ = _denied_run(monkeypatch, tmp_path, cancel_during_review, persist=True)
    assert result.status.value == "cancelled" and provider.mains == 2
    with scoped_workdir(tmp_path / "workspace"):
        saved = load_session(result.session_id)
    answer = result.final_text
    assert "未执行" in answer and FALSE_CLAIM not in answer and "/message" not in answer
    assert not result.metadata["authored_content"]
    assert saved["metadata"][TERMINAL_RESULT_KEY]["last_result"]["answer"] == answer
    final = next(m for m in reversed(saved["messages"]) if m["role"] == "assistant" and m.get("_nz_visible") is not False)
    assert final["content"] == answer
    assert next(p["text"] for p in final["_nz_parts"] if p["type"] == "text") == answer


@pytest.mark.parametrize("structured", [False, True])
def test_freeze_without_a_lifecycle_result_keeps_denial_and_respects_output_schema(tmp_path, structured):
    fixture = Path(__file__).parents[1] / "fixtures/delivery_followup/denied-venv-report.json"
    messages = json.loads(fixture.read_text(encoding="utf-8"))["messages"]
    owner = messages[0]
    partial = '{"partial": true}'
    messages.append({"role": "assistant", "content": partial,
                     "_nz_interaction_run_id": owner["_nz_interaction_run_id"]})
    request = delivery_probes._request(tmp_path)
    agent = replace(request.agent, output_schema={"type": "object"}) if structured else request.agent
    request = replace(request, agent=agent, session_id=owner["_nz_session_id"],
                      interaction_run_id=owner["_nz_interaction_run_id"])
    session = Session.create(request.session_id, messages, workspace=tmp_path)
    run = RunContext(request, session, agent.name)
    run.freeze_execution(RunStatus.CANCELLED)
    freeze_terminal_result(run)
    answer = session.metadata[TERMINAL_RESULT_KEY]["last_result"]["answer"]
    if structured:
        assert answer == partial
    else:
        assert "未执行" in answer and partial not in answer and "/message" not in answer
        assert ".fresh-venv" in answer
        assert session.transcript[-1]["content"] == answer
        assert next(p["text"] for p in session.transcript[-1]["_nz_parts"] if p["type"] == "text") == answer
    frozen = session.metadata[TERMINAL_RESULT_KEY]["last_result"]["metadata"]
    assert frozen["verification"]["denied_commands"]["total"] == 1
    assert "structured_output_source" not in frozen["verification"]
    assert not frozen["authored_content"]


def test_live_denials_survive_successive_history_compactions_without_reopening_archives(tmp_path):
    from nz_coder.runtime.core.run_context import scoped_run_context
    from nz_coder.runtime.verification.hooks import manual_compact_hook

    fixture = Path(__file__).parents[1] / "fixtures/delivery_followup/denied-venv-report.json"
    owner = json.loads(fixture.read_text(encoding="utf-8"))["messages"][0]
    request = replace(delivery_probes._request(tmp_path), session_id=owner["_nz_session_id"],
                      interaction_run_id=owner["_nz_interaction_run_id"])
    run = RunContext(request, Session.create(request.session_id, [], workspace=tmp_path), request.agent.name)
    loop = SimpleNamespace(tracer=SimpleNamespace(log=lambda *args, **kwargs: None),
        _compact_messages=lambda messages: [{"role": "user", "content": "<session-summary>…</session-summary>",
                                             "_nz_compaction": {"archive": "unavailable"}}],
        _on_context_compacted=lambda: None)
    with scoped_run_context(run):
        for index in range(7):
            message = deepcopy(owner)
            message["_nz_message_id"] = message["_nz_parts"][0]["message_id"] = f"msg-{index}"
            message["tool_calls"][0]["id"] = message["_nz_parts"][0]["call_id"] = f"call-{index}"
            run.transcript.append(message)
            if index in {2, 5}:
                manual_compact_hook(SimpleNamespace(loop=loop, messages=run.transcript, manual_compact=True, on_text=None))
    run.freeze_execution(RunStatus.CANCELLED)
    freeze_terminal_result(run)
    result = run.session.metadata[TERMINAL_RESULT_KEY]["last_result"]
    receipts = result["metadata"]["verification"]["denied_commands"]
    assert receipts["total"] == 7 and [item["call_id"] for item in receipts["entries"]] == [f"call-{i}" for i in range(3, 7)]
    assert "共 7 次" in result["answer"]


@pytest.mark.parametrize("deny", [False, True])
def test_streaming_and_running_snapshots_only_publish_submitted_execution_reports(tmp_path, monkeypatch, deny):
    import asyncio
    from nz_coder.foundation import config
    from nz_coder.providers.normalized import chunk
    from nz_coder.protocol.message_schema import legacy_messages
    from nz_coder.interface.timeline import latest_assistant_text
    from nz_coder.protocol.session_events import SessionEventBus
    from tests.test_streaming_state_consistency import _chunk, _drain, _reasoning_chunk

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(config, "WORKDIR", root)
    session_id = "stream-owned-report"
    bus = SessionEventBus(session_id=session_id)
    subscription = bus.subscribe()
    report = "I verified the fresh install in the denied directory."
    thought = "private-model-thought"
    def tool_call(index, name, args):
        call = SimpleNamespace(index=0, id=f"call-stream-{index}", function=SimpleNamespace(name=name,
                               arguments=json.dumps(args)), provider_extra=None)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="tool_calls",
            delta=SimpleNamespace(content=None, tool_calls=[call], reasoning_content=None))])
    first = tool_call(0, "bash", {"command": f"cat {tmp_path}/outside.txt"}) if deny else tool_call(0, "list_directory", {"path": "."})
    responses = [iter([first]), iter([tool_call(1, "list_directory", {"path": "."})]),
                 iter([_reasoning_chunk(thought), _chunk(report), chunk(finish_reason="stop")])]
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs: responses.pop(0))))
    callbacks, checkpoints = [], []
    with scoped_workdir(root):
        agent = AgentLoop("unused", client=client, permission_mode="auto", trace_enabled=False,
                          event_bus=bus, session_id=session_id, sidecar_verifier=False)
        original = agent._checkpoint_messages
        def checkpoint(messages, status):
            if any(m.get("content") == report for m in messages):
                checkpoints.append((legacy_messages(messages), message_records(deepcopy(messages), session_id),
                                    format_transcript(session_id, messages), latest_assistant_text(messages)))
            original(messages, status)
        monkeypatch.setattr(agent, "_checkpoint_messages", checkpoint)
        try:
            result = asyncio.run(agent.run([{"role": "user", "content": "Inspect this project."}],
                stream=True, on_text=callbacks.append, on_token=callbacks.append))
            events = _drain(subscription)
        finally:
            agent.close()
            bus.close()
    assert result["status"] == "completed_unverified"
    assert checkpoints
    public_events = repr([(event.type, event.properties) for event in events])
    assert "list_directory" in public_events
    if deny:
        assert "permission_stopped" in public_events and "未执行" in result["content"]
        assert result["authored_content"] == report
        for surface in (public_events, repr(callbacks), repr(checkpoints)):
            assert report not in surface and thought not in surface
    else:
        assert "denied_commands" not in result["verification"]
        assert any(event.type == "message.part.delta" and report == event.properties.get("delta") for event in events)
        assert report in repr(checkpoints)


@pytest.mark.parametrize("deferred", [False, True])
def test_publishing_the_frozen_answer_does_not_reveal_its_deferred_reasoning(deferred):
    from nz_coder.protocol.message_schema import DEFERRED_MODEL_TEXT_KEY, replace_assistant_final_text, upsert_message_part
    record = {"role": "assistant", "content": "original draft"}
    attach_message_identity(record, "msg-draft", session_id="public-report")
    upsert_message_part(record, {"id": "part-legacy-reasoning", "message_id": "msg-draft",
                                 "type": "reasoning", "text": "private draft reasoning", "visible": True})
    if deferred:
        record[DEFERRED_MODEL_TEXT_KEY] = True
    replace_assistant_final_text(record, "Frozen host facts.")
    for public in (repr(message_records([record], "public-report")), format_transcript("public-report", [record])):
        assert "Frozen host facts." in public
        assert ("private draft reasoning" in public) is (not deferred)
