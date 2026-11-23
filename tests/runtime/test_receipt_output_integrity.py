"""完整进程输出的分类及恢复边界。"""
import shlex
import sys
import threading
from types import SimpleNamespace

import pytest

from nz_coder.runtime.adapters.verification_facts import host_execution_receipts, bind_execution_facts
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tools.bash import run_bash
from nz_coder.runtime.verification.domain.checkpoint import AuthoritySourceUnavailable, SCHEMA, restore_domain
from nz_coder.runtime.verification.domain.models import CheckSpec, RunScope, TaskScope, WorkspaceVersion
from nz_coder.runtime.verification.domain.rules import evaluate_check, is_test_selection_failure
from nz_coder.runtime.verification.domain.service import VerificationDomain


def test_long_real_unittest_output_survives_receipt_and_checkpoint(tmp_path):
    (tmp_path / 'test_example.py').write_text('import unittest\nclass Cases(unittest.TestCase):\n pass\n')
    argv = [sys.executable, '-m', 'unittest', *[f'test_example.Cases.test_missing_{i}' for i in range(12)]]
    command = shlex.join(argv)
    with scoped_workdir(tmp_path):
        output = run_bash(command)
    receipt, = host_execution_receipts('bash', output)
    assert receipt.exit_code == 1 and len(str(output)) > 2000
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    version = WorkspaceVersion()
    fact, = bind_execution_facts(SimpleNamespace(executed=True, permission_denied=False, execution_facts=(receipt,)), scope, version, version)
    spec = CheckSpec('test', fact.command, 'test', required=True, source='user_declared')
    other = CheckSpec('other', fact.command.__class__(('pytest', 'other.py'), str(tmp_path), original='pytest other.py'), 'test')
    assert fact.test_selection_failure == 'unittest_method_lookup'
    assert len(fact.reason) <= 2000
    assert evaluate_check(spec, fact, version).status == 'pending'
    assert not is_test_selection_failure(other, fact)
    assert evaluate_check(other, fact, version).status == 'pending'
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution(fact.execution_id, (spec.id,))
    domain.observe_execution(fact)
    saved = domain.checkpoint()
    assert saved['schema'] == SCHEMA
    restored = restore_domain(saved, lock=threading.RLock(), scope=scope, version=version)
    assert restored.snapshot().executions[0].test_selection_failure == 'unittest_method_lookup'
    assert not restored.snapshot().obligations


