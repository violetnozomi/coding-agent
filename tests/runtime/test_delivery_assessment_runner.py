import asyncio
import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.verification.sidecar_verifier import ResolvedVerifierProvider, create_sidecar_verifier_hook


def run_probe(monkeypatch, tmp_path, *, reports, candidate='A candidate.', task='Explain the interface.', always=True, initial_actions=()):
    class Provider:
        name = 'offline'
        mains = 0
        reviews = 0
        seen = []
        def create_completion(self, _client, **kwargs):
            if any(t.get('function', {}).get('name') == 'emit_sidecar_verdict' for t in kwargs.get('tools', [])):
                self.reviews += 1
                payload, finish = reports(self.reviews, kwargs, environment)
                if isinstance(payload, Exception):
                    raise payload
                call = SimpleNamespace(function=SimpleNamespace(name='emit_sidecar_verdict', arguments=json.dumps(payload)))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]), finish_reason=finish)])
            self.mains += 1
            assert self.mains <= 6, 'probe exceeded expected review budget'
            self.seen.append(kwargs)
            if self.mains <= len(initial_actions):
                calls = [SimpleNamespace(id=f'probe-{self.mains}-{i}', type='function', function=SimpleNamespace(name=name, arguments=json.dumps(args))) for i, (name, args) in enumerate(initial_actions[self.mains-1])]
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=calls), finish_reason='tool_calls')], usage=None)
            return _completion(candidate if self.mains == len(initial_actions)+1 else 'A revised answer.')
    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = replace(_request(tmp_path), messages=({'role': 'user', 'content': task},))
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    hook = create_sidecar_verifier_hook(environment, ResolvedVerifierProvider(provider, object(), 'offline-model', 'offline', 'inherit-main'), env={'KODAX_VERIFIER_ALWAYS': '1'} if always else {})
    environment._configured_sidecar_verifier = hook
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        snapshot = SimpleNamespace(reviews=tuple(environment.verification_runtime.domain._reviews.values()), basis=environment.verification_runtime.domain.review_basis(), current=environment.verification_runtime.domain.snapshot())
        print('OBSERVED', result.status.value, hook.stats, 'mains', provider.mains, 'reviews', provider.reviews, 'facts', snapshot.reviews, 'basis', snapshot.basis, 'version', snapshot.current.version, 'ready', snapshot.current.ready_for_completion)
        return result, hook, provider, snapshot
    finally:
        environment.close()


def accepted(status='reviewed'):
    return {'verdict': 'accept', 'reason': 'Controlled judgment', 'delivery_assessment': {'status': status, 'issues': []}}


@pytest.mark.parametrize('candidate', ['tool --wrong', 'client.invoke(wrong=True)', 'mode: wrong', 'First choose the wrong entry.'])
def test_issue_reanimates_real_runner(monkeypatch, tmp_path, candidate):
    def reports(index, kwargs, env):
        if index == 1:
            assert not env.verification_runtime.domain._reviews
            assert 'bare material ID copied verbatim' in kwargs['messages'][0]['content']
            schema = kwargs['tools'][0]['function']['parameters']
            assert schema['properties']['delivery_assessment']['properties']['issues']['items']['properties']['evidence_reference']['description'] == 'Copy the displayed bare material ID verbatim.'
            reference = re.search(r'^user: (user-[0-9a-f]+)$', kwargs['messages'][1]['content'], re.MULTILINE).group(1)
            payload = accepted()
            payload['delivery_assessment']['issues'] = [{'candidate_excerpt': candidate, 'evidence_reference': reference, 'mismatch': 'The requested interface differs from this delivered text.'}]
            return payload, 'tool_calls'
        if index == 2:
            assert not env.verification_runtime.domain._reviews
            assert 'The requested interface differs' in kwargs['messages'][1]['content']
            return {'verdict': 'revise', 'reason': 'The requested interface differs from the delivery.'}, 'tool_calls'
        return accepted(), 'tool_calls'
    result, hook, provider, snapshot = run_probe(monkeypatch, tmp_path, reports=reports, candidate=candidate)
    assert result.status.value == 'completed'
    assert provider.mains == 2 and provider.reviews == 3
    assert hook.stats['verdict_counts'] == {'accept': 1, 'revise': 1, 'blocked': 0, 'unavailable': 0}
    assert len(snapshot.reviews) == 2
    assert not snapshot.reviews[0].accepted and snapshot.reviews[-1].accepted
    # 模型投影可能省略内部戳；真实主请求仍须包含补救指导。
    assert 'The requested interface differs' in str(provider.seen[1]['messages'])


