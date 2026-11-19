"""工作计划只改唯一正文，来源、提交版本和保存结果必须一致。"""
from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, attach_message_identity
from nz_coder.runtime.agent import planning_runtime as plans
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.tool_platform.execution import ToolExecutionResult
from test_task_intent_state import setup_run


PLAN = [{"title": "Inspect resource ownership", "target": "runtime/command.py",
         "verification": "Reproduce cancellation without leftover processes"}]
TEXT = ("## Plan\n1. Inspect resource ownership - runtime/command.py - "
        "Reproduce cancellation without leftover processes")


def publish(state, run, *, steps=None, revision=0, scope=None, call_id="plan-one", path=None,
            superseded=lambda: False, extra=None):
    from nz_coder.tools.working_plan import update_working_plan

    payload = {"plan": copy.deepcopy(PLAN if steps is None else steps), "expected_revision": revision}
    proposal = update_working_plan(**payload)
    raw = {**payload, **(extra or {})}
    call = {"id": call_id, "type": "function", "function": {
        "name": "update_working_plan", "arguments": json.dumps(raw)}}
    owner = {"role": "assistant", "content": "", "tool_calls": [call]}
    attach_message_identity(owner, session_id="intent-state")
    run.transcript.append(owner)
    scope = scope or plans.capture_working_plan_scope(state, run, owner[MESSAGE_ID_KEY], run.transcript)
    result = ToolExecutionResult("update_working_plan", payload, str(proposal), True, False, False, False,
                                 metadata=proposal.metadata)
    return plans.submit_working_plan(state, scope, call, result, run.transcript, run_context=run,
                                     path=path, superseded=superseded), scope, call, owner


def test_published_route_persists_without_becoming_initial_plan_or_verification(tmp_path):
    state, run = setup_run(tmp_path)
    before = (copy.deepcopy(state.task_contract), state.mutation_generation, state.verification_attempts)
    path = tmp_path / "runtime.json"
    result, _, _, _ = publish(state, run, path=path)
    assert not result.dispatch_failed, result.output
    assert state.plan_text == TEXT and state.plan_revision == 1
    assert state.plan_generated is False
    assert before == (state.task_contract, state.mutation_generation, state.verification_attempts)
    assert result.metadata["working_plan_commit"] == {
        "status": "accepted", "revision": 1, "persisted": True, "reason": ""}
    restored = RuntimeState()
    assert restored.restore(json.loads(path.read_text()))
    assert restored.plan_text == TEXT and restored.plan_revision == 1
    assert restored.plan_last_commit == state.plan_last_commit


def test_unchanged_commits_advance_receipt_but_latest_replay_does_not(tmp_path):
    state, run = setup_run(tmp_path)
    first, old, call, _ = publish(state, run)
    second, scope, newer, _ = publish(state, run, revision=1, call_id="plan-two")
    assert second.metadata["working_plan_commit"]["status"] == "unchanged"
    assert state.plan_revision == 2 and state.plan_last_commit["call_id"] == "plan-two"
    again = plans.submit_working_plan(state, scope, newer, copy.deepcopy(second), run.transcript, run_context=run)
    assert again.metadata["working_plan_commit"]["status"] == "already_accepted"
    assert state.plan_revision == 2
    stale = plans.submit_working_plan(state, old, call, first, run.transcript, run_context=run)
    assert stale.dispatch_failed and "stale" in stale.output


def test_same_receipt_id_cannot_replace_payload_even_after_unchanged(tmp_path):
    state, run = setup_run(tmp_path)
    publish(state, run)
    result, scope, call, _ = publish(state, run, revision=1, call_id="plan-two")
    payload = {"plan": [{"title": "Different route"}], "expected_revision": 1}
    call["function"]["arguments"] = json.dumps(payload)
    result.tool_input = payload
    result.metadata["working_plan_proposal"] = payload
    rejected = plans.submit_working_plan(state, scope, call, result, run.transcript, run_context=run)
    assert rejected.dispatch_failed and "different payload" in rejected.output
    assert state.plan_text == TEXT and state.plan_revision == 2


@pytest.mark.parametrize("change", ["cancel", "freeze", "followup", "epoch", "instruction", "task", "run"])
def test_late_result_cannot_publish_to_changed_run(tmp_path, change):
    state, run = setup_run(tmp_path)
    initial, scope, call, _ = publish(state, run)
    if change == "cancel":
        run.request_cancel()
    elif change == "freeze":
        run.freeze_execution(RunStatus.COMPLETED)
    elif change == "epoch":
        state.task_authority_epoch += 1
    elif change == "instruction":
        message = {"role": "user", "content": "A separate instruction"}
        attach_message_identity(message, session_id="intent-state")
        run.transcript.append(message)
    elif change == "task":
        scope = replace(scope, task=replace(scope.task, task_lineage_id="different"))
    elif change == "run":
        scope = replace(scope, interaction_run_id="different")
    result = plans.submit_working_plan(state, scope, call, initial, run.transcript,
                                      run_context=run, superseded=lambda: change == "followup")
    assert result.dispatch_failed and state.plan_revision == 1


@pytest.mark.parametrize("extra", [{"acceptance_commands": ["true"]}, {"permission": "allow"}])
def test_original_unknown_arguments_are_rejected_after_dispatch_filtering(tmp_path, extra):
    state, run = setup_run(tmp_path)
    result, _, _, _ = publish(state, run, extra=extra)
    assert result.dispatch_failed and not state.plan_text


