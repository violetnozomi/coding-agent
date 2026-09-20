"""Offline production requirement-scope regressions from the paid M trace."""
from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.model_gateway import ResolvedModelRuntime

FIXTURE = Path(__file__).parents[1] / 'fixtures/paid_m_requirement_fragment.json'


def _run(monkeypatch, workspace, task, actions, *, semantic=('accept',), label='replay'):
    requests, reviews, states, permissions = [], [], [], []
    initial_files = {str(p.relative_to(workspace)): p.read_text()
                     for p in workspace.rglob('*') if p.is_file()}
    home = workspace.parent / (workspace.name + '-home')
    home.mkdir(exist_ok=True)
    monkeypatch.setenv('HOME', str(home))
    for key in ('API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'API_BASE_URL',
                'KODAX_VERIFIER_PROVIDER', 'KODAX_VERIFIER_MODEL'):
        monkeypatch.delenv(key, raising=False)

    def deny_network(*_args, **_kwargs):
        raise AssertionError('offline scope regression attempted network access')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    caps = ModelCapabilities(provider='offline', model_id='scope-fixture', supports_streaming=False)
    env = None

    class Provider:
        name = 'offline'

        def create_completion(self, _client, **kwargs):
            states.append(copy.deepcopy(env.runtime_state.to_dict()))
            names = [t.get('function', {}).get('name') for t in kwargs.get('tools', [])]
            if names == ['emit_sidecar_verdict']:
                reviews.append(copy.deepcopy(kwargs))
                assert len(reviews) <= len(semantic), 'unexpected semantic request'
                verdict = semantic[len(reviews) - 1]
                action = [('emit_sidecar_verdict', {'verdict': verdict, 'reason': (
                    'Controlled semantic judgment: report actual test result and limitations.'
                    if verdict == 'revise' else 'Controlled semantic acceptance of the final report.'
                )})]
            else:
                requests.append(copy.deepcopy(kwargs))
                assert len(requests) <= len(actions), 'unexpected main request'
                action = actions[len(requests) - 1]
            calls = [] if isinstance(action, str) else [
                SimpleNamespace(id=f'call-{len(requests)}-{i}', type='function',
                    function=SimpleNamespace(name=name, arguments=json.dumps(args)))
                for i, (name, args) in enumerate(action)
            ]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=action if isinstance(action, str) else '', tool_calls=calls),
                finish_reason='tool_calls' if calls else 'stop')], usage=None)

    runtime = ResolvedModelRuntime(provider_id='offline', model_id='scope-fixture',
        request_model_id='scope-fixture', variant=None, provider=Provider(),
        client=SimpleNamespace(close=lambda: None), capabilities=caps, owns_client=True)
    for target in (native_sdk,):
        monkeypatch.setattr(target, 'resolve_model_runtime', lambda *_a, **_k: runtime)
    monkeypatch.setattr('nz_coder.runtime.execution.loop.resolve_model_runtime', lambda *_a, **_k: runtime)
    monkeypatch.setattr('nz_coder.runtime.model_gateway.resolve_model_runtime', lambda *_a, **_k: runtime)

    def allow(name, args):
        allowed = name == 'bash' and args.get('command', '').startswith('python -m pytest -q tests')
        permissions.append((name, args, allowed))
        return allowed

    request = RunRequest(agent=AgentDefinition(name='scope-replay', instructions='Read the requirements and report actual results.'),
        profile=MAIN_PROFILE, workspace=workspace, session_id='scope-replay', stream=False,
        provider='offline', model='scope-fixture', messages=({'role': 'user', 'content': task},),
        metadata={'persist_session': False, 'permission_mode': 'auto', 'max_turns': len(actions)})
    options = RunOptions(permission_asker=allow)
    env = native_sdk.build_product_run_environment(request, options)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        state = copy.deepcopy(env.runtime_state.to_dict())
        trace = [json.loads(line) for line in env.tracer.path.read_text().splitlines()]
        capture = {'state': state, 'states_before_requests': states, 'runtime': trace,
                   'requests': requests, 'semantic_requests': reviews, 'permissions': permissions,
                   'result': result.status.value, 'paid_model_requests': 0}
        evidence = os.environ.get('NZ_SCOPE_EVIDENCE_DIR')
        if evidence:
            output = Path(evidence) / label
            output.mkdir(parents=True, exist_ok=True)
            (output / 'replay.json').write_text(json.dumps(capture, ensure_ascii=False, indent=2))
            files = {str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in workspace.rglob('*') if p.is_file() and not any(
                         x.startswith('.') or x == '__pycache__' for x in p.relative_to(workspace).parts)}
            (output / 'workspace-hashes.json').write_text(json.dumps(files, indent=2))
            final_files = {name: (workspace / name).read_text() for name in files}
            diff = []
            for name in sorted(initial_files.keys() | final_files.keys()):
                diff.extend(difflib.unified_diff(initial_files.get(name, '').splitlines(keepends=True),
                    final_files.get(name, '').splitlines(keepends=True), fromfile='a/' + name, tofile='b/' + name))
                if name in final_files:
                    target = output / 'final-files' / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(final_files[name])
            (output / 'workspace.diff').write_text(''.join(diff))
        return result, state, trace, requests, reviews
    finally:
        env.close()


