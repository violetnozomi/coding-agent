"""通用需求从真实产物到独立评审的闭环，不能用测试通过替代文档。"""
from dataclasses import replace
import asyncio
import hashlib
import threading
from types import SimpleNamespace

import pytest

from nz_coder.runtime.adapters.verification_workspace import collect_artifact_facts
from nz_coder.runtime.verification.completion_gate import CompletionGate
from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.identity import command_identity
from nz_coder.runtime.verification.domain.models import (
    ArtifactFact, CheckSpec, ExecutionFact, ReviewFact, RunScope, TaskDefinition,
    TaskScope, WorkspaceObservation, WorkspaceVersion,
)
from nz_coder.runtime.verification.domain.requirements import Requirement, RequirementLedgerView, TaskContract
from nz_coder.runtime.verification.domain.service import VerificationDomain


def _setup(root, *, explicit=False):
    scope = RunScope(TaskScope('session', 'task', str(root)), 'run', 'agent')
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=WorkspaceVersion())
    contract = TaskContract(requirements=(
        Requirement('R2', 'Add tests', 'test', ('tests/test_cli.py',) if explicit else (), 'semantic', required_evidence=('semantic_review',)),
        Requirement('R3', 'Update docs', 'docs', ('README.md',) if explicit else (), 'mixed', required_evidence=('semantic_review',)),
    ))
    domain.declare_task(TaskDefinition(scope.task, contract=contract))
    spec = CheckSpec('acceptance', command_identity('python -m unittest discover -s tests -t . -v', str(root)),
                     'test', required=True, source='user_declared', requirement_ids=('R2', 'R3'))
    domain.register_check(spec)
    return domain, spec


def _write(domain, root, paths=('tests/test_cli.py', 'README.md'), *, operation='create'):
    for path in paths:
        p = root / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('import unittest\nclass Example(unittest.TestCase):\n def test_ok(self): self.assertEqual(2 + 2, 4)\n'
                     if path.endswith('.py') else '# Usage\nRun the CLI.\n')
        domain.observe_artifact(ArtifactFact(domain.scope.task, path, operation, 0,
            hashlib.sha256(p.read_bytes()).hexdigest()))


def _pass(domain, spec, execution='pass'):
    domain.begin_execution(execution, (spec.id,))
    v = domain.snapshot().version
    domain.observe_execution(ExecutionFact(execution, domain.scope, spec.command, 'succeeded', v, v,
        capabilities=('test',), targets=spec.targets, exit_code=0, test_count=1))


def _review(domain, *, accepted=True):
    domain.observe_review(ReviewFact('review', domain.scope.task, ('R2', 'R3'), domain.snapshot().version,
        'reviewed-materials', accepted, basis_fingerprint=domain.review_basis()))


def test_generic_requirements_need_current_pass_and_independent_review(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    view = RequirementLedgerView.from_snapshot(domain.snapshot())
    assert view.semantic_review_pending_only(generation=0)
    assert not domain.snapshot().ready_for_completion
    decision = CompletionGate().evaluate(view, mutation_generation=0)
    assert 'run the user-declared acceptance command exactly' not in decision.message
    _review(domain)
    assert domain.snapshot().ready_for_completion
    assert all(not r.requirement.expected_artifacts for r in domain.snapshot().requirements)


@pytest.mark.parametrize('paths', [('tests/test_cli.py',), ('README.md',), ()])
def test_generic_review_cannot_replace_missing_kind_of_artifact(tmp_path, paths):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path, paths)
    _pass(domain, spec)
    _review(domain)
    assert not domain.snapshot().ready_for_completion


def test_completion_guidance_identifies_missing_docs_without_repeating_passed_test(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path, ('tests/test_cli.py',))
    _pass(domain, spec)
    decision = CompletionGate().evaluate(RequirementLedgerView.from_snapshot(domain.snapshot()), mutation_generation=0)
    assert 'missing observed documentation modification' in decision.message
    assert 'run the user-declared acceptance command exactly' not in decision.message


