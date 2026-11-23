"""终止规则、权威终稿和SDK宿主契约的跨模块行为。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from nz_coder.runtime.adapters.runner import _PolicyService
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.execution.runner import AgentRunner, _typed_result
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.execution.work_budget import WorkBudgetController
from nz_coder.runtime.verification.hooks import AgentHooks, StopHookDecision
from nz_coder.sdk import AgentClient


@pytest.mark.parametrize('action, expected_status, expected_reason', [
    ('abort', 'blocked', 'terminal_rules_stopped'),
    ('complete', 'completed_unverified', 'ordinary_verification_unconfirmed'),
])
def test_pending_final_rules_execute_without_semantic_review(monkeypatch, tmp_path, action, expected_status, expected_reason):
    monkeypatch.setattr("nz_coder.runtime.execution.runner.strict_local_tools", lambda: True)
    calls = []
    def reject(context):
        calls.append(context.last_assistant_text)
        return StopHookDecision(action=action, message='User rule')
    hooks = AgentHooks(stop_hooks=[reject])
    snapshot = {'verification_needed': True, 'verification_state': 'verifying',
                'verification_unavailable': {'reason': 'not executable'}}
    host = SimpleNamespace(hooks=hooks, runtime_state=RuntimeState())
    async def semantic(*args):
        raise AssertionError('must not start semantic review')
    async def rules(messages, status, content):
        return await _PolicyService(host, None).check_terminal_rules(messages, status, content)
    async def checkpoint(*args): pass
    context = SimpleNamespace(
        runtime_state=host.runtime_state,
        policy=SimpleNamespace(verification_status=lambda: snapshot, verify_completion=semantic,
                               check_terminal_rules=rules),
        hooks=SimpleNamespace(trace=lambda *args, **kwargs: None),
        control=SimpleNamespace(persist_runtime_state=lambda **kwargs: None,
                                stop_hook_reason=lambda: hooks.stop_hook_reason),
    )
    decision = asyncio.run(AgentRunner()._settle_strict_terminal_boundary(
        context, SimpleNamespace(session_runtime=SimpleNamespace(checkpoint=checkpoint)),
        SimpleNamespace(request=SimpleNamespace(workspace=tmp_path), interaction_run_id='fixture-run', verification=None, check_cancelled=lambda: None),
        [{'role': 'user', 'content': 'test'}, {'role': 'assistant', 'content': 'draft'}],
        boundary='natural_completion', content='draft', completed_turns=1,
        work_budget=WorkBudgetController(max_turns=20, nominal_turns=20),
        resolve_tool_runtime_context=lambda: None, natural_completion=True,
    ))
    assert calls == ['draft']
    assert decision.action == 'finalize'
    assert decision.status == expected_status
    assert decision.reason == expected_reason


@pytest.mark.parametrize('stream', [False, True])
def test_unverified_final_is_authoritative_and_preserves_authored_draft(stream):
    from tests.runtime.test_run_result_text import _lifecycle_context
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    draft = 'OLD_SUCCESS_CLAIM: all checks are done.'
    events, tokens = [], []
    context = _lifecycle_context(lambda name, payload: events.append((name, payload)))
    context._scenario.command_result({"command": "pytest tests | tail -5"}, "1 passed", False, False, exit_code=0)
    context.verification_runtime.run_context.metadata['authored_content'] = draft
    context.output_schema_requested = lambda: False
    messages = [{'role': 'user', 'content': 'test'}, {'role': 'assistant', 'content': draft, '_nz_finish': 'stop'}]
    result = ProductionRunLifecycle().finalize_sync(context, messages, 'completed_unverified',
        stream=stream, on_token=tokens.append, content_text=draft)
    assert draft in result['content']
    assert "未经独立确认" in result['content']
    assert result['authored_content'] == draft
    assert '验证' in result['content'] and 'pytest tests | tail -5' in result['content']
    assert messages[-1]['content'] == result['content']
    assert messages[-1]['_nz_parts'][-1]['text'] == result['content']
    if stream:
        assert tokens == [result['content'], None]


@pytest.mark.parametrize('status', list(RunStatus))
def test_factory_uses_typed_status_and_current_content(tmp_path, status):
    request = RunRequest(agent=AgentDefinition(name='worker', instructions='test'), profile=MAIN_PROFILE,
        messages=({'role': 'user', 'content': 'old'}, {'role': 'assistant', 'content': 'PREVIOUS_SUCCESS'},
                  {'role': 'user', 'content': 'current'}), workspace=tmp_path, session_id='factory-interface')
    expected = '' if status is RunStatus.CANCELLED else 'CURRENT_RESULT'
    class Host:
        closed = False
        async def run(self, messages, **kwargs):
            return {'status': status.value, 'content': expected,
                    'authored_content': 'DRAFT', 'verification': {'verification_needed': True}}
        def close(self): self.closed = True
    host = Host()
    result = asyncio.run(AgentClient(agent_factory=lambda _: host).run(request))
    assert result.status is status
    assert result.final_text == expected
    assert result.metadata['authored_content'] == 'DRAFT'
    assert result.metadata['verification'] == {'verification_needed': True}
    assert host.closed


def test_factory_requires_explicit_content(tmp_path):
    request = RunRequest(agent=AgentDefinition(name='worker', instructions='test'), profile=MAIN_PROFILE,
        messages=({'role': 'user', 'content': 'current'},), workspace=tmp_path, session_id='factory-contract')
    class Host:
        async def run(self, messages, **kwargs):
            messages.append({'role': 'assistant', 'content': 'guessed'})
            return {'status': 'completed'}
        def close(self): pass
    with pytest.raises(ValueError, match='content'):
        asyncio.run(AgentClient(agent_factory=lambda _: Host()).run(request))


def test_native_final_preserves_authored_audit_metadata(tmp_path):
    from nz_coder.runtime.core.run_context import RunContext
    from nz_coder.runtime.session.model import Session
    request = RunRequest(agent=AgentDefinition(name='worker', instructions='test'), profile=MAIN_PROFILE,
        messages=({'role': 'user', 'content': 'current'},), workspace=tmp_path, session_id='native-audit')
    context = RunContext(request, Session.create(request.session_id, list(request.messages), workspace=tmp_path), 'worker')
    raw = {'status': 'completed_unverified', 'content': 'authoritative', 'authored_content': 'original draft',
           'verification': {'verification_needed': True}}
    from nz_coder.runtime.core.result import freeze_terminal_result
    context.freeze_execution(RunStatus.COMPLETED_UNVERIFIED)
    freeze_terminal_result(context, raw)
    result = _typed_result(request, context, raw)
    assert result.metadata['authored_content'] == 'original draft'
    assert result.final_text == 'authoritative'


def test_new_activation_does_not_persist_previous_authored_draft_on_error(tmp_path):
    from nz_coder.runtime.session.model import Session, SessionStatus
    from nz_coder.runtime.session.runtime import SessionRuntime
    from tests.runtime.session.test_session_runtime import MemorySessionStore, _request

    previous = Session.create('s1', [{'role': 'user', 'content': 'old'}], workspace=tmp_path,
                              metadata={'authored_content': 'PREVIOUS_DRAFT'})
    previous.finish(SessionStatus.COMPLETED)
    store = MemorySessionStore(previous)
    runtime = SessionRuntime(store)

    async def scenario():
        context = await runtime.open(_request(tmp_path, [{'role': 'user', 'content': 'new'}]))
        await runtime.checkpoint(context)
        await runtime.finalize(context, RunStatus.ERROR)

    asyncio.run(scenario())
    assert len(store.saved) == 2
    assert all('authored_content' not in saved.metadata for saved in store.saved)


def test_native_terminal_rules_run_once_per_candidate_after_reanimation(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request

    class Provider:
        name = 'offline'
        calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            return _completion(f'draft-{self.calls}')

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request, options = _request(tmp_path), RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    candidates = []

    def check(context):
        candidates.append(context.last_assistant_text)
        return StopHookDecision(action='reanimate', message='revise') if len(candidates) == 1 else None

    environment.hooks.register_stop_hook(check)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert candidates == ['draft-1', 'draft-2']
    assert provider.calls == 2
    assert result.status is RunStatus.COMPLETED_UNVERIFIED
    assert 'draft-2' not in result.final_text and '/message' in result.final_text
    assert result.metadata['authored_content'] == 'draft-2'


def test_native_terminal_rule_downgrade_reaches_frozen_result(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request

    draft = 'All checks passed; finished.'

    class Provider:
        name = 'offline'
        calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            return _completion(draft)

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request, options = _request(tmp_path), RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    candidates = []

    def downgrade(context):
        candidates.append(context.last_assistant_text)
        return StopHookDecision(action='complete_unverified')

    environment.hooks.register_stop_hook(downgrade)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert candidates == [draft]
    assert provider.calls == 1
    assert result.status is RunStatus.COMPLETED_UNVERIFIED
    assert draft not in result.final_text
    assert "没有已采信的测试结果" in result.final_text
    assert result.metadata['authored_content'] == draft


@pytest.mark.parametrize('configured', [False, True])
def test_run_control_keeps_review_hooks_out_of_final_rules(monkeypatch, tmp_path, configured):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request

    class Provider:
        name = 'offline'

        def create_completion(self, _client, **kwargs):
            return _completion('done')

    _install_runtime(monkeypatch, Provider())
    request, options = _request(tmp_path), RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    observed = []

    def sidecar(context):
        observed.append('sidecar')

    def review(context):
        observed.append('review')

    def rule(context):
        observed.append('rule')

    hooks = AgentHooks(completion_review_hooks=[review], stop_hooks=[rule])
    environment._configured_sidecar_verifier = sidecar
    environment._configured_hooks = hooks if configured else None
    environment.hooks = hooks
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert result.status is RunStatus.COMPLETED_UNVERIFIED
    assert observed == ['rule', 'sidecar', 'review']
    assert hooks.completion_review_hooks == [review]


def test_sync_review_downgrade_still_runs_final_rules():
    hooks = AgentHooks(
        completion_review_hooks=[lambda _: StopHookDecision(action='complete_unverified')],
        stop_hooks=[lambda _: StopHookDecision(action='abort')],
    )
    assert hooks.handle_no_tool_response(SimpleNamespace(), [], message='draft') == 'blocked'


def test_reviewed_candidate_survives_stopped_terminal_summary(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request

    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            return _completion('CURRENT DRAFT')
    _install_runtime(monkeypatch, Provider())
    request, options = _request(tmp_path), RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    seen = []
    def review(context):
        seen.append(context.last_assistant_text)
        return StopHookDecision(action='abort', message='PUBLIC STOP REASON')
    environment.hooks.register_stop_hook(review)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert seen == ['CURRENT DRAFT']
    assert 'PUBLIC STOP REASON' in result.final_text and 'CURRENT DRAFT' not in result.final_text
    assert result.metadata['authored_content'] == 'CURRENT DRAFT'
    from nz_coder.runtime.agent.child_contracts import evaluate_child_verification
    verdict = evaluate_child_verification({'min_final_text_chars': 15},
        state={}, final_text=result.metadata['authored_content'])
    assert verdict['ok'] is False
    assert len(result.final_text) >= 15
    assert result.status not in {RunStatus.COMPLETED, RunStatus.COMPLETED_UNVERIFIED}


def test_authored_candidate_uses_guardrail_approved_content(monkeypatch, tmp_path):
    from dataclasses import replace
    from nz_coder.runtime.agent.guardrails import OutputGuardrail
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            return _completion('SECRET')
    async def rewrite(message, _context):
        return {'action': 'rewrite', 'payload': {'role': 'assistant', 'content': 'SAFE'}}
    _install_runtime(monkeypatch, Provider())
    original = _request(tmp_path)
    request = replace(original, agent=replace(original.agent,
        guardrails=(OutputGuardrail('redact', rewrite),)))
    options = RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    seen = []
    def review(context):
        seen.append(context.last_assistant_text)
    environment.hooks.register_stop_hook(review)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert seen == ['SAFE']
    assert result.metadata['authored_content'] == 'SAFE'
    assert 'SECRET' not in repr(result.metadata)


def test_authored_candidate_expires_before_tool_turn(monkeypatch, tmp_path):
    from dataclasses import replace
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request
    class Provider:
        name = 'offline'
        calls = 0
        def create_completion(self, _client, **kwargs):
            self.calls += 1
            return _completion('D1') if self.calls == 1 else _completion(tool='list_directory')
    _install_runtime(monkeypatch, Provider())
    request = replace(_request(tmp_path), metadata={'permission_mode': 'auto',
        'persist_session': False, 'max_turns': 2})
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    environment.hooks.register_stop_hook(lambda context: StopHookDecision(
        action='reanimate', message='Revise D1'))
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert result.status is RunStatus.MAX_TURNS
    assert result.metadata['authored_content'] == ''


def test_new_user_after_completed_continuation_replaces_command_authority():
    from nz_coder.runtime.core.execution_context import command_activation
    messages = [
        {'role': 'user', 'content': 'old task'},
        {'role': 'assistant', 'content': 'stopped', '_nz_continuation': {
            'status': 'max_turns', 'summary': '## Latest User Instruction\nold task'}},
        {'role': 'user', 'content': 'continue'},
        {'role': 'assistant', 'content': 'done'},
        {'role': 'user', 'content': 'audit renderer'},
    ]
    activation = command_activation(messages)
    assert activation.task_text == 'audit renderer'
    assert not activation.pure_continuation
    assert activation.current_user_message['content'] == 'audit renderer'
    messages.append({'role': 'assistant', 'content': 'done again'})
    consumed = command_activation(messages)
    assert consumed.current_user_message is None
    assert not consumed.resume_activation


@pytest.mark.parametrize('blocked_reason', ['', 'permission stopped'])
def test_runtime_verification_mutation_invalidates_current_candidate(monkeypatch, tmp_path, blocked_reason):
    source = tmp_path / 'source.py'
    source.write_text('before')
    state = RuntimeState()
    reviewed = []
    async def status_review(*args):
        reviewed.append(args)
        raise AssertionError('stale candidate was sent for review')
    context = SimpleNamespace(runtime_state=state,
        policy=SimpleNamespace(verification_status=lambda: {}, verify_completion=status_review),
        hooks=SimpleNamespace(trace=lambda *args, **kwargs: None))
    run_context = SimpleNamespace(metadata={'authored_content': 'D1'})
    runner = AgentRunner()
    async def contract(*args):
        return SimpleNamespace(blocked_reason='')
    async def mutate(*args):
        source.write_text('after')
        state.source_mutation_generation += 1
        return SimpleNamespace(blocked_reason=blocked_reason)
    monkeypatch.setattr(runner, '_execute_due_verification_contract', contract)
    monkeypatch.setattr(runner, '_execute_completion_verifications', mutate)
    decision = asyncio.run(runner._settle_strict_terminal_boundary(
        context, SimpleNamespace(), run_context, [{'role': 'user', 'content': 'test'}],
        boundary='natural_completion', content='D1', completed_turns=1,
        work_budget=WorkBudgetController(max_turns=20, nominal_turns=20),
        resolve_tool_runtime_context=lambda: None, natural_completion=True))
    assert source.read_text() == 'after'
    assert decision.action == ('finalize' if blocked_reason else 'continue')
    assert run_context.metadata['authored_content'] == ''
    assert reviewed == []


@pytest.mark.parametrize('mutate,cancel_after', [(False, False), (True, False), (True, True)])
def test_real_completion_verification_tracks_candidate_across_workspace_change(
    monkeypatch, tmp_path, mutate, cancel_after,
):
    import sys
    from dataclasses import replace
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.verification.verification_scheduler import VerificationAction, VerificationScheduler
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request

    source = tmp_path / 'source.py'
    source.write_text('VALUE = 1\n')
    body = ('from pathlib import Path\nimport unittest\n'
            'class TestCheck(unittest.TestCase):\n def test_check(self):\n')
    body += ("  Path('source.py').write_text('VALUE = 2\\n')\n" if mutate else
             "  self.assertIn('VALUE', Path('source.py').read_text())\n")
    (tmp_path / 'test_check.py').write_text(body)
    requests, reviews, selected = [], [], []

    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            current = environment.active_run_context
            requests.append((current.metadata.get('authored_content'),
                             current.verification.snapshot().version))
            return _completion('D1' if len(requests) == 1 else 'D2')

    _install_runtime(monkeypatch, Provider())
    def select_local_check(self, status, *, mutation_generation):
        if selected:
            return ()
        selected.append(True)
        check_id = next(item['id'] for stage in status['verification_pipeline']['stages'] for item in stage['commands']
                        if item['command'] == f'{sys.executable} -m unittest test_check.py')
        return (VerificationAction(kind='stage', stage='targeted',
            command=f'{sys.executable} -m unittest test_check.py', cwd=str(tmp_path),
            mutation_generation=mutation_generation, check_id=check_id),)
    monkeypatch.setattr(VerificationScheduler, 'completion_actions', select_local_check)
    request = replace(_request(tmp_path), messages=({'role': 'user', 'content':
        f'Inspect source.py. Acceptance command: `{sys.executable} -m unittest test_check.py`.'},), metadata={'permission_mode': 'auto',
        'persist_session': False, 'max_turns': 3})
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    if cancel_after:
        runtime = environment.runtime_services.session_runtime
        checkpoint = runtime.checkpoint
        async def checkpoint_then_cancel(*args, **kwargs):
            await checkpoint(*args, **kwargs)
            if source.read_text() == 'VALUE = 2\n':
                raise asyncio.CancelledError('post-verification checkpoint')
        monkeypatch.setattr(runtime, 'checkpoint', checkpoint_then_cancel)
    def review(context):
        reviews.append(context.last_assistant_text)
        return StopHookDecision(action='abort', message='PUBLIC STOP')
    environment.hooks.register_stop_hook(review)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        context = environment.active_run_context
        version = context.verification.snapshot().version
        assert any('Ran 1 test' in str(item.get('content', ''))
                   for item in context.session.transcript if item.get('role') == 'tool')
        if not mutate:
            assert source.read_text() == 'VALUE = 1\n'
            assert version.source_generation == version.acceptance_generation == 0
            assert reviews == ['D1']
            assert result.metadata['authored_content'] == 'D1'
        else:
            assert source.read_text() == 'VALUE = 2\n'
            assert version.source_generation == version.acceptance_generation == 1
            if cancel_after:
                assert result.status is RunStatus.CANCELLED
                assert reviews == []
                assert result.metadata['authored_content'] == ''
            else:
                assert len(requests) >= 2 and requests[1][0] == ''
                assert reviews == ['D2']
                assert result.metadata['authored_content'] == 'D2'
    finally:
        environment.close()


@pytest.mark.parametrize('stage', ['scheduled', 'contract', 'completion'])
@pytest.mark.parametrize('changed', [False, True])
def test_verification_exception_preserves_or_expires_current_candidate(monkeypatch, stage, changed):
    state = RuntimeState()
    context = SimpleNamespace(runtime_state=state,
        policy=SimpleNamespace(verification_status=lambda: {}),
        hooks=SimpleNamespace(trace=lambda *args, **kwargs: None))
    run_context = SimpleNamespace(metadata={'authored_content': 'D1'})
    runner = AgentRunner()
    async def execute(name):
        if stage == name:
            if changed:
                state.source_mutation_generation += 1
            raise ValueError('verification interrupted')
        return SimpleNamespace(blocked_reason='')
    async def scheduled(*args): return await execute('scheduled')
    async def contract(*args): return await execute('contract')
    async def completion(*args): return await execute('completion')
    monkeypatch.setattr(runner, '_execute_scheduled_verification', scheduled)
    monkeypatch.setattr(runner, '_execute_due_verification_contract', contract)
    monkeypatch.setattr(runner, '_execute_completion_verifications', completion)
    monkeypatch.setattr(runner, '_verification_action', lambda *args: SimpleNamespace(
        kind='stage', stage='targeted', command='test', mutation_generation=0, reason='test'))
    budget = SimpleNamespace(nominal_turns=20, max_turns=20, zone=lambda _: 'yellow')
    with pytest.raises(ValueError, match='verification interrupted'):
        asyncio.run(runner._settle_strict_terminal_boundary(
            context, SimpleNamespace(), run_context, [{'role': 'user', 'content': 'test'}],
            boundary='natural_completion', content='D1', completed_turns=1,
            work_budget=budget, resolve_tool_runtime_context=lambda: None,
            natural_completion=True))
    assert run_context.metadata['authored_content'] == ('' if changed else 'D1')


def test_real_sidecar_length_reaches_native_runner_unverified(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.verification.sidecar_verifier import ResolvedVerifierProvider, create_sidecar_verifier_hook
    from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request

    class Provider:
        name = 'offline'
        def create_completion(self, _client, **kwargs):
            if any(t.get('function', {}).get('name') == 'emit_sidecar_verdict' for t in kwargs.get('tools', [])):
                report = {'verdict': 'accept', 'reason': 'ok', 'delivery_assessment': {'status': 'reviewed', 'issues': []}}
                call = SimpleNamespace(function=SimpleNamespace(name='emit_sidecar_verdict', arguments=json.dumps(report)))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='', tool_calls=[call]), finish_reason='length')])
            return _completion('A candidate answer.')

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request, options = _request(tmp_path), RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    hook = create_sidecar_verifier_hook(environment, ResolvedVerifierProvider(provider, object(), 'offline-model', 'offline', 'inherit-main'), env={'KODAX_VERIFIER_ALWAYS': '1'})
    environment._configured_sidecar_verifier = hook
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
    finally:
        environment.close()
    assert result.status is RunStatus.COMPLETED_UNVERIFIED
    assert hook.stats['verdict_counts']['unavailable'] == 1
