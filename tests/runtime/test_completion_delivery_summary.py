"""补测交接保留原任务，正式附录展示全部当前真实检查。"""
from __future__ import annotations

import json

from tests.verification_scenarios import VerificationScenario
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.verification.completion_gate import (
    ordinary_verification_guidance, verification_appendix,
)


FULL = 'python -m unittest discover -s tests -t . -v'
API = 'python -m unittest tests.test_api'


def _project(root):
    root.mkdir(exist_ok=True)
    (root / 'app.py').write_text('value = 2\n')
    (root / 'tests').mkdir()
    (root / 'tests/__init__.py').touch()
    for name in ['test_api.py', 'test_value.py']:
        (root / 'tests' / name).write_text(
            'import unittest\nfrom app import value\n'
            'class AppTest(unittest.TestCase):\n def test_value(self): self.assertEqual(value, 2)\n')


def _observe(scenario, root, command):
    result = scenario.settle_shell(command, cwd=root)
    assert all(receipt.exit_code == 0 for receipt in result.execution_facts), result.output


def test_summary_keeps_full_suite_after_later_api_subset(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = VerificationScenario(workspace=tmp_path, plan={"stages": []})
        vm.mutate(1, ["app.py"])
        _observe(vm, tmp_path, FULL)
        _observe(vm, tmp_path, API)
        frozen = vm.runtime.status()
        assert frozen['verification_needed'] is False
        assert frozen['last_verification']['command'] == API
        text = verification_appendix(frozen)
        assert FULL in text
        assert text.count(API) == 1


def test_contract_satisfied_plan_is_not_reported_as_executed(tmp_path):
    _project(tmp_path)
    unexecuted = 'python -m py_compile app.py'
    plan = {'stages': [{'name': 'static', 'required': True,
                       'commands': [{'command': unexecuted, 'required': True}]}]}
    with scoped_workdir(tmp_path):
        vm = VerificationScenario(workspace=tmp_path, plan=plan)
        vm.mutate(1, ["app.py"])
        vm.state.verification_definition = {"command": FULL, "targets": ["tests"]}
        vm.runtime.synchronize_task()
        _observe(vm, tmp_path, FULL)
        _observe(vm, tmp_path, API)
        text = verification_appendix(vm.runtime.status())
        assert FULL in text
        assert unexecuted not in text


def test_verified_commands_in_other_cwd_are_kept_separate(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    _project(first)
    _project(second)
    with scoped_workdir(first):
        vm = VerificationScenario(workspace=tmp_path, plan={"stages": []})
        vm.mutate(1, ["app.py"])
        _observe(vm, first, API)
        _observe(vm, second, API)
        text = verification_appendix(vm.runtime.status())
        assert str(first) in text and str(second) in text
        assert text.count(API) == 2


def test_current_report_does_not_restore_old_pass_after_source_mutation(tmp_path):
    _project(tmp_path)
    with scoped_workdir(tmp_path):
        vm = VerificationScenario(workspace=tmp_path, plan={"stages": []})
        vm.mutate(1, ["app.py"])
        _observe(vm, tmp_path, FULL)
        vm.mutate(2, ["app.py"])
        _observe(vm, tmp_path, API)
        text = verification_appendix(vm.runtime.status())
        assert FULL not in '\n'.join(line for line in text.splitlines() if '已采信检查' in line)
        assert '待验证（不是执行记录）' not in text
        assert vm.runtime.status()['verification_needed'] is False
        assert API in text


def test_guidance_returns_to_user_delivery_after_all_pending_checks(tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_api.py').touch()
    snapshot = {'test_obligations': [{'command': API, 'cwd': str(tmp_path), 'status': 'pending'}]}
    guidance = ordinary_verification_guidance(snapshot, tmp_path)
    assert API in guidance and str(tmp_path) in guidance
    # 该消息通过 user 角色发给模型，正文必须明确内部续跑及最终交付的范围。
    assert 'runtime-owned continuation' in guidance
    assert 'complete final response' in guidance
    assert 'run/use instructions' in guidance
    assert 'last verification command' in guidance


def test_native_two_repairs_preserve_delivery_instruction_and_all_confirmed_checks(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool

    _project(tmp_path)
    for name in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED',
                 'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM'):
        monkeypatch.setenv(name, '0')
    original = ('Perform delivery closure on the existing application.\nRequirements:\n'
                f'- Run the full suite: `{FULL}`.\n'
                f'- Then run the API check: `{API}`.\n'
                '- Include complete validation results and startup instructions.')
    unsupported_draft = 'UNSUPPORTED_OLD_DRAFT: 999 HTTP checks passed.'
    monkeypatch.setenv('NZ_PLANNING_ENABLED', '1')
    plan = {'objective': 'Verify delivery', 'requirements': [
        {'id': 'R1', 'description': 'Verify delivery behavior', 'kind': 'behavior',
         'satisfaction_mode': 'deterministic'},
    ], 'acceptance_commands': [FULL, API]}
    actions = [
        [_tool('bash', command=FULL + ' 2>&1 | tail -4 || true')],
        [_tool('bash', command=API + ' 2>&1 | tail -4 || true')],
        unsupported_draft,
        [_tool('bash', command=FULL)], 'Full suite checked.',
        [_tool('bash', command=API)], 'Last API check passed.',
    ]
    commands = tuple(action[0][1]['command'] for action in actions
                     if isinstance(action, list) and action[0][0] == 'bash')
    result, state, trace, requests, *_ = _run(
        monkeypatch, tmp_path, actions, task=original,
        allowed_commands=commands, limit=len(actions), planning_response=json.dumps(plan),
        semantic_response=["revise", "accept"],
    )
    assert result.status.value == 'completed', result.final_text
    assert result.metadata['verification']['verification_needed'] is False
    accepted = '\n'.join(line for line in result.final_text.splitlines() if '已采信检查' in line)
    assert FULL in accepted and API in accepted
    assert unsupported_draft not in result.final_text
    assert result.metadata['authored_content'] != unsupported_draft
    assert state['completion_gate_prompts'] == 0
    assert sum(e.get('event') == 'sidecar_finished' for e in trace) == 2
    assert any(message.get('role') == 'user' and message.get('content') == original
               for message in requests[-1]['messages'])
