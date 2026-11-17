"""Node 参数用途、实际测试计数及凭据命名源码的边界。"""
from __future__ import annotations

import shutil

import pytest

from nz_coder.foundation.workspace_paths import WorkspacePathError, WorkspacePathPolicy
from nz_coder.state.workdir import scoped_workdir
from nz_coder.tool_platform.policies.shell_path_analysis import analyze_shell_paths


@pytest.mark.parametrize('command', [
    'node --test demo.test.cjs',
    'node --test --test-name-pattern -e demo.test.cjs',
    'node --check demo.cjs',
    'node -c demo.cjs',
    'node --require ./preload.cjs --test demo.test.cjs',
])
def test_node_file_or_option_data_is_not_inline_program(tmp_path, command):
    assert not analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).rejection


@pytest.mark.parametrize('command,kind', [
    ('node -c /outside/probe.cjs', 'outside'),
    ('node --check .env', 'forbidden'),
    ('node --require=/outside/load.cjs --test demo.cjs', 'outside'),
    ('node -r/outside/load.cjs demo.cjs', 'outside'),
    ('node --import file:///outside/load.mjs --test demo.cjs', 'outside'),
    ('node --experimental-loader=file:///outside/load.mjs demo.cjs', 'outside'),
    ('node --import=data:text/javascript,secret --test demo.cjs', 'unsupported'),
    ('node --unknown-switch /outside/probe.cjs', 'unsupported'),
    ('node demo.cjs --require /outside/script-argument', 'outside'),
])
def test_node_code_file_arguments_keep_path_protection(tmp_path, command, kind):
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).kind == kind


@pytest.mark.skipif(shutil.which('node') is None, reason='Node runtime is unavailable')
@pytest.mark.parametrize('body,expected_count,failed', [
    ("test('ok', () => {});", 1, False),
    ("test('bad', () => { throw new Error('bad') });", 1, True),
    ("test.skip('skip', () => {});", 0, False),
    ("test.todo('todo');", 0, False),
    ('', 0, False),
])
def test_real_node_tests_run_through_existing_bash_and_report_executed_count(tmp_path, body, expected_count, failed):
    from nz_coder.tools.bash import run_bash

    if body:
        (tmp_path / 'demo.test.cjs').write_text("const test = require('node:test');\n" + body + '\n')
    command = 'node --test demo.test.cjs' if body else 'node --test'
    with scoped_workdir(tmp_path):
        output = run_bash(command, timeout=15)
    assert 'unsupported interpreter option' not in str(output), output
    facts = output.execution_facts
    receipt = next(item for item in facts if item.command == command)
    assert receipt.test_count == expected_count
    assert (receipt.outcome == 'failed') == failed


@pytest.mark.parametrize('filename', ['src/credentials.py', 'credential_store.cpp', 'service_account.ts'])
def test_normal_credential_named_source_can_be_read_written_and_indexed(tmp_path, filename):
    from nz_coder.tools.files import read_file, write_file, list_directory

    path = tmp_path / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('value = 42\n')
    policy = WorkspacePathPolicy(tmp_path)
    assert policy.validate_model_read(filename) == path
    with scoped_workdir(tmp_path):
        assert '42' in read_file(filename)
        assert 'Updated' in write_file(filename, 'value = 43\n')
        assert path.name in list_directory(str(path.parent.relative_to(tmp_path)))
    if path.suffix == '.py':
        from nz_coder.intelligence.code_index import PersistentCodeIndex
        entries, _ = PersistentCodeIndex(tmp_path).scan(tmp_path, max_files=10)
        assert any(str(entry.path).endswith(filename) for entry in entries)


@pytest.mark.parametrize('filename', ['.env.py', '.ssh/credentials.py', 'credentials.json', 'prod_credentials.yaml', 'service_account.yml', 'id_rsa.py', 'credential.pem'])
def test_source_exception_never_weakens_private_files(tmp_path, filename):
    with pytest.raises(WorkspacePathError):
        WorkspacePathPolicy(tmp_path).validate_model_read(filename)


def test_credential_source_symlink_cannot_expose_private_target(tmp_path):
    (tmp_path / '.env').write_text('TOKEN=private')
    (tmp_path / 'credentials.py').symlink_to('.env')
    with pytest.raises(WorkspacePathError):
        WorkspacePathPolicy(tmp_path).validate_model_read('credentials.py')


@pytest.mark.skipif(shutil.which('node') is None, reason='Node runtime is unavailable')
@pytest.mark.parametrize('option', ['-pe', '-ep'])
def test_node_combined_eval_flags_cannot_hide_private_program(tmp_path, option):
    from nz_coder.tools.bash import run_bash
    (tmp_path / '.env').write_text('PRIVATE_SENTINEL_DO_NOT_EXPOSE')
    command = f'''node {option} "require('fs').readFileSync('.env','utf8')"'''
    with scoped_workdir(tmp_path):
        output = run_bash(command, timeout=5)
    assert 'PRIVATE_SENTINEL_DO_NOT_EXPOSE' not in str(output)
    from nz_coder.tools import ToolOutput
    if isinstance(output, ToolOutput):
        assert all(r.outcome != 'succeeded' for r in output.execution_facts)
    else:
        assert output.startswith('Error:')


@pytest.mark.skipif(shutil.which('node') is None, reason='Node runtime is unavailable')
@pytest.mark.parametrize('reporter', ['tap', 'spec', 'dot', 'junit'])
@pytest.mark.parametrize('fails', [False, True])
def test_node_reporter_unknown_count_never_proves_skipped_suite_passed(tmp_path, reporter, fails):
    from nz_coder.tools.bash import run_bash
    from nz_coder.tool_platform.execution import ToolExecutionResult
    from nz_coder.runtime.adapters.verification_facts import bind_execution_facts
    from nz_coder.runtime.verification.domain.models import TaskScope, RunScope, WorkspaceVersion, CheckSpec
    from nz_coder.runtime.verification.domain.rules import evaluate_check

    body = "test('bad', () => { throw new Error('bad') });" if fails else "test.skip('skipped', () => {});"
    (tmp_path / 'skip.test.cjs').write_text("const test = require('node:test');\n" + body)
    command = f'node --test --test-reporter={reporter} skip.test.cjs'
    with scoped_workdir(tmp_path):
        output = run_bash(command, timeout=5)
    result = ToolExecutionResult('bash', {}, str(output), True, False, False, False, execution_facts=output.execution_facts)
    version = WorkspaceVersion()
    scope = RunScope(TaskScope('review', 'task', str(tmp_path)), 'run', 'main')
    fact, = bind_execution_facts(result, scope, version, version)
    decision = evaluate_check(CheckSpec('reporter', fact.command, 'test', targets=fact.targets), fact, version)
    assert decision.status == ('failed' if fails else 'pending')
    if not fails and fact.test_count is None:
        assert not fact.reliable
        assert fact.output_complete


@pytest.mark.skipif(shutil.which('node') is None, reason='Node runtime is unavailable')
def test_node_combined_print_eval_uses_actual_next_source_argument(tmp_path):
    from nz_coder.tools.bash import run_bash
    from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization
    arguments = {'command': 'node -pe "2 + 3"', 'timeout': 5}
    with scoped_workdir(tmp_path), scoped_command_authorization('bash', arguments):
        output = run_bash(**arguments)
    assert str(output).strip() == '5'
