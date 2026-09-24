"""Offline verify frozen baseline and evidence checksums; no model code invoked."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = 'ee26ac5410e63206a2c2e079629e996ba651bff5'


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=30)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    assert git('diff', BASE, '--', 'nz_coder') == b''
    tracked = git('ls-tree', '-r', '--name-only', BASE, 'docs/evidence').decode().splitlines()
    for name in tracked:
        assert (ROOT / name).read_bytes() == git('show', f'{BASE}:{name}')
    for name in ('generic', 'c'):
        data = json.loads((OUT / f'red/{name}-packet.json').read_text())
        assert data['candidates']
        for forbidden in ('11/12', 'invalid_write_preserves_destination', 'oracle/'):
            assert forbidden not in data['packet']
    assert '2 failed, 3 passed' in (OUT / 'red/focused-tests.txt').read_text()
    assert '32 passed' in (OUT / 'after/existing-dependency-tests.txt').read_text()
    hashes = json.loads((OUT / 'SHA256SUMS.json').read_text())
    for name, expected in hashes.items():
        assert sha(OUT / name) == expected, name
    print('PASS: evidence hashes, production baseline, all historical tracked evidence, packet isolation')


if __name__ == '__main__':
    main()