def test_generic_review_cannot_replace_explicit_acceptance(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _review(domain)
    assert domain.snapshot().decision(spec.id).status == 'pending'
    assert not domain.snapshot().ready_for_completion


def test_generic_test_cannot_be_satisfied_by_static_check_and_review(tmp_path):
    domain, original = _setup(tmp_path)
    _write(domain, tmp_path)
    spec = replace(original, id='static', capability='static', required=False,
        command=command_identity('python -m compileall tests', str(tmp_path)), source='model_execution')
    domain.register_check(spec)
    domain.begin_execution('static-pass', (spec.id,))
    v = domain.snapshot().version
    domain.observe_execution(ExecutionFact('static-pass', domain.scope, spec.command, 'succeeded', v, v,
        capabilities=('static',), exit_code=0))
    _review(domain)
    assert domain.snapshot().decision(original.id).status == 'pending'
    assert not domain.snapshot().ready_for_completion


def test_generic_reads_do_not_prove_requested_modifications(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path, operation='read')
    _pass(domain, spec)
    _review(domain)
    assert not domain.snapshot().ready_for_completion


def test_generic_older_modifications_cannot_satisfy_new_requirements(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    contract = domain.task_definition.contract
    domain.declare_task(TaskDefinition(domain.scope.task, authority_revision=1,
        contract=replace(contract, requirements=tuple(replace(r, description=r.description + ' again') for r in contract.requirements))))
    _pass(domain, spec)
    _review(domain)
    assert not domain.snapshot().ready_for_completion


def test_explicit_artifacts_cannot_be_replaced_by_other_files(tmp_path):
    domain, spec = _setup(tmp_path, explicit=True)
    _write(domain, tmp_path, ('tests/test_other.py', 'docs/other.md'))
    _pass(domain, spec)
    _review(domain)
    assert not domain.snapshot().ready_for_completion


def test_collects_real_generic_modifications_and_rechecks_previous_facts(tmp_path):
    domain, _ = _setup(tmp_path)
    _write(domain, tmp_path)
    domain, _ = _setup(tmp_path)
    facts = collect_artifact_facts(domain.snapshot(), tmp_path, {'tests/test_cli.py': 'change', 'README.md': 'change'}, 0)
    assert {f.path for f in facts if f.complete} == {'tests/test_cli.py', 'README.md'}
    for fact in facts:
        domain.observe_artifact(fact)
    (tmp_path / 'README.md').unlink()
    facts = collect_artifact_facts(domain.snapshot(), tmp_path, {}, 0)
    assert not next(f for f in facts if f.path == 'README.md').complete


def test_generic_collector_does_not_invent_modifications_from_existing_files(tmp_path):
    domain, _ = _setup(tmp_path)
    (tmp_path / 'README.md').write_text('# Existing documentation\n')
    assert collect_artifact_facts(domain.snapshot(), tmp_path, {}, 0) == ()


def test_generic_checkpoint_needs_revalidation_execution_and_new_review(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    _review(domain)
    assert domain.snapshot().ready_for_completion
    original_revisions = {f.path: f.established_revision for f in domain.snapshot().artifacts}
    restored = restore_domain(domain.checkpoint(), lock=threading.RLock(),
        scope=replace(domain.scope, interaction_run_id='continued'), version=domain.snapshot().version)
    assert not restored.snapshot().ready_for_completion
    for fact in collect_artifact_facts(restored.snapshot(), tmp_path, {}, 0):
        restored.observe_artifact(fact)
    assert {f.path: f.established_revision for f in restored.snapshot().artifacts} == original_revisions
    _pass(restored, spec, 'new-pass')
    assert not restored.snapshot().ready_for_completion
    _review(restored)
    assert restored.snapshot().ready_for_completion


def test_doc_change_invalidates_review_even_when_acceptance_version_stays_same(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    _review(domain)
    assert domain.snapshot().ready_for_completion
    domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 0, 0), paths=('README.md',)))
    _write(domain, tmp_path, ('README.md',), operation='change')
    assert domain.snapshot().execution_ready
    assert not domain.snapshot().ready_for_completion


def _sidecar(domain, spec, root, *, on_request=None):
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.sidecar_verifier import ResolvedVerifierProvider, create_sidecar_verifier_hook

    requests = []

    class Provider:
        name = 'offline-review'

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            if on_request:
                on_request()
            call = SimpleNamespace(function=SimpleNamespace(name='emit_sidecar_verdict',
                arguments='{"verdict":"accept","reason":"Actual tests and documentation cover the request","delivery_assessment":{"status":"reviewed","issues":[]}}'))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]))])

    loop = SimpleNamespace(workdir=root, verification_runtime=SimpleNamespace(domain=domain, reconcile_workspace=lambda: None),
        change_tracker=SimpleNamespace(current_changed_paths=lambda: [], current_deleted_paths=lambda: [],
            render_current_diff=lambda: ''),
        tracer=SimpleNamespace(log=lambda *args, **kwargs: None), _model_gateway_observer_for_run=lambda: None)
    hook = create_sidecar_verifier_hook(loop,
        ResolvedVerifierProvider(Provider(), object(), 'judge', 'offline-review', 'test'), env={})
    context = StopHookContext(transcript=({'role': 'user', 'content': 'Add tests and documentation'},),
        last_assistant_text='Finished', runtime_state={
            'task_contract': domain.task_definition.contract.to_dict(),
            'requirement_ledger': RequirementLedgerView.from_snapshot(domain.snapshot()).to_dict(),
            'verification_contract': {'command': spec.command.original, 'passed': True, 'attempted_generation': 0},
            'mutation_generation': 0,
        })
    return hook, context, requests


