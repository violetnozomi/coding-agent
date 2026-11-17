"""联合提交与冻结之间不得暴露两份状态，也不能污染下一次运行。"""
from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

from test_verification_state_authority import _state
from nz_coder.runtime.adapters.verification_runtime import VerificationRuntime
from nz_coder.runtime.adapters.verification_workspace import WorkspaceSettlement
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.verification.domain.models import WorkspaceVersion
from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.tool_platform.execution import ToolExecutionResult


def test_callback_collected_before_freeze_cannot_write_into_next_activation(monkeypatch, tmp_path):
    state, context, domain = _state(tmp_path)
    host = SimpleNamespace(runtime_state=state, workdir=tmp_path, workspace_snapshots=None,
        _best_effort_tool_input=lambda value: value, tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None))
    runtime = VerificationRuntime(host, context)
    start = runtime.begin_tool({"id": "old", "function": {"name": "bash", "arguments": {"command": "python quiet.py"}}})
    result = ToolExecutionResult("bash", {"command": "python quiet.py"}, "", True, False, True, False,
                                 execution_id=start.execution_id)
    entered, release = threading.Event(), threading.Event()

    def collect(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return WorkspaceSettlement({"app.py": "replace"}, {"app.py": "replace"}, True, True)

    monkeypatch.setattr("nz_coder.runtime.adapters.verification_runtime.collect_workspace_settlement", collect)
    errors = []
    def settle():
        try:
            runtime.record_result(result)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=settle)
    worker.start()
    try:
        assert entered.wait(3)
        context.finish(RunStatus.CANCELLED)
        next_context = RunContext(context.request, context.session, "main")
        state.bind_run_context(next_context)
        state.reset()
        next_domain = VerificationDomain(lock=next_context._state_lock,
            scope=replace(domain.scope, interaction_run_id=next_context.interaction_run_id), version=WorkspaceVersion())
        next_context.attach_verification(next_domain)
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert not errors
    assert state.mutation_generation == 0
    assert state.verification_failures == 0
    assert next_domain.snapshot().revision == 0
    assert domain.snapshot().terminal_reason == "cancelled"


def test_failed_file_tool_partial_write_still_advances_shared_workspace_version(tmp_path):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.process.workspace_scope_execution import execute_observed

    (tmp_path / "app.py").write_text("value = 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    scenario.settle_shell("python -m py_compile app.py")
    assert scenario.domain.snapshot().execution_ready
    start = scenario.runtime.begin_tool({"id": "partial", "function": {
        "name": "write_files_batch", "arguments": {"files": [{"path": "app.py", "content": "value = 2\n"}]}}})
    def partial():
        (tmp_path / "app.py").write_text("value = 2\n")
        return ToolExecutionResult("write_files_batch", {"files": [{"path": "app.py"}]},
            "Error: stopped after first write", False, True, False, True)
    result = execute_observed(scenario.store, 0, partial)
    result.execution_id = start.execution_id
    scenario.runtime.record_result(result)
    assert scenario.state.mutation_generation == scenario.domain.snapshot().version.generation == 1
    assert scenario.domain.snapshot().version.scope_complete
    assert not scenario.domain.snapshot().ready_for_completion


def test_failed_file_tool_with_missing_after_proof_waits_for_observed_change(tmp_path, monkeypatch):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.process.workspace_scope_execution import execute_observed
    from nz_coder.runtime.process.workspace_snapshot import SnapshotError

    (tmp_path / "app.py").write_text("value = 1\n")
    scenario = VerificationScenario(workspace=tmp_path, plan={"stages": []})
    start = scenario.runtime.begin_tool({"id": "unknown", "function": {
        "name": "write_file", "arguments": {"path": "app.py", "content": "value = 2\n"}}})
    original, calls = scenario.store.verification_snapshot, []
    def snapshot(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise SnapshotError("controlled after-snapshot loss")
        return original(*args, **kwargs)
    monkeypatch.setattr(scenario.store, "verification_snapshot", snapshot)
    def partial():
        (tmp_path / "app.py").write_text("value = 2\n")
        return ToolExecutionResult("write_file", {"path": "app.py"}, "Error: interrupted", False, True, False, True)
    result = execute_observed(scenario.store, 0, partial)
    result.execution_id = start.execution_id
    scenario.runtime.record_result(result)
    assert scenario.state.mutation_generation == scenario.domain.snapshot().version.generation == 0
    assert not scenario.domain.snapshot().version.scope_complete
    scenario.runtime.reconcile_workspace()
    assert scenario.state.mutation_generation == scenario.domain.snapshot().version.generation == 1
    assert scenario.domain.snapshot().version.scope_complete


def test_diff_status_projection_cannot_erase_observed_workspace_paths():
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    state = RuntimeState()
    state._record_workspace_mutation(['bad.py'])
    state.observe_tool('diff_status', {}, 'has_non_empty_diff: false\nChanged files:\n  marker.md')
    assert state.changed_files == ['bad.py', 'marker.md']
    assert state.has_diff


def test_runtime_checkpoint_failure_reports_unsettled_without_rewriting_frozen_result(monkeypatch, tmp_path):
    from test_node_verification_runtime import _run, _tool
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    (tmp_path / 'note.txt').write_text('hello\n')
    def fail_save(*_args, **_kwargs):
        raise OSError('controlled runtime checkpoint failure')
    monkeypatch.setattr(RuntimeState, 'save', fail_save)
    result, *_ = _run(monkeypatch, tmp_path, [[_tool('read_file', path='note.txt')], 'Read complete.'],
        task='Read note.txt and report its content. Do not change files.', limit=2)
    assert result.status.value == 'completed'
    assert result.metadata['verification']['terminal_reason'] == 'completed'
    assert result.metadata['settlement']['status'] == 'pending'
    assert result.metadata['settlement']['code'] == 'runtime_checkpoint_pending'