@pytest.mark.parametrize('reply,expected', [
    ({'1': 'The help and tests confirm the gc interface.'}, 'historical'),
    ({'1': 'The implementation and help agree.', '2': 'The example agrees with the evidence.'}, 'completed'),
    ({}, 'completed_unverified'),
    ({'1': 'The implementation and help agree.'}, 'completed_unverified'),
    ({'1': 'Agrees.', '9': 'Other.'}, 'completed_unverified'),
    ({'1': 'Agrees.', '2': '   '}, 'completed_unverified'),
])
def test_gc_review_clarification_stays_inside_sidecar(monkeypatch, tmp_path, reply, expected):
    candidate = 'python3 -m vaultpack gc    ~/backups/vault'
    note = 'The README command reference lists gc with --dry-run/--json; implementation/tests support it, so no functional defect—recorded as note only.'
    original_only = expected == 'historical'
    if original_only:
        expected = 'completed'
    def reports(index, kwargs, environment):
        report = accepted()
        message = kwargs['messages'][1]['content']
        assert not environment.verification_runtime.domain._reviews
        if index == 1:
            ref = re.search(r'^user: (user-[0-9a-f]+)$', message, re.MULTILINE).group(1)
            report['delivery_assessment']['issues'] = [
                dict(candidate_excerpt=candidate, evidence_reference=ref, mismatch=mismatch)
                for mismatch in ((note,) if original_only else (note, 'Example is consistent.'))
            ]
        else:
            assert note in message and (original_only or 'Example is consistent.' in message)
            assert 'clarifications' in kwargs['tools'][0]['function']['parameters']['properties']
            report['clarifications'] = reply
        return report, 'tool_calls'
    result, hook, provider, snapshot = run_probe(monkeypatch, tmp_path, reports=reports, candidate=candidate)
    assert result.status.value == expected
    assert (provider.mains, provider.reviews) == (1, 2)
    assert [item.accepted for item in snapshot.reviews] == ([True] if expected == 'completed' else [])
    assert bool(hook._accepted_evidence_key) == (expected == 'completed')


@pytest.mark.parametrize('kind', ['unknown', 'missing', 'provider', 'blocked', 'revise'])
def test_negative_or_unavailable_runner(monkeypatch, tmp_path, kind):
    def reports(index, kwargs, env):
        if kind == 'blocked':
            return {'verdict': 'blocked', 'reason': 'Concrete blocker', 'delivery_assessment': None}, 'length'
        if kind == 'revise':
            return {'verdict': 'revise', 'reason': 'Concrete correction', 'delivery_assessment': None}, 'length'
        if kind == 'provider':
            return RuntimeError('offline failure'), 'stop'
        if kind == 'missing':
            return {'verdict': 'accept', 'reason': 'missing assessment'}, 'stop'
        return accepted('unknown'), 'stop'
    result, hook, provider, snapshot = run_probe(monkeypatch, tmp_path, reports=reports)
    expected = 'blocked' if kind in ('blocked', 'revise') else 'completed_unverified'
    assert result.status.value == expected
    if kind in ('blocked', 'revise'):
        assert result.metadata['raw_status'] == 'blocked'
        assert '交付评审未通过' in result.final_text
    else:
        assert '交付评审尚未确认' in result.final_text
    assert bool(snapshot.reviews) == (kind in ('blocked', 'revise'))
    assert all(not item.accepted for item in snapshot.reviews)
    assert hook._accepted_evidence_key is None
    if kind == 'revise':
        assert provider.mains == 3 and provider.reviews == 3


def test_unknown_review_finishes_without_empty_reanimation(monkeypatch, tmp_path):
    from tests.runtime.test_requirement_scope_runtime import _small
    initial = _small(tmp_path) + [[('bash', {'command': 'python -m pytest -q tests'})]]
    result, hook, provider, snapshot = run_probe(monkeypatch, tmp_path,
        reports=lambda *args: (accepted('unknown'), 'stop'), initial_actions=initial,
        task='Fix api.py. Preserve compatibility. Run python -m pytest -q tests.')
    assert result.status.value == 'completed_unverified'
    # 每组工具返回后仍由主模型提出交付；不可用评审不产生空补救循环。
    assert provider.mains == len(initial) + 1 and provider.reviews == 1
    assert snapshot.current.candidate_fingerprint
    assert hook.stats['last_gate_reason'] == 'ordinary-delivery'
    assert not snapshot.reviews
    assert hook._accepted_evidence_key is None


def test_pure_explanation_not_applicable(monkeypatch, tmp_path):
    result, hook, provider, snapshot = run_probe(monkeypatch, tmp_path,
        reports=lambda *args: (accepted('not_applicable'), 'stop'))
    assert result.status.value == 'completed'
    assert provider.mains == provider.reviews == 1


