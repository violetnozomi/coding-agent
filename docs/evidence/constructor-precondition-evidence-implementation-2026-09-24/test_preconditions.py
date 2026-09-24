"""Offline production-graph prerequisite checks; no implementation is installed."""
import dataclasses
import json
from pathlib import Path
import runpy
import shutil

import pytest
from nz_coder.intelligence.service import RepoIntelligenceService

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
HELPERS = runpy.run_path(str(ROOT / 'tests/runtime/test_semantic_dependency_evidence.py'))
packet = HELPERS['packet']


def record(name, data):
    (OUT / name).write_text(json.dumps(data, indent=2) + '\n')


@pytest.fixture(autouse=True)
def forbid_provider(monkeypatch):
    def forbidden(*a, **kw):
        raise AssertionError('No model calls permitted')
    sidecar = HELPERS['sidecar']
    monkeypatch.setattr(sidecar, 'invoke_sidecar_verifier', forbidden)
    monkeypatch.setattr(sidecar.SidecarVerifierHook, '__call__', forbidden)
    monkeypatch.setattr(RepoIntelligenceService, 'configure_semantic', forbidden)


def route_a(service, changed):
    """Only consume existing changed identities and direct callee edges."""
    scope = service.changed_scope(changed_paths=changed, limit=100,
        max_depth=1, node_limit=100, time_budget_ms=100,
        confidence_threshold=.85, wait_budget_ms=0)
    assert scope['freshness'] == 'indexed'
    candidates = {}
    for identity in scope['changed_symbol_ids']:
        context = service.symbol_context(identity, limit=100, wait_budget_ms=0)
        for edge in context['callees']:
            target = edge.get('callee_symbol_id')
            if not target or edge['confidence'] < .85:
                continue
            definition = service.symbol_context(target, limit=100, wait_budget_ms=0)['definition']
            if (definition and definition['kind'] == 'class'
                    and definition['path'] not in changed
                    and not definition['path'].startswith('tests/')):
                candidates[target] = {'definition': definition, 'edge': edge}
    return [candidates[key] for key in sorted(candidates)]


def test_generic_packet_red(tmp_path):
    (tmp_path / 'model.py').write_text('from dataclasses import dataclass\n@dataclass\nclass Payload:\n    value: int\n')
    (tmp_path / 'factory.py').write_text('from model import Payload\ndef decode(value):\n    return Payload(value)\n')
    service = RepoIntelligenceService(tmp_path)
    try:
        service.prewarm().result(timeout=15)
        candidates = route_a(service, ['factory.py'])
        assert [x['definition']['name'] for x in candidates] == ['Payload']
        context, text, _, _ = packet(tmp_path, service, ['factory.py'], '')
        record('red/generic-packet.json', {'candidates': candidates, 'packet': text})
        assert '@dataclass\nclass Payload:' in context.supporting_repository_evidence
    finally:
        service.close()


def test_frozen_c_packet_red(tmp_path):
    shutil.copytree(HELPERS['HISTORY'] / 'final-files', tmp_path / 'workspace')
    workspace = tmp_path / 'workspace'
    historical = json.loads((HELPERS['CF'] / 'packets/historical-verifier2-summary.json').read_text())
    diff = json.loads((HELPERS['CF'] / 'packets/native-diff-source.json').read_text())['text']
    state = historical['runtime_state']
    service = RepoIntelligenceService(workspace)
    try:
        service.prewarm().result(timeout=15)
        candidates = route_a(service, state['changed_files'])
        assert any(x['definition']['name'] == 'Config' for x in candidates)
        context, text, _, _ = packet(workspace, service, state['changed_files'], diff,
            state, historical['transcript'], historical['final_assistant_text'])
        assert 'def save(path, config):' in text and '46 passed' in text
        assert json.dumps((workspace / 'CONFIG_SPEC.md').read_text(), ensure_ascii=False) in text
        assert 'def to_dict(config):' in text
        record('red/c-packet.json', {'candidates': candidates, 'packet': text,
            'context': dataclasses.asdict(context)})
        assert '@dataclass(frozen=True)\nclass Config:' in context.supporting_repository_evidence
    finally:
        service.close()


def test_route_a_cannot_reject_unrelated_class_by_relation_alone(tmp_path):
    (tmp_path / 'model.py').write_text('class Payload:\n    pass\nclass AuditMarker:\n    pass\n')
    (tmp_path / 'factory.py').write_text('from model import Payload, AuditMarker\ndef decode(value):\n    AuditMarker()\n    return Payload()\n')
    service = RepoIntelligenceService(tmp_path)
    try:
        service.prewarm().result(timeout=15)
        candidates = route_a(service, ['factory.py'])
        names = {item['definition']['name'] for item in candidates}
        record('analysis/relevance-counterexample.json', {'source': (tmp_path / 'factory.py').read_text(),
            'candidates': candidates, 'relevance_filter_exists': False})
        assert names == {'Payload', 'AuditMarker'}
        assert all(x['edge']['confidence'] >= .85 for x in candidates)
    finally:
        service.close()


def test_previous_envelope_omits_multiline_decorator():
    old = runpy.run_path(str(ROOT / 'docs/evidence/constructor-precondition-evidence-design-2026-09-23/audit.py'))
    source = '@decorator(\n    validate=True,\n    frozen=True,\n)\nclass Payload:\n    value: int\n'
    actual = old['envelope'](source, 'Payload')
    record('analysis/multiline-counterexample.json', {'source': source, 'previous_envelope': actual})
    assert '@decorator(' not in actual['text']


def test_previous_envelope_truncates_validation_method():
    old = runpy.run_path(str(ROOT / 'docs/evidence/constructor-precondition-evidence-design-2026-09-23/audit.py'))
    source = '@dataclass\nclass Payload:\n    value: int\n' + '    # filler\n' * 150 + '    def __post_init__(self):\n        raise ValueError\n'
    actual = old['envelope'](source, 'Payload')
    record('analysis/truncation-counterexample.json', {'previous_envelope': actual})
    assert actual['truncated'] and '__post_init__' not in actual['text']
