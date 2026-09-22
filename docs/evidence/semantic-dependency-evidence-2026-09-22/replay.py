"""Offline fixture validation and packet audits. No model entry point."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
HISTORY = ROOT / 'docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder'
SUITE = ROOT / 'tests/evaluation/fixtures/agent_core_diagnostic_v1'
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(path.read_text())


def save(name, value):
    (OUT / name).write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')


def main():
    loader = importlib.util.spec_from_file_location('validator', SUITE / 'validate.py')
    validator = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(validator)
    with tempfile.TemporaryDirectory(prefix='dependency-frozen-c-') as td:
        workspace = Path(td) / 'workspace'
        hashes = validator.snapshot(HISTORY / 'final-files', workspace)
        assert hashes == read(HISTORY / 'final-hashes.json')
        public = validator.project_tests('C_long_horizon', workspace)
        acceptance = validator.acceptance('C_long_horizon', workspace)
        assert public['exit'] == 0 and '46 passed' in public['stdout']
        assert acceptance['passed'] == 11 and acceptance['total'] == 12
        assert [c['name'] for c in acceptance['checks'] if not c['passed']] == ['invalid_write_preserves_destination']
        assert validator.hashes(workspace) == hashes
        save('baseline/c-frozen-state.json', {'hashes': hashes, 'project_tests': public, 'acceptance': acceptance,
                                             'business_source_changed': False})
    old = read(OUT / 'red/c-sidecar-context.json')
    new = read(OUT / 'after/c-sidecar-context.json')
    for key, value in old['context'].items():
        assert value == new['context'][key], key
    assert 'def save(path, config):' not in old['packet']
    assert 'def save(path, config):' in new['packet']
    assert 'invalid_write_preserves_destination' not in new['packet']
    assert '11/12' not in new['packet']
    assert 'encoded = dumps(config)' in new['context']['supporting_repository_evidence']
    assert 'write_text(encoded)' in new['context']['supporting_repository_evidence']
    scope = read(OUT / 'after/c-repo-scope.json')['scope']
    selection = read(OUT / 'after/evidence-selection.json')
    item = next(i for i in selection['items'] if i['path'] == 'configkit/store.py' and i['symbol'] == 'save')
    assert item['source_hash'] == hashlib.sha256((HISTORY / 'final-files/configkit/store.py').read_bytes()).hexdigest()
    save('analysis/c-dependency-chain.json', {
        'changed_source': ['configkit/config/writer.py'], 'changed_symbols': scope['changed_symbols'],
        'repo_direct_callers': scope['direct_callers'], 'repo_impacted_callers': scope['impacted_callers'],
        'selected_supporting_evidence': selection['items'], 'store_save_selected': True,
        'selection_reason': item['reason'], 'source_current_hash': item['source_hash'],
        'hidden_evaluator_data_used': False,
    })
    save('analysis/packet-audit.json', {'all_preexisting_context_fields_identical': True,
         'full_original_spec': True, 'writer_diff_unchanged': True, 'store_labeled_unchanged': True,
         'verification_fact_unchanged': True, 'evaluator_truth_not_in_packet': True,
         'supporting_chars': len(new['context']['supporting_repository_evidence']),
         'independent_budget': 4000, 'provider_requests': 0})
    # JSON preserves exact packet bytes without unified-diff trailing-space warnings.
    frozen = read(ROOT / 'docs/evidence/c-sidecar-fixed-diff-counterfactual-2026-09-22/packets/packet-fixed.json')
    save('baseline/historical-frozen-packet.json', frozen)
    save('baseline/c-changed-paths.json', scope['changed_files'])
    print('Frozen C: 46 passed; 11/12 as expected. Current hook packet delta: only supporting channel. No Provider.')


if __name__ == '__main__':
    main()
