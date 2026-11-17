"""脚本、文本与文件参数分别判定，既不误拦也不扩大只读权限。"""
from __future__ import annotations

import shlex

import pytest

from nz_coder.foundation.workspace_paths import WorkspacePathError, WorkspacePathPolicy
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.tool_platform.policies.command_policy import classify_bash, is_known_read_only_command
from nz_coder.tool_platform.policies.shell_path_analysis import analyze_shell_paths
from nz_coder.tools.bash import run_bash
from nz_coder.tools.process import execute_process_local
from nz_coder.runtime.process.process_service import ProcessService


def _python(code):
    return "python -c " + shlex.quote(code)


@pytest.mark.parametrize("command", [
    "echo " + "x" * 350,
    _python("print(" + repr("x" * 350) + ")"),
    "python - <<'PY'\n# " + "x" * 350 + "\nprint('ok')\nPY",
    "printf '%s' '/etc/passwd'",
    _python("from pathlib import Path; Path('README.md').write_text('shutdown .env /etc/passwd')"),
    "printf '%s' curl",
    "env MODE=test python - <<'PY'\nprint('ok')\nPY",
    "python -c 'print(1)' <<'DATA'\nshutdown /etc/passwd .env\nDATA",
    "python - <<-'PY'\n\tprint('ok')\n\tPY\npython -m py_compile app.py",
])
def test_data_and_python_body_are_not_filesystem_operands(tmp_path, command):
    result = analyze_shell_paths(command, tmp_path, command_cwd=tmp_path)
    assert result.rejection == "", result
    assert classify_bash(command)["dangerous"] is False


@pytest.mark.parametrize("command", [
    "echo 'shutdown sudo reboot'", "grep shutdown README.md", "rg sudo .",
    _python("print('shutdown')"),
    _python("import subprocess; subprocess.run(['echo', 'shutdown'])"),
    "python - <<'PY'\n# controlled shutdown\nprint('safe')\nPY",
])
def test_dangerous_words_in_data_are_not_executed_commands(command):
    assert not classify_bash(command)["dangerous"]


@pytest.mark.parametrize("command", [
    "shutdown -h now", "/sbin/shutdown -h now", "env MODE=test sudo id",
    "sh -c 'shutdown -h now'", "command sudo id",
    _python("import subprocess as sp; sp.run(args=['shutdown', '-h', 'now'])"),
    _python("import os; os.system('sudo id')"),
    "python - <<'PY'\nprint('ok')\nPY\nshutdown -h now",
])
def test_actual_dangerous_entrypoints_remain_blocked(command):
    assert classify_bash(command)["dangerous"]


@pytest.mark.parametrize("command", [
    _python("from pathlib import Path; print(Path('/etc/passwd').read_text())"),
    _python("from pathlib import Path as P; print(P('.env').read_text())"),
    _python("import pathlib as p; print((p.Path('.') / '.env').read_text())"),
    _python("open(file='.env').read()"),
    _python("from builtins import open as read; read('.env')"),
    _python("p='.env'; open(p); p='README.md'"),
    _python("import os; os.remove(path='.env')"),
    _python("import os; os.chdir(path='/etc')"),
    _python("import shutil; shutil.rmtree(path='.git')"),
    _python("p='/etc'; q=p+'/passwd'; open(q)"),
    "python -W ignore -c \"from pathlib import Path; Path('/etc/passwd').read_text()\"",
    "python - <<'PY'\nfrom pathlib import Path\nprint(Path('.git/config').read_text())\nPY",
    "grep -f .env README.md", "rg --file=.env .",
    "cat .env", "echo value > .env",
    "grep -n needle .env", "rg -n needle .env",
    "sed -f .env data.txt", "awk -f .env data.txt",
    "git log --output=.env", "git diff --output=../outside",
])
def test_real_python_and_shell_file_access_remains_rejected(tmp_path, command):
    result = analyze_shell_paths(command, tmp_path, command_cwd=tmp_path)
    assert result.rejection


@pytest.mark.parametrize("command,read_only", [
    ("echo '$(touch marker)'", True),
    ('echo "$(touch marker)"', False),
    ("git branch new-feature", False), ("git branch -D old-feature", False),
    ("git branch --list", True), ("rg --pre ./helper needle .", False),
    ("git branch --list -D old-feature", False),
    ("git log --output=changed", False),
    ("git grep --open-files-in-pager=shutdown needle", False),
    ("rg --hostname-bin=shutdown needle .", False),
    ("echo done > output", False), ("env MODE=x echo done", False),
    (_python("print(1)"), False),
])
def test_readonly_uses_real_execution_roles(command, read_only):
    assert is_known_read_only_command(command) is read_only


