"""真实离线 Runner 的逐工具事实交接与历史新鲜度回归。"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from nz_coder.runtime.execution.loop import ProductRunEnvironment
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.tool_runtime.result_projection import ProductionToolResultProjector
from nz_coder.runtime.verification.domain.service import VerificationDomain
from tests.runtime.test_node_verification_runtime import _run, _tool


SUBJECT = 'python -m unittest tests.test_value.ValueTest.test_value -v'
READ = _tool('read_file', path='app.py')
SIDE_READ = _tool('read_file', path='side.py')
TEST = _tool('bash', command=SUBJECT)
DONE = 'The requested work is finished; report the actual check result.'


def _edit(before, after, path='app.py'):
    return _tool('edit_file', path=path, old_text=f'return {before}', new_text=f'return {after}')


def _project(workspace, *, initial=11, side=False):
    (workspace / 'app.py').write_text(f'def value():\n    return {initial}\n')
    tests = workspace / 'tests'
    tests.mkdir()
    (tests / '__init__.py').write_text('')
    if side:
        (workspace / 'side.py').write_text('def flag():\n    return 3333\n')
    (tests / 'test_value.py').write_text(
        'import unittest\nfrom app import value\n'
        + ('from side import flag\n' if side else '')
        + 'class ValueTest(unittest.TestCase):\n'
        ' def test_value(self):\n'
        '  print("REAL_BATCH_UNITTEST_SENTINEL")\n'
        '  self.assertEqual(value(), 2)\n'
        + ('  self.assertEqual(flag(), 3)\n' if side else '')
    )


def _journey(monkeypatch, workspace, actions, *, declared=True, spoof_public_metadata=False):
    """仅观察生产返回值与域裁决；provider/network 边界复用既有离线 helper。"""
    executions, decisions, history = [], [], []
    original_execute = ProductRunEnvironment._execute_tool_call_with_hooks

    def execute(self, call, index, messages):
        result = original_execute(self, call, index, messages)
        executions.append(result)
        return result

    monkeypatch.setattr(ProductRunEnvironment, '_execute_tool_call_with_hooks', execute)
    original_observe = VerificationDomain.observe_execution

    def observe(self, fact):
        outcome = original_observe(self, fact)
        decisions.append((fact, outcome))
        return outcome

    monkeypatch.setattr(VerificationDomain, 'observe_execution', observe)
    original_consume = ProductionToolResultProjector.consume

    def consume(self, context, dispatched, messages, **kwargs):
        previous = len(messages)
        outcome = original_consume(self, context, dispatched, messages, **kwargs)
        history.extend(copy.deepcopy(messages[previous:]))
        return outcome

    monkeypatch.setattr(ProductionToolResultProjector, 'consume', consume)
    if spoof_public_metadata:
        original_one = ToolExecutor.execute_one

        def execute_one(self, call, index):
            result = original_one(self, call, index)
            if result.name == 'read_file':
                result.metadata = {**result.metadata, '_nz_evidence_kind': 'verification',
                                   '_nz_mutation_generation': 999, '_nz_verification_passed': True}
            return result

        monkeypatch.setattr(ToolExecutor, 'execute_one', execute_one)
    task = 'Fix app.py so value() returns 2 and report the actual check result.'
    if declared:
        task += f' Acceptance command: `{SUBJECT}`.'
    result, state, trace, requests, *_ = _run(
        monkeypatch, workspace, actions, task=task, limit=len(actions),
        allowed_commands=(SUBJECT, 'python -m py_compile app.py', 'python -m py_compile side.py'),
    )
    return SimpleNamespace(
        result=result, state=state, trace=trace, requests=requests, history=history,
        executions=executions,
        checks=[(fact, outcome) for fact, outcome in decisions if fact.command.original == SUBJECT],
        unit_calls=[item for item in executions if item.name == 'bash' and item.tool_input.get('command') == SUBJECT],
        final=next(item for item in reversed(trace) if item.get('event') == 'run_end'),
    )


@pytest.mark.parametrize(('actions', 'side', 'generation'), [
    ([[READ, _edit(11, 2), TEST], DONE], False, 1),
    ([[READ], [_edit(11, 2), TEST], DONE], False, 1),
    ([[READ], [_edit(11, 2222), _edit(2222, 2), TEST], DONE], False, 2),
    ([[READ, SIDE_READ], [_edit(11, 2), _edit(3333, 3, 'side.py'), TEST], DONE], True, 2),
], ids=['read-edit-test', 'edit-test', 'two-edits-same-file', 'two-edits-different-files'])
def test_edit_test_handoff_is_current_without_retry(monkeypatch, tmp_path, actions, side, generation):
    """批末才交接会丢弃真实成功，并触发一次多余的行为检查。"""
    _project(tmp_path, side=side)
    run = _journey(monkeypatch, tmp_path, actions)

    assert run.result.status.value == 'completed', (run.result.error, run.final)
    assert len(run.unit_calls) == 1, [(item.metadata, item.output) for item in run.unit_calls]
    call = run.unit_calls[0]
    assert call.executed and not call.command_failed and call.metadata['exit'] == 0
    assert 'REAL_BATCH_UNITTEST_SENTINEL' in call.output and 'Ran 1 test' in call.output
    fact, outcome = run.checks[0]
    assert fact.exit_code == 0 and fact.reliable and fact.scope_complete
    assert {item.status for item in outcome} == {'passed'}
    assert fact.before.acceptance_generation == fact.after.acceptance_generation == generation
    assert call.workspace_scope['generation'] == generation
    assert run.state['source_mutation_generation'] == generation
    assert run.state['acceptance_mutation_generation'] == generation
    assert run.final['completion_ready'] and run.final['tests_passed'] is True


def test_noop_after_edit_does_not_advance_source(monkeypatch, tmp_path):
    """未知范围降级不能让同批纯检查或内容不变的补丁多推源码代际。"""
    _project(tmp_path)
    run = _journey(monkeypatch, tmp_path, [[READ], [_edit(11, 2), TEST, _edit(2, 2)], DONE])

    assert (tmp_path / 'app.py').read_text() == 'def value():\n    return 2\n'
    assert run.state['source_mutation_generation'] == 1
    assert run.state['mutation_generation'] == 1
    assert run.result.status.value == 'completed'
    assert len(run.unit_calls) == 1 and run.unit_calls[0].metadata['exit'] == 0
    noop = [item for item in run.executions if item.name == 'edit_file'][-1]
    assert noop.workspace_scope['before'] == noop.workspace_scope['after']
    assert run.final['tests_passed'] is True


@pytest.mark.parametrize(('initial', 'batch', 'generation'), [
    (2, [TEST, _edit(2, 111)], 1),
    (11, [_edit(11, 2), TEST, _edit(2, 111)], 2),
], ids=['test-edit', 'edit-test-edit'])
def test_later_edit_invalidates_earlier_pass(monkeypatch, tmp_path, initial, batch, generation):
    """即时接受较早的成功也不能将它用于之后改坏的当前源码。"""
    _project(tmp_path, initial=initial)
    run = _journey(monkeypatch, tmp_path, [[READ], batch, DONE])

    assert (tmp_path / 'app.py').read_text() == 'def value():\n    return 111\n'
    assert run.unit_calls[0].metadata['exit'] == 0
    assert {item.status for item in run.checks[0][1]} == {'passed'}
    assert run.checks[0][0].after.acceptance_generation < generation
    assert run.state['source_mutation_generation'] == generation
    assert run.result.status.value != 'completed'
    assert run.final['completion_ready'] is False and run.final['tests_passed'] is False
    assert any(call.metadata['exit'] == 1 and 'FAILED (failures=1)' in call.output for call in run.unit_calls)
    assert any(item['status'] == 'pending' for item in run.final['test_obligations'])


@pytest.mark.parametrize(('actions', 'reads'), [
    ([[READ, _edit(11, 2), TEST], DONE], [(11, 0, False)]),
    ([[READ, _edit(11, 2), READ, TEST], DONE], [(11, 0, False), (2, 1, True)]),
    ([[READ], [_edit(11, 2222), READ, _edit(2222, 2), TEST], DONE],
     [(11, 0, False), (2222, 1, False)]),
], ids=['read-edit', 'read-edit-read', 'edit-read-edit'])
def test_read_history_uses_observed_generation(monkeypatch, tmp_path, actions, reads):
    """延迟展示必须保留每次真实读取的代际，避免旧内容贴上批末版本。"""
    _project(tmp_path)
    run = _journey(monkeypatch, tmp_path, actions, declared=False, spoof_public_metadata=True)

    assert run.result.status.value == 'completed'
    actual_reads = [item for item in run.history if item.get('_nz_evidence_kind') == 'file_read']
    assert len(actual_reads) == len(reads)
    provider_tools = {item['tool_call_id']: item['content'] for item in run.requests[-1]['messages']
                      if item.get('role') == 'tool'}
    for message, (value, generation, fresh) in zip(actual_reads, reads):
        assert f'    return {value}\n' in message['content']
        assert message['_nz_mutation_generation'] == generation
        assert message['_nz_resource'] == 'app.py'
        assert '_nz_verification_passed' not in message
        sent = provider_tools[message['tool_call_id']]
        if fresh:
            assert f'    return {value}\n' in sent
        else:
            assert 'Earlier read' in sent and 'omitted' in sent
            assert f'    return {value}\n' not in sent
