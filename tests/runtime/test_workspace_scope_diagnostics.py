"""真实捕获失败必须保留原因，不能在展示中冒充已观察到的修改。"""
from nz_coder.runtime.process.workspace_scope_execution import execute_observed
from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
from nz_coder.runtime.verification.completion_gate import verification_appendix
from nz_coder.tool_platform.execution import ToolExecutionResult


def test_before_and_after_capture_errors_survive_tool_result(tmp_path):
    workspace = tmp_path / 'project'
    workspace.mkdir()
    (workspace / 'data.bin').write_bytes(b'1234')
    store = WorkspaceSnapshotStore(workspace, tmp_path / 'snapshots', max_file_bytes=1)
    result = execute_observed(store, 0, lambda: ToolExecutionResult(
        'bash', {'command': 'true'}, '', True, False, False, False,
        metadata={'exit': 0}))
    assert result.workspace_scope['before'] is None
    assert result.workspace_scope['after'] is None
    errors = result.workspace_scope['errors']
    assert set(errors) == {'before', 'after'}
    assert all('byte limit' in reason for reason in errors.values())


def test_unknown_scope_display_does_not_assert_a_mutation():
    rendered = verification_appendix({'mutation_scope_unknown': True,
                                     'verification_state': 'unverified'})
    assert '工作区范围尚未确认' in rendered
    assert '存在范围未知的工作区变更' not in rendered


def test_normal_probe_is_recorded_without_becoming_ignored_verification(tmp_path):
    from tests.verification_scenarios import VerificationScenario

    vm = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    events = []
    vm.runtime.host.tracer.log = lambda event, **fields: events.append((event, fields))
    result = vm.settle_shell('ls; uname -a')
    assert not result.command_failed
    assert len(vm.domain.snapshot().executions) == 1
    assert not any(event == 'verification_result_ignored' for event, _ in events)
    assert not vm.runtime.status()['ignored_verifications']
