"""真实消费入口的摘要保留与硬预算；数据是人工夹具，不是模型收益实验。"""
from types import SimpleNamespace

from nz_coder.runtime.conversation.prompt_builder import build_context_layers
from nz_coder.state.context import estimate_request_tokens, prompt_budget
from nz_coder.tools.scratchpad import Scratchpad, scoped_scratchpad
from test_planning_recovery import planning_agent as planning_agent


def test_summaries_and_fixed_facts_exceed_soft_target_without_truncation():
    summary = 'SUMMARY ' * 10000 + 'LAST_SUMMARY'
    fixed = 'INSTRUCTION ' * 5000
    facts = 'CURRENT_FACT ' * 100
    stable, dynamic, stats = build_context_layers(fixed, 'BACKGROUND ' * 1000,
        'ORDINARY_STATE ' * 1000, summary, 6000, essential_block=facts)
    assert stable == fixed
    assert summary in dynamic
    assert facts in dynamic
    assert 'BACKGROUND' not in dynamic and 'ORDINARY_STATE' not in dynamic
    assert stats['after_total_tokens'] > 6000


def test_parent_summary_snapshot_has_all_sections_and_no_false_read_promise(tmp_path):
    from nz_coder.runtime.agent.subagent import _parent_context_block
    pad = Scratchpad(tmp_path / 'notes.json')
    long = 'PARENT_FOCUS ' * 400
    pad.update_section('focus', [{'summary': long, 'details': 'PARENT_PRIVATE_DETAILS'}])
    pad.update_section('lessons', [{'summary': 'FINAL_LESSON', 'details': ''}])
    with scoped_scratchpad(pad):
        block = _parent_context_block()
    pad.clear()
    assert long in block and 'FINAL_LESSON' in block
    assert 'PARENT_PRIVATE_DETAILS' not in block
    assert 'read_scratchpad' not in block


def test_projected_request_counts_full_summaries_without_freezing(planning_agent):
    agent, _ = planning_agent([])
    agent._sp = Scratchpad(persistent=False)
    history = [{'role': 'user', 'content': agent.runtime_state.initial_task_text}]
    baseline = agent._projected_request_tokens(history)
    summary = 'SUMMARY ' * 15000
    agent._sp.update_section('focus', [{'summary': summary, 'details': 'DETAILS ' * 10000}])
    before = agent._sp.snapshot()
    assert agent._projected_request_tokens(history) > baseline + 10000
    assert agent._sp.snapshot() == before






def test_soft_overflow_summary_fits_complete_request_but_hard_overflow_does_not():
    summary = 'SUMMARY ' * 10000
    system, dynamic, _ = build_context_layers('system', '', '', summary, 6000,
                                              essential_block='current facts')
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': dynamic}]
    estimate = estimate_request_tokens(messages, [])
    assert estimate > 6000
    assert estimate < prompt_budget(100000, 8192).usable_input_tokens
    assert estimate > prompt_budget(10000, 8192).usable_input_tokens


def test_final_preflight_rejects_full_summary_over_hard_budget():
    from nz_coder.runtime.execution.services import ProductionTurnModelRuntime
    summary = 'FULL_SUMMARY ' * 10000
    system, dynamic, _ = build_context_layers('system', '', '', summary, 6000)
    messages = [{'role': 'system', 'content': system}, {'role': 'user', 'content': dynamic}]
    events = []
    context = SimpleNamespace(prompt_budget=lambda: prompt_budget(10000, 8192),
                              trace=lambda *a, **kw: events.append(a))
    result = ProductionTurnModelRuntime().preflight_request(context, messages, tools=[])
    assert result.needs_compaction
    assert 'exceeds' in result.compaction_error
    assert summary in messages[1]['content']
    assert events
