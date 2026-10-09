"""正式入口条件与真实隔离容器；所有模型边界仅使用回环假服务。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]


def startup():
    spec = importlib.util.spec_from_file_location('review_startup', ROOT/'scripts/review_effects_execution.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_condition_gate_separates_technical_fees_and_fake_model_scope(tmp_path, monkeypatch):
    entry = startup()
    monkeypatch.setattr(entry, 'isolation_status', lambda _p: True)
    status = entry.execution_status(tmp_path, entry.local_counter, local=True)
    assert status['technical_ready'] and not status['paid_authorization_valid']
    assert not status['online_allowed']
    remote = entry.execution_status(tmp_path, entry.local_counter)
    assert not remote['technical_ready'], 'local byte contract cannot be promoted to hosted API by a ready flag'
    contract = entry.local_contract_status()
    grant = {'purpose':'review-effects-formal', 'account':'offline-authorization-fixture', 'authorization_text':'fixture only',
             'cost_limit':1, 'manifest_sha256':entry.sha(entry.FROZEN/'manifest.json'), 'contract_id':contract['contract_id'],
             'counter_source_sha256':contract['counter_source_sha256'], 'model':'deepseek-v4-flash',
             'endpoint':'https://api.deepseek.com/v1/chat/completions', 'bounds':vars(entry.RelayLimits()),
             'valid_from':time.time()-5, 'valid_until':time.time()+5}
    assert entry.authorization_valid(grant, contract)
    assert entry.execution_status(tmp_path, entry.local_counter, grant=grant, local=True)['paid_authorization_valid']
    grant['counter_source_sha256'] = 'stale'
    assert not entry.authorization_valid(grant, contract)
    grant['counter_source_sha256'] = contract['counter_source_sha256']
    grant['valid_until'] = time.time()-1
    assert not entry.authorization_valid(grant, contract)


def test_launch_uses_host_selected_frozen_source(tmp_path):
    entry = startup()
    for version in ('old', 'new'):
        argv = entry.launch_argv(tmp_path, tmp_path/'u1', image='fixture', version=version)
        assert f'type=bind,source={tmp_path}/code-{version},target=/runtime,readonly' in argv
        assert '--network=none' in argv
    with pytest.raises(ValueError):
        entry.launch_argv(tmp_path, tmp_path/'u1', image='fixture', version='client-override')


def test_formal_local_entry_frozen_versions_and_review_boundaries(tmp_path):
    runtime = os.environ.get('NZ_REVIEW_RUNTIME_ROOT')
    if runtime is None:
        pytest.skip('frozen Docker runtime not selected: set NZ_REVIEW_RUNTIME_ROOT; no automatic build/download')
    output = tmp_path/'formal'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/review_effects_execution.py'), 'formal-local',
                             '--artifact-root', runtime, '--output-root', str(output)],
                            capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout+result.stderr
    report = json.loads((output/'formal-results.json').read_text())
    assert [(r['unit'],r['version']) for r in report['units']] == [
        ('u01','new'),('u02','old'),('u03','old'),('u04','new'),('a01','new')]
    fake_requests = json.loads(output.with_name(output.name+'-fake-requests.json').read_text())
    events = [json.loads(line) for line in (output/'ledger.jsonl').read_text().splitlines()]
    admitted = [e for e in events if e['event']=='admitted']
    assert len(admitted) == report['physical_attempts'] == len(fake_requests) <= 26
    assert report['remote_provider_requests'] == 0 and report['failed_contracts'] == {}
    entry = startup()
    assert [r['requests'] for r in report['units'][:4]] == [1,1,0,1]
    for row in report['units']:
        case = output/row['unit']
        info = json.loads((case/'container-inspect.json').read_text())
        assert next(m['Source'] for m in info['Mounts'] if m['Destination']=='/runtime') == str(Path(runtime)/('code-'+row['version']))
        assert row['source_commit'] == entry.VERSIONS[row['version']]
        driver = (case/'driver/entry.py').read_bytes()
        assert hashlib.sha256(driver).hexdigest() == report['driver_sha256']
        transcript = [json.loads(line) for line in (case/'result/runtime.jsonl').read_text().splitlines()]
        assert any(e.get('event')=='verification_result' and e.get('status')=='passed' for e in transcript)
        if row['unit']!='a01':
            assert row['preparation_requests']==3
            packet = json.loads((case/'result/review.json').read_text())
            assert packet['packet']['file_edit_summary'] and packet['state_before_review']['mutation_generation']>0
        else:
            assert row['preparation_requests']==0
            assert 'correlation_id' not in (case/'workspace/app/api.py').read_text(), 'offline probe does not preload an A patch'
    review = [r for r in fake_requests if [t['function']['name'] for t in r.get('tools',[])]==['emit_sidecar_verdict']]
    assert len(review)==3
    for request in review:
        assert request['max_tokens']==1024 and request['stream'] is False
        assert 'passed-current-generation' in str(request['messages'])
        assert not any(token in str(request['messages']) for token in ('candidate-01','candidate-02','independent-check.py'))
    assert report['units'][2]['review_stats']['last_trace']=='deterministic_compatibility_guard'
    assert any(e['event']=='local_rejection' for e in events), 'incomplete A must retain bounded budget admission'


def test_unknown_counter_rejects_online_before_key_lookup_or_any_execution(tmp_path, monkeypatch):
    entry = startup()
    monkeypatch.setattr(entry, 'isolation_status', lambda _p: True)
    monkeypatch.delenv('NZ_REVIEW_DEEPSEEK_API_KEY', raising=False)
    monkeypatch.setattr(entry, 'run_formal', lambda *_a, **_k: pytest.fail('unauthorized execution'))
    monkeypatch.setattr(sys, 'argv', ['entry', 'online', '--artifact-root', str(tmp_path)])
    with pytest.raises(SystemExit) as stopped:
        entry.main()
    assert stopped.value.code == 2


def test_ready_json_cannot_substitute_isolation_receipts(tmp_path, monkeypatch):
    entry = startup()
    monkeypatch.setattr(entry, 'runtime_identity', lambda _p: {})
    (tmp_path/'runtime.json').write_text('{}')
    (tmp_path/'isolation-verified.json').write_text(json.dumps({'isolation_verified':True,
        'runtime_sha256':entry.sha(tmp_path/'runtime.json'), 'receipts':{}}))
    assert entry.isolation_status(tmp_path) is False


def test_complete_condition_branch_dispatches_only_after_bound_authorization(tmp_path, monkeypatch):
    entry = startup()
    # 只检查费用/技术门分支，不把这个契约替身当作DeepSeek计数证明。
    contract = {**entry.local_contract_status(), 'service_scope':'hosted-deepseek', 'contract_id':'conditional-branch-only'}
    class Counter:
        def __init__(self, _tokenizer):
            pass
        def status(self):
            return contract
    monkeypatch.setattr('nz_coder.evaluation.deepseek_counting.DeepSeekV41Counter', Counter)
    monkeypatch.setattr(entry, 'isolation_status', lambda _p: True)
    grant = {'purpose':'review-effects-formal', 'account':'offline-fixture', 'authorization_text':'test branch only',
             'cost_limit':1, 'manifest_sha256':entry.sha(entry.FROZEN/'manifest.json'), 'contract_id':contract['contract_id'],
             'counter_source_sha256':contract['counter_source_sha256'], 'model':'deepseek-v4-flash',
             'endpoint':'https://api.deepseek.com/v1/chat/completions', 'bounds':vars(entry.RelayLimits()),
             'valid_from':time.time()-5, 'valid_until':time.time()+5}
    path = tmp_path/'grant.json'
    path.write_text(json.dumps(grant))
    calls = []
    monkeypatch.setattr(entry, 'run_formal', lambda *args, **fields: calls.append((args, fields)))
    monkeypatch.setenv('NZ_REVIEW_DEEPSEEK_API_KEY', 'offline-test-value')
    monkeypatch.setattr(sys, 'argv', ['entry','online','--artifact-root',str(tmp_path),
                                    '--tokenizer','conditional-fixture','--authorization',str(path)])
    entry.main()
    assert len(calls)==1 and calls[0][1]['online'] is True
    assert calls[0][1]['upstream']=='https://api.deepseek.com/v1/chat/completions'
