"""重复事实只来自真实结算，并且绑定发送请求时的运行身份。"""

import pytest

from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.verification.recovery import RecoveryState
from test_task_intent_state import setup_run


def observe(recovery, source, *, output="waiting", args=None, **flags):
    args = args or {"id": "job-one"}
    recovery.observe_tool_call("process", args, threshold=3)
    return recovery.record_tool_result_evidence(
        "process", args, output, executed=True, dispatch_failed=False,
        source=source, **flags,
    )


def test_notice_counts_execution_not_candidates_and_ack_is_once(tmp_path):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    for _ in range(8):
        recovery.observe_tool_call("process", {"id": "job-one"}, threshold=3)
    observe(recovery, source)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    observe(recovery, source)
    notice = recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)
    assert notice.executed_repeat_count == 2
    assert notice.result_relation == "normalized_evidence_equal"
    assert len(notice.text) <= 1000
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) == notice
    recovery.acknowledge_repeat_notice(notice.notice_id, source)
    observe(recovery, source)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None


@pytest.mark.parametrize("boundary", ["run", "persona", "instruction", "mutation", "cancel", "freeze", "compact"])
def test_old_source_cannot_publish_or_ack_new_context(tmp_path, boundary):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, source)
    observe(recovery, source)
    persona = "worker"
    if boundary == "run":
        run.interaction_run_id = "new-run"
    elif boundary == "persona":
        persona = "reviewer"
    elif boundary == "instruction":
        state.task_authority_epoch += 1
    elif boundary == "mutation":
        state.mutation_generation += 1
    elif boundary == "cancel":
        run.request_cancel()
    elif boundary == "freeze":
        run.freeze_execution(RunStatus.COMPLETED)
    else:
        recovery.reset_tool_call_history("context_compacted", context_changed=True)
    assert not recovery.repeat_source_current(source, state, run, persona, run.transcript)
    assert recovery.repeat_notice_snapshot(state, run, persona, run.transcript) is None


def test_new_evidence_and_arguments_replace_event_without_invalidating_request(tmp_path):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, source)
    observe(recovery, source)
    old = recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)
    assert observe(recovery, source, output="complete")
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    observe(recovery, source, output="complete")
    newer = recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)
    assert newer.notice_id != old.notice_id
    recovery.acknowledge_repeat_notice(old.notice_id, source)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) == newer
    recovery.observe_tool_call("process", {"id": "job-two"}, threshold=3)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    assert recovery.repeat_source_current(source, state, run, "worker", run.transcript)


def test_cache_dispatch_failure_and_detached_result_do_not_create_repeat(tmp_path):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, source)
    observe(recovery, source, cache_hit=True)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    recovery.record_tool_result_evidence("process", {"id": "job-one"}, "waiting",
        executed=False, dispatch_failed=True, source=source)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    observe(recovery, None)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None


def test_run_reset_clears_digest_and_persona_roundtrip_rejects_late_source(tmp_path):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    old = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, old)
    observe(recovery, old)
    recovery.start_tool_call_run()
    assert recovery._last_tool_result_digest is None
    recovery.reset_tool_call_history("agent_transition", context_changed=True)
    recovery.reset_tool_call_history("agent_transition", context_changed=True)
    assert not recovery.repeat_source_current(old, state, run, "worker", run.transcript)
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, old)
    observe(recovery, old)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    observe(recovery, source)
    observe(recovery, source)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)


def test_no_formal_instruction_does_not_invent_scope(tmp_path):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    state.task_assessment_state["binding"]["instruction_message_id"] = ""
    run.transcript.clear()
    assert recovery.capture_repeat_source(state, run, "worker", []) is None
    assert recovery.capture_repeat_source(state, None, "worker", []) is None


@pytest.mark.parametrize("flags", [
    {"executed": False, "dispatch_failed": True},
    {"executed": True, "dispatch_failed": True},
    {"executed": True, "dispatch_failed": False, "cache_hit": True},
])
def test_unexecuted_changed_arguments_still_end_old_result_event(tmp_path, flags):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, source)
    observe(recovery, source)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)
    recovery.record_tool_result_evidence("process", {"id": "different"}, "not new evidence",
                                         source=source, **flags)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    assert recovery.repeat_source_current(source, state, run, "worker", run.transcript)