def _money(workspace):
    fixture = json.loads(FIXTURE.read_text())
    for name, content in fixture['initial'].items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return fixture


def _money_actions(fixture):
    return [
        [('read_file', {'path': name}) for name in fixture['initial']],
        [('write_files_batch', {'overwrite': True, 'files': [
            {'path': name, 'content': content} for name, content in fixture['before_repair'].items()
        ]})],
        [('bash', {'command': 'python -m pytest -q tests'})],
        fixture['repair'],
        [('bash', {'command': 'python -m pytest -q tests'})],
        'Implemented the pricing migration and all five callers. python -m pytest -q tests: 20 passed. '
        'Limitations: finite test coverage; no external integration was exercised.',
    ]


def test_paid_money_reference_does_not_block_completion(monkeypatch, tmp_path):
    fixture = _money(tmp_path)
    result, state, trace, requests, reviews = _run(
        monkeypatch, tmp_path, fixture['task'], _money_actions(fixture), label='money-reference')
    checks = [x['status'] for x in trace if x.get('event') == 'verification_result']
    assert checks == ['failed', 'passed']
    assert '6 failed' in str(requests[3]['messages'])
    assert state['verification_contract']['passed'] is True
    assert state['verification_generation'] == state['mutation_generation']
    assert state['verification_contract']['attempted_generation'] == state['acceptance_mutation_generation']
    for name, expected in fixture['final_hashes'].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == expected, name
    acceptance_script = Path(__file__).parents[2] / 'docs/evidence/complex-paired-preflight-2026-09-20/acceptance/M.py'
    checked = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', str(acceptance_script)],
        cwd=tmp_path, env={**os.environ, 'PYTHONPATH': str(tmp_path), 'TASK_WORKSPACE': str(tmp_path)},
        text=True, capture_output=True, timeout=30)
    evidence = os.environ.get('NZ_SCOPE_EVIDENCE_DIR')
    if evidence:
        (Path(evidence) / 'money-reference/independent-acceptance.json').write_text(json.dumps(
            {'exit_code': checked.returncode, 'stdout': checked.stdout, 'stderr': checked.stderr}, indent=2))
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert '19 passed' in checked.stdout
    assert result.status.value == 'completed', state['requirement_ledger']
    assert reviews, 'production semantic hook must run'
    assert all(x['status'] == 'satisfied' for x in state['requirement_ledger']['items'])
    assert 'REQUIREMENTS.md' not in state['requested_paths']
    assert any(e.get('event') == 'tool_call' and e.get('name') == 'read_file'
               and e.get('input', {}).get('path') == 'REQUIREMENTS.md'
               and e.get('executed') for e in trace)
    assert 'REQUIREMENTS.md' not in state['changed_files']


