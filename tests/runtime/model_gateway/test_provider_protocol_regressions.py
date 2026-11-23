"""Offline regressions for provider completion evidence and attempt accounting."""
from __future__ import annotations

from dataclasses import replace
from io import BytesIO
import json
import threading
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from nz_coder.foundation.project_control import capture_project_control_snapshot
from nz_coder.providers import AnthropicProvider, GeminiProvider
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.http import UrllibTransport
from nz_coder.providers.normalized import chunk
from nz_coder.providers.registry import ModelPricing
from nz_coder.protocol.public_error import to_public_error
from nz_coder.runtime.model_gateway import (
    ModelCall, ModelCallPurpose, ModelCallStatus, ProductionModelGateway,
    ResolvedModelRuntime,
)
from nz_coder.state.instructions import load_instruction_context


class Transport:
    def __init__(self, *, response=None, events=()):
        self.response = response
        self.events = events

    def post_json(self, *_args):
        return self.response

    def post_sse(self, *_args):
        return iter(self.events)


class Provider:
    name = 'offline'

    def __init__(self, streams):
        self.streams = iter(streams)
        self.requests = []

    def create_completion(self, _client, **kwargs):
        self.requests.append(kwargs)
        return next(self.streams)


def gateway(provider, **kwargs):
    runtime = ResolvedModelRuntime(
        provider_id=provider.name, model_id='offline', request_model_id='offline',
        variant=None, provider=provider,
        client=provider.create_client() if hasattr(provider, 'create_client') else object(),
        capabilities=ModelCapabilities(provider=provider.name, model_id='offline'),
        pricing=ModelPricing(input=1, output=2),
    )
    return ProductionModelGateway(runtime, **kwargs)


def call(*, stream=True):
    return ModelCall(purpose=ModelCallPurpose.CODING,
                     messages=[{'role': 'user', 'content': 'inspect'}],
                     max_output_tokens=100, streaming=stream, timeout_seconds=2)


@pytest.mark.parametrize('scope', ['project', 'global'])
def test_multiple_snapshot_rules_load_in_relative_path_order(tmp_path, scope):
    root = tmp_path / 'project'
    root.mkdir()
    rules = root / '.nz-coder' / 'rules'
    rules.mkdir(parents=True)
    for name in ('b', 'a'):
        (rules / f'{name}.md').write_text(f'Rule {name.upper()}')
    snapshot = replace(capture_project_control_snapshot(root), trusted=True)
    if scope == 'global':
        snapshot = replace(snapshot, files={
            key.removeprefix('.nz-coder/'): replace(item, relative_path=item.relative_path.removeprefix('.nz-coder/'))
            for key, item in snapshot.files.items()
        })
    config = SimpleNamespace(project_control=snapshot if scope == 'project' else None,
                             workspace=root, user_instructions=snapshot if scope == 'global' else None)
    bundle = load_instruction_context(root, config_snapshot=config)
    assert bundle.source_count == 2
    assert bundle.reminder.index('Rule A') < bundle.reminder.index('Rule B')
    from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
    from nz_coder.runtime.process.workdir import scoped_workdir

    host = SimpleNamespace(
        _active_tool_specs=lambda: [], _sp=SimpleNamespace(build_prompt_block=lambda: ''),
        runtime_state=SimpleNamespace(strict_progress_nudges=0, task_assessment_state={},
            plan_revision=0, build_prompt_block=lambda **_kwargs: ''),
        _memory_block=lambda _query: '', _project_profile_block=lambda: '',
        _hook_prompt_block=lambda: '', _lineage_recovery_block=lambda _messages: '',
        _implementation_bundle_block=lambda _query: '', _repo_retrieval_block=lambda _query: '',
        _plan_mode_prompt_block=lambda: '', _sanitize_messages=lambda messages: list(messages),
        system_prompt='Stable rules.', config_snapshot=config,
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )
    with scoped_workdir(root):
        frozen = ProductionPromptBuilder().freeze(host, [{'role': 'user', 'content': 'inspect'}])
    for name in ('a', 'b'):
        (rules / f'{name}.md').write_text('changed after snapshot')
    projected = frozen.project([{'role': 'user', 'content': 'continue'}])
    assert 'Rule A' in projected[1]['content']
    assert 'Rule B' in projected[1]['content']
    assert 'changed after snapshot' not in str(projected)


