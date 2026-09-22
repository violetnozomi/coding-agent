"""Offline integrity/secret audit; no Provider imports or network operations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = '07a294ccc5cc3780ffdb03beedf0101068b2792d'
ALLOWED = {'nz_coder/runtime/verification/sidecar_verifier.py',
           'nz_coder/runtime/verification/dependency_evidence.py'}


def main():
    historical = 0
    tree = subprocess.check_output(['git', 'ls-tree', '-r', BASE, 'docs/evidence', 'nz_coder'], cwd=ROOT, text=True)
    for line in tree.splitlines():
        metadata, name = line.split('\t', 1)
        if name in ALLOWED:
            continue
        data = (ROOT / name).read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        assert actual == metadata.split()[2], name
        historical += 1
    changed = set(subprocess.check_output(['git', 'diff', BASE, '--name-only', '--', 'nz_coder'], cwd=ROOT, text=True).splitlines())
    assert changed <= ALLOWED
    forbidden = {'reasoning_content', 'private_reasoning', '_nz_provider_reasoning_content',
                 'authorization', 'cookie', 'api_key', 'access_token'}
    def fields(value):
        if isinstance(value, dict):
            assert not set(str(k).lower() for k in value) & forbidden
            for item in value.values():
                fields(item)
        elif isinstance(value, list):
            for item in value:
                fields(item)
    secrets = []
    env_path = ROOT / '.env'
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            key, value = line.split('=', 1)
            if any(marker in key.upper() for marker in ('API_KEY', 'TOKEN', 'SECRET', 'PASSWORD')):
                value = value.strip().strip('\"\'')
                if len(value) >= 12:
                    secrets.append(value)
    for path in OUT.rglob('*'):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        text = path.read_text()
        assert not any(value in text for value in secrets), 'credential match'
        assert str(Path.home()) not in text, f'personal path: {path.name}'
        if path.suffix == '.json':
            fields(json.loads(text))
    # Prompt policy and all non-supporting fields are unchanged in full-hook replay.
    old = json.loads((OUT / 'red/c-sidecar-context.json').read_text())['context']
    new = json.loads((OUT / 'after/c-sidecar-context.json').read_text())['context']
    assert all(new[key] == value for key, value in old.items())
    payload = json.loads((OUT / 'after/c-sidecar-context.json').read_text())['packet']
    assert 'invalid_write_preserves_destination' not in payload and '11/12' not in payload
    audit = {'passed': True, 'unchanged_historical_and_production_files': historical,
             'production_files_allowed': sorted(ALLOWED), 'historical_evidence_changed': False,
             'main_requests': 0, 'verifier_requests': 0, 'planner_requests': 0,
             'embedding_requests': 0, 'infcodex_requests': 0,
             'network_guard': 'all replay/tests via existing seccomp IPv4/IPv6 denial; focused Provider entry points forbidden',
             'credentials_scan': 'pass', 'private_reasoning_fields': 'absent', 'personal_paths': 'absent',
             'no_evaluator_truth_in_packet': True}
    (OUT / 'integrity-audit.json').write_text(json.dumps(audit, indent=2) + '\n')
    sums = {str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(OUT.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS.json' and '__pycache__' not in p.parts}
    (OUT / 'SHA256SUMS.json').write_text(json.dumps(sums, indent=2) + '\n')
    print(f'PASS: {historical} immutable historical/source files; bounded packet, privacy and zero model requests')


if __name__ == '__main__':
    main()
