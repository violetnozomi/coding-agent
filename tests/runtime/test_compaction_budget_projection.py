"""Compaction uses the current model budget and actual outbound history."""
from __future__ import annotations

import copy
import math

import pytest

from nz_coder.protocol.public_error import PublicRuntimeError
from nz_coder.state import context
from nz_coder.state.context import estimate_request_tokens, prompt_budget
from nz_coder.state.sessions import scoped_session
from nz_coder.runtime.process.workdir import scoped_workdir
from tests.fixtures.compaction_summary import summary_json
from tests.test_context_budget import _CompactionClient


def _compact(tmp_path, monkeypatch, client, budget, **kwargs):
    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    with scoped_workdir(tmp_path), scoped_session("compaction-budget-projection"):
        return context.auto_compact(
            [{"role": "user", "content": "Keep public APIs and fix the parser."}],
            client, "fake-model", budget=budget, **kwargs,
        )


@pytest.mark.parametrize("output", [256_000, 12_000])
def test_summary_uses_current_normal_output_budget(tmp_path, monkeypatch, output):
    client = _CompactionClient(summary_json())
    _compact(tmp_path, monkeypatch, client, prompt_budget(1_000_000, output))
    assert client.request["max_tokens"] == output


def test_summary_budget_fits_full_input_in_small_window(tmp_path, monkeypatch):
    client = _CompactionClient(summary_json())
    budget = prompt_budget(8000, 8000)
    _compact(tmp_path, monkeypatch, client, budget, current_facts="current fact " * 1900)
    input_tokens = estimate_request_tokens(client.request["messages"])
    assert 0 < client.request["max_tokens"] < budget.output_reserve_tokens
    assert math.ceil(input_tokens * 1.15) + client.request["max_tokens"] <= budget.context_tokens


def test_summary_does_not_dispatch_when_full_input_cannot_fit(tmp_path, monkeypatch):
    client = _CompactionClient(summary_json())
    with pytest.raises(PublicRuntimeError, match="fit"):
        _compact(tmp_path, monkeypatch, client, prompt_budget(2000, 1000),
                 current_facts="necessary current fact " * 2000)
    assert client.request == {}


def test_summary_longer_than_old_cap_is_accepted_with_current_budget(tmp_path, monkeypatch):
    goal = "保留高成本调查依据。" * 500
    client = _CompactionClient(summary_json(goal))
    result = _compact(tmp_path, monkeypatch, client, prompt_budget(1_000_000, 256_000))
    assert context.estimate_tokens(client.summary) > 4000
    assert client.request["max_tokens"] == 256_000
    assert goal in result[0]["content"]


def test_summary_keeps_current_model_and_thinking_capabilities(tmp_path, monkeypatch):
    from dataclasses import replace
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime
    from types import SimpleNamespace

    response = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=summary_json()), finish_reason="stop",
    )])
    provider = _Provider([response])
    runtime = _runtime(provider)
    runtime.capabilities = replace(runtime.capabilities, context_tokens=1_000_000,
        output_tokens=256_000, supports_reasoning=True,
        selected_variant="high", variant_options_json='{"reasoning_effort":"high"}')
    original = copy.deepcopy(runtime.capabilities)
    _compact(tmp_path, monkeypatch, object(), prompt_budget(1_000_000, 256_000),
             provider=provider, capabilities=runtime.capabilities, model_runtime=runtime)
    assert provider.calls[0][1]["model"] == runtime.request_model_id
    assert provider.calls[0][1]["max_tokens"] == 256_000
    assert provider.calls[0][1]["_capabilities"] == original
    assert "reasoning_effort" not in provider.calls[0][1]
    assert runtime.capabilities == original