def test_stream_retry_wait_cancellation_archives_usage_and_cost_once():
    cancel = threading.Event()
    observed = []

    def broken():
        yield chunk(content='partial', usage={'input_tokens': 5, 'output_tokens': 1,
                                             'total_tokens': 6, 'cost': 0.25})
        raise ConnectionError('controlled disconnect')

    provider = Provider([broken()])
    result = gateway(provider, wait=lambda _seconds: cancel.set(),
                     observer=lambda name, payload: observed.append((name, payload))).complete_stream_sync(
                         call(), cancel_event=cancel)
    assert result.status is ModelCallStatus.CANCELLED
    assert len(provider.requests) == 1
    assert result.usage.total_tokens == 6
    assert result.cost == 0.25
    finish = [payload for name, payload in observed if name == 'model_call_finish']
    assert len(finish) == 1
    assert finish[0]['usage']['total'] == 6
    assert finish[0]['cost'] == 0.25


def test_native_http_500_retains_retry_after_and_retries(monkeypatch):
    requests = []
    waits = []

    def open_response(*_args, **_kwargs):
        requests.append(True)
        if len(requests) == 1:
            raise HTTPError('https://offline.invalid', 500, 'Internal server error',
                            {'Retry-After': '2'}, BytesIO(b'private-api-key-response'))
        return BytesIO(json.dumps({'content': [{'type': 'text', 'text': 'recovered'}],
                                  'stop_reason': 'end_turn'}).encode())

    monkeypatch.setattr('nz_coder.providers.http.urlopen', open_response)
    result = gateway(AnthropicProvider(api_key='offline'), wait=waits.append).complete_sync(call(stream=False))
    assert result.status is ModelCallStatus.COMPLETED
    assert result.content == 'recovered'
    assert len(requests) == 2
    assert waits == [2.0]


def test_native_http_error_private_body_stays_out_of_public_projection(monkeypatch):
    def fail(*_args, **_kwargs):
        raise HTTPError('https://offline.invalid', 500, 'Internal server error',
                        {'Retry-After': '2'}, BytesIO(b'private-api-key-response'))

    monkeypatch.setattr('nz_coder.providers.http.urlopen', fail)
    with pytest.raises(RuntimeError) as caught:
        UrllibTransport().post_json('https://offline.invalid', {}, {})
    assert caught.value.status_code == 500
    assert caught.value.headers['Retry-After'] == '2'
    assert 'private-api-key-response' not in json.dumps(to_public_error(caught.value).to_dict())


@pytest.mark.parametrize('stream', [False, True])
def test_gemini_candidates_and_thought_tokens_remain_separate(stream):
    payload = {'candidates': [{'content': {'parts': [{'text': 'answer'}]}, 'finishReason': 'STOP'}],
               'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 20,
                                 'thoughtsTokenCount': 30, 'totalTokenCount': 60}}
    model = gateway(GeminiProvider(api_key='offline', transport=Transport(response=payload, events=[payload])))
    result = model.complete_stream_sync(call()) if stream else model.complete_sync(call(stream=False))
    assert result.status is ModelCallStatus.COMPLETED
    assert result.usage.as_legacy_dict() == {'input': 10, 'output': 20, 'reasoning': 30,
                                           'total': 60, 'cache_read': 0, 'cache_write': 0}
    assert result.cost == 0.00011


@pytest.mark.parametrize('kind', ['anthropic', 'gemini', 'openai'])
def test_partial_text_eof_without_finish_is_aborted(kind):
    events = []
    if kind == 'anthropic':
        provider = AnthropicProvider(api_key='offline', transport=Transport(events=[
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}}]))
    elif kind == 'gemini':
        provider = GeminiProvider(api_key='offline', transport=Transport(events=[
            {'candidates': [{'content': {'parts': [{'text': 'partial'}]}}]}]))
    else:
        provider = Provider([iter([chunk(content='partial')])])
    result = gateway(provider, max_retries=0).complete_stream_sync(call(), on_event=events.append)
    assert result.status is ModelCallStatus.ABORTED
    assert result.retryable is True
    assert not any(event.kind == 'finish' for event in events)


def test_partial_text_eof_recovers_through_existing_stream_retry():
    provider = Provider([iter([chunk(content='partial')]),
                         iter([chunk(content='recovered', finish_reason='stop')])])
    result = gateway(provider, wait=lambda _seconds: None).complete_stream_sync(call())
    assert result.status is ModelCallStatus.COMPLETED
    assert result.content == 'recovered'
    assert len(provider.requests) == 2
    assert all(request.get('stream') is True for request in provider.requests)


