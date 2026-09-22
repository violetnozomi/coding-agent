"""Publication audit for offline diagnostic fixtures; no online capabilities."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[2]
SUITE = ROOT / 'tests/evaluation/fixtures/agent_core_diagnostic_v1'
TEST = ROOT / 'tests/evaluation/test_agent_core_diagnostic_v1.py'
sys.path.insert(0, str(ROOT))
from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts  # noqa: E402
from nz_coder.runtime.execution.runtime_state import RuntimeState  # noqa: E402


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2) + '\n')


task = SUITE / 'C_long_horizon'
text = (task / 'task.md').read_text()
ws = task / 'workspace'
state = RuntimeState()
state.bind_task_references(text, workspace=ws)
roles = [{'path': a.path, 'role': a.role, 'authority': a.authority, 'required': a.required}
         for a in resolve_bootstrap_artifacts(text, workspace=ws).artifacts]
save('authority-preflight.json', {'instruction': text, 'roles': roles,
     'initial_hash': sha(ws / 'CONFIG_SPEC.md'), 'task_reference_evidence': state.task_reference_evidence,
     'omitted_count': state.task_reference_omitted_count, 'model_read_occurred': False})

baseline = json.loads((OUT / 'baseline-source-history-hashes.json').read_text())
changed = [name for name, digest in baseline.items() if sha(ROOT / name) != digest]
assert not changed, changed
assert not subprocess.check_output(['git', 'diff', 'HEAD', '--', 'nz_coder'], cwd=ROOT)
assert not subprocess.check_output(['git', 'diff', '--cached', '--', 'nz_coder'], cwd=ROOT)
old_diff = subprocess.check_output(['git', 'diff', 'f6cfa381fed47d9a82fd4e11a20c440a72b1512d',
                                    '--', 'nz_coder', 'docs/evidence'], cwd=ROOT, text=True)
# Before staging, untracked new evidence is absent; after staging only new evidence is permitted.
assert all(line[6:].startswith(str(OUT.relative_to(ROOT))) for line in old_diff.splitlines()
           if line.startswith('+++ b/'))

files = sorted([p for folder in (SUITE, OUT) for p in folder.rglob('*')
                if p.is_file() and not any(x in {'__pycache__', '.pytest_cache'} for x in p.parts)
                and p.name not in {'SHA256SUMS.json', 'integrity-audit.json'}] + [TEST])
secrets = []
for line in (ROOT / '.env').read_text().splitlines():
    key, sep, value = line.partition('=')
    value = value.strip().strip('\"\'')
    if sep and re.search('KEY|TOKEN|PASSWORD|SECRET', key, re.I) and len(value) >= 16:
        secrets.append(value.encode())
failures = []
for path in files:
    data = path.read_bytes()
    if str(Path.home()).encode() in data:
        failures.append(str(path.relative_to(ROOT)) + ': personal path')
    if any(secret in data for secret in secrets):
        failures.append(str(path.relative_to(ROOT)) + ': credential match')
    if re.search(rb'\bsk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----', data):
        failures.append(str(path.relative_to(ROOT)) + ': credential pattern')
assert not failures, failures
save('integrity-audit.json', {'passed': True, 'baseline': 'f6cfa381fed47d9a82fd4e11a20c440a72b1512d',
     'production_files': sum(p.startswith('nz_coder/') for p in baseline),
     'historical_evidence_files': sum(p.startswith('docs/evidence/') for p in baseline),
     'changed_old_files': changed, 'scanned_files': len(files), 'scan_failures': failures,
     'paid_model_requests': 0, 'agent_runs': 0, 'production_changes': False,
     'limits': 'Finite secret patterns; no model requests/responses exist in this offline suite.'})
files.append(OUT / 'integrity-audit.json')
save('SHA256SUMS.json', {str(p.relative_to(ROOT)): sha(p) for p in sorted(files)})
print('audit passed:', len(baseline), 'source/history hashes;', len(files), 'new files')
