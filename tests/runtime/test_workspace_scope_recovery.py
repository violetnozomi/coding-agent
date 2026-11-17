"""未知区间失效的是证据，不是凭空发生的编辑；恢复不能复活旧通过。"""
from __future__ import annotations

from dataclasses import replace
import os
import threading

import pytest

from tests.verification_scenarios import VerificationScenario
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import (
    CheckSpec, ExecutionFact, ReviewFact, RunScope, TaskScope, WorkspaceObservation, WorkspaceVersion,
)
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _domain(root):
    scope = RunScope(TaskScope('session', 'task', str(root)), 'run', 'agent')
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    spec = CheckSpec('test', command_identity('python check.py', str(root)), 'custom_assert',
                     required=True, source='user_declared')
    domain.register_check(spec)
    return domain, spec


def _success(domain, spec, execution_id, version, started_revision=None):
    return ExecutionFact(execution_id, domain.scope, spec.command, 'succeeded', version, version,
                         capabilities=('custom_assert',), exit_code=0, started_revision=started_revision)


def test_late_success_cannot_cross_scope_loss_and_fresh_execution_can_pass(tmp_path):
    domain, spec = _domain(tmp_path)
    version = domain.snapshot().version
    old_start = domain.begin_execution('old', (spec.id,))
    domain.observe_workspace(WorkspaceObservation(replace(version, scope_complete=False)))
    domain.observe_workspace(WorkspaceObservation(version))
    domain.observe_execution(_success(domain, spec, 'old', version, old_start))
    assert domain.snapshot().decision(spec.id).status == 'pending'
    assert domain.contract_projection('python check.py', ())['attempted_generation'] == -1
    domain.begin_execution('fresh', (spec.id,))
    domain.observe_execution(_success(domain, spec, 'fresh', version))
    assert domain.snapshot().decision(spec.id).status == 'passed'


def test_old_review_does_not_reappear_when_same_version_scope_recovers(tmp_path):
    domain, _ = _domain(tmp_path)
    version = domain.snapshot().version
    basis = domain.review_basis()
    domain.observe_review(ReviewFact('review', domain.scope.task, (), version, 'materials', True,
                                    basis_fingerprint=basis))
    assert domain.snapshot().latest_review is not None
    domain.observe_workspace(WorkspaceObservation(replace(version, scope_complete=False)))
    domain.observe_workspace(WorkspaceObservation(version))
    assert domain.snapshot().latest_review is None
    assert domain.review_basis() != basis


def test_scope_recovery_and_checkpoint_keep_old_positive_pending(tmp_path):
    domain, spec = _domain(tmp_path)
    version = domain.snapshot().version
    domain.begin_execution('old', (spec.id,))
    domain.observe_execution(_success(domain, spec, 'old', version))
    domain.observe_workspace(WorkspaceObservation(replace(version, scope_complete=False)))
    domain.observe_workspace(WorkspaceObservation(version))
    assert domain.snapshot().decision(spec.id).status == 'pending'
    restored = restore_domain(domain.checkpoint(), lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id='resumed'), version=version)
    assert restored.snapshot().decision(spec.id).status == 'pending'
    assert not restored.rebuild_from_current_acceptance()
    restored.begin_execution('fresh', (spec.id,))
    restored.observe_execution(_success(restored, spec, 'fresh', version))
    assert restored.snapshot().decision(spec.id).status == 'passed'


def test_incomplete_readonly_commands_do_not_claim_source_edits(tmp_path):
    (tmp_path / 'app.py').write_text('value = 1\n')
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path)
        scenario.settle_shell('python -m py_compile app.py')
        assert scenario.domain.snapshot().checks[0].status == 'passed'
        os.mkfifo(tmp_path / 'unobserved-pipe')
        scenario.settle_shell('cat app.py')
        scenario.settle_shell('cat app.py')
        assert scenario.state.edits_this_run == 0
        assert scenario.state.mutation_generation == scenario.state.source_mutation_generation == 0
        assert not scenario.state.has_diff
        assert not scenario.domain.snapshot().version.scope_complete
        assert scenario.domain.snapshot().checks[0].status == 'pending'