def test_unknown_step_fields_cannot_publish_a_second_contract(tmp_path):
    state, run = setup_run(tmp_path)
    result, scope, call, _ = publish(state, run)
    payload = {"plan": [{"title": "Route", "requirements": ["forged"]}], "expected_revision": 1}
    call["function"]["arguments"] = json.dumps(payload)
    result.tool_input = payload
    result.metadata["working_plan_proposal"] = payload
    scope = replace(scope, revision=1)
    rejected = plans.submit_working_plan(state, scope, call, result, run.transcript, run_context=run)
    assert rejected.dispatch_failed and state.plan_text == TEXT


@pytest.mark.parametrize("error_type", [OSError, ValueError])
def test_save_failure_preserves_body_revision_and_receipt(monkeypatch, tmp_path, error_type):
    state, run = setup_run(tmp_path)
    publish(state, run)
    before = copy.deepcopy(state.plan_last_commit)
    def fail(*_args):
        raise error_type("Could not save the private checkpoint")
    monkeypatch.setattr(state, "_write_snapshot", fail)
    result, _, _, _ = publish(state, run, steps=[{"title": "New route"}], revision=1,
                             call_id="plan-two", path=tmp_path / "state.json")
    assert result.dispatch_failed
    assert result.output == "Error: working plan rejected: An internal error occurred."
    assert result.metadata["working_plan_commit"]["reason"] == "An internal error occurred."
    assert state.plan_revision == 1 and state.plan_text == TEXT and state.plan_last_commit == before


def test_cancel_during_write_restores_previous_disk_value(monkeypatch, tmp_path):
    state, run = setup_run(tmp_path)
    original = state._write_snapshot
    def interrupt(path, snapshot):
        original(path, snapshot)
        run.request_cancel()
    monkeypatch.setattr(state, "_write_snapshot", interrupt)
    path = tmp_path / "runtime.json"
    result, _, _, _ = publish(state, run, path=path)
    assert result.dispatch_failed and state.plan_text == "" and state.plan_revision == 0
    data = json.loads(path.read_text())
    assert data["plan_text"] == "" and data["plan_revision"] == 0


def test_same_response_can_commit_only_first_frozen_revision(tmp_path):
    state, run = setup_run(tmp_path)
    result, scope, call, owner = publish(state, run)
    second = copy.deepcopy(call)
    second["id"] = "plan-two"
    owner["tool_calls"].append(second)
    rejected = plans.submit_working_plan(state, scope, second, result, run.transcript, run_context=run)
    assert rejected.dispatch_failed and state.plan_revision == 1


def test_unbound_proposal_has_no_permission_to_write_current_route(tmp_path):
    state, run = setup_run(tmp_path)
    result, _, call, _ = publish(state, run)
    rejected = plans.submit_working_plan(state, None, call, result, run.transcript, run_context=run)
    assert rejected.dispatch_failed and state.plan_revision == 1


def test_restoring_old_snapshot_clears_previous_receipt_and_revision(tmp_path):
    state, run = setup_run(tmp_path)
    publish(state, run)
    assert state.restore({"active": True, "plan_text": "Old advisory route", "plan_generated": True})
    assert state.plan_text == "Old advisory route" and state.plan_generated is True
    assert state.plan_revision == 0 and state.plan_last_commit == {}


def test_initial_plan_uses_same_body_and_version_with_distinct_source(tmp_path):
    state, run = setup_run(tmp_path)
    scope = plans.capture_working_plan_scope(state, run, "", run.transcript)
    path = tmp_path / "state.json"
    plans.install_initial_working_plan(state, scope, TEXT, run_context=run, path=path)
    assert state.plan_text == TEXT and state.plan_generated and state.plan_revision == 1
    assert state.plan_last_commit["source_kind"] == "initial"
    assert json.loads(path.read_text())["plan_text"] == TEXT


@pytest.mark.parametrize("receipt", [None, "bad", {"call_id": "broken"}])
def test_malformed_restored_receipt_cannot_be_replay_authority(tmp_path, receipt):
    state, run = setup_run(tmp_path)
    assert state.restore({"active": True, "plan_text": "Advisory", "plan_revision": 3,
                          "plan_last_commit": receipt})
    assert state.plan_revision == 3 and state.plan_last_commit == {}
    result, _, _, _ = publish(state, run, revision=3)
    assert not result.dispatch_failed and state.plan_revision == 4


def test_invalidation_preserves_unloaded_checkpoint_fields(tmp_path):
    state, run = setup_run(tmp_path)
    publish(state, run)
    run.finish(RunStatus.COMPLETED)
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"active": False, "plan_text": "Future", "plan_generated": True,
                               "plan_revision": 8, "plan_last_commit": {"call_id": "future"},
                               "mutation_generation": 123}))
    plans.invalidate_working_plan(state, path=path)
    assert state.plan_text == "" and state.plan_revision == 0 and state.plan_last_commit == {}
    assert json.loads(path.read_text()) == {"active": False, "plan_text": "", "plan_generated": False,
        "plan_revision": 0, "plan_last_commit": {}, "mutation_generation": 123}


def test_invalidation_write_failure_does_not_publish_empty_memory(monkeypatch, tmp_path):
    state, run = setup_run(tmp_path)
    publish(state, run)
    run.finish(RunStatus.COMPLETED)
    def fail(*_args):
        raise OSError("disk full")
    monkeypatch.setattr(state, "_write_snapshot", fail)
    with pytest.raises(OSError):
        plans.invalidate_working_plan(state, path=tmp_path / "state.json")
    assert state.plan_text == TEXT and state.plan_revision == 1


def test_frozen_but_unsettled_run_still_refuses_plan_invalidation(tmp_path):
    state, run = setup_run(tmp_path)
    publish(state, run)
    run.freeze_execution(RunStatus.COMPLETED)
    with pytest.raises(ValueError, match="active run"):
        plans.invalidate_working_plan(state)
    assert state.plan_text == TEXT