def test_sidecar_reads_complete_generic_materials_without_current_diff(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    path = tmp_path / 'README.md'
    path.write_text('# Usage\n' + 'Detailed usage instructions.\n' * 400 + '\nUNIQUE_END_OF_DOCUMENT\n')
    domain.observe_artifact(ArtifactFact(domain.scope.task, 'README.md', 'change', 0,
        hashlib.sha256(path.read_bytes()).hexdigest()))
    _pass(domain, spec)
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    decision = asyncio.run(hook(context))
    assert decision.action == 'complete'
    prompt = str(requests[0]['messages'])
    assert 'UNIQUE_END_OF_DOCUMENT' in prompt
    assert 'def test_ok(self)' in prompt
    assert domain.snapshot().ready_for_completion


@pytest.mark.parametrize('when', ['before', 'during'])
@pytest.mark.parametrize('missing', [False, True], ids=['changed', 'missing'])
def test_sidecar_rejects_generic_file_identity_change(tmp_path, when, missing):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    def change_file():
        path = tmp_path / 'README.md'
        if missing:
            path.unlink()
        else:
            path.write_text('unreviewed replacement')
    hook, context, requests = _sidecar(domain, spec, tmp_path, on_request=change_file if when == 'during' else None)
    if when == 'before':
        change_file()
    decision = asyncio.run(hook(context))
    assert decision.action == 'abort'
    reason = 'An internal error occurred.' if missing else 'Artifact identity changed.'
    assert decision.message == f'Runtime review evidence unavailable: {reason}'
    assert not domain.snapshot().ready_for_completion
    assert len(requests) == (0 if when == 'before' else 1)


def test_sidecar_does_not_attach_old_materials_to_changed_domain(tmp_path):
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    _pass(domain, spec)
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    evidence = hook._evidence
    def concurrent_change(*args, **kwargs):
        result = evidence(*args, **kwargs)
        domain.observe_workspace(WorkspaceObservation(WorkspaceVersion(1, 1, 1), paths=('app.py',)))
        return result
    hook._evidence = concurrent_change
    decision = asyncio.run(hook(context))
    assert decision.action == 'abort'
    assert requests == []
    assert not domain.snapshot().ready_for_completion


def test_sidecar_cannot_certify_truncated_generic_materials(tmp_path):
    from nz_coder.providers.capabilities import ModelCapabilities
    domain, spec = _setup(tmp_path)
    _write(domain, tmp_path)
    path = tmp_path / 'README.md'
    path.write_text('x' * 65000)
    domain.observe_artifact(ArtifactFact(domain.scope.task, 'README.md', 'change', 0,
        hashlib.sha256(path.read_bytes()).hexdigest()))
    _pass(domain, spec)
    hook, context, requests = _sidecar(domain, spec, tmp_path)
    hook._resolved.provider.capabilities = lambda model: ModelCapabilities(
        provider='offline-review', model_id=model, context_tokens=16_000)
    decision = asyncio.run(hook(context))
    assert decision.action == 'abort'
    assert requests == []
    assert not domain.snapshot().ready_for_completion


def test_empty_project_completes_through_real_native_runner(monkeypatch, tmp_path):
    from tests.runtime.test_requirement_scope_runtime import _run

    actions = [
        [('write_file', {'path': 'calculator.py', 'content': 'def add(a, b):\n    return a + b\n'})],
        [('write_file', {'path': 'tests/test_calculator.py',
                         'content': 'from calculator import add\ndef test_add():\n    assert add(2, 3) == 5\n'})],
        [('write_file', {'path': 'README.md',
                         'content': '# Calculator\nUse calculator.add(a, b).\nTest: python -m pytest -q tests\n'})],
        [('bash', {'command': 'python -m pytest -q tests'})],
        'Implemented calculator.add, tests and documentation. One real test passed.',
    ]
    result, state, trace, requests, reviews = _run(monkeypatch, tmp_path,
        '从空目录创建一个小型计算项目，编写真实自动化测试和文档。验收命令：`python -m pytest -q tests`。允许完整测试。', actions,
        assessment={'status': 'reviewed', 'issues': []})
    assert result.status.value == 'completed'
    items = state['requirement_ledger']['items']
    assert all(item['status'] == 'satisfied' for item in items)
    generic = [item for item in items if item['requirement']['kind'] in {'test', 'docs'}]
    assert generic == []  # 泛化描述不生成硬卡片，实际内容仍由评审检查。
    assert len(reviews) == 1
    assert not any(e['event'] == 'requirement_completion_blocked' for e in trace)


@pytest.mark.parametrize('existing_plan', [False, True])
@pytest.mark.parametrize('command', ['python -m pytest -q tests', 'python -m pytest -q tests/test_cli.py'])
def test_executed_tests_bind_actual_generic_artifacts_without_exact_acceptance(tmp_path, existing_plan, command):
    from tests.verification_scenarios import VerificationScenario
    from nz_coder.runtime.adapters.verification_plans import check_spec

    template, _ = _setup(tmp_path)
    scenario = VerificationScenario(template.task_definition.contract, workspace=tmp_path)
    domain = scenario.domain
    _write(domain, tmp_path)
    if existing_plan:
        domain.register_check(check_spec(command, str(tmp_path), capability='test', source='planner', required=True))
    ids = scenario.runtime._register_command(command, str(tmp_path))
    assert ids
    spec = next(s for s in domain.snapshot().specs if s.capability == 'test')
    _pass(domain, spec)
    assert domain.snapshot().decision(spec.id).status == 'passed'
    _review(domain)
    assert domain.snapshot().ready_for_completion


def test_noop_patch_cannot_prove_generic_test_modification(monkeypatch, tmp_path):
    from tests.runtime.test_requirement_scope_runtime import _run

    source = 'def test_existing():\n    assert 2 + 2 == 4\n'
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_existing.py').write_text(source)
    (tmp_path / 'README.md').write_text('# Existing documentation\n')
    actions = [
        [('apply_patch', {'changes': [
            {'path': 'tests/test_existing.py', 'op': 'replace', 'old_text': source, 'new_text': source},
            {'path': 'README.md', 'op': 'replace', 'old_text': '# Existing documentation\n', 'new_text': '# Existing documentation\n'},
        ]})],
        [('bash', {'command': 'python -m pytest -q tests'})],
        'Updated tests and documentation.',
    ]
    monkeypatch.setenv('KODAX_VERIFIER_ALWAYS', '1')
    result, state, trace, requests, reviews = _run(monkeypatch, tmp_path,
        '更新真实自动化测试和文档。验收命令：`python -m pytest -q tests`。允许完整测试。', actions,
        semantic=('blocked',))
    assert result.status.value != 'completed'
    assert not any(item['requirement']['kind'] == 'test' for item in state['requirement_ledger']['items'])
    assert reviews
    assert (tmp_path / 'tests/test_existing.py').read_text() == source


@pytest.mark.parametrize('path,command,subdir', [
    ('tests/test_cli.py', 'python -m unittest discover -s other -v', ''),
    ('tests/test_cli.py', 'python -m unittest discover -s tests -v', ''),
    ('tests/test_cli.py', 'python -m unittest other.test_cli -v', ''),
    ('tests/test_cli.py', 'python -m unittest tests.test_cli -v', ''),
    ('logfile/logfile_test.go', 'go test ./other', ''),
    ('logfile/logfile_test.go', 'go test ./logfile -run TestReady', 'other'),
    ('logfile/logfile_test.go', 'go test ./logfile -run TestReady', ''),
    ('logfile/logfile_test.go', 'go test ./logfile', ''),
    ('tests/test_cli.py', 'python -m pytest -q tests/test_other.py', ''),
    ('tests/test_cli.py', 'echo pytest tests/test_cli.py && python -m unittest discover -s other -v', ''),
    ('tests/test_cli.py', 'python -m pytest -q --ignore tests', ''),
    ('tests/test_cli.py', 'python -m pytest -q tests --ignore=tests/test_cli.py', ''),
    ('tests/test_cli.py', 'python -m pytest -q -m unrelated', ''),
    ('tests/test_cli.py', 'python -m pytest -q -k unrelated', ''),
    ('tests/test_cli.py', 'python -m unittest discover -s tests -k test_other -v', ''),
])
def test_commands_do_not_claim_semantic_requirements_by_filename(tmp_path, path, command, subdir):
    from tests.verification_scenarios import VerificationScenario

    scenario = VerificationScenario(TaskContract(requirements=(Requirement('T', 'Add tests', 'test'),)), workspace=tmp_path)
    _write(scenario.domain, tmp_path, (path,))
    cwd = tmp_path / subdir
    cwd.mkdir(exist_ok=True)
    ids = scenario.runtime._register_command(command, str(cwd))
    assert all('T' not in spec.requirement_ids for spec in scenario.domain.snapshot().specs if spec.id in ids)


def test_static_acceptance_and_real_tests_reach_runtime_owned_review(tmp_path):
    from tests.verification_scenarios import VerificationScenario

    template, _ = _setup(tmp_path)
    contract = replace(template.task_definition.contract, acceptance_commands=('python -m py_compile calculator.py',))
    scenario = VerificationScenario(contract, workspace=tmp_path)
    domain = scenario.domain
    _write(domain, tmp_path)
    scenario.runtime.synchronize_task()
    static = next(s for s in domain.snapshot().specs if s.capability == 'syntax')
    domain.begin_execution('syntax', (static.id,))
    v = domain.snapshot().version
    domain.observe_execution(ExecutionFact('syntax', domain.scope, static.command, 'succeeded', v, v,
        capabilities=('syntax',), exit_code=0))
    scenario.runtime._register_command('python -m pytest -q tests', str(tmp_path))
    spec = next(s for s in domain.snapshot().specs if s.capability == 'test')
    _pass(domain, spec)
    view = RequirementLedgerView.from_snapshot(domain.snapshot())
    assert view.semantic_review_pending_only(generation=0, acceptance_required=True)
    _review(domain)
    assert domain.snapshot().ready_for_completion


def test_generic_artifacts_enter_review_without_invented_execution_guidance(tmp_path):
    domain, _ = _setup(tmp_path)
    _write(domain, tmp_path)
    decision = CompletionGate().evaluate(RequirementLedgerView.from_snapshot(domain.snapshot()),
        mutation_generation=0, acceptance_required=False)
    assert 'Runtime-owned evidence pending' in decision.message
    assert 'run the user-declared acceptance command exactly' not in decision.message
    assert 'Keep the test selectors' not in decision.message
