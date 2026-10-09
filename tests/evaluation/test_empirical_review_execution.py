"""本轮经验准入走原账本与真实生产审查边界，不产生远程模型请求。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import threading
import time

import pytest

from nz_coder.evaluation.model_relay import AdmissionDenied, InputAccounting, RelayBinding, RelayLedger, RelayLimits


ROOT = Path(__file__).resolve().parents[2]


def grant(contract="reference-fixture"):
    return {"purpose": "review-effects-formal", "account": "local-fixture", "authorization_text": "offline fixture only",
            "input_mode": "empirical", "empirical_risk_accepted": True, "cost_currency": "CNY", "cost_limit": 10,
            "contract_id": contract, "model": "deepseek-v4-flash", "scope": ["u01", "u02", "u03", "u04", "a01"],
            "valid_from": time.time()-5, "valid_until": time.time()+1800,
            "prices_cny_per_million": {"input": 2, "cached_input": 0.04, "output": 8}}


def request():
    payload = {"model": "deepseek-v4-flash", "max_tokens": 1024, "messages": [{"role": "user", "content": "test"}]}
    raw = json.dumps(payload).encode()
    accounting = InputAccounting(None, "official-reference-fixture", contract_id="reference-fixture",
                                 payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=100)
    binding = RelayBinding("review-effects-formal", "u01", "u01", "auxiliary", 2, payload["model"], 1024, time.monotonic()+5)
    return binding, raw, payload, accounting


def test_strict_still_rejects_reference_count_without_a_bound(tmp_path):
    ledger = RelayLedger(tmp_path/'ledger', RelayLimits())
    binding, raw, payload, accounting = request()
    try:
        with pytest.raises(AdmissionDenied, match="input_token_bound_unknown"):
            ledger.reserve(binding, 'one', raw, payload, accounting)
        assert not ledger.attempts
    finally:
        ledger.close()


@pytest.mark.parametrize("field,value", [("empirical_risk_accepted", False), ("cost_limit", None),
                                       ("cost_currency", None), ("account", ""), ("valid_until", 0)])
def test_empirical_requires_explicit_risk_and_complete_fee_grant(tmp_path, field, value):
    authorization = grant()
    authorization[field] = value
    with pytest.raises(ValueError, match="empirical_authorization"):
        RelayLedger(tmp_path/'ledger', RelayLimits(), empirical_authorization=authorization)


def test_empirical_preserves_unknown_grade_hash_and_estimated_cost(tmp_path):
    authorization = grant()
    ledger = RelayLedger(tmp_path/'ledger', RelayLimits(), empirical_authorization=authorization)
    binding, raw, payload, accounting = request()
    try:
        attempt = ledger.reserve(binding, 'one', raw, payload, accounting)
        row = ledger.attempts[attempt]
        assert row['input_admission_mode'] == 'empirical' and row['evidence_level'] == 'unknown'
        assert row['input_exact'] is False and accounting.trusted is False and accounting.upper_bound is None
        assert row['payload_sha256'] == hashlib.sha256(raw).hexdigest()
        assert row['estimated_cost_reserved_cny'] == pytest.approx((100*2+1024*8)/1_000_000)
        ledger.settle(attempt, status='complete', finish_reason='stop', usage={'prompt_tokens':100, 'completion_tokens':10})
        assert ledger.cost_occupied_cny == pytest.approx((100*2+10*8)/1_000_000)
        assert not ledger.failed_contracts
    finally:
        ledger.close()


@pytest.mark.parametrize('variant', ['unknown_count', 'changed_hash', 'changed_contract', 'changed_model', 'changed_unit', 'expired', 'fee_exhausted'])
def test_empirical_cannot_widen_scope_or_skip_a_count(tmp_path, variant):
    authorization = grant()
    if variant == 'fee_exhausted':
        authorization['cost_limit'] = 0.000001
    ledger = RelayLedger(tmp_path/'ledger', RelayLimits(), empirical_authorization=authorization)
    binding, raw, payload, accounting = request()
    if variant == 'unknown_count':
        accounting = InputAccounting(None, 'unsupported', contract_id='reference-fixture', payload_sha256=hashlib.sha256(raw).hexdigest())
    elif variant == 'changed_hash':
        accounting = InputAccounting(None, 'reference', contract_id='reference-fixture', payload_sha256='wrong', reference_count=100)
    elif variant == 'changed_contract':
        accounting = InputAccounting(None, 'reference', contract_id='other', payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=100)
    elif variant == 'changed_model':
        payload['model'] = 'other'
    elif variant == 'changed_unit':
        binding = RelayBinding(binding.experiment, 'other', 'other', binding.role, 2, binding.model, 1024, binding.deadline)
    elif variant == 'expired':
        ledger.empirical_authorization['valid_until'] = 0
    try:
        with pytest.raises(AdmissionDenied):
            ledger.reserve(binding, 'one', raw, payload, accounting)
        assert not ledger.attempts
    finally:
        ledger.close()


@pytest.mark.parametrize('usage', [None, {'prompt_tokens': -1, 'completion_tokens': 1}, {'prompt_tokens': 101, 'completion_tokens': 1}])
def test_empirical_unreliable_or_over_reserved_usage_stops_other_units(tmp_path, usage):
    ledger = RelayLedger(tmp_path/'ledger', RelayLimits(), empirical_authorization=grant())
    binding, raw, payload, accounting = request()
    try:
        attempt = ledger.reserve(binding, 'one', raw, payload, accounting)
        ledger.settle(attempt, status='complete', finish_reason='stop', usage=usage)
        next_binding = RelayBinding(binding.experiment, 'a01', 'a01', 'main', 12, binding.model, 1024, binding.deadline)
        with pytest.raises(AdmissionDenied, match='experiment_accounting_contract_failed'):
            ledger.reserve(next_binding, 'two', raw, payload, accounting)
        assert len(ledger.attempts) == 1
    finally:
        ledger.close()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_empirical_gate_is_separate_from_strict_and_fees(tmp_path, monkeypatch):
    entry = load(ROOT/'scripts/review_effects_execution.py', 'empirical_startup')
    monkeypatch.setattr(entry, 'isolation_status', lambda _p: True)
    contract = {**entry.local_contract_status(), 'service_scope':'hosted-deepseek', 'evidence_level':'unknown',
                'strict_input_bound_verified':False, 'contract_id':'reference-fixture'}
    class Counter:
        def status(self):
            return contract
    authorization = grant()
    authorization.update(manifest_sha256=entry.sha(entry.FROZEN/'manifest.json'),
                         counter_source_sha256=contract['counter_source_sha256'],
                         relay_source_sha256=contract['relay_source_sha256'],
                         startup_source_sha256=entry.sha(Path(entry.__file__)),
                         endpoint='https://api.deepseek.com/v1/chat/completions', bounds=vars(RelayLimits()))
    assert not entry.execution_status(tmp_path, Counter(), grant=authorization)['online_allowed']
    assert not entry.execution_status(tmp_path, Counter(), input_mode='empirical')['online_allowed']
    status = entry.execution_status(tmp_path, Counter(), grant=authorization, input_mode='empirical')
    assert status['online_allowed'] and status['empirical_admission_ready']
    assert status['technical_ready'] is False and status['input_contract_verified'] is False
    authorization['empirical_risk_accepted'] = False
    assert not entry.execution_status(tmp_path, Counter(), grant=authorization, input_mode='empirical')['online_allowed']


def test_official_reference_empirical_reaches_real_old_new_runner_judge(tmp_path):
    runtime, tokenizer = os.environ.get('NZ_REVIEW_RUNTIME_ROOT'), os.environ.get('NZ_DEEPSEEK_V41_TOKENIZER')
    if not runtime or not tokenizer:
        pytest.skip('frozen runtime / official tokenizer not selected; no downloads or remote model calls')
    from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter
    counter = DeepSeekV41Counter(Path(tokenizer))
    entry = load(ROOT/'scripts/review_effects_execution.py', 'empirical_runtime_startup')
    module = load(ROOT/'tests/evaluation/fixtures/review_execution_fake.py', 'empirical_upstream')
    fake = module.FormalFake(input_counter=counter)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    output = tmp_path/'formal'
    try:
        entry.run_formal(Path(runtime), output, counter=counter,
                         upstream=f'http://127.0.0.1:{fake.server_port}/v1/chat/completions',
                         input_mode='empirical', grant=grant(counter.contract_id), units={'u01','u02','u03','u04'})
    finally:
        fake.shutdown()
        fake.server_close()
        thread.join(2)
    report = json.loads((output/'formal-results.json').read_text())
    assert [(r['unit'],r['version'],r['requests']) for r in report['units']] == [
        ('u01','new',1),('u02','old',1),('u03','old',0),('u04','new',1)]
    assert len(fake.requests) == report['physical_attempts'] == 3 and report['remote_provider_requests'] == 0
    events = [json.loads(line) for line in (output/'ledger.jsonl').read_text().splitlines()]
    admitted = [e for e in events if e['event']=='admitted']
    assert all(e['input_admission_mode']=='empirical' and e['evidence_level']=='unknown' for e in admitted)
    assert not report['failed_contracts']
    for event in admitted:
        case = output/event['unit']
        raw = (case/'provider'/(event['attempt_id']+'.request.json')).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == event['payload_sha256']
        payload = json.loads(raw)
        assert payload in fake.requests and payload['max_tokens']==1024 and payload['stream'] is False
        assert 'passed-current-generation' in str(payload['messages']) and 'payment.py' in str(payload['messages'])
        response = json.loads((case/'provider'/(event['attempt_id']+'.response.txt')).read_text())
        assert response['usage']['prompt_tokens'] == counter(raw,payload).reference_count
        state = json.loads((case/'result/review.json').read_text())
        assert state['packet']['file_edit_summary'] and state['state_before_review']['mutation_generation'] > 0
