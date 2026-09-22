"""Reproducible offline suite checks; deliberately no Provider entrypoint."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
SUITE = ROOT / 'tests/evaluation/fixtures/agent_core_diagnostic_v1'


def write(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2) + '\n')


def run(label, argv, timeout=240):
    env = dict(os.environ)
    env.pop('NZ_RUN_PAIRED_SMOKE', None)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    result = subprocess.run(argv, cwd=ROOT, env=env, text=True,
                            capture_output=True, timeout=timeout)
    def clean(text):
        return text.replace(str(ROOT), '<REPO>').replace(str(Path.home()), '<USER_HOME>')
    row = {'label': label, 'argv': [clean(x) for x in argv], 'exit': result.returncode,
           'stdout': clean(result.stdout), 'stderr': clean(result.stderr)}
    write(label + '.json', row)
    with (OUT / 'commands.jsonl').open('a') as stream:
        stream.write(json.dumps(row) + '\n')
    print(label, result.returncode, result.stdout[-500:], flush=True)
    assert result.returncode == 0, row


if __name__ == '__main__':
    offline = [sys.executable, str(ROOT / 'tests/evaluation/fixtures/offline_exec.py')]
    run('suite-validation', offline + [sys.executable, str(SUITE / 'validate.py'), 'validate', 'all', str(OUT)])
    run('focused-tests', offline + [sys.executable, '-m', 'pytest', '-q', 'tests/evaluation/test_agent_core_diagnostic_v1.py'])
    run('evaluation-security-architecture', offline + [sys.executable, '-m', 'pytest', '-q',
        'tests/evaluation', 'tests/architecture', 'tests/test_architecture_boundary.py',
        'tests/runtime/test_task_reference_evidence.py'])
    run('ruff', [sys.executable, '-m', 'ruff', 'check', str(SUITE),
                'tests/evaluation/test_agent_core_diagnostic_v1.py', str(OUT / 'verify.py')])
    run('compile-import', offline + [sys.executable, '-c',
        "import ast,pathlib; root=pathlib.Path('tests/evaluation/fixtures/agent_core_diagnostic_v1'); "
        "files=list(root.rglob('*.py')); [compile(p.read_text(),str(p),'exec') for p in files]; "
        "print(str(len(files))+' Python files compiled without writing caches')"])
    paths = subprocess.check_output(['git', 'ls-files', 'nz_coder', 'docs/evidence'], cwd=ROOT, text=True).splitlines()
    original = {}
    for name in paths:
        if name.startswith(str(OUT.relative_to(ROOT)) + '/'):
            continue
        original[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    write('baseline-source-history-hashes.json', original)