@pytest.mark.parametrize('text, expected', [
    ('Read REQUIREMENTS.md and implement X', []),
    ('Update REQUIREMENTS.md and implement X', ['REQUIREMENTS.md']),
    ('Follow SPEC.md and change api.py', ['api.py']),
    ('Modify SPEC.md to match api.py', ['SPEC.md']),
    ('Implement the API described in contract.json', []),
    ('According to notes.txt, migrate api.py', ['api.py']),
    ('Use the requirements in guide.rst and change api.py', ['api.py']),
    ('Read source.py and update target.py according to fixtures/spec.json', ['target.py']),
    ('Run node --test literal.test.cjs', []),
    ('Modify literal.test.cjs and run node --test literal.test.cjs', ['literal.test.cjs']),
    ('Implement X and run tests/test_money.py', []),
    ('Fix api.py and validate against fixtures/rounding.json', ['api.py']),
    ('Read SPEC.md and update SPEC.md', ['SPEC.md']),
    ('Update web.py, cli.py, jobs.py, mobile.py and reporting.py', ['web.py', 'cli.py', 'jobs.py', 'mobile.py', 'reporting.py']),
    ('Create migration-notes.md', ['migration-notes.md']),
    ('Delete obsolete.md', ['obsolete.md']),
    ('Rename old.md to new.md', ['old.md', 'new.md']),
    ('Inspect README.md and summarize the API changes', []),
])
def test_reference_mutation_and_verification_roles(text, expected):
    from nz_coder.runtime.execution.runtime_state import extract_explicit_mutation_paths
    assert extract_explicit_mutation_paths(text) == expected


def test_money_missing_named_caller_is_not_closed_by_green_test(monkeypatch, tmp_path):
    fixture = _money(tmp_path)
    actions = _money_actions(fixture)
    batch = actions[1][0][1]['files']
    batch[:] = [f for f in batch if f['path'] != 'reporting.py']
    # This existing real test checks the central API, deliberately not callers.
    command = 'python -m pytest -q tests/test_consumers.py::test_rounding_happens_after_multiplying_not_per_unit'
    actions[2] = [('bash', {'command': command})]
    actions[4] = [('bash', {'command': command})]
    task = fixture['task'].replace('python -m pytest -q tests', command)
    task = 'Migrate web.py, cli.py, jobs.py, mobile.py and reporting.py. ' + task
    result, state, trace, *_ = _run(monkeypatch, tmp_path, task, actions, label='missing-caller')
    assert state['verification_contract']['passed'] is True
    assert result.status.value != 'completed'
    assert (tmp_path / 'reporting.py').read_text() == fixture['initial']['reporting.py']
    assert any('reporting.py' in p['requirement']['expected_artifacts'] and p['status'] != 'satisfied'
               for p in state['requirement_ledger']['items'])


def _small(workspace):
    (workspace / 'api.py').write_text('value = 1\n')
    (workspace / 'REQUIREMENTS.md').write_text('The API value must be 2.\n')
    (workspace / 'obsolete.md').write_text('obsolete\n')
    (workspace / 'tests').mkdir()
    (workspace / 'tests/test_api.py').write_text('from api import value\ndef test_value():\n    assert value == 2\n')
    return [
        [('read_file', {'path': 'REQUIREMENTS.md'}), ('read_file', {'path': 'api.py'}),
         ('read_file', {'path': 'obsolete.md'})],
        [('edit_file', {'path': 'api.py', 'old_text': 'value = 1', 'new_text': 'value = 2'})],
    ]


@pytest.mark.parametrize('instruction', [
    'update REQUIREMENTS.md with the new contract',
    'create migration-notes.md',
    'delete obsolete.md',
    'rename obsolete.md to migration-notes.md',
])
def test_explicit_artifact_obligation_remains_without_mutation(monkeypatch, tmp_path, instruction):
    actions = _small(tmp_path)
    actions += [[('bash', {'command': 'python -m pytest -q tests'})],
                'All files modified, including REQUIREMENTS.md and migration-notes.md; deletion and rename complete.']
    result, state, *_ = _run(monkeypatch, tmp_path,
        f'Implement api.py and {instruction}. Run python -m pytest -q tests.', actions,
        label='missing-' + instruction.split()[0])
    assert state['verification_contract']['passed'] is True
    assert result.status.value != 'completed'


