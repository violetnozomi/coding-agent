"""Verify this offline audit, without models or repository-code execution."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = '4695b46d89cce8366eaaf7941448bc972c476953'


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=30)


def main():
    names = git('ls-tree', '-r', '--name-only', BASE, 'nz_coder', 'docs/evidence').decode().splitlines()
    for name in names:
        assert (ROOT / name).read_bytes() == git('show', f'{BASE}:{name}'), name
    for name, expected in json.loads((OUT / 'SHA256SUMS.json').read_text()).items():
        assert hashlib.sha256((OUT / name).read_bytes()).hexdigest() == expected, name
    nested = json.loads((OUT / 'fixtures/nested.json').read_text())['calls']
    assert [c['proposed_usage_role'] for c in nested] == ['returned', 'argument']
    assert nested[0]['raw']['line'] == nested[1]['raw']['line']
    assert nested[0]['column'] != nested[1]['column']
    same = json.loads((OUT / 'fixtures/same-name-nested.json').read_text())['calls']
    assert [c['proposed_usage_role'] for c in same] == ['returned', 'argument']
    c = json.loads((OUT / 'c/config-edge.json').read_text())
    assert c['proposed_usage_role'] == 'returned' and c['candidate_eligible']
    assert 'usage_role' not in c['persisted_edge']
    for filename in ('c/proposed-role.json', 'c/config-edge.json'):
        text = (OUT / filename).read_text()
        assert all(s not in text for s in ('11/12', 'invalid_write_preserves_destination', 'oracle/'))
    print('PASS: hashes, all production/history bytes, nested node roles, C proposal and no persisted role')


if __name__ == '__main__':
    main()
