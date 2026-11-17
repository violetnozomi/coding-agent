"""参数用途修复：匹配数据、真实访问和未分析效果不可互相冒充。"""
from __future__ import annotations

import os
import shlex
import shutil

import pytest

from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tool_platform.permissioning.checker import PermissionChecker
from nz_coder.tool_platform.permissioning.command_authorization import scoped_command_authorization
from nz_coder.tool_platform.permissioning.rules import PermissionRule
from nz_coder.tool_platform.policies.command_policy import assess_shell_command, classify_bash
from nz_coder.tool_platform.policies.shell_path_analysis import analyze_shell_paths
from nz_coder.tools.bash import execute_bash_local


def _python(source):
    return 'python -c ' + shlex.quote(source)


@pytest.mark.parametrize('command', [
    'find . -path ./.git -prune -o -path ./.nz-coder -prune -o -type f -print',
    "find . -type f -not -path './.git/*' -not -path './.nz-coder/*'",
    "find . -name '.env' -type f -print",
    "grep -A 1 '.env' public.txt", "grep -A1 '.env' public.txt",
    "grep --after-context 1 '.env' public.txt", "grep --after-context=1 '.env' public.txt",
    "rg -B 1 '.env' public.txt", "rg -B1 '.env' public.txt",
    "rg --glob '.git' '.env' public.txt", "rg --glob=.git '.env' public.txt",
    "ls --ignore .git", "ls --ignore=.git", "ls -I .git", "ls -I.git",
    "grep -e .env -- public.txt", "grep --regexp=.env -- public.txt",
])
def test_match_and_option_data_do_not_acquire_file_access(tmp_path, command):
    # 选项值 f 即使恰好也是私有文件别名，仍不能成为文件访问。
    (tmp_path / '.env').write_text('fake sentinel')
    (tmp_path / 'f').symlink_to('.env')
    result = analyze_shell_paths(command, tmp_path, command_cwd=tmp_path)
    assert not result.rejection, result
    assert result.kind != 'unsupported', result


@pytest.mark.parametrize('command', [
    'find .git -type f', 'find .git -unknown-option',
    'find . -fprint .env', "find . -fprintf .env '%p'",
    'find . -files0-from .env -print',
    "grep -f .env public.txt", "grep --file=.env public.txt",
    "rg --file .env public.txt", "grep -e needle -- .env", "ls -- .git",
    "grep --line-buffered -e fake -- .env", "ls --hyperlink=never -- .git",
    "rg --unknown-option -e fake -- .env",
    r'find . -exec cat .env \;', r'find . -execdir cat .env \;',
])
def test_true_file_roles_still_override_unknown_and_tool_allow(tmp_path, command):
    result = analyze_shell_paths(command, tmp_path, command_cwd=tmp_path)
    assert result.kind == 'forbidden', result
    with scoped_workdir(tmp_path):
        decision = PermissionChecker('auto').check('bash', {'command': command},
            [PermissionRule('bash', 'allow')], [], [])
    assert decision['behavior'] == 'deny', decision


@pytest.mark.parametrize('command', [
    r'find . -exec echo safe \;', r'find . -exec sh read.sh \;',
    r'find . -execdir echo safe {} +', 'find . -unknown-option .env',
    "grep --unknown-option '.env' public.txt", 'ls --unknown-option .git',
])
def test_unrecognized_effects_require_authorization_without_invented_paths(tmp_path, command):
    result = assess_shell_command(command, tmp_path, command_cwd=tmp_path)
    assert result.paths.kind == 'unsupported', result
    assert result.requires_approval and not result.rejection


def test_static_find_callbacks_keep_dangerous_and_user_deny_facts(tmp_path):
    assert classify_bash(r'find . -exec sudo id \;')['dangerous']
    with scoped_workdir(tmp_path):
        decision = PermissionChecker('auto').check('bash', {'command': r'find . -exec git push origin main \;'},
            [PermissionRule('bash', 'allow')], [PermissionRule('bash', 'deny', 'prefix:git push')], [])
    assert decision['behavior'] == 'deny', decision


def test_find_execdir_does_not_guess_callback_working_directory(tmp_path):
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub/leaf').write_text('leaf')
    result = analyze_shell_paths(r'find sub/leaf -execdir cat ../public.txt \;',
                                 tmp_path, command_cwd=tmp_path)
    assert result.kind == 'unsupported' and not result.rejection, result


@pytest.mark.parametrize('command', [
    r'find . -execdir echo {} \; -exec cat ../outside.txt \;',
    r'find . -exec cat ../outside.txt \; -execdir echo {} \;',
    r'find . -execdir cat ../outside.txt \; -exec cat ../outside.txt \;',
])
def test_find_callback_directory_uncertainty_belongs_to_each_invocation(tmp_path, command):
    result = analyze_shell_paths(command, tmp_path, command_cwd=tmp_path)
    assert result.kind == 'outside', result
    with scoped_workdir(tmp_path):
        decision = PermissionChecker('auto').check('bash', {'command': command},
            [PermissionRule('bash', 'allow')], [], [])
    assert decision['behavior'] == 'deny', decision


def test_unknown_option_terminator_does_not_turn_a_pending_pattern_into_a_file(tmp_path):
    result = assess_shell_command('grep --unknown-option -- .env public.txt',
                                  tmp_path, command_cwd=tmp_path)
    assert result.paths.kind == 'unsupported' and not result.rejection, result