@pytest.mark.parametrize("path", ["x" * 350, "~__nz_missing_user_712489__/file"])
def test_invalid_real_paths_are_controlled_input_errors(tmp_path, path):
    with pytest.raises(WorkspacePathError) as raised:
        WorkspacePathPolicy(tmp_path).validate_model_read(path)
    assert "x" * 350 not in str(raised.value)


def test_real_long_script_executes_after_normal_permission_decision(tmp_path):
    code = ("from pathlib import Path\n# " + "notes " * 100 + "\n"
            "p=Path('app.py')\ns=p.read_text()\n"
            "p.write_text(s.replace('VALUE = 0', 'VALUE = 1'))\nprint('patched')\n")
    command = "python - <<'PY'\n" + code + "PY\npython -m py_compile app.py"
    (tmp_path / "app.py").write_text("VALUE = 0\n")
    with scoped_workdir(tmp_path):
        decision = PermissionManager("default").check("bash", {"command": command})
        assert decision["behavior"] == "ask", decision
        result = run_bash(command, timeout=5)
    assert result.execution_facts[0].exit_code == 0, result
    assert (tmp_path / "app.py").read_text() == "VALUE = 1\n"
    assert "patched" in result


@pytest.mark.parametrize("command", ["python - <<'PY'\nprint(1)\n", 'echo "unfinished'])
def test_unclosed_shell_structure_is_rejected_without_execution(tmp_path, command):
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).rejection


@pytest.mark.parametrize("command", [
    "bash -ec 'shutdown now'", "sh -s -- foo <<'EOF'\nshutdown now\nEOF",
    _python("import subprocess; c='shutdown now'; subprocess.run(c, shell=True)"),
    _python("import subprocess; subprocess.run(executable='shutdown', args=['echo', 'now'])"),
    "git grep --open-files-in-pager=shutdown needle", "git grep -Oshutdown needle",
    "rg --hostname-bin=shutdown needle .",
    "sh <<'A' <<'B'\necho safe\nA\nshutdown now\nB\n",
    "sh 3<<'A' <<'B'\necho safe\nA\nshutdown now\nB\n",
])
def test_interpreter_options_and_explicit_child_entrypoints_remain_dangerous(command):
    assert classify_bash(command)["dangerous"]


def test_successful_cd_conjunction_uses_new_directory(tmp_path):
    (tmp_path / 'sub').mkdir()
    result = analyze_shell_paths('cd sub && cat ../README.md', tmp_path, command_cwd=tmp_path)
    assert not result.rejection, result


def test_absolute_curl_uses_the_same_closed_profile(tmp_path):
    assert analyze_shell_paths('/usr/bin/curl http://127.0.0.1:8080/health',
                               tmp_path, command_cwd=tmp_path).kind == 'supported'


def test_deep_program_is_a_controlled_input_error(tmp_path):
    command = _python("open(" + "'a'+" * 1000 + "'b')")
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).kind == 'unsupported'
    assert not is_known_read_only_command(command)
    assert classify_bash(command)['mutating']


@pytest.mark.parametrize('code', [
    "import os; os.chdir('sub'); open('alias')",
    "import subprocess; subprocess.run(['cat','alias'],cwd='sub')",
])
def test_unknown_interpreter_cwd_requires_authorization_before_private_symlink_access(tmp_path, code):
    (tmp_path / 'sub').mkdir()
    (tmp_path / '.env').write_text('fake credential')
    (tmp_path / 'sub/alias').symlink_to(tmp_path / '.env')
    command = _python(code)
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).kind == 'unsupported'
    with scoped_workdir(tmp_path):
        assert PermissionManager('default').check('bash', {'command': command})['behavior'] == 'ask'
        output = run_bash(command, timeout=5)
    assert str(output).startswith('Denied:')
    assert 'fake credential' not in str(output)


