"""真实读取、pytest 和完成审查请求；仅替换全部外部模型边界。"""
from __future__ import annotations

import hashlib
import json

import pytest

from tests.runtime.test_requirement_scope_runtime import _run


@pytest.mark.parametrize('verdict', ['accept', 'revise', 'invalid'])
def test_actual_test_read_reaches_serialized_semantic_request(monkeypatch, tmp_path, verdict):
    source = 'def amount(value, allow_refund=False):\n    if value < 0:\n        raise ValueError("negative")\n    return value\n'
    tests = ('import pytest\nfrom payment import amount\n'
             'def test_explicit(): assert amount(-3, allow_refund=True) == -3\n'
             'def test_default():\n    with pytest.raises(ValueError): amount(-3)\n'
             'def test_positive(): assert amount(3) == 3\n')
    (tmp_path / 'payment.py').write_text(source)
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_amount.py').write_text(tests)
    result, state, trace, requests, reviews = _run(monkeypatch, tmp_path,
        'Fix payment.py: permit negative values only when allow_refund=True. '
        'Preserve default rejection and positive behavior. Run python -m pytest -q tests.',
        [[('read_file', {'path': 'payment.py'}), ('read_file', {'path': 'tests/test_amount.py'})],
         [('bash', {'command': 'python -m pytest -q tests'})],
         [('edit_file', {'path': 'payment.py', 'old_text': 'if value < 0:',
                         'new_text': 'if value < 0 and not allow_refund:'})],
         [('bash', {'command': 'python -m pytest -q tests'})],
         'Implemented and verified; finite test coverage.'],
        semantic=(verdict,) * 8, label='read-test-' + verdict)
    assert reviews
    # 序列化实际 Provider 边界的请求，不绕过 Hook 手拼 VerifierContext。
    wire = json.loads(json.dumps(reviews[0], ensure_ascii=False))
    text = '\n'.join(m.get('content', '') for m in wire['messages'])
    assert 'def test_explicit(): assert amount(-3, allow_refund=True) == -3' in text
    assert 'def test_positive(): assert amount(3) == 3' in text
    assert 'with pytest.raises(ValueError): amount(-3)' in text
    assert hashlib.sha256(tests.encode()).hexdigest() in text
    evidence_line = next(line for line in text.splitlines() if line.startswith('{"complete_file"'))
    evidence = json.loads(evidence_line)
    assert evidence['version'] == 'current'
    assert evidence['start_line'] == 1 and evidence['end_line'] == 6
    assert evidence['complete_file'] is True
    assert evidence['source'] == tests.rstrip('\n')
    assert [e['status'] for e in trace if e.get('event') == 'verification_result'] == ['failed', 'passed']
    assert '1 failed' in str(requests[2]['messages'])
    assert 'passed-current-generation' in text
    assert state['verification_contract']['passed'] is True
    assert state['verification_generation'] == state['mutation_generation'] == 1
    assert (result.status.value == 'completed') is (verdict == 'accept')
    assert [t['function']['name'] for t in wire['tools']] == ['emit_sidecar_verdict']
    tools = [e for e in trace if e.get('event') == 'tool_call' and e.get('executed')]
    assert [e['name'] for e in tools].count('read_file') == 2
    assert [e['name'] for e in tools].count('edit_file') == 1
    assert [e['name'] for e in tools].count('bash') == 2


def test_reading_test_source_does_not_satisfy_failed_verification(monkeypatch, tmp_path):
    (tmp_path / 'payment.py').write_text('def amount(value): return value + 1\n')
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests/test_amount.py').write_text(
        'from payment import amount\ndef test_positive(): assert amount(3) == 3\n')
    result, state, trace, _, _ = _run(monkeypatch, tmp_path,
        'Fix payment.py and run python -m pytest -q tests. Preserve amount(3) == 3.',
        [[('read_file', {'path': 'tests/test_amount.py'})],
         [('bash', {'command': 'python -m pytest -q tests'})], 'Done'],
        semantic=('accept',) * 8, label='read-test-still-failing')
    assert result.status.value != 'completed'
    assert state['verification_contract']['passed'] is not True
    assert all(e['status'] != 'passed' for e in trace if e.get('event') == 'verification_result')
    assert state['verification_generation'] == -1