@pytest.mark.parametrize('recovery', ['command', 'freeze'])
def test_recovered_real_change_advances_content_version_once(tmp_path, recovery):
    (tmp_path / 'app.py').write_text('value = 1\n')
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path)
        os.mkfifo(tmp_path / 'unobserved-pipe')
        scenario.settle_shell("printf 'value = 2\\n' > app.py")
        assert scenario.state.mutation_generation == 0
        (tmp_path / 'unobserved-pipe').unlink()
        if recovery == 'command':
            scenario.settle_shell('cat app.py')
        else:
            scenario.runtime.prepare_freeze()
        assert scenario.state.mutation_generation == scenario.state.source_mutation_generation == 1
        assert 'app.py' in scenario.state.changed_files
        assert scenario.domain.snapshot().version.scope_complete
        scenario.runtime.reconcile_workspace()
        assert scenario.state.mutation_generation == 1


@pytest.mark.parametrize('recovery', ['command', 'freeze', 'verification'])
def test_initial_scope_failure_can_establish_fresh_baseline_without_claiming_old_changes(tmp_path, recovery):
    (tmp_path / 'app.py').write_text('value = 1\n')
    os.mkfifo(tmp_path / 'unobserved-pipe')
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path)
        events = []
        scenario.runtime.host.tracer.log = lambda event, **fields: events.append((event, fields))
        scenario.settle_shell("printf 'value = 2\\n' > app.py")
        (tmp_path / 'unobserved-pipe').unlink()
        if recovery == 'verification':
            scenario.settle_shell('python -m py_compile app.py')
            assert any(check.status == 'passed' for check in scenario.domain.snapshot().checks)
        elif recovery == 'command':
            scenario.settle_shell('cat app.py')
        else:
            scenario.runtime.prepare_freeze()
        assert scenario.domain.snapshot().version.scope_complete
        assert scenario.state.mutation_generation == 0
        assert not scenario.state.changed_files
        assert any(event == 'workspace_scope_baseline_established' and not fields['history_observed']
                   for event, fields in events)
        scenario.settle_shell('python -m py_compile app.py')
        assert any(check.status == 'passed' for check in scenario.domain.snapshot().checks)


@pytest.mark.parametrize('recovery', ['command', 'freeze'])
@pytest.mark.parametrize('later_change', [False, True])
def test_known_write_is_not_counted_twice_but_later_same_path_change_is(tmp_path, recovery, later_change):
    from nz_coder.runtime.process.workspace_scope_execution import execute_observed
    from nz_coder.tool_platform.execution import ToolExecutionResult

    (tmp_path / 'app.py').write_text('value = 1\n')
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(workspace=tmp_path)
        os.mkfifo(tmp_path / 'unobserved-pipe')
        args = {'path': 'app.py', 'content': 'value = 2\n'}
        start = scenario.runtime.begin_tool({'id': 'write', 'function': {'name': 'write_file', 'arguments': args}})
        def write():
            (tmp_path / 'app.py').write_text(args['content'])
            return ToolExecutionResult('write_file', args, 'Updated app.py', True, False, False, True)
        result = execute_observed(scenario.store, 0, write)
        result.execution_id = start.execution_id
        scenario.runtime.record_result(result)
        assert scenario.state.mutation_generation == scenario.state.edits_this_run == 1
        if later_change:
            (tmp_path / 'app.py').write_text('value = 3\n')
        (tmp_path / 'unobserved-pipe').unlink()
        if recovery == 'command':
            scenario.settle_shell('cat app.py')
        else:
            scenario.runtime.prepare_freeze()
        assert scenario.state.mutation_generation == scenario.state.edits_this_run == 1 + int(later_change)
        assert scenario.domain.snapshot().version.scope_complete


def test_recovering_check_that_also_changes_source_cannot_use_new_version(tmp_path):
    from nz_coder.runtime.verification.domain.requirements import TaskContract

    (tmp_path / 'app.py').write_text('value = 1\n')
    (tmp_path / 'repair_and_check.py').write_text(
        "from pathlib import Path\nimport py_compile\n"
        "Path('app.py').write_text('value = 3\\n')\npy_compile.compile('app.py', doraise=True)\n")
    command = 'python repair_and_check.py'
    with scoped_workdir(tmp_path):
        scenario = VerificationScenario(TaskContract(acceptance_commands=(command,)), workspace=tmp_path)
        os.mkfifo(tmp_path / 'unobserved-pipe')
        scenario.settle_shell("printf 'value = 2\\n' > app.py")
        (tmp_path / 'unobserved-pipe').unlink()
        result = scenario.settle_shell(command)
        assert not result.command_failed
        check = next(item for item in scenario.domain.snapshot().checks if item.execution_id)
        assert check.status == 'pending'
        assert check.reason == 'relevant workspace version changed'
        assert scenario.state.mutation_generation == 1