@pytest.mark.parametrize('change_during_clarification', [False, True])
def test_source_change_during_review_invalidates_accept(monkeypatch, tmp_path, change_during_clarification):
    from tests.runtime.test_requirement_scope_runtime import _small
    initial = _small(tmp_path) + [[('bash', {'command': 'python -m pytest -q tests'})]]
    def reports(index, kwargs, environment):
        # 真实临时源码在评审调用尚未返回时改变；不伪造域投影或工具结果。
        report = accepted()
        if change_during_clarification and index == 1:
            ref = re.search(r'^user: (user-[0-9a-f]+)$', kwargs['messages'][1]['content'], re.MULTILINE).group(1)
            report['delivery_assessment']['issues'] = [
                dict(candidate_excerpt='A candidate.', evidence_reference=ref, mismatch='Check the interface.')]
        else:
            (tmp_path / 'api.py').write_text('value = 3\n')
            if change_during_clarification and index == 2:
                report['clarifications'] = {'1': 'Source and interface agreed when checked.'}
        return report, 'tool_calls'
    result, hook, provider, snapshot = run_probe(monkeypatch, tmp_path,
        reports=reports, initial_actions=initial,
        task='Fix api.py. Preserve compatibility. Run python -m pytest -q tests.')
    assert provider.reviews >= (2 if change_during_clarification else 1)
    assert snapshot.current.version.generation == 2
    # 第一代材料的 accept 不得提交；之后重新执行和评审可以合法完成。
    assert all(item.version.generation == snapshot.current.version.generation for item in snapshot.reviews)
    assert result.status.value != 'completed' or snapshot.current.ready_for_completion





def test_user_revision_skips_review_of_already_rejected_candidate(monkeypatch, tmp_path):
    from nz_coder.runtime.verification.hooks import StopHookDecision

    class Provider:
        name = 'offline'
        main_count = 0
        review_messages = []
        def create_completion(self, _client, **kwargs):
            if any(t.get('function', {}).get('name') == 'emit_sidecar_verdict' for t in kwargs.get('tools', [])):
                self.review_messages.append(kwargs['messages'][1]['content'])
                call = SimpleNamespace(function=SimpleNamespace(name='emit_sidecar_verdict', arguments=json.dumps(accepted())))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]), finish_reason='tool_calls')])
            self.main_count += 1
            return _completion('First candidate.' if self.main_count == 1 else 'Second candidate.')

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request, options = _request(tmp_path), RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    hook = create_sidecar_verifier_hook(environment, ResolvedVerifierProvider(provider, object(), 'offline-model', 'offline', 'inherit-main'), env={'KODAX_VERIFIER_ALWAYS': '1'})
    environment._configured_sidecar_verifier = hook
    candidates = []
    def terminal_rule(context):
        candidates.append(context.last_assistant_text)
        if len(candidates) == 1:
            return StopHookDecision(action='reanimate', message='Provide a revised answer.')
        return StopHookDecision()
    environment.hooks.register_stop_hook(terminal_rule)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert result.status.value == 'completed'
    assert candidates == ['First candidate.', 'Second candidate.']
    assert provider.main_count == 2
    assert len(provider.review_messages) == 1
    assert 'Second candidate.' in provider.review_messages[0]
    assert hook.stats['verdict_counts']['accept'] == 1


def test_next_model_request_distinguishes_verified_and_unattributed_commands(monkeypatch, tmp_path):
    (tmp_path / 'test_one.py').write_text(
        'import unittest\nclass Sample(unittest.TestCase):\n def test_one(self): self.assertTrue(True)\n')
    (tmp_path / 'smoke.sh').write_text('exit 0\n')
    suite = 'python -m unittest discover'
    result, _, provider, _ = run_probe(monkeypatch, tmp_path, reports=lambda *_: (accepted(), 'tool_calls'),
        task='Check this project and explain the results.', initial_actions=[
            [('update_scratchpad', {'section': 'focus', 'entries': [
                {'summary': 'Suite still blocking; check same-second ordering.', 'details': 'Check fresh receipts.'}]})],
            [('bash', {'command': suite})],
            [('bash', {'command': 'ls && ' + suite + ' 2>&1 | tail -4; bash smoke.sh'})],
            [('bash', {'command': 'bash smoke.sh'})],
        ])
    current = next(message['content'] for message in provider.seen[-1]['messages']
                   if '<current-task-facts>' in str(message.get('content')))
    assert 'Verified check' in current and suite in current
    assert 'individual checks unproven' in current
    assert 'no matching check decision' in current and 'bash smoke.sh' in current
    assert 'check same-second ordering' in current
    assert result.status.value == 'completed'
