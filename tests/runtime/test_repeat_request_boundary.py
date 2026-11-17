"""冻结不消费，正式可用提交确认；观察层可以单独撤回。"""
import copy
from dataclasses import replace

import pytest

from nz_coder.runtime.conversation.model_result import LLMResult
from nz_coder.runtime.conversation.prompt_builder import build_context_layers
from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot, acknowledge_usable_repeat_notice
from nz_coder.runtime.verification.recovery import RecoveryState
from test_task_intent_state import setup_run
from test_repeat_observations import observe


def ready(tmp_path):
    state, run = setup_run(tmp_path)
    recovery = RecoveryState()
    source = recovery.capture_repeat_source(state, run, "worker", run.transcript)
    observe(recovery, source)
    observe(recovery, source)
    notice = recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)
    snapshot = FrozenRequestSnapshot("fixed", "frozen memory", "rule", "version", copy.deepcopy, (), repeat_notice=notice)
    return state, run, recovery, snapshot


def test_projection_estimate_and_optional_clipping_keep_whole_notice(tmp_path):
    state, run, recovery, snapshot = ready(tmp_path)
    before = copy.deepcopy(run.transcript)
    without = replace(snapshot, repeat_notice=None)
    for _ in range(3):
        assert "<repeat-observation>" in str(snapshot.project(run.transcript))
        assert snapshot.estimate(run.transcript) > without.estimate(run.transcript)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) == snapshot.repeat_notice
    assert run.transcript == before
    _, dynamic, stats = build_context_layers("fixed", "background " * 1000, "", "", 1000,
                                            reserved_block=snapshot.repeat_notice.text)
    assert stats["after_reserved_tokens"] > 0
    assert stats["after_total_tokens"] <= 1000
    assert snapshot.repeat_notice.text not in dynamic
    assert replace(snapshot, repeat_notice=None).dynamic == "frozen memory"


@pytest.mark.parametrize("result", [LLMResult(), LLMResult(extra={"reasoning_content": "thinking"}),
    LLMResult(content="incomplete", finish_reason="length"), LLMResult(content="failed", finish_reason="error"),
    LLMResult(tool_calls=[{"id": "partial", "function": {"name": "bash", "arguments": "{"}}])])
def test_unusable_response_does_not_acknowledge(tmp_path, result):
    state, run, recovery, snapshot = ready(tmp_path)
    acknowledge_usable_repeat_notice(snapshot, result, recovery, state, run, "worker", run.transcript)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) == snapshot.repeat_notice


def test_usable_commit_ack_is_idempotent_and_does_not_clear_new_event(tmp_path):
    state, run, recovery, snapshot = ready(tmp_path)
    result = LLMResult(content="Continue observing.", finish_reason="stop")
    acknowledge_usable_repeat_notice(snapshot, result, recovery, state, run, "worker", run.transcript)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    source = snapshot.repeat_notice.source
    observe(recovery, source, output="changed")
    observe(recovery, source, output="changed")
    newer = recovery.repeat_notice_snapshot(state, run, "worker", run.transcript)
    acknowledge_usable_repeat_notice(snapshot, result, recovery, state, run, "worker", run.transcript)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) == newer


def test_compaction_refreshes_only_unsent_source_identity(tmp_path):
    state, run, recovery, snapshot = ready(tmp_path)
    source = snapshot.repeat_notice.source
    recovery.reset_tool_call_history("context_compacted", context_changed=True)
    refreshed = recovery.refresh_repeat_source_after_compaction(source, state, run, "worker", run.transcript)
    assert replace(refreshed, observer_epoch=source.observer_epoch) == source
    assert not recovery.repeat_source_current(source, state, run, "worker", run.transcript)
    assert recovery.repeat_notice_snapshot(state, run, "worker", run.transcript) is None
    assert recovery.refresh_repeat_source_after_compaction(source, state, run, "other", run.transcript) is None
    state.task_authority_epoch += 1
    assert recovery.refresh_repeat_source_after_compaction(source, state, run, "worker", run.transcript) is None


@pytest.mark.parametrize("reply, expected_prompts", [("once", 2), ("always", 1)])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_repeat_approval_preserves_real_once_and_always_protocol(reply, expected_prompts, asynchronous):
    import asyncio
    from dataclasses import replace
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.tool_runtime.policy import ProductionToolPolicy
    from tests.runtime.tool_runtime.test_focused_policy import _context

    prompts = []
    context = replace(_context(), permissions=PermissionManager("default", asker=lambda name, metadata: (
        prompts.append((name, metadata)) or reply)))
    policy = ProductionToolPolicy()
    call = {"id": "repeat", "function": {"name": "process", "arguments": {"id": "one"}}}
    for _ in range(2):
        assert policy.find_repeated_tool_calls(context, [call]) == {}
        assert policy.find_repeated_tool_calls(context, [call]) == {}
        blocked = policy.find_repeated_tool_calls(context, [call])
        assert blocked[0].metadata == {"stall_kind": "consecutive"}
        remaining = (asyncio.run(policy.resolve_doom_loop_permissions_async(context, blocked, [call]))
                     if asynchronous else policy.resolve_doom_loop_permissions(context, blocked, [call]))
        assert remaining == {}
    assert len(prompts) == expected_prompts
    assert all(name == "doom_loop" for name, _ in prompts)


def test_historical_stall_purpose_roundtrip_does_not_recreate_observer(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    state = RuntimeState()
    state.observe_provider_call("stall_sidecar", provider_id="offline", model_id="old",
                               usage={"input": 2, "output": 1, "total": 3}, attempts=1, duration_ms=1)
    restored = RuntimeState()
    assert restored.restore(state.to_dict())
    assert restored.provider_calls_by_purpose["stall_sidecar"] == 1
    assert restored.provider_usage["total"] == 3
    assert RecoveryState()._repeat_notice is None
