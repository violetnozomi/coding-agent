"""模型任务判断的来源、版本、恢复与原子持久化边界。"""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from nz_coder.protocol.message_schema import attach_message_identity, MESSAGE_ID_KEY
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.verification.domain.models import RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.agent.task_intent import (
    parse_task_assessment, initialize_task_assessment, capture_assessment_scope,
    current_task_assessment, render_task_assessment, assessment_cache_key,
    submit_task_assessment,
)


VALUE = {"task_kinds": ["review", "explain"], "complexity": "moderate",
         "reason": "Need to trace cancellation; no implementation requested.",
         "uncertainties": ["Provider exit order is not confirmed."]}


def setup_run(tmp_path, *, lineage="task-one", interaction="activation-one"):
    user = {"role": "user", "content": "Explain create_connection errors without changes."}
    attach_message_identity(user, session_id="intent-state")
    request = RunRequest(AgentDefinition("worker", "Inspect only."), MAIN_PROFILE, [user], tmp_path,
                         "intent-state", stream=False)
    run = RunContext(request, Session.create("intent-state", [user], workspace=tmp_path), "worker",
                     interaction_run_id=interaction)
    domain = VerificationDomain(lock=run._state_lock,
        scope=RunScope(TaskScope("intent-state", lineage, str(tmp_path)), interaction, "worker"),
        version=WorkspaceVersion())
    run.attach_verification(domain)
    state = RuntimeState(initial_task_text=user["content"])
    state.bind_run_context(run)
    initialize_task_assessment(state, run, run.transcript, pure_continuation=False)
    return state, run


def publish(state, run, *, value=None, revision=0, scope=None, call_id="call-one", path=None, superseded=lambda: False):
    payload = {**copy.deepcopy(VALUE if value is None else value), "expected_revision": revision}
    call = {"id": call_id, "type": "function", "function": {
        "name": "update_task_assessment", "arguments": json.dumps(payload)}}
    owner = {"role": "assistant", "content": "", "tool_calls": [call]}
    attach_message_identity(owner, session_id="intent-state")
    run.transcript.append(owner)
    scope = scope or capture_assessment_scope(state, run, owner[MESSAGE_ID_KEY])
    result = ToolExecutionResult("update_task_assessment", payload, "proposed", True, False, False, False,
                                 metadata={"task_assessment_proposal": payload})
    return submit_task_assessment(state, scope, call, result, run.transcript, run_context=run,
                                  path=path, superseded=superseded), scope, call, owner


def test_first_epoch_zero_without_authority_message_can_publish_and_reopen(tmp_path):
    state, run = setup_run(tmp_path)
    assert state.task_authority_epoch == 0 and state.task_authority_message_id == ""
    assert current_task_assessment(state) is None
    assert "expected_revision=0" in render_task_assessment(state)
    path = tmp_path / "runtime.json"
    result, _, _, _ = publish(state, run, path=path)
    assert not result.dispatch_failed
    assert result.metadata["task_assessment_commit"]["persisted"] is True
    assert current_task_assessment(state) == VALUE
    assert state.task_assessment_state["revision"] == 1
    restored = RuntimeState()
    assert restored.restore(json.loads(path.read_text()))
    restored.bind_run_context(run)
    initialize_task_assessment(restored, run, run.transcript, pure_continuation=True)
    assert current_task_assessment(restored) == VALUE
    assert restored.task_assessment_state["revision"] == 1


@pytest.mark.parametrize("change", [
    lambda v: v.update(complexity="L3"), lambda v: v.update(task_kinds="review"),
    lambda v: v.update(task_kinds=["delete_all"]), lambda v: v.update(permission="allow"),
    lambda v: v.update(uncertainties=[3]), lambda v: v.pop("reason"),
])
def test_invalid_authored_value_is_not_an_authority_channel(change):
    value = copy.deepcopy(VALUE)
    change(value)
    with pytest.raises(ValueError):
        parse_task_assessment(value)


def test_latest_call_replay_is_idempotent_but_replaced_old_call_is_stale(tmp_path):
    state, run = setup_run(tmp_path)
    result, scope, call, _ = publish(state, run)
    assert not result.dispatch_failed
    repeated = submit_task_assessment(state, scope, call, copy.deepcopy(result), run.transcript, run_context=run)
    assert repeated.metadata["task_assessment_commit"]["status"] == "already_accepted"
    assert state.task_assessment_state["revision"] == 1
    newer = {**VALUE, "complexity": "complex"}
    assert not publish(state, run, value=newer, revision=1, call_id="call-two")[0].dispatch_failed
    stale = submit_task_assessment(state, scope, call, copy.deepcopy(result), run.transcript, run_context=run)
    assert stale.dispatch_failed
    assert state.task_assessment_state["revision"] == 2
    assert current_task_assessment(state) == newer


def test_new_real_instruction_does_not_rebind_late_result_to_current_epoch(tmp_path):
    state, run = setup_run(tmp_path)
    previous, old, call, _ = publish(state, run)
    key = assessment_cache_key(state)
    user = {"role": "user", "content": "Now implement it."}
    attach_message_identity(user, session_id="intent-state")
    run.transcript.append(user)
    state.task_authority_epoch += 1
    initialize_task_assessment(state, run, run.transcript, pure_continuation=False)
    assert assessment_cache_key(state) != key
    result = submit_task_assessment(state, old, call, previous, run.transcript, run_context=run)
    assert result.dispatch_failed and current_task_assessment(state) is None
    assert "superseded" in result.metadata["task_assessment_commit"]["reason"]