def _reasoning_history():
    from nz_coder.protocol.message_schema import provider_private_state

    reasoner = {
        "role": "assistant", "content": "Inspect the source.",
        "tool_calls": [{"id": "read-1", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"}}],
        **provider_private_state(
            {"reasoning_content": "costly thinking " * 12_000},
            provider_id="fake", model_id="logical", provider_instance_id="instance-a",
        ),
    }
    return [
        {"role": "user", "content": "Fix the parser."},
        {"role": "assistant", "content": "Prior investigation."},
        {"role": "user", "content": "Continue."}, reasoner,
        {"role": "tool", "tool_call_id": "read-1", "content": "source"},
        {"role": "assistant", "content": "Ready to implement."},
    ]


@pytest.mark.parametrize("instance,keeps_reasoner", [("instance-a", False), ("instance-b", True)])
def test_tail_counts_only_reasoning_forwarded_to_target(instance, keeps_reasoner):
    from functools import partial
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.conversation.message_projection import project_provider_messages

    messages = _reasoning_history()
    original = copy.deepcopy(messages)
    project = partial(project_provider_messages,
        capabilities=ModelCapabilities("fake", "logical", preserve_reasoning_content=True),
        target_provider_id="fake", target_model_id="logical", target_provider_instance_id=instance)
    head, tail = context._select_compaction_parts(
        messages, prompt_budget(1_000_000, 256_000), project_history=project,
    )
    assert (messages[3] in tail) is keeps_reasoner
    assert context.estimate_tokens(project(tail)) <= 8000
    assert head + tail == original
    assert messages == original


def test_formal_host_uses_same_projection_for_compaction_tail_and_outbound_request(tmp_path, monkeypatch):
    from dataclasses import replace
    from types import SimpleNamespace
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot, message_version
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime

    provider = _Provider([SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=summary_json()), finish_reason="stop",
    )])])
    host = AgentLoop.__new__(AgentLoop)
    host.provider = provider
    host.client = object()
    host.model_runtime = _runtime(provider)
    host.model_runtime.provider_instance_id = "instance-a"
    host.model_capabilities = replace(host.model_runtime.capabilities,
        context_tokens=1_000_000, output_tokens=256_000, preserve_reasoning_content=True)
    host.model_id = "logical"
    host.runtime_state = None
    host._model_gateway_observer_for_run = lambda: None
    host.prompt_builder = SimpleNamespace(freeze=lambda _host, rows:
        FrozenRequestSnapshot("", "", "", message_version(rows), copy.deepcopy, ()))
    messages = _reasoning_history()
    from nz_coder.protocol.message_schema import ensure_message_identities
    ensure_message_identities(messages, "session-compaction")
    original = copy.deepcopy(messages)
    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    with scoped_workdir(tmp_path), scoped_session("host-compaction-projection"):
        frame = host._freeze_api_request(messages)
        result = host._compact_messages(messages)
    assert messages[3] not in result
    assert context.estimate_tokens(frame.project_history(result[2:])) <= 8000
    assert messages == original


@pytest.mark.parametrize("physical", [False, True])
def test_product_run_continues_after_optional_summary_length_failure(tmp_path, monkeypatch, physical):
    import asyncio
    from dataclasses import replace
    from nz_coder.runtime.execution import native_sdk
    from tests.runtime.test_working_memory_replay_pressure import _environment, _kind
    from tests.runtime.test_working_memory_reminders import _reply

    requests = []
    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if _kind(kwargs) == "compaction":
                response = _reply(kwargs, text='{"goal":')
                response.choices[0].finish_reason = "length"
                return response
            return _reply(kwargs, text="Read-only inspection finished.")

    env, request, options = _environment(monkeypatch, tmp_path, Provider(), interval=0, replay=5000)
    history = [
        {"role": "user", "content": "Read only. Explain these repository observations."},
        {"role": "assistant", "content": "costly evidence " * 3000},
        {"role": "user", "content": "Continue the read-only explanation."},
    ]
    request = replace(request, messages=tuple(history))
    if physical:
        env.model_capabilities = replace(env.model_capabilities, context_tokens=12_000, output_tokens=4000)
        env.model_runtime.capabilities = env.model_capabilities
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
    finally:
        env.close()
    assert sum(_kind(row) == "compaction" for row in requests) == 1
    if physical:
        assert result.status.value == "error"
        assert not any(_kind(row) == "coding" for row in requests)
    else:
        assert result.status.value in {"completed", "completed_unverified"}
        coding = [row for row in requests if _kind(row) == "coding"]
        assert len(coding) == 1
        assert any(row.get("content") == history[1]["content"] for row in coding[0]["messages"])


