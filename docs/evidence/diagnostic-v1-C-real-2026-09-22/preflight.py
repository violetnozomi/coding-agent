"""C-only offline preflight. No Provider construction or Agent execution."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = '391e12d8e17f397476094cba8828c33b6b07e172'
SUITE = ROOT / 'tests/evaluation/fixtures/agent_core_diagnostic_v1'
TASK = SUITE / 'C_long_horizon'
sys.path.insert(0, str(ROOT))
from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts  # noqa: E402
from nz_coder.runtime.execution.runtime_state import RuntimeState  # noqa: E402
from nz_coder.runtime.verification.reference_evidence import reference_digest  # noqa: E402
from nz_coder.foundation import config  # noqa: E402


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, timeout=20).decode().strip()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(name, data):
    path = OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n')


assert git('rev-parse', 'HEAD') == git('rev-parse', 'origin/main') == BASE
assert not git('status', '--short'), 'Baseline must be clean; ignored preflight evidence only'
frozen = json.loads((ROOT / 'docs/evidence/agent-core-diagnostic-suite-v1-2026-09-22/SHA256SUMS.json').read_text())
for name, digest in frozen.items():
    assert sha(ROOT / name) == digest, name
sources = {p: sha(ROOT / p) for p in git('ls-files', 'nz_coder').splitlines()}
for name in sources:
    committed = subprocess.check_output(['git', 'show', BASE + ':' + name], cwd=ROOT, timeout=20)
    assert hashlib.sha256(committed).hexdigest() == sources[name]
save('preflight/source-hashes.json', sources)
source_tree = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
spec = importlib.util.spec_from_file_location('diagnostic_validator', SUITE / 'validate.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)
ws = Path(tempfile.mkdtemp(prefix='nz-diagnostic-C-preflight-')) / 'workspace'
initial = validator.prepare('C_long_horizon', ws)
expected = json.loads((ROOT / 'docs/evidence/agent-core-diagnostic-suite-v1-2026-09-22/C_long_horizon/initial-hashes.json').read_text())
assert initial == expected
assert not any(part in {'oracle', 'acceptance', 'manifest.json', 'requirement-map.json'}
               for name in initial for part in Path(name).parts)
save('preflight/fixture-hashes.json', {'workspace': initial, 'task_sha256': sha(TASK / 'task.md'),
     'spec_sha256': initial['CONFIG_SPEC.md'], 'acceptance_sha256': sha(TASK / 'acceptance/checks.json')})
tests = validator.project_tests('C_long_horizon', ws)
acceptance = validator.acceptance('C_long_horizon', ws)
assert tests['exit'] == 0 and '5 passed' in tests['stdout']
assert acceptance['passed'] == 1 and acceptance['total'] == 12 and acceptance['exit'] == 1
save('preflight/V0-project-tests.json', tests)
save('preflight/V0-acceptance.json', acceptance)
task = (TASK / 'task.md').read_text()
state = RuntimeState()
state.bind_task_references(task, workspace=ws)
roles = [{'path': a.path, 'role': a.role, 'authority': a.authority, 'required': a.required}
         for a in resolve_bootstrap_artifacts(task, workspace=ws).artifacts]
ref = next(a for a in roles if a['path'] == 'CONFIG_SPEC.md')
assert ref['role'] == 'task_reference' and ref['authority'] == 'task_spec' and not ref['required']
assert state.task_reference_evidence[0]['complete']
assert not state.task_reference_evidence[0]['model_observed']
save('preflight/authority.json', {'roles': roles, 'references': state.task_reference_evidence,
     'reference_digest': reference_digest(state.task_reference_evidence, state.task_reference_omitted_count),
     'omitted_count': state.task_reference_omitted_count, 'scope': 'offline preflight instance, not a real run state'})
mapping = json.loads((TASK / 'requirement-map.json').read_text())
ledger = [{'id': name, 'source': 'CONFIG_SPEC.md', 'quote': item['quote'],
           'kind': 'docs' if name == 'documentation' else 'compatibility' if name == 'v1_read' else 'behavior',
           'acceptance_checks': [name], 'status': 'unknown', 'evidence': []}
          for name, item in mapping.items()]
extra = [
 ('spec_immutable', 'artifact', 'CONFIG_SPEC.md is the task authority and\nmust not be edited.'),
 ('enabled_default', 'behavior', 'enabled may\nbe absent and then defaults to true.'),
 ('unknown_fields', 'compatibility', 'Unknown extra fields are ignored.'),
 ('config_construction', 'compatibility', 'Existing Config construction and v1 read tests work.'),
 ('validation_types', 'behavior', 'name and host must be nonempty strings (whitespace-only is invalid); port must\nbe an integer 1..65535, excluding bool; enabled must be bool.'),
 ('validation_order_first_only', 'behavior', 'Validate in this order: version, name, host, port, enabled, and report only the\nfirst problem.'),
 ('unsupported_version', 'behavior', 'Unsupported versions produce path "version", code "unsupported".'),
 ('config_error_shape', 'behavior', 'Raise ConfigError, still a ValueError subclass, with to_dict() exactly:\n{"error":"invalid_config","issues":[{"path":"...","code":"..."}]}.') ,
 ('migration_idempotence', 'behavior', 'Repeating migration has identical semantic output.'),
 ('cli_show', 'compatibility', 'python -m configkit show PATH keeps its current summary JSON for either version.'),
 ('cli_success_exit', 'behavior', 'Successful commands exit 0.'),
 ('export_preservation', 'behavior', 'Service export_config(source, destination) writes v2 to destination, never\nchanges source, and returns the existing summary.'),
 ('docs_error_shape', 'docs', 'and the invalid_config error shape.'),
 ('meaningful_tests', 'tests', 'Add meaningful tests'),
 ('old_test_coverage', 'compatibility', 'without dropping old\ncompatibility coverage.'),
 ('exact_project_verification', 'verification', 'Run python -m pytest -q tests after implementation.'),
 ('actual_test_report', 'reporting', 'Report actual test results'),
 ('remaining_limitations', 'reporting', 'and remaining limitations.'),
]
spec_text = (TASK / 'workspace/CONFIG_SPEC.md').read_text()
for identity, kind, quote in extra:
    assert quote in spec_text, identity
    ledger.append({'id': identity, 'source': 'CONFIG_SPEC.md', 'quote': quote, 'kind': kind,
                   'acceptance_checks': [], 'status': 'unknown', 'evidence': []})
save('preflight/user-obligations.json', {'scope': 'evaluator-only; never injected into Provider context',
     'completion_dimensions': ['behavioral_acceptance', 'project_test_obligation', 'test_addition_obligation',
                               'documentation_obligation', 'final_report_obligation', 'runtime_terminal_state'],
     'obligations': ledger, 'note': 'Checks are partial witnesses, not proof of every quoted obligation. Empty mappings need separate audit.'})
save('preflight/config.json', {'baseline': BASE, 'origin_main': git('rev-parse', 'origin/main'),
     'initial_worktree': 'clean', 'source_tree_hash': source_tree,
     'source_tree_hash_algorithm': 'SHA256 of sorted compact JSON path-to-SHA256 map',
     'fixture_file_count': len(initial), 'workspace_materialized': True,
     'workspace_location': '<TEMP>/nz-diagnostic-C-preflight-*/workspace',
     'model': 'deepseek-v4-flash', 'provider': 'openai-compatible', 'endpoint': 'https://api.deepseek.com',
     'stream': True, 'max_tokens': 64000, 'thinking': 'not explicitly set', 'reasoning_effort': 'not explicitly set',
     'nominal_main_cap': 24, 'hard_main_cap': 24, 'physical_auxiliary_cap': 8,
     'retries_count_against_caps': True, 'maximum_physical_requests': 32,
     'bash_timeout_seconds': config.BASH_TIMEOUT_SECONDS,
     'provider_idle_timeout_seconds': config.PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS,
     'provider_hard_timeout_seconds': config.PROVIDER_HARD_TIMEOUT_SECONDS,
     'runner': 'NativeSDKRunner -> AgentRunner (planned; not instantiated)',
     'auxiliary_policy': 'production natural triggers only; no manual verifier; historical verifier max_tokens=1024/thinking disabled',
     'permission_policy': 'unchanged recent-Q run-nz callback plus production built-in guards; no suite-proposed expansion',
     'network': 'planned child seccomp non-Unix socket denial, model-only UDS proxy; preflight entirely offline',
     'historical_difference': 'C workspace/task/acceptance and separate test/report obligation audit; same production source/settings as recent Q',
     'launch_ready': False, 'remaining_prelaunch': ['fresh authorization', 'C capture/analyzer wiring with settled mutation boundaries',
         'verify evaluator-path access rejection and audit hooks', 'recheck source/workspace hashes and effective isolated runtime settings'],
     'authorized': False, 'main_requests': 0, 'auxiliary_requests': 0})
command_record = {'command': 'python tests/evaluation/fixtures/offline_exec.py python docs/evidence/diagnostic-v1-C-real-2026-09-22/preflight.py',
                  'results': {'project_tests': '5 passed', 'acceptance': '1/12', 'paid_requests': 0}}
(OUT / 'commands.jsonl').write_text(json.dumps(command_record) + '\n')
save('preflight/SHA256SUMS.json', {str(p.relative_to(OUT)): sha(p) for p in sorted(OUT.rglob('*.json'))
                                 if p.name != 'SHA256SUMS.json'})
print(json.dumps({'preflight': 'passed', 'project_tests': '5 passed', 'acceptance': '1/12',
                  'obligations': len(ledger), 'paid_requests': 0, 'real_run_executed': False}))