@pytest.mark.parametrize('instruction, changes, ready', [
    ('Delete obsolete.md', [{'op': 'replace', 'path': 'obsolete.md', 'old_text': 'obsolete', 'new_text': 'changed'}], False),
    ('Delete obsolete.md', [{'op': 'delete', 'path': 'obsolete.md'}], True),
    ('Rename obsolete.md to migration-notes.md', [{'op': 'create', 'path': 'migration-notes.md', 'content': 'obsolete\n'}], False),
    ('Rename obsolete.md to migration-notes.md', [{'op': 'delete', 'path': 'obsolete.md'}, {'op': 'create', 'path': 'migration-notes.md', 'content': 'obsolete\n'}], True),
    ('Create migration-notes.md', [{'op': 'create', 'path': 'migration-notes.md', 'content': 'API value is 2.\n'}], True),
])
def test_mutation_operation_uses_real_workspace_fact(monkeypatch, tmp_path, instruction, changes, ready):
    actions = _small(tmp_path)
    actions += [[('apply_patch', {'changes': changes})],
                [('bash', {'command': 'python -m pytest -q tests'})], 'API value is 2. Tests passed.']
    result, state, trace, *_ = _run(monkeypatch, tmp_path,
        f'Fix api.py. {instruction}. Run python -m pytest -q tests.', actions,
        label='operation-' + instruction.split()[0].lower() + '-' + str(ready))
    assert state['verification_contract']['passed'] is True
    assert (result.status.value == 'completed') is ready, state['requirement_ledger']
    if ready and instruction.startswith(('Delete', 'Rename')):
        assert not (tmp_path / 'obsolete.md').exists()
    if ready and instruction.startswith(('Create', 'Rename')):
        assert (tmp_path / 'migration-notes.md').is_file()


def test_scope_fix_preserves_semantic_revise_then_accept(monkeypatch, tmp_path):
    actions = _small(tmp_path)
    actions += [[('bash', {'command': 'python -m pytest -q tests'})],
                'python -m pytest -q tests: 1 passed. Remaining limitation: synthetic example, no external integration.']
    result, state, trace, _, reviews = _run(monkeypatch, tmp_path,
        'Read REQUIREMENTS.md and fix api.py. Preserve compatibility. '
        'Run python -m pytest -q tests and report actual results and limitations.',
        actions, semantic=('revise', 'accept'), label='semantic-revise')
    assert result.status.value == 'completed'
    assert len(reviews) == 2
    verdicts = [e['verdict'] for e in trace if e.get('event') == 'sidecar_finished']
    assert verdicts == ['revise', 'accept']
    boundaries = [e for e in trace if e.get('event') == 'terminal_boundary_settled']
    assert any(e['decision'] == 'continue' for e in boundaries)
    assert boundaries[-1]['status'] == 'completed'


@pytest.mark.parametrize('text, expected', [
    ('不要修改 SPEC.md；修改 api.py', ['api.py']),
    ('Do not modify SPEC.md and update api.py', ['api.py']),
    ('Never change SPEC.md; fix api.py', ['api.py']),
    ('Read README.md and implement api.py', ['api.py']),
    ('Add the new API documentation to REQUIREMENTS.md', ['REQUIREMENTS.md']),
])
def test_scope_negation_and_reference_are_not_write_authority(text, expected):
    from nz_coder.runtime.execution.runtime_state import extract_explicit_mutation_paths
    assert extract_explicit_mutation_paths(text) == expected