def test_unsettled_provider_worker_is_not_an_optional_compaction_failure(tmp_path):
    import threading
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime
    from tests.runtime.test_compaction_candidates import _context

    provider = _Provider([])
    runtime = _runtime(provider)
    release = threading.Event()
    worker = threading.Thread(target=release.wait)
    call_id = runtime.begin_inflight(worker, release.set)
    worker.start()
    messages = [{"role": "user", "content": "source " * 100}]

    def compact(_messages):
        return context._create_compaction_completion(
            object(), provider, runtime.capabilities,
            {"model": "logical", "messages": [{"role": "user", "content": "summarize"}], "max_tokens": 100},
            model_runtime=runtime,
        )

    runtime_context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    try:
        with pytest.raises(RuntimeError, match="not settled") as caught:
            ProductionContextManager().prepare_sync(runtime_context, messages, request_snapshot=frame)
        assert not isinstance(caught.value, context.CompactionGenerationError)
        assert provider.calls == []
    finally:
        release.set()
        worker.join(2)
        runtime.finish_inflight(call_id)
        runtime.close()


@pytest.mark.parametrize("asynchronous", [False, True])
def test_provider_cancelled_error_survives_gateway_and_cost_failure_boundary(tmp_path, asynchronous):
    import asyncio
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime
    from tests.runtime.test_compaction_candidates import _context

    provider = _Provider([asyncio.CancelledError("provider cancelled")])
    runtime = _runtime(provider)
    messages = [{"role": "user", "content": "source " * 100}]
    def compact(_rows):
        return context._create_compaction_completion(object(), provider, runtime.capabilities,
            {"model": "logical", "messages": [{"role": "user", "content": "summarize"}], "max_tokens": 100},
            model_runtime=runtime)

    runtime_context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    try:
        with pytest.raises(asyncio.CancelledError):
            if asynchronous:
                asyncio.run(ProductionContextManager().prepare_async(runtime_context, messages, request_snapshot=frame))
            else:
                ProductionContextManager().prepare_sync(runtime_context, messages, request_snapshot=frame)
    finally:
        runtime.close()


@pytest.mark.parametrize("control", [KeyboardInterrupt, SystemExit])
def test_process_control_exception_survives_summary_gateway(tmp_path, control):
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime
    from tests.runtime.test_compaction_candidates import _context

    provider = _Provider([control("stop")])
    runtime = _runtime(provider)
    messages = [{"role": "user", "content": "source " * 100}]
    def compact(_rows):
        return context._create_compaction_completion(object(), provider, runtime.capabilities,
            {"model": "logical", "messages": [{"role": "user", "content": "summarize"}], "max_tokens": 100},
            model_runtime=runtime)

    runtime_context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    try:
        with pytest.raises(control):
            ProductionContextManager().prepare_sync(runtime_context, messages, request_snapshot=frame)
    finally:
        runtime.close()


@pytest.mark.parametrize("closing", [False, True])
def test_retired_runtime_is_not_an_optional_compaction_failure(tmp_path, closing):
    import threading
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime
    from tests.runtime.test_compaction_candidates import _context

    entered, release = threading.Event(), threading.Event()
    class Client:
        def close(self):
            entered.set()
            assert release.wait(2)

    provider = _Provider([])
    runtime = _runtime(provider)
    runtime.client = Client()
    runtime.owns_client = True
    if closing:
        closer = threading.Thread(target=runtime.close)
        closer.start()
        assert entered.wait(2)
    else:
        release.set()
        runtime.close()
    messages = [{"role": "user", "content": "source " * 100}]
    def compact(_rows):
        return context._create_compaction_completion(object(), provider, runtime.capabilities,
            {"model": "logical", "messages": [{"role": "user", "content": "summarize"}], "max_tokens": 100},
            model_runtime=runtime)

    runtime_context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    try:
        with pytest.raises(RuntimeError, match="unavailable|not settled"):
            ProductionContextManager().prepare_sync(runtime_context, messages, request_snapshot=frame)
        assert provider.calls == []
    finally:
        release.set()
        if closing:
            closer.join(2)
        runtime.close()
