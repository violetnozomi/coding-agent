"""Offline Native Runner characterization of streamed tool failure ownership."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import socket
import threading
import traceback
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.normalized import chunk
from nz_coder.runtime.core import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunOptions, RunRequest
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution.services import _StreamToolBridge
from nz_coder.runtime.model_gateway import ResolvedModelRuntime


def _run(monkeypatch, tmp_path, *, command='sleep 5', denied=False, idle=60,
         infrastructure=False, provider_error=False, cancel=False, verification=False,
         rerun=False, hard=600):
    requests, exceptions, tool_results, bridge_outcomes, request_states = [], [], [], [], []
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS', str(idle))
    monkeypatch.setenv('NZ_PROVIDER_MAX_RETRIES', '0')
    monkeypatch.setenv('BASH_TIMEOUT_SECONDS', '1')
    monkeypatch.setenv('NZ_PROVIDER_HARD_TIMEOUT_SECONDS', str(hard))
    for key in ('API_KEY', 'OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'API_BASE_URL',
                'KODAX_VERIFIER_PROVIDER', 'KODAX_VERIFIER_MODEL'):
        monkeypatch.delenv(key, raising=False)
    network_attempts = []
    started = threading.Event()
    if verification:
        (workspace / 'test_wait.py').write_text(
            'import time\nfrom pathlib import Path\ndef test_wait():\n'
            '    marker = Path("attempted")\n'
            + ('    if not marker.exists():\n' if rerun else '    if True:\n') +
            '        marker.touch()\n        time.sleep(5)\n')
        command = 'python -m pytest -q test_wait.py'

    def deny_network(*_args, **_kwargs):
        network_attempts.append(True)
        raise AssertionError('offline regression attempted network access')

    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny_network)

    class Provider:
        name = 'offline'

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            request_states.append(copy.deepcopy(env.runtime_state.to_dict()))
            assert kwargs.get('stream') is True
            assert len(requests) <= (3 if verification else 2), 'unexpected Provider request'
            if verification and len(requests) == 1:
                return iter([chunk(tool_calls=[{
                    'index': 0, 'id': 'call-write', 'name': 'write_file',
                    'arguments': json.dumps({'path': 'helper.py', 'content': 'VALUE = 1\n'})}]),
                    chunk(finish_reason='tool_calls')])
            if len(requests) == (2 if verification else 1) or rerun:
                chunks = [
                    chunk(tool_calls=[{'index': 0, 'id': f'call-{len(requests)}', 'name': 'bash',
                                       'arguments': json.dumps({'command': command, 'timeout': 1})}]),
                    chunk(finish_reason='tool_calls'),
                ]

                def stream():
                    yield from chunks
                    if provider_error:
                        raise ConnectionError('controlled Provider transport failure')

                return stream()
            if verification:
                return iter([chunk(tool_calls=[{
                    'index': 0, 'id': 'call-inspect', 'name': 'read_file',
                    'arguments': json.dumps({'path': 'test_wait.py'})}]),
                    chunk(finish_reason='tool_calls')])
            return iter([chunk(content='I observed the tool result and will adjust.', finish_reason='stop')])

    runtime = ResolvedModelRuntime(
        provider_id='offline', model_id='failure-domains', request_model_id='failure-domains',
        variant=None, provider=Provider(), client=SimpleNamespace(close=lambda: None),
        capabilities=ModelCapabilities(provider='offline', model_id='failure-domains'), owns_client=True)
    for target in ('nz_coder.runtime.execution.native_sdk.resolve_model_runtime',
                   'nz_coder.runtime.execution.loop.resolve_model_runtime',
                   'nz_coder.runtime.model_gateway.resolve_model_runtime'):
        monkeypatch.setattr(target, lambda *_a, **_k: runtime)

    from nz_coder.runtime.model_gateway import gateway
    original_iterator = gateway.iter_stream_with_timeouts
    original_error_metadata = gateway._error_metadata

    def observe_error(exc):
        exceptions.append(''.join(traceback.format_exception(exc)))
        return original_error_metadata(exc)

    monkeypatch.setattr(gateway, '_error_metadata', observe_error)

    def observe_iterator(*args, **kwargs):
        try:
            yield from original_iterator(*args, **kwargs)
        except BaseException:
            exceptions.append(traceback.format_exc())
            raise

    monkeypatch.setattr(gateway, 'iter_stream_with_timeouts', observe_iterator)
    original_bridge = _StreamToolBridge.execute

    def observe_bridge(self, result):
        try:
            outcome = original_bridge(self, result)
            bridge_outcomes.append(outcome)
            return outcome
        except BaseException:
            exceptions.append(traceback.format_exc())
            raise

    monkeypatch.setattr(_StreamToolBridge, 'execute', observe_bridge)
    request = RunRequest(
        agent=AgentDefinition(name='failure-domains', instructions='Inspect command outcomes.'),
        profile=MAIN_PROFILE, workspace=workspace, session_id='failure-domains', stream=True,
        provider='offline', model='failure-domains',
        messages=({'role': 'user', 'content': (
            'Create helper.py. Run python -m pytest -q test_wait.py and report the result.' if verification else
            'Inspect the command outcome and explain it. Do not modify files.')},),
        metadata={'permission_mode': 'auto', 'persist_session': True, 'max_turns': 3 if verification else 2})
    options = RunOptions(permission_asker=lambda *_a: not denied)
    env = native_sdk.build_product_run_environment(request, options)
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    original_execute = ToolExecutor.execute_one

    def execute(*args, **kwargs):
        started.set()
        return original_execute(*args, **kwargs)

    monkeypatch.setattr(ToolExecutor, 'execute_one', execute)
    if infrastructure:
        def broken_hook(_ctx):
            raise RuntimeError('controlled lifecycle invariant failure')
        env.hooks.after_tool_result_hooks.append(broken_hook)
    original_record = env._record_tool_result

    def record(result):
        tool_results.append(copy.deepcopy(asdict(result)))
        return original_record(result)

    monkeypatch.setattr(env, '_record_tool_result', record)
    try:
        async def exercise():
            task = asyncio.create_task(native_sdk.NativeSDKRunner(env).run_result(request, options))
            if cancel:
                assert await asyncio.to_thread(started.wait, 5)
                await asyncio.sleep(0.1)
                task.cancel()
            return await task

        result = asyncio.run(exercise())
        assert not network_attempts, 'unexpected network attempt was swallowed by a component'
        state = env.runtime_state.to_dict()
        trace = [json.loads(line) for line in env.tracer.path.read_text().splitlines()]
        from nz_coder.runtime.session.model import SessionIdentity
        saved = asyncio.run(env.runtime_services.session_runtime.store.load(
            SessionIdentity('failure-domains'), workspace))
        assert saved is not None
        durable = copy.deepcopy(saved.transcript)
        capture = dict(requests=requests, exceptions=exceptions, tools=tool_results,
                       result=asdict(result), state=state, runtime=trace,
                       run_evidence=env.run_evidence.to_dict(), verification=env.vm.status(),
                       durable=durable, bridge_outcomes=bridge_outcomes, request_states=request_states)
        evidence = os.environ.get('NZ_TIMEOUT_EVIDENCE_DIR')
        if evidence:
            out = Path(evidence) / tmp_path.name
            out.mkdir(parents=True, exist_ok=True)
            (out / 'replay.json').write_text(json.dumps(capture, indent=2, default=str))
            (out / 'stack.txt').write_text('\n'.join(exceptions) or 'No bridge exception.\n')
        return capture
    finally:
        env.close()


@pytest.mark.parametrize('idle,hard', [(60, 600), (0.2, 600), (0, 1)])
def test_successful_streamed_tool_timeout_is_recoverable(monkeypatch, tmp_path, idle, hard):
    capture = _run(monkeypatch, tmp_path, idle=idle, hard=hard)
    assert capture['tools'], capture['exceptions']
    tool = capture['tools'][0]
    assert tool['executed'] and tool['dispatch_failed']
    assert not tool['command_failed'] and not tool['permission_denied']
    assert tool['metadata']['timed_out'] is True
    assert tool['metadata']['exit'] is None
    assert tool['metadata']['cancelled'] is False
    assert len(capture['requests']) == 2, capture['exceptions']
    feedback = [m for m in capture['requests'][1]['messages'] if m.get('role') == 'tool']
    assert any(m['tool_call_id'] == 'call-1' and 'timed out' in m['content'] for m in feedback)
    assert not capture['exceptions']
    assert capture['bridge_outcomes'][0] == 'continue'
    assert len(tool['output']) < 512
    assert any(m.get('role') == 'tool' and m.get('tool_call_id') == 'call-1'
               and 'timed out' in m.get('content', '') for m in capture['durable'])
    parts = [p for m in capture['durable'] for p in m.get('_nz_parts', [])
             if p.get('type') == 'tool']
    assert any(p.get('state', {}).get('status') == 'error' for p in parts)
    finish = [e for e in capture['runtime'] if e.get('event') == 'model_call_finish']
    assert finish[0]['status'] == 'completed'
    assert finish[0]['finish_reason'] == 'tool_calls'


@pytest.mark.parametrize('command,denied,expected', [
    ('exit 7', False, 'command_failed'),
    ('sleep 5', True, 'permission_denied'),
    ('printf recovered', False, 'success'),
])
def test_streamed_tool_result_domains_continue(monkeypatch, tmp_path, command, denied, expected):
    capture = _run(monkeypatch, tmp_path, command=command, denied=denied)
    assert len(capture['requests']) == 2, capture['exceptions']
    assert not capture['exceptions']
    tool = capture['tools'][0]
    if expected == 'success':
        assert tool['executed'] and not tool['dispatch_failed'] and not tool['command_failed']
    else:
        assert tool[expected]
    feedback = [m for m in capture['requests'][1]['messages'] if m.get('role') == 'tool']
    assert any(m['tool_call_id'] == 'call-1' and tool['output'] in m['content'] for m in feedback)


def test_streamed_tool_infrastructure_exception_remains_fatal(monkeypatch, tmp_path):
    capture = _run(monkeypatch, tmp_path, command='printf recovered', infrastructure=True)
    assert len(capture['requests']) == 1
    assert capture['result']['status'] == 'error'
    assert capture['tools'][0]['executed']
    assert any('StreamToolExecutionFailed' in stack and 'controlled lifecycle invariant failure' in stack
               for stack in capture['exceptions'])


def test_genuine_provider_stream_failure_remains_fatal(monkeypatch, tmp_path):
    capture = _run(monkeypatch, tmp_path, command='printf recovered', provider_error=True)
    assert len(capture['requests']) == 1
    assert capture['result']['status'] == 'error'
    assert any('ConnectionError' in stack for stack in capture['exceptions'])
    assert not any('StreamToolExecutionFailed' in stack for stack in capture['exceptions'])


def test_user_cancellation_is_not_a_recoverable_timeout(monkeypatch, tmp_path):
    capture = _run(monkeypatch, tmp_path, cancel=True)
    assert len(capture['requests']) == 1
    assert capture['result']['status'] == 'cancelled'
    assert not any('TimeoutError' in stack for stack in capture['exceptions'])


def test_streamed_verification_timeout_never_passes(monkeypatch, tmp_path):
    capture = _run(monkeypatch, tmp_path, idle=0.2, verification=True)
    assert len(capture['requests']) == 3, capture['exceptions']
    bash_tools = [t for t in capture['tools'] if t['name'] == 'bash']
    assert bash_tools[0]['metadata']['timed_out']
    before_repair = capture['request_states'][2]
    assert before_repair['verification_contract']['passed'] is not True
    assert before_repair['verification_generation'] != before_repair['mutation_generation']
    state = capture['state']
    assert state['verification_generation'] != state['mutation_generation']
    assert state['verification_contract']['passed'] is not True
    assert capture['result']['status'] != 'completed'
    assert not any(
        v.get('command') == 'python -m pytest -q test_wait.py' and v.get('status') == 'passed'
        for v in capture['run_evidence']['verification_results']
    )


def test_verification_success_requires_real_rerun(monkeypatch, tmp_path):
    capture = _run(monkeypatch, tmp_path, idle=0.2, verification=True, rerun=True)
    assert len(capture['requests']) == 3, capture['exceptions']
    bash_tools = [t for t in capture['tools'] if t['name'] == 'bash']
    assert bash_tools[0]['metadata']['timed_out']
    before_rerun = capture['request_states'][2]
    assert before_rerun['verification_generation'] != before_rerun['mutation_generation']
    assert before_rerun['verification_contract']['passed'] is not True
    second = bash_tools[1]
    assert second['executed'] and not second['dispatch_failed'] and not second['command_failed']
    assert second['metadata']['exit'] == 0
    assert '1 passed' in second['output']
    state = capture['state']
    assert state['verification_generation'] == state['mutation_generation']
    assert state['verification_contract']['passed'] is True