@pytest.mark.parametrize('has_finish', [False, True])
def test_stream_tool_only_runs_after_finish_and_never_replays_after_tail_error(tmp_path, monkeypatch, has_finish):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    (tmp_path / 'evidence.txt').write_text('real safe tool output')
    monkeypatch.setattr(config, 'WORKDIR', tmp_path)
    requests = []
    outputs = []

    class Completions:
        def create(self, **kwargs):
            requests.append(kwargs)
            yield chunk(tool_calls=[{'index': 0, 'id': 'call-read', 'name': 'read_file',
                                     'arguments': '{"path":"evidence.txt"}'}])
            if has_finish:
                yield chunk(finish_reason='tool_calls')
                raise ConnectionError('controlled tail disconnect')

    def execute(result):
        tool = result.tool_calls[0]
        outputs.append(read_file(**json.loads(tool['function']['arguments'])))
        return 'continue'

    with scoped_workdir(tmp_path):
        agent = AgentLoop('offline', permission_mode='auto', trace_enabled=False,
                          client=SimpleNamespace(chat=SimpleNamespace(completions=Completions())),
                          session_id='protocol-regression')
        original_gateway = agent._gateway

        def without_wait(**kwargs):
            model = original_gateway(**kwargs)
            model.wait = lambda _seconds: None
            return model

        monkeypatch.setattr(agent, '_gateway', without_wait)
        try:
            result = agent._call_streaming([{'role': 'user', 'content': 'read evidence'}],
                                           stream_tool_handler=execute)
        finally:
            agent.close()
    if has_finish:
        assert len(requests) == 1
        assert len(outputs) == 1
        assert 'real safe tool output' in outputs[0]
        assert result.tools_executed_in_stream is True
        assert result.assistant_error["data"]["public_error"]["code"] == "model_stream_interrupted"
        assert "1 次" in result.post_tool_stream_error
    else:
        assert len(requests) == config.PROVIDER_MAX_RETRIES + 1
        assert outputs == []
        assert result.aborted is True
        assert result.assistant_error["data"]["public_error"]["metadata"]["attempts"] == len(requests)


def test_anthropic_usage_delta_alone_is_not_a_finish_event():
    provider = AnthropicProvider(api_key='offline', transport=Transport(events=[
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'partial'}},
        {'type': 'message_delta', 'delta': {}, 'usage': {'output_tokens': 4}},
    ]))
    result = gateway(provider, max_retries=0).complete_stream_sync(call())
    assert result.status is ModelCallStatus.ABORTED


@pytest.mark.parametrize('kind', ['anthropic', 'gemini', 'openai'])
def test_formal_stream_finish_keeps_trailing_usage(kind):
    if kind == 'anthropic':
        provider = AnthropicProvider(api_key='offline', transport=Transport(events=[
            {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'complete'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 2}},
            {'type': 'message_stop'},
        ]))
    elif kind == 'gemini':
        provider = GeminiProvider(api_key='offline', transport=Transport(events=[
            {'candidates': [{'content': {'parts': [{'text': 'complete'}]}, 'finishReason': 'STOP'}]},
            {'usageMetadata': {'promptTokenCount': 3, 'candidatesTokenCount': 2, 'totalTokenCount': 5}},
        ]))
    else:
        import httpx
        from openai import OpenAI
        from nz_coder.providers.openai_compatible import OpenAICompatibleProvider

        payload = ('data: {"id":"offline","object":"chat.completion.chunk","created":1,"model":"offline",'
                   '"choices":[{"index":0,"delta":{"content":"complete"},"finish_reason":"stop"}]}\n\n'
                   'data: {"id":"offline","object":"chat.completion.chunk","created":1,"model":"offline",'
                   '"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}\n\n'
                   'data: [DONE]\n\n')
        provider = OpenAICompatibleProvider(api_key='offline', base_url='https://offline.invalid',
            client_factory=lambda **kwargs: OpenAI(**kwargs, http_client=httpx.Client(
                transport=httpx.MockTransport(lambda _request: httpx.Response(
                    200, headers={'content-type': 'text/event-stream'}, content=payload)))))
    model = gateway(provider)
    try:
        result = model.complete_stream_sync(call())
        assert result.status is ModelCallStatus.COMPLETED
        assert result.content == 'complete'
        assert result.usage.output_tokens == 2
        assert result.usage.total_tokens == (2 if kind == 'anthropic' else 5)
    finally:
        close = getattr(model.runtime.client, 'close', None)
        if close:
            close()