def test_same_text_different_task_lineage_cannot_share_assessment(tmp_path):
    state, run = setup_run(tmp_path)
    assert not publish(state, run)[0].dispatch_failed
    other_state, other = setup_run(tmp_path, lineage="task-two", interaction="activation-two")
    other_state.task_assessment_state = copy.deepcopy(state.task_assessment_state)
    initialize_task_assessment(other_state, other, other.transcript, pure_continuation=True)
    assert current_task_assessment(other_state) is None


@pytest.mark.parametrize("interruption", ["cancel", "freeze", "followup"])
def test_interrupted_result_does_not_publish(tmp_path, interruption):
    state, run = setup_run(tmp_path)
    if interruption == "cancel":
        run.request_cancel()
    elif interruption == "freeze":
        run.freeze_execution(RunStatus.COMPLETED)
    result, _, _, _ = publish(state, run, superseded=lambda: interruption == "followup")
    assert result.dispatch_failed and current_task_assessment(state) is None


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_save_failure_preserves_previous_value_and_revision(monkeypatch, tmp_path, error_type):
    state, run = setup_run(tmp_path)
    assert not publish(state, run)[0].dispatch_failed
    before = copy.deepcopy(state.task_assessment_state)

    def fail(*_args):
        raise error_type("Could not save the private checkpoint")

    monkeypatch.setattr(state, "_write_snapshot", fail)
    result, _, _, _ = publish(state, run, value={**VALUE, "complexity": "complex"}, revision=1,
                             call_id="call-two", path=tmp_path / "state.json")
    assert result.dispatch_failed and state.task_assessment_state == before
    assert result.output == "Error: task assessment rejected: An internal error occurred."
    assert result.metadata["task_assessment_commit"]["reason"] == "An internal error occurred."


def test_cancellation_during_save_does_not_leave_new_value_on_disk(monkeypatch, tmp_path):
    state, run = setup_run(tmp_path)
    write = state._write_snapshot

    def interrupt(path, snapshot):
        write(path, snapshot)
        run.request_cancel()

    monkeypatch.setattr(state, "_write_snapshot", interrupt)
    path = tmp_path / "state.json"
    result, _, _, _ = publish(state, run, path=path)
    assert result.dispatch_failed and current_task_assessment(state) is None
    assert json.loads(path.read_text())["task_assessment_state"]["value"] is None


def test_memory_only_receipt_and_scope_are_explicit(tmp_path):
    state, run = setup_run(tmp_path)
    result, scope, _, _ = publish(state, run)
    assert result.metadata["task_assessment_commit"]["persisted"] is False
    assert scope.instruction_message_id == run.transcript[0][MESSAGE_ID_KEY]
    assert current_task_assessment(state) == VALUE
    detached = replace(scope, interaction_run_id="old-activation")
    assert publish(state, run, revision=1, scope=detached, call_id="call-two")[0].dispatch_failed


def test_same_model_response_accepts_only_first_current_revision(tmp_path):
    state, run = setup_run(tmp_path)
    result, scope, call, owner = publish(state, run)
    second = copy.deepcopy(call)
    second["id"] = "same-response-two"
    owner["tool_calls"].append(second)
    rejected = submit_task_assessment(state, scope, second, copy.deepcopy(result), run.transcript, run_context=run)
    assert rejected.dispatch_failed
    assert "stale assessment revision" in rejected.output
    assert state.task_assessment_state["revision"] == 1


def test_incomplete_restored_receipt_is_not_published_as_current(tmp_path):
    state, run = setup_run(tmp_path)
    assert not publish(state, run)[0].dispatch_failed
    del state.task_assessment_state["last_commit"]["scope"]
    initialize_task_assessment(state, run, run.transcript, pure_continuation=True)
    assert current_task_assessment(state) is None
    assert not publish(state, run, call_id="after-restore")[0].dispatch_failed


def test_same_call_cannot_replace_accepted_payload(tmp_path):
    state, run = setup_run(tmp_path)
    previous, scope, call, _ = publish(state, run)
    payload = {**VALUE, "complexity": "complex", "expected_revision": 0}
    call["function"]["arguments"] = json.dumps(payload)
    previous.tool_input = payload
    previous.metadata["task_assessment_proposal"] = payload
    rejected = submit_task_assessment(state, scope, call, previous, run.transcript, run_context=run)
    assert rejected.dispatch_failed
    assert "different payload" in rejected.output
    assert current_task_assessment(state) == VALUE


def test_standalone_proposal_cannot_bind_itself_to_current_task(tmp_path):
    state, run = setup_run(tmp_path)
    previous, _, call, _ = publish(state, run)
    rejected = submit_task_assessment(state, None, call, previous, run.transcript, run_context=run)
    assert rejected.dispatch_failed
    assert "context is unavailable" in rejected.output
    assert state.task_assessment_state["revision"] == 1


def test_restored_historical_receipt_is_not_replay_authority_for_new_activation(tmp_path):
    state, run = setup_run(tmp_path)
    result, source, call, _ = publish(state, run)
    restored, other = setup_run(tmp_path, interaction="activation-two")
    other.transcript[:] = copy.deepcopy(run.transcript)
    restored.task_assessment_state = copy.deepcopy(state.task_assessment_state)
    initialize_task_assessment(restored, other, other.transcript, pure_continuation=True)
    assert current_task_assessment(restored) == VALUE
    rejected = submit_task_assessment(restored, source, call, result, other.transcript, run_context=other)
    assert rejected.dispatch_failed
    assert "superseded" in rejected.output
    assert restored.task_assessment_state["revision"] == 1
