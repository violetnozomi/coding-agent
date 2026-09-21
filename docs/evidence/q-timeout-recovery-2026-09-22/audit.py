"""Offline source/history/publication integrity audit; no model calls."""
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
BASE = '4e59fe75032dd32186b73d92b7ad98c955866176'
PRODUCTION = 'nz_coder/runtime/model_gateway/stream.py'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def main():
    for path in OUT.rglob('__pycache__'):
        shutil.rmtree(path)
    # Retain stack frames/messages, removing only host identifiers.
    for path in OUT.rglob('*'):
        if path.is_file() and path.suffix in {'.json', '.jsonl', '.txt', '.md'}:
            text = path.read_text().replace(str(ROOT), '<REPO>').replace(str(Path.home()), '<USER_HOME>')
            text = re.sub(r'/tmp/pytest-of-[^/\s"\']+', '<PYTEST_ROOT>', text)
            if path.suffix == '.txt':
                text = ''.join(line.rstrip() + '\n' for line in text.splitlines())
            path.write_text(text)

    failures = []
    historical = 0
    sources = {}
    changed_source = []
    tree = subprocess.check_output(['git', 'ls-tree', '-r', BASE, '--', 'docs/evidence', 'nz_coder'], cwd=ROOT, text=True)
    for row in tree.splitlines():
        metadata, name = row.split('\t', 1)
        expected = metadata.split()[2]
        path = ROOT / name
        data = path.read_bytes()
        actual = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
        if name.startswith('docs/evidence/'):
            historical += 1
            if actual != expected:
                failures.append('historical changed: ' + name)
        else:
            sources[name] = sha(path)
            if actual != expected:
                changed_source.append(name)
    if changed_source != [PRODUCTION]:
        failures.append('unexpected production changes: ' + repr(changed_source))
    save(OUT / 'source-hashes.json', sources)
    patch = subprocess.check_output(['git', 'diff', BASE, '--', PRODUCTION], cwd=ROOT, text=True)
    (OUT / 'implementation.patch').write_text(patch)

    secrets = set()
    for k, v in os.environ.items():
        if re.search('API_KEY|TOKEN|PASSWORD|SECRET', k, re.I) and len(v) >= 16:
            secrets.add(v.encode())
    if (ROOT / '.env').is_file():
        for line in (ROOT / '.env').read_text().splitlines():
            k, sep, v = line.partition('=')
            v = v.strip().strip('\"\'')
            if sep and re.search('API_KEY|TOKEN|PASSWORD|SECRET', k, re.I) and len(v) >= 16:
                secrets.add(v.encode())
    private = {'reasoning_content', 'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'api_key', 'access_token', 'refresh_token'}

    def inspect(value, label):
        if isinstance(value, dict):
            for k, v in value.items():
                if k.lower() in private and v:
                    failures.append('private structured field: ' + label + ':' + k)
                inspect(v, label)
        elif isinstance(value, list):
            for item in value:
                inspect(item, label)

    scanned = 0
    for path in sorted(OUT.rglob('*')):
        if not path.is_file() or path.name in {'integrity-audit.json', 'SHA256SUMS.json'}:
            continue
        scanned += 1
        label = str(path.relative_to(OUT))
        data = path.read_bytes()
        if any(secret in data for secret in secrets):
            failures.append('secret exact match: ' + label)
        if re.search(rb'\bsk-[A-Za-z0-9_-]{20,}\b|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', data):
            failures.append('secret pattern: ' + label)
        if str(Path.home()).encode() in data:
            failures.append('host home path: ' + label)
        if path.suffix == '.json':
            inspect(json.loads(data), label)
        elif path.suffix == '.jsonl':
            for line in data.splitlines():
                inspect(json.loads(line), label)
    report = dict(passed=not failures, baseline=BASE, production_changed=changed_source,
                  historical_files_checked=historical, historical_files_changed=0 if not any('historical' in x for x in failures) else None,
                  scanned_files=scanned, source_files=len(sources), failures=failures,
                  paid_model_requests=0,
                  scans=['configured secret exact values', 'credential patterns', 'nonempty private reasoning/header fields', 'host identifiers', 'git historical blob equality'],
                  limitation='Pattern scanning cannot prove absence of every unknown secret; no historical Q private stack is reconstructed.')
    save(OUT / 'integrity-audit.json', report)
    print(json.dumps(report, indent=2))
    if failures:
        raise SystemExit(1)
    save(OUT / 'SHA256SUMS.json', {str(p.relative_to(OUT)): sha(p) for p in sorted(OUT.rglob('*')) if p.is_file() and p.name != 'SHA256SUMS.json'})


if __name__ == '__main__':
    main()
