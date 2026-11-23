"""生成项目用真实子进程验证，冷启动缓存与 Shell 合并输出按实际归属结算。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from nz_coder.permissions import PermissionManager
from nz_coder.project_creation import verifier  # noqa: F401  注册专用验证工具。
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.process.workspace_scope_execution import execute_observed
from tests.verification_scenarios import VerificationScenario


def _project(root, test_body='assert value == 7'):
    project = root / 'ledger'
    (project / 'tests').mkdir(parents=True)
    (project / 'app.py').write_text('value = 7\n')
    (project / 'tests' / 'test_app.py').write_text(
        'from pathlib import Path\nfrom app import value\n\ndef test_value():\n    ' + test_body + '\n')
    return project


def _verify(vm, commands):
    args = {'project_dir': 'ledger', 'commands': commands}
    call = {'id': 'check-build', 'function': {'name': 'verify_project_build', 'arguments': json.dumps(args)}}
    with scoped_workdir(vm.root):
        start = vm.runtime.begin_tool(call)
        executor = ToolExecutor(PermissionManager('auto'))
        result = execute_observed(vm.store, start.version.generation, lambda: executor.execute_one(call, 0),
            on_before=lambda before: vm.runtime.record_before(start, before))
        result.execution_id = start.execution_id
        vm.runtime.record_result(result)
    return vm.domain.snapshot()


@pytest.mark.parametrize('runner', ['python', 'python3'])
def test_cold_project_keeps_each_real_check_and_unexecuted_helper(tmp_path, monkeypatch, runner):
    if not shutil.which(runner):
        pytest.skip(f'{runner} not installed')
    monkeypatch.delenv('PYTHONDONTWRITEBYTECODE', raising=False)
    project = _project(tmp_path)
    vm = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    commands = [f'{runner} -m py_compile app.py', f'{runner} -m pytest -q tests/test_app.py',
        'PYTHONPATH=src python3 -m ledger --help']
    snapshot = _verify(vm, commands)
    compilation, test, skipped = snapshot.executions
    assert (test.test_count, test.exit_code, test.command.cwd) == (1, 0, str(project))
    assert (skipped.outcome, skipped.exit_code) == ('not_executed', None)
    assert compilation.before == compilation.after == test.before == test.after
    assert (project / '.pytest_cache/v/cache/nodeids').is_file()
    assert any((project / '__pycache__').glob('app.*.pyc'))
    assert {spec.command.original for spec in snapshot.specs if snapshot.decision(spec.id).status == 'passed'} == set(commands[:2])
    assert snapshot.state == 'passed'


@pytest.mark.parametrize('body', ["Path('app.py').write_text('value = 8\\n')", "Path('.pytest_cache/business.json').write_text('{}')"])
def test_project_test_writes_remain_visible(tmp_path, body):
    project = _project(tmp_path, body)
    (project / '.pytest_cache').mkdir()
    vm = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    snapshot = _verify(vm, ['python -m pytest -q'])
    fact, = snapshot.executions
    assert fact.exit_code == 0 and fact.test_count == 1
    assert fact.before.acceptance_generation < fact.after.acceptance_generation
    assert snapshot.state != 'passed'


@pytest.mark.skipif(not shutil.which('npm'), reason='npm not installed')
def test_other_executed_program_cannot_borrow_project_pytest_cache(tmp_path):
    project = _project(tmp_path)
    (project / 'package.json').write_text(json.dumps({'scripts': {'test': 'node build.js'}}))
    (project / 'build.js').write_text("require('node:fs').writeFileSync('.pytest_cache/v/cache/lastfailed', '{}')")
    vm = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    snapshot = _verify(vm, ['python -m pytest -q', 'npm test'])
    assert [fact.exit_code for fact in snapshot.executions] == [0, 0]
    assert 'ledger/.pytest_cache/v/cache/lastfailed' in vm.state.changed_files
    assert snapshot.state != 'passed'


@pytest.mark.skipif(sys.platform != 'linux', reason='uses a second installed CPython')
@pytest.mark.parametrize('foreign_cache', [False, True])
def test_two_project_compilers_keep_their_own_bytecode_identity(tmp_path, monkeypatch, foreign_cache):
    system = Path('/usr/bin/python3')
    if not system.is_file():
        pytest.skip('system Python not installed')
    tag = subprocess.check_output([str(system), '-c', 'import sys; print(sys.implementation.cache_tag)'], text=True).strip()
    if tag == sys.implementation.cache_tag:
        pytest.skip('requires two separate bytecode formats')
    launchers = tmp_path / 'bin'
    launchers.mkdir()
    (launchers / 'python').symlink_to(sys.executable)
    (launchers / 'python3').symlink_to(system)
    monkeypatch.setenv('PATH', os.pathsep.join([str(launchers), '/usr/bin', '/bin']))
    monkeypatch.delenv('PYTHONDONTWRITEBYTECODE', raising=False)
    body = "__import__('subprocess').run(['/usr/bin/python3', '-m', 'py_compile', 'other.py'], check=True)" if foreign_cache else 'assert value == 7'
    project = _project(tmp_path, body)
    (project / 'other.py').write_text('value = 6\n')
    vm = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    second = 'python -m pytest -q' if foreign_cache else 'python -m py_compile other.py'
    snapshot = _verify(vm, ['python3 -m py_compile app.py', second])
    assert (project / '__pycache__' / f'app.{tag}.pyc').is_file()
    assert [fact.exit_code for fact in snapshot.executions] == [0, 0]
    if foreign_cache:
        assert f'ledger/__pycache__/other.{tag}.pyc' in vm.state.changed_files
        assert snapshot.state != 'passed'
    else:
        assert (project / '__pycache__' / f'other.{sys.implementation.cache_tag}.pyc').is_file()
        assert all(snapshot.decision(spec.id).status == 'passed' for spec in snapshot.specs)
        assert snapshot.version.acceptance_generation == 0


@pytest.mark.parametrize(('selector', 'exit_code', 'count'), [('-k missing', 5, 0), ('', 1, 1)])
def test_project_real_empty_or_failed_pytest_stays_unconfirmed(tmp_path, selector, exit_code, count):
    _project(tmp_path, 'assert False')
    vm = VerificationScenario(workspace=tmp_path, plan={'stages': []})
    snapshot = _verify(vm, [f'python -m pytest -q {selector}'.strip()])
    fact, = snapshot.executions
    assert (fact.exit_code, fact.test_count) == (exit_code, count)
    assert snapshot.state != 'passed'


def test_project_tool_reports_collection_without_claiming_test_success(tmp_path):
    from nz_coder.project_creation.verifier import verify_project_build
    from nz_coder.runtime.process.workdir import scoped_workdir

    project = _project(tmp_path)
    (project / "pytest.ini").write_text("[pytest]\naddopts = --collect-only\n")
    with scoped_workdir(tmp_path):
        report = verify_project_build("ledger", ["python -m pytest -q"])
    assert report.startswith("WARN:") and "[unverified]" in report
    receipt, = report.execution_facts
    assert (receipt.exit_code, receipt.test_count) == (0, 0)


@pytest.mark.skipif(os.name == 'nt' or not shutil.which('python3'), reason='Linux Bash replay')
@pytest.mark.parametrize('compound', [
    'chmod +x ledger/ledger && cd ledger && python3 -m pytest -q 2>&1 | tail -30',
    'cd ledger && python3 -m pytest -q 2>&1 | tail -30',
    'cd ledger && python3 -m pytest -q && ls',
])
def test_model_receives_standalone_retry_and_final_current_receipt(tmp_path, monkeypatch, compound):
    from test_node_verification_runtime import _run, _tool

    for name in ('NZ_PLANNING_ENABLED', 'NZ_REFLECTION_ENABLED', 'NZ_MEMORY_AUTO_EXTRACT', 'NZ_MEMORY_AUTO_DREAM', 'NZ_STRICT_LOCAL_TOOLS'):
        monkeypatch.setenv(name, '0')
    project = _project(tmp_path)
    (project / 'ledger').write_text('ledger\n')
    direct = 'python3 -m pytest -q'
    actions = [[_tool('bash', command=compound)], [_tool('bash', command=direct, workdir='ledger')], '已运行账本项目测试。']
    result, _, trace, requests, _, _ = _run(monkeypatch, tmp_path, actions, task='Check the ledger project and report the result.',
        allowed_commands=(compound, direct), limit=8)
    assert result.status.value == 'completed', (result.error, result.final_text)
    first_output = next(m['content'] for m in requests[1]['messages'] if m.get('role') == 'tool')
    assert 'bash.workdir' in first_output and '1 passed' in first_output
    last_output = [m['content'] for m in requests[2]['messages'] if m.get('role') == 'tool'][-1]
    assert 'bash.workdir' not in last_output and '1 passed' in last_output
    checks = [event for event in trace if event.get('event') == 'verification_result' and event['status'] == 'passed']
    assert {(entry['command'], entry['cwd']) for entry in checks} == {(direct, str(project))}
    assert result.metadata['verification']['verification_state'] == 'passed'
    assert result.metadata['verification']['tests_passed'] is None