def test_node_test_can_be_both_mutation_and_verification_artifact(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract
    from nz_coder.runtime.execution.runtime_state import RuntimeState
    text = 'Modify literal.test.cjs and run node --test literal.test.cjs.'
    (tmp_path / 'literal.test.cjs').write_text('// existing tests\n')
    state = RuntimeState()
    state.set_acceptance_criteria_from_text(text)
    contract = derive_task_contract(text, acceptance_command=state.verification_contract['command'],
                                   workspace=tmp_path, explicit_path_allowlist=tuple(state.requested_paths))
    assert any('literal.test.cjs' in r.expected_artifacts for r in contract.requirements)
    assert contract.acceptance_commands == ('node --test literal.test.cjs',)


def test_paid_storage_replay_keeps_semantic_report_review(monkeypatch, tmp_path):
    # Reuse the frozen real S initial/final files, not a fabricated resolved ledger.
    root = Path(__file__).parents[2] / 'docs/evidence'
    pre = root / 'complex-paired-preflight-2026-09-20'
    final = root / 'complex-paired-2026-09-20/S/nzcoder/final-files'
    initial = pre / 'initial/S'
    for p in initial.rglob('*'):
        if p.is_file():
            target = tmp_path / p.relative_to(initial)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(p.read_bytes())
    manifest = json.loads((pre / 'manifest.json').read_text())
    task = next(t['prompt'] for t in manifest['tasks'] if t['id'] == 'S')
    actions = [
        [('read_file', {'path': str(p.relative_to(initial))}) for p in initial.rglob('*') if p.is_file()],
        [('write_files_batch', {'overwrite': True, 'files': [
            {'path': str(p.relative_to(final)), 'content': p.read_text()}
            for p in final.rglob('*') if p.is_file() and p.name != 'REQUIREMENTS.md'
            and (not (initial / p.relative_to(final)).exists() or p.read_bytes() != (initial / p.relative_to(final)).read_bytes())
        ]})],
        [('bash', {'command': 'python -m pytest -q tests'})],
        'python -m pytest -q tests: 83 passed. Limitations: no cross-process locking; '
        'revision read/replace has a TOCTOU window; no directory fsync or crash-leftover cleanup.',
    ]
    result, state, trace, _, reviews = _run(monkeypatch, tmp_path, task, actions,
        semantic=('revise', 'accept'), label='storage-revise')
    assert result.status.value == 'completed'
    assert '83 passed' in state['verification_contract']['output']
    assert len(reviews) == 2
    assert [e['verdict'] for e in trace if e.get('event') == 'sidecar_finished'] == ['revise', 'accept']
    assert state['mutation_generation'] == 1
    assert (tmp_path / 'REQUIREMENTS.md').read_bytes() == (initial / 'REQUIREMENTS.md').read_bytes()


@pytest.mark.parametrize('text, expected', [
    ('Fix api.py.', ['api.py']),
    ('Update config.json.', ['config.json']),
    ('Modify literal.test.cjs.', ['literal.test.cjs']),
])
def test_mutation_paths_keep_full_extension_and_sentence_boundary(text, expected):
    from nz_coder.runtime.execution.runtime_state import extract_explicit_mutation_paths
    assert extract_explicit_mutation_paths(text) == expected


def test_adding_documentation_updates_existing_reference_file(monkeypatch, tmp_path):
    actions = _small(tmp_path)
    actions += [[('edit_file', {'path': 'REQUIREMENTS.md', 'old_text': 'must be 2.', 'new_text': 'must be 2. The new contract is value=2.'})],
                [('bash', {'command': 'python -m pytest -q tests'})], 'Implemented and documented value=2; tests passed.']
    result, state, *_ = _run(monkeypatch, tmp_path,
        'Fix api.py and add the new API documentation to REQUIREMENTS.md. Run python -m pytest -q tests.',
        actions, label='explicit-documentation-update')
    assert result.status.value == 'completed', state['requirement_ledger']


def test_todo_and_model_file_claims_cannot_complete_migration(monkeypatch, tmp_path):
    _small(tmp_path)
    (tmp_path / 'api.py').write_text('value = 2\n')  # Old tests already green.
    actions = [
        [('read_file', {'path': 'REQUIREMENTS.md'})],
        [('todo', {'items': [{'content': 'Implement requested migration', 'status': 'completed'}]})],
        [('bash', {'command': 'python -m pytest -q tests'})],
        'Done. modified_files=["api.py", "REQUIREMENTS.md"]. All requirements complete.',
    ]
    result, state, trace, *_ = _run(monkeypatch, tmp_path,
        'Implement the new API in api.py and update REQUIREMENTS.md. Run python -m pytest -q tests.',
        actions, label='todo-self-report')
    assert result.status.value != 'completed'
    assert state['mutation_generation'] == 0
    assert not state['changed_files']
    assert any(e.get('event') == 'tool_call' and e.get('name') == 'todo' and e.get('executed') for e in trace)


def test_explicit_documentation_target_does_not_require_other_docs(tmp_path):
    from nz_coder.runtime.agent.task_contract import derive_task_contract
    (tmp_path / 'README.md').write_text('Unrelated overview\n')
    (tmp_path / 'REQUIREMENTS.md').write_text('API contract\n')
    contract = derive_task_contract(
        'Add the new API documentation to REQUIREMENTS.md. Run python -m pytest -q tests.',
        acceptance_command='python -m pytest -q tests', workspace=tmp_path)
    assert [r.expected_artifacts for r in contract.requirements if r.kind == 'docs'] == [('REQUIREMENTS.md',)]


def test_operation_provenance_survives_contract_and_ledger_round_trip(tmp_path):
    from nz_coder.runtime.agent.task_contract import TaskContract, RequirementLedger, derive_task_contract
    contract = derive_task_contract('Rename old.md to new.md. Run python -m pytest -q tests.',
                                   acceptance_command='python -m pytest -q tests', workspace=tmp_path)
    restored = TaskContract.from_dict(contract.to_dict(), workspace=tmp_path)
    assert restored == contract
    ledger = RequirementLedger.from_contract(restored)
    assert RequirementLedger.from_dict(ledger.to_dict()).to_dict() == ledger.to_dict()
    docs = next(r for r in restored.requirements if r.kind == 'docs')
    assert dict(docs.artifact_operations) == {'old.md': 'delete', 'new.md': 'create'}


@pytest.mark.parametrize('instruction', ['Create migration-notes.md', 'Delete obsolete.md', 'Rename obsolete.md to migration-notes.md'])
def test_explicit_file_obligation_without_declared_test_still_blocks_missing_work(monkeypatch, tmp_path, instruction):
    actions = _small(tmp_path)[:1]
    actions.append('Done.')
    result, state, *_ = _run(monkeypatch, tmp_path, instruction, actions, label='no-command-' + instruction.split()[0].lower())
    assert result.status.value != 'completed'


@pytest.mark.parametrize('instruction, changes', [
    ('Create migration-notes.md', [{'op': 'create', 'path': 'migration-notes.md', 'content': 'Migration notes.\n'}]),
    ('Delete obsolete.md', [{'op': 'delete', 'path': 'obsolete.md'}]),
    ('Rename obsolete.md to migration-notes.md', [{'op': 'delete', 'path': 'obsolete.md'}, {'op': 'create', 'path': 'migration-notes.md', 'content': 'obsolete\n'}]),
    ('Update REQUIREMENTS.md', [{'op': 'replace', 'path': 'REQUIREMENTS.md', 'old_text': 'must be 2.', 'new_text': 'must be exactly 2.'}]),
])
def test_explicit_file_obligation_without_command_resolves_after_real_write(monkeypatch, tmp_path, instruction, changes):
    actions = _small(tmp_path)[:1]
    actions += [[('apply_patch', {'changes': changes})], 'Requested file operation completed.']
    result, state, *_ = _run(monkeypatch, tmp_path, instruction, actions,
        label='no-command-done-' + instruction.split()[0].lower())
    assert result.status.value == 'completed', state['requirement_ledger']
    assert state['mutation_generation'] == 1
    assert state['verification_contract'] == {}
    assert all(r['status'] == 'satisfied' for r in state['requirement_ledger']['items'])
