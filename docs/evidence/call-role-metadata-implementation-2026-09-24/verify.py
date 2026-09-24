"""Bounded local integrity verification. No provider/model execution."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = '64a6bba2983f363b14ebb1a936d3715ef87ff2a5'
ALLOWED = {'nz_coder/intelligence/analyzers.py', 'nz_coder/intelligence/code_index.py'}


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=30)


def main():
    changed = set(git('diff', BASE, '--name-only', '--', 'nz_coder').decode().splitlines())
    assert changed == ALLOWED, changed
    names = git('ls-tree', '-r', '--name-only', BASE, 'docs/evidence').decode().splitlines()
    for name in names:
        assert (ROOT / name).read_bytes() == git('show', f'{BASE}:{name}'), name
    edge = json.loads((OUT / 'after/frozen-c-config-edge.json').read_text())
    assert edge['usage_role'] == 'returned' and edge['resolution_kind'] == 'imported-binding'
    assert '38 failed' in (OUT / 'red/focused-tests.txt').read_text()
    assert '128 passed' in (OUT / 'after/focused-final.txt').read_text()
    for name in ('returned', 'unknown'):
        observed = json.loads((OUT / f'after/lsp-{name}.json').read_text())
        assert observed['before']['usage_role'] == observed['after']['usage_role'] == name
    warm = json.loads((OUT / 'after/warm-reuse.json').read_text())
    assert warm['cold']['indexed'] == warm['warm']['reused'] == 2
    assert warm['warm']['indexed'] == 0
    expected = json.loads((OUT / 'SHA256SUMS.json').read_text())
    for name, sha in expected.items():
        assert hashlib.sha256((OUT / name).read_bytes()).hexdigest() == sha, name
    for name in ALLOWED:
        diff = git('diff', BASE, '--', name).decode()
        additions = '\n'.join(x for x in diff.splitlines() if x.startswith('+') and not x.startswith('+++'))
        assert all(x not in additions for x in ('Config', 'configkit', 'invalid_write_preserves_destination', '11/12'))
    print('PASS: production scope, frozen historical evidence, C role, RED/GREEN, LSP, warm reuse, hashes')


if __name__ == '__main__':
    main()
