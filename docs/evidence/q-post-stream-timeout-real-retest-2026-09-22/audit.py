"""Offline publication and frozen-source audit."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


for path in OUT.rglob('__pycache__'):
    shutil.rmtree(path)
failures = []
checks = {}
for name in ('source', 'historical'):
    manifest = json.loads((OUT / f'preflight/{name}-hashes.json').read_text())
    changed = [p for p, h in manifest.items() if sha(ROOT / p) != h]
    checks[name] = {'count': len(manifest), 'changed': changed}
    failures.extend(changed)
base = OUT / 'Q/nzcoder'
frozen = json.loads((base / 'FROZEN.json').read_text())['sha256']
assert all(sha(base / p) == h for p, h in frozen.items())
assert not subprocess.check_output(['git', 'diff', 'HEAD', '--', 'nz_coder'], cwd=ROOT)

secrets = set()
for key, value in os.environ.items():
    if re.search('API_KEY|TOKEN|PASSWORD|SECRET', key, re.I) and len(value) >= 16:
        secrets.add(value.encode())
for line in (ROOT / '.env').read_text().splitlines():
    key, sep, value = line.partition('=')
    value = value.strip().strip('\"\'')
    if sep and re.search('API_KEY|TOKEN|PASSWORD|SECRET', key, re.I) and len(value) >= 16:
        secrets.add(value.encode())
private = {'reasoning_content', 'authorization', 'cookie', 'set-cookie', 'api_key', 'access_token', 'refresh_token'}


def inspect(value, label):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in private and item:
                failures.append(label + ':private-field:' + key)
            inspect(item, label)
    elif isinstance(value, list):
        for item in value:
            inspect(item, label)


files = [p for p in OUT.rglob('*') if p.is_file() and p.name not in {'SHA256SUMS.json', 'integrity-audit.json'}]
for path in files:
    data = path.read_bytes()
    label = str(path.relative_to(OUT))
    if any(secret in data for secret in secrets):
        failures.append(label + ':secret-exact-match')
    if str(Path.home()).encode() in data:
        failures.append(label + ':personal-path')
    if re.search(rb'\bsk-[A-Za-z0-9_-]{20,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', data):
        failures.append(label + ':credential-pattern')
    if path.suffix == '.json':
        inspect(json.loads(data), label)
    elif path.suffix == '.jsonl':
        for line in data.splitlines():
            inspect(json.loads(line), label)
result = {'passed': not failures, 'checks': checks, 'frozen_files': len(frozen),
          'scanned_files': len(files), 'failures': failures,
          'main_requests': 12, 'auxiliary_requests': 1, 'cost': 'unknown',
          'production_changes': False, 'limitation': 'Pattern scans do not prove absence of every unknown secret.'}
save(OUT / 'integrity-audit.json', result)
print(json.dumps(result, indent=2))
assert not failures
save(OUT / 'SHA256SUMS.json', {str(p.relative_to(OUT)): sha(p) for p in sorted(OUT.rglob('*'))
                              if p.is_file() and p.name != 'SHA256SUMS.json'})