@pytest.mark.parametrize('tool', ['bash', 'process'])
@pytest.mark.parametrize('workdir', ['.', 'sub'])
@pytest.mark.parametrize('operand', ['.env', '../outside', 'long', 'alias'])
def test_permission_and_execution_reject_the_same_bad_paths_without_side_effects(tmp_path, tool, workdir, operand):
    (tmp_path / 'sub').mkdir()
    (tmp_path / '.env').write_text('fake credential')
    (tmp_path / 'alias').symlink_to(tmp_path / '.env')
    (tmp_path / 'sub/alias').symlink_to(tmp_path / '.env')
    if operand == 'long':
        operand = 'x' * 350
    elif operand == '../outside':
        operand = '../../outside' if workdir == 'sub' else '../outside'
    command = 'cat ' + shlex.quote(operand) + '; echo ran > marker'
    arguments = {'command': command, 'workdir': workdir}
    if tool == 'process':
        arguments.update(operation='start', tty=False)
    with scoped_workdir(tmp_path):
        decision = PermissionManager('default').check(tool, arguments)
        assert decision['behavior'] == 'deny', decision
        if tool == 'bash':
            output = run_bash(**arguments)
        else:
            service = ProcessService(tmp_path)
            try:
                output = execute_process_local(arguments, service=service,
                    owner_session_id='test', owner_agent_id='test', event_bus=None)
                assert service.list(owner_session_id='test') == []
            finally:
                service.close()
    assert str(output).startswith('Error:'), output
    assert not (tmp_path / workdir / 'marker').exists()


@pytest.mark.parametrize('tool', ['bash', 'process'])
def test_invalid_workdir_never_starts_a_command(tmp_path, tool):
    arguments = {'command': 'echo ran > marker', 'workdir': 'x' * 350}
    if tool == 'process':
        arguments.update(operation='start', tty=False)
    with scoped_workdir(tmp_path):
        assert PermissionManager('default').check(tool, arguments)['behavior'] == 'deny'
        if tool == 'bash':
            output = run_bash(**arguments)
        else:
            service = ProcessService(tmp_path)
            try:
                output = execute_process_local(arguments, service=service,
                    owner_session_id='test', owner_agent_id='test', event_bus=None)
                assert service.list(owner_session_id='test') == []
            finally:
                service.close()
    assert 'Error:' in output
    assert not (tmp_path / 'marker').exists()


@pytest.mark.parametrize('code', [
    "import os as x; x.remove('.env'); import math as x",
    "from os import remove as f; f('.env'); from builtins import print as f",
    "import subprocess as s; s.run(['shutdown','now']); import math as s",
])
def test_conflicting_import_aliases_are_controlled_not_erased(tmp_path, code):
    assert analyze_shell_paths(_python(code), tmp_path, command_cwd=tmp_path).kind == 'unsupported'
    assert not is_known_read_only_command(_python(code))


@pytest.mark.parametrize('command', ['cd -P sub && cat ../outside', 'cd && cat .env',
                                      'cd missing ; cat ../outside', 'cd sub || cat ../outside',
                                      'cd sub && echo ok ; cat ../outside'])
def test_unproved_working_directory_does_not_become_an_admission_proof(tmp_path, command):
    (tmp_path / 'sub').mkdir()
    from nz_coder.tool_platform.policies.command_policy import assess_shell_command
    assessment = assess_shell_command(command, tmp_path, command_cwd=tmp_path)
    assert assessment.rejection or assessment.requires_approval


def test_inaccessible_real_operand_is_a_controlled_failure(tmp_path):
    locked = tmp_path / 'locked'
    locked.mkdir()
    locked.chmod(0)
    try:
        result = analyze_shell_paths('cat locked/file ; echo ran > marker', tmp_path, command_cwd=tmp_path)
        assert result.kind == 'invalid', result
    finally:
        locked.chmod(0o700)
    assert not (tmp_path / 'marker').exists()


def test_interpreter_uses_last_stdin_heredoc_and_ignores_other_descriptors(tmp_path):
    command = "python <<'A' <<'B'\nprint('safe')\nA\nopen('.env')\nB\n"
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).rejection
    data = "python 3<<'A' 0<<'B'\nopen('.env')\nA\nprint('safe')\nB\n"
    assert not analyze_shell_paths(data, tmp_path, command_cwd=tmp_path).rejection


def test_interpreter_stdin_descriptor_copy_is_controlled_unsupported(tmp_path):
    command = "sh 3<<'CODE' <&3\nshutdown now\nCODE\n"
    assert analyze_shell_paths(command, tmp_path, command_cwd=tmp_path).kind == 'unsupported'
    assert not is_known_read_only_command(command)