@pytest.mark.parametrize(('command', 'kind'), [
    ('grep --line-buffered -e fake -- ../outside.txt', 'outside'),
    ('ls --hyperlink=never -- ../outside.txt', 'outside'),
    ('grep --line-buffered -e fake -- public.txt', 'unsupported'),
    ('ls --hyperlink=never -- public.txt', 'unsupported'),
])
def test_unknown_option_preserves_confirmed_boundary_and_incompleteness(tmp_path, command, kind):
    result = assess_shell_command(command, tmp_path, command_cwd=tmp_path)
    assert result.paths.kind == kind, result
    assert bool(result.rejection) == (kind == 'outside'), result
    assert result.requires_approval == (kind == 'unsupported'), result


@pytest.mark.parametrize('source', [
    "from pathlib import Path; print(Path('.git'))",
    "from pathlib import Path; p=Path('.') / '.env'; print(str(p))",
    "from pathlib import Path as P; p=P('.env'); print(p.name)",
    "from pathlib import Path; read=Path('.env').read_text; print(read)",
    "from pathlib import Path; p=Path('.env') if False else Path('public.txt'); p.read_text()",
])
def test_path_values_without_io_are_data(tmp_path, source):
    result = analyze_shell_paths(_python(source), tmp_path, command_cwd=tmp_path)
    assert not result.rejection, result


@pytest.mark.parametrize('source', [
    "from pathlib import Path; Path('.env').read_text()",
    "from pathlib import Path; p=Path('.env'); p.read_bytes()",
    "from pathlib import Path as P; p=P('.') / '.env'; p.open()",
    "from pathlib import Path; Path('.env').exists()",
    "from pathlib import Path; Path('public.txt').rename('.env')",
    "from pathlib import Path; open(str(Path('.env'))).read()",
    "from pathlib import Path; Path.read_text(Path('.env'))",
    "from pathlib import Path; read=Path('.env').read_text; read()",
    "from pathlib import Path; read=Path('.env').read_text; again=read; again()",
    "from pathlib import Path; p=Path('.env') if True else Path('public.txt'); p.read_text()",
    "from pathlib import Path; read=Path('.env').read_text if True else Path('public.txt').read_text; read()",
])
def test_path_io_receivers_and_targets_remain_explicit_file_access(tmp_path, source):
    command = _python(source)
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).kind == 'forbidden'
    with scoped_workdir(tmp_path):
        decision = PermissionChecker('auto').check('bash', {'command': command},
            [PermissionRule('bash', 'allow')], [], [])
    assert decision['behavior'] == 'deny', decision


@pytest.mark.parametrize('source', [
    "p=''.join(['.git', '/config']); open(p).read()",
    "import sys; open(sys.argv[1]).read()",
    "from pathlib import Path; import sys; p=Path(sys.argv[1]); p.read_text()",
    "from pathlib import Path; read=Path(input()).read_text; read()",
    "from pathlib import Path; p=Path('public.txt') if input() else Path(input()); p.read_text()",
])
def test_unresolved_python_io_is_unknown_not_an_empty_success(tmp_path, source):
    command = _python(source) + ' .git/config'
    result = assess_shell_command(command, tmp_path, command_cwd=tmp_path)
    assert result.requires_approval and not result.rejection, result
    with scoped_workdir(tmp_path):
        output = execute_bash_local(command, timeout=5)
    assert output.metadata['command_not_executed']


@pytest.mark.parametrize('source', [
    "p='public.txt'; p=input(); open(p)",
    "import os; os.system(input())",
    "exec(input())",
])
def test_partly_known_python_effects_retain_incompleteness(tmp_path, source):
    result = assess_shell_command(_python(source), tmp_path, command_cwd=tmp_path)
    assert result.requires_approval and not result.rejection, result


@pytest.mark.parametrize('command', [
    'node -e "console.log(\'.git\')"',
    'node -e "require(\'fs\').readFileSync(\'.env\',\'utf8\')"',
    'ruby -e "puts \'.git\'"', 'perl -e "print \'.git\'"',
])
def test_opaque_programs_remain_unknown_instead_of_scanning_strings(tmp_path, command):
    result = assess_shell_command(command, tmp_path, command_cwd=tmp_path)
    assert result.paths.kind == 'unsupported' and not result.rejection, result
    with scoped_workdir(tmp_path):
        output = execute_bash_local(command, timeout=5)
    assert output.metadata['command_not_executed']


def test_real_path_resolution_error_is_invalid_and_still_stops_execution(tmp_path):
    command = 'cat ' + 'x' * 350 + '; echo ran > marker'
    result = analyze_shell_paths(command, tmp_path, command_cwd=tmp_path)
    assert result.kind == 'invalid', result
    with scoped_workdir(tmp_path):
        output = execute_bash_local(command, timeout=5)
    assert str(output).startswith('Error:')
    assert not (tmp_path / 'marker').exists()


@pytest.mark.skipif(os.name != 'posix' or shutil.which('find') is None, reason='Requires POSIX find')
def test_find_prune_and_filter_preserve_real_traversal_semantics(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('root can read mode-000 directories')
    private = tmp_path / '.git'
    private.mkdir()
    (private / 'sentinel').write_text('fake metadata')
    (tmp_path / 'public.txt').write_text('public')
    private.chmod(0)
    try:
        outputs = []
        for command in ["find . -path ./.git -prune -o -type f -print",
                        "find . -type f -not -path './.git/*'"]:
            arguments = {'command': command, 'timeout': 5}
            with scoped_workdir(tmp_path), scoped_command_authorization('bash', arguments):
                output = execute_bash_local(**arguments)
                assert hasattr(output, 'metadata'), output
                outputs.append(output)
        assert [output.metadata['exit'] for output in outputs] == [0, 1]
        assert './public.txt' in outputs[0]
    finally:
        private.chmod(0o700)