def test_invalid_checkpoint_raises_controlled_error(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    with pytest.raises(CheckpointRestoreError):
        restore_domain({'schema': 'invalid'}, lock=threading.RLock(), scope=scope, version=WorkspaceVersion())


def _restore_legacy_with_instruction(saved, scope, version, instruction):
    from nz_coder.runtime.adapters.verification_runtime import _restore_verification_domain
    from nz_coder.runtime.execution.runtime_state import RuntimeState

    state = RuntimeState()
    state.initial_task_text = instruction
    state._restored_verification_domain = saved
    user = {"role": "user", "content": instruction, "_nz_message_id": "msg-request"}
    return _restore_verification_domain(state, session_id=scope.task.session_id, workspace=scope.task.workspace,
        interaction_run_id=scope.interaction_run_id, agent_id=scope.agent_id, lock=threading.RLock(),
        version=version, require_targeted=False, messages=[user])


def test_v1_negative_fact_and_references_are_preserved(tmp_path):
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    version = WorkspaceVersion()
    from nz_coder.runtime.verification.domain.identity import command_identity
    command = command_identity('pytest missing.py', str(tmp_path))
    spec = CheckSpec('selected', command, 'test', required=True, source='user_declared')
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution('old-failure', (spec.id,))
    domain.observe_execution(ExecutionFact('old-failure', scope, command, 'failed', version, version,
        capabilities=('test',), exit_code=5, test_count=0, reason='no tests ran'))
    saved = domain.checkpoint()
    saved['schema'] = 'nz.verification.v1'
    for fact in saved['facts']:
        del fact['test_selection_failure']
    with pytest.raises(AuthoritySourceUnavailable):
        restore_domain(saved, lock=threading.RLock(), scope=scope, version=version)
    restored = _restore_legacy_with_instruction(saved, scope, version, "Acceptance command: `pytest missing.py`.")
    assert restored.task_definition.contract.acceptance_commands == ("pytest missing.py",)
    old, = restored.snapshot().executions
    assert (old.execution_id, old.exit_code, old.test_count, old.test_selection_failure) == ('old-failure', 5, 0, '')
    assert not restored.snapshot().obligations


@pytest.mark.parametrize('field,value', [('test_count', True), ('exit_code', True),
    ('output_complete', False), ('reliable', False), ('test_selection_failure', 'unknown')])
def test_v2_rejects_inconsistent_classification(tmp_path, field, value):
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    version = WorkspaceVersion()
    command = command_identity('python -m unittest example.Cases.test_missing', str(tmp_path))
    spec = CheckSpec('selected', command, 'test', required=True, source='user_declared')
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution('failure', (spec.id,))
    domain.observe_execution(ExecutionFact('failure', scope, command, 'failed', version, version,
        capabilities=('test',), exit_code=1, test_count=1, test_selection_failure='unittest_method_lookup'))
    saved = domain.checkpoint()
    saved['facts'][0][field] = value
    with pytest.raises(CheckpointRestoreError):
        restore_domain(saved, lock=threading.RLock(), scope=scope, version=version)


def test_runner_rejects_disk_checkpoint_before_model_and_save(tmp_path, monkeypatch):
    import hashlib
    import json
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
    from nz_coder.state.sessions import session_runtime_state_path
    from test_node_verification_runtime import _run
    from nz_coder.runtime.session.runtime import SessionRuntime

    finalized = []
    original_finalize = SessionRuntime.finalize
    async def observe_finalize(self, context, status):
        finalized.append(status)
        return await original_finalize(self, context, status)
    monkeypatch.setattr(SessionRuntime, "finalize", observe_finalize)
    build = native_sdk.build_product_run_environment
    evidence = {}

    def build_with_checkpoint(request, options):
        environment = build(request, options)
        with scoped_workdir(request.workspace):
            path = session_runtime_state_path(request.session_id)
        environment.runtime_state.save(path, active=True)
        raw = json.loads(path.read_text())
        raw['verification_domain'] = {'schema': 'broken'}
        path.write_text(json.dumps(raw))
        evidence['path'] = path
        evidence['sha'] = hashlib.sha256(path.read_bytes()).hexdigest()
        return environment

    monkeypatch.setattr(native_sdk, 'build_product_run_environment', build_with_checkpoint)
    with pytest.raises(CheckpointRestoreError):
        _run(monkeypatch, tmp_path, [], task='Continue the previous task.', limit=1)
    assert not finalized
    assert hashlib.sha256(evidence['path'].read_bytes()).hexdigest() == evidence['sha']


def test_explicit_null_checkpoint_is_rejected_as_existing_material(tmp_path):
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
    state = RuntimeState()
    assert state.restore({'active': True, 'verification_domain': None})
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    with pytest.raises(CheckpointRestoreError):
        restore_domain(state._restored_verification_domain, lock=threading.RLock(), scope=scope, version=WorkspaceVersion())


def test_damaged_resolved_reference_is_rejected(tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    version = WorkspaceVersion()
    command = command_identity('pytest example.py', str(tmp_path))
    spec = CheckSpec('test', command, 'test', required=True, source='user_declared')
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution('failed', (spec.id,))
    domain.observe_execution(ExecutionFact('failed', scope, command, 'failed', version, version, capabilities=('test',), exit_code=1))
    saved = domain.checkpoint()
    saved['obligations'][0].update(resolved=True, resolved_by='missing')
    with pytest.raises(CheckpointRestoreError):
        restore_domain(saved, lock=threading.RLock(), scope=scope, version=version)


@pytest.mark.parametrize('source,required', [('model_execution', False), ('user_declared', True), ('planner', False), ('model_execution', True)])
def test_v1_attributed_negative_survives_current_second_activation(tmp_path, source, required):
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    version = WorkspaceVersion()
    command = command_identity('python -m unittest discover', str(tmp_path))
    spec = CheckSpec('test', command, 'test', source=source, required=required)
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution('old-failure', (spec.id,))
    domain.observe_execution(ExecutionFact('old-failure', scope, command, 'failed', version, version, capabilities=('test',), exit_code=1))
    saved = domain.checkpoint()
    saved['schema'] = 'nz.verification.v1'
    for fact in saved['facts']:
        del fact['test_selection_failure']
    first = _restore_legacy_with_instruction(saved, scope, version, "Inspect the project.")
    assert not first.task_definition.contract.acceptance_commands
    second = restore_domain(first.checkpoint(), lock=threading.RLock(), scope=scope, version=version)
    # 负证据及其绑定的命令必须保留；旧 source/required 标签不能授予自动复测权。
    assert not second.snapshot().obligations
    assert all(not spec.required for spec in second.snapshot().specs)
    old, = second.snapshot().executions
    assert (old.execution_id, old.command, old.exit_code) == ('old-failure', command, 1)
    assert second.snapshot().decision('test').status == 'failed'


def test_v1_misregistered_check_retires_with_failure_fact_retained(tmp_path):
    from nz_coder.runtime.verification.domain.identity import command_identity
    from nz_coder.runtime.verification.domain.models import ExecutionFact
    scope = RunScope(TaskScope('session', 'task', str(tmp_path)), 'run', 'agent')
    version = WorkspaceVersion()
    command = command_identity('python demo.py', str(tmp_path))
    spec = CheckSpec('auto', command, 'custom_assert')
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version)
    domain.register_check(spec)
    domain.begin_execution('old-failure', (spec.id,))
    domain.observe_execution(ExecutionFact('old-failure', scope, command, 'failed', version, version, capabilities=('custom_assert',), exit_code=7))
    saved = domain.checkpoint()
    saved['schema'] = 'nz.verification.v1'
    for fact in saved['facts']:
        del fact['test_selection_failure']
    restored = _restore_legacy_with_instruction(saved, scope, version, "Inspect the project.")
    assert restored.snapshot().obligations == ()
    assert restored.snapshot().specs == ()
    assert restored.snapshot().executions[0].execution_id == 'old-failure'
    assert restored.snapshot().executions[0].outcome == 'failed'
