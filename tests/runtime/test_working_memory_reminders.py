"""模型自主维护笔记的请求边界与正式配置回归。"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder
from nz_coder.runtime.core.run_settings import RunSettings
from nz_coder.state.context import PromptBudget
from nz_coder.tools.scratchpad import Scratchpad


def _host(monkeypatch, tmp_path, *, soft=50000, hard=100000):
    instructions = SimpleNamespace(reminder="", source_count=0, included_count=0,
        truncated_count=0, per_file_truncated_count=0, total_truncated_count=0,
        omitted_count=0, included_bytes=0, paths=[], disabled_count=0, warnings=[])
    monkeypatch.setattr("nz_coder.runtime.conversation.prompt_builder.load_instruction_context",
                        lambda _path: instructions)
    budget = PromptBudget(hard + 1000, 1000, hard, soft, 0, 0, 0, False)
    return SimpleNamespace(
        runtime_profile="main", _sp=Scratchpad(tmp_path / "notes.json"),
        runtime_state=SimpleNamespace(task_assessment_state={}, plan_revision=0, strict_progress_nudges=0, investigation_calls_since_edit=0,
                                     mutation_generation=0, build_prompt_block=lambda **_: ""),
        _memory_block=lambda _: "", _project_profile_block=lambda: "", _hook_prompt_block=lambda: "",
        _lineage_recovery_block=lambda _: "", _implementation_bundle_block=lambda _: "",
        _repo_retrieval_block=lambda _: "", system_prompt="system", _plan_mode_prompt_block=lambda: "",
        _sanitize_messages=lambda messages: list(messages), _prompt_budget=lambda: budget,
        _active_tool_specs=lambda: [{"type": "function", "function": {
            "name": "update_scratchpad", "parameters": {"type": "object", "properties": {}}}}],
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )


@pytest.mark.parametrize("interval", [0, 7, 25])
def test_reminder_interval_uses_public_config_and_run_snapshot(tmp_path, interval):
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": str(interval)})
    assert snapshot.get_int("NZ_WORKING_MEMORY_REMINDER_STEPS", -1) == interval
    assert RunSettings.from_snapshot(snapshot).working_memory_reminder_steps == interval


def test_freeze_reads_notes_without_consuming_due_maintenance(monkeypatch, tmp_path):
    host = _host(monkeypatch, tmp_path)
    for _ in range(25):
        host._sp.complete_step(host._sp.revision)
    messages = [{"role": "user", "content": "continue development"}]
    before = copy.deepcopy(messages)
    builder = ProductionPromptBuilder()
    first = builder.freeze(host, messages)
    for _ in range(3):
        frozen = builder.freeze(host, messages)
        assert frozen.project(messages) == first.project(messages)
        frozen.estimate(messages)
    assert first.working_memory_revision == host._sp.revision
    assert "<working-memory-check>" not in first.dynamic
    assert messages == before
    assert host._sp.reminder_reason(25, False) == "periodic"


def _reply(kwargs, *, text="", calls=()):
    from nz_coder.providers.normalized import chunk, completion

    if kwargs.get("stream"):
        chunks = [chunk(tool_calls=[{"index": i, "id": call["id"],
                    "name": call["function"]["name"], "arguments": call["function"]["arguments"]}])
                  for i, call in enumerate(calls)]
        chunks.append(chunk(content=text or None, finish_reason="tool_calls" if calls else "stop",
                            usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3}))
        return iter(chunks)
    return completion(content=text, tool_calls=list(calls), finish_reason="tool_calls" if calls else "stop",
                      usage={"input_tokens": 2, "output_tokens": 1, "total_tokens": 3})


def _call(number, name, args):
    return {"id": f"memory-{number}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.mark.parametrize("stream", [False, True])
def test_native_25_batches_then_serial_maintenance_and_consume_replacement(monkeypatch, tmp_path, stream):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    for i in range(50):
        (tmp_path / f"source_{i}.txt").write_text(f"evidence {i}")

    class Provider:
        name = "offline"

        def __init__(self):
            self.requests = []

        def create_completion(self, _client, **kwargs):
            self.requests.append(copy.deepcopy(kwargs["messages"]))
            if kwargs["messages"][0]["content"].startswith("Maintain working notes"):
                assert not kwargs.get("tools")
                assert not kwargs.get("stream")
                return _reply(kwargs, text=json.dumps({"status": "replace", "memory": {'focus': [{"summary": "MODEL_SELECTED_FACT", "details": ""}], 'findings': [], 'decisions': [], 'lessons': []}}))
            step = sum(not rows[0]["content"].startswith("Maintain working notes") for rows in self.requests)
            if step <= 25:
                return _reply(kwargs, calls=[_call(2 * step + offset, "read_file", {
                    "path": f"source_{2 * (step - 1) + offset}.txt"}) for offset in range(2)])
            if step == 26:
                return _reply(kwargs, calls=[_call(101, "update_scratchpad", {"section": 'focus', "entries": [{"summary": "MODEL_CONSOLIDATED_FACT", "details": ""}], "mode": "replace"})])
            return _reply(kwargs, text="Inspection complete.")

    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=stream)
    request = replace(_request(tmp_path), stream=stream)
    options = RunOptions(permission_asker=lambda *_: True)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        requests = [str(messages[-1].get("content", "")) for messages in provider.requests]
        assert len(requests) == 28
        assert all("<working-memory-check>" not in dynamic for dynamic in requests[:25])
        assert provider.requests[25][0]["content"].startswith("Maintain working notes")
        assert "MODEL_SELECTED_FACT" in requests[26]
        assert "<working-memory-check>" not in requests[26]
        assert "MODEL_CONSOLIDATED_FACT" in requests[27]
        assert "MODEL_SELECTED_FACT" not in requests[27]
        assert environment._sp.read().count("MODEL_CONSOLIDATED_FACT") == 1
        assert "MODEL_SELECTED_FACT" not in environment._sp.read()
        assert environment._sp.completed_steps == 27
        assert result.usage.input_tokens == 56
        assert result.usage.output_tokens == 28
    finally:
        environment.close()


@pytest.mark.parametrize("first_response", ["empty", "length", "retry"])
def test_native_unusable_response_and_provider_retry_do_not_add_memory_steps(
    monkeypatch, tmp_path, first_response,
):
    from nz_coder.providers.normalized import completion
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            if self.calls == 1:
                if first_response == "retry":
                    raise ConnectionError("temporary offline test failure")
                if first_response == "length":
                    return completion(content="partial", finish_reason="length")
                return completion(content="", finish_reason="stop")
            return _reply(kwargs, text="Inspection complete.")

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    snapshot = load_config_snapshot(tmp_path, environ={"MAX_AGENT_TURNS": "3"})
    options = RunOptions(config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert provider.calls == 2
        assert environment._sp.completed_steps == 1
    finally:
        environment.close()


def test_native_failed_command_counts_as_completed_step_without_automatic_notes(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    class Provider:
        name = "offline"

        def __init__(self):
            self.calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return _reply(kwargs, calls=[_call(1, "bash", {
                    "command": "python -c 'raise AssertionError(\"known test failure\")'"})])
            if kwargs["messages"][0]["content"].startswith("Maintain working notes"):
                return _reply(kwargs, text='{"status":"unchanged"}')
            return _reply(kwargs, text="Observed the expected command failure.")

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": "1"})
    options = RunOptions(permission_asker=lambda *_: True, config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert provider.calls == 3
        assert environment._sp.completed_steps == 2
        assert environment._sp.read() == "Scratchpad is empty."
        assert environment._sp.reminder_reason(1, False) == "periodic"
    finally:
        environment.close()


def test_native_reminder_cadence_resumes_same_message_and_resets_for_new_message(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    class Provider:
        name = "offline"

        def __init__(self):
            self.requests = []

        def create_completion(self, _client, **kwargs):
            self.requests.append(copy.deepcopy(kwargs["messages"]))
            if kwargs["messages"][0]["content"].startswith("Maintain working notes"):
                return _reply(kwargs, text='{"status":"unchanged"}')
            return _reply(kwargs, text="Inspection complete.")

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": "1"})
    request = _request(tmp_path)
    options = RunOptions(permission_asker=lambda *_: True, config_snapshot=snapshot)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)

    async def scenario():
        runner = native_sdk.NativeSDKRunner(environment)
        await runner.run_result(request, options)
        await runner.run_result(replace(request, messages=tuple(environment.active_run_context.transcript)), options)
        await runner.run_result(replace(request, messages=({"role": "user", "content": "new inspection"},)), options)

    try:
        asyncio.run(scenario())
        assert len(provider.requests) == 4
        dynamics = [str(messages[-1].get("content", "")) for messages in provider.requests]
        assert "<working-memory-check>" not in dynamics[0]
        assert provider.requests[1][0]["content"].startswith("Maintain working notes")
        assert "<working-memory-check>" not in dynamics[2]
        assert "<working-memory-check>" not in dynamics[3]
    finally:
        environment.close()


def test_native_cancelled_partial_stream_does_not_complete_memory_step(monkeypatch, tmp_path):
    from nz_coder.providers.normalized import chunk
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    entered, release, exited = threading.Event(), threading.Event(), threading.Event()

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            def response():
                try:
                    yield chunk(content="partial")
                    entered.set()
                    assert release.wait(5), "stream not released"
                    yield chunk(content="complete", finish_reason="stop")
                finally:
                    exited.set()
            return response()

    runtime = _install_runtime(monkeypatch, Provider())
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=True)
    request = replace(_request(tmp_path), stream=True)
    options = RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            try:
                await asyncio.wait_for(task, 3)
            except asyncio.CancelledError:
                pass
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 3)

    try:
        asyncio.run(scenario())
        assert environment._sp.completed_steps == 0
    finally:
        release.set()
        environment.close()


def test_local_preflight_rejection_does_not_complete_memory_step(monkeypatch, tmp_path):
    from nz_coder.runtime.conversation.model_result import LLMResult
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    class Provider:
        name = "offline"

        def create_completion(self, *_args, **_kwargs):
            pytest.fail("local preflight must not reach provider")

    _install_runtime(monkeypatch, Provider())
    request = _request(tmp_path)
    snapshot = load_config_snapshot(tmp_path, environ={"MAX_AGENT_TURNS": "2"})
    options = RunOptions(config_snapshot=snapshot)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    monkeypatch.setattr(environment.runtime_services.model, "preflight_request",
                        lambda *_a, **_k: LLMResult(diagnostic="local preflight rejected"))
    try:
        asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert environment._sp.completed_steps == 0
    finally:
        environment.close()


@pytest.mark.parametrize("replay", [False, True])
def test_compaction_needs_no_extra_memory_step_or_model_call(monkeypatch, tmp_path, replay):
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager
    from nz_coder.runtime.core.context import ContextExecutionContext

    hard, soft = (10000, 9000) if replay else (500, 400)
    host = _host(monkeypatch, tmp_path, soft=soft, hard=hard)
    messages = [{"role": "user", "content": "inspect " * 800}]
    frozen = ProductionPromptBuilder().freeze(host, messages)
    calls = []

    def compact(_rows):
        calls.append("compact")
        return [{"role": "user", "content": "summary"}]

    budget = replace(host._prompt_budget(), replay_compaction_tokens=100 if replay else 0)
    context = ContextExecutionContext(
        workspace=tmp_path, budget=budget, projected_tokens=frozen.estimate,
        projected_replay_tokens=frozen.estimate, compact=compact,
        freeze_request=lambda _: frozen, stamp_auto_compaction=lambda _: None,
        trace=lambda *_a, **_k: None,
    )
    assert ProductionContextManager().prepare_sync(context, messages, request_snapshot=frozen)
    assert calls == ["compact"]
    assert host._sp.completed_steps == 0


@pytest.mark.parametrize("stream", [False, True])
def test_cancel_before_tool_batch_settles_does_not_complete_memory_step(monkeypatch, tmp_path, stream):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    (tmp_path / "evidence.txt").write_text("source fact")
    entered, release = threading.Event(), threading.Event()

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            return _reply(kwargs, calls=[_call(1, "read_file", {"path": "evidence.txt"})])

    runtime = _install_runtime(monkeypatch, Provider())
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=stream)
    request = replace(_request(tmp_path), stream=stream)
    options = RunOptions()
    environment = native_sdk.build_product_run_environment(request, options)
    original = environment._execute_tool_call_with_hooks

    def execute(*args, **kwargs):
        entered.set()
        assert release.wait(5), "tool batch not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(environment, "_execute_tool_call_with_hooks", execute)

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
        result = await task
        assert result.status.value == "cancelled"

    try:
        asyncio.run(scenario())
        assert environment._sp.completed_steps == 0
    finally:
        release.set()
        environment.close()


@pytest.mark.parametrize("reply", [
    '{"status":"unchanged"}', '{"status":"unable","reason":"none"}', "invalid JSON",
    '{"status": "replace", "memory": {"focus": [{"summary": "LATE", "details": ""}], "findings": [], "decisions": [], "lessons": []}}',
])
def test_followup_during_maintenance_stops_all_outcomes_before_next_coding(monkeypatch, tmp_path, reply):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    (tmp_path / "source.txt").write_text("source")
    queued, calls = {"value": False}, []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _reply(kwargs, calls=[_call(1, "read_file", {"path": "source.txt"})])
            assert kwargs["messages"][0]["content"].startswith("Maintain working notes")
            queued["value"] = True
            return _reply(kwargs, text=reply)

    _install_runtime(monkeypatch, Provider())
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": "1"})
    options = RunOptions(config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    environment.set_followup_pending(lambda: queued["value"])
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert result.status.value == "interrupted"
        assert len(calls) == 2
        assert environment._sp.completed_steps == 1
        assert environment._sp.read() == "Scratchpad is empty."
        assert not any(str(row.get("content", "")).startswith("Maintain working notes")
                       for row in environment.active_run_context.transcript)
    finally:
        environment.close()


def test_native_maintenance_provider_refusal_does_not_continue_coding(monkeypatch, tmp_path):
    from nz_coder.runtime.conversation.model_result import ModelCompletionError
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    (tmp_path / "source.txt").write_text("source")
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return _reply(kwargs, calls=[_call(1, "read_file", {"path": "source.txt"})])
            raise RuntimeError("offline budget refusal")

    _install_runtime(monkeypatch, Provider())
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": "1"})
    options = RunOptions(config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    try:
        with pytest.raises(ModelCompletionError):
            asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert len(calls) == 2
        assert environment.active_run_context.finalized
        assert environment._sp.completed_steps == 1
    finally:
        environment.close()


def test_native_incomplete_maintenance_cannot_clear_confirmed_notes(monkeypatch, tmp_path):
    from nz_coder.providers.normalized import completion
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    (tmp_path / "source.txt").write_text("source")
    calls = []

    class Provider:
        name = "offline"
        coding = 0

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            if kwargs["messages"][0]["content"].startswith("Maintain working notes"):
                return completion(content='{"status": "replace", "memory": {"focus": [], "findings": [], "decisions": [], "lessons": []}}', finish_reason="error")
            self.coding += 1
            if self.coding == 1:
                return _reply(kwargs, calls=[_call(1, "update_scratchpad", {"section": 'focus', "entries": [{"summary": "KEEP_CONFIRMED_FACT", "details": ""}]})])
            if self.coding == 2:
                return _reply(kwargs, calls=[_call(2, "read_file", {"path": "source.txt"})])
            return _reply(kwargs, text="Inspection complete.")

    _install_runtime(monkeypatch, Provider())
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": "1"})
    options = RunOptions(config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert len(calls) == 4
        assert "KEEP_CONFIRMED_FACT" in environment._sp.read()
        assert "KEEP_CONFIRMED_FACT" in calls[-1]["messages"][-1]["content"]
    finally:
        environment.close()


@pytest.mark.parametrize("supports_tools,interval", [(False, 25), (True, 0)])
def test_native_soft_pressure_respects_model_tools_and_disabled_config(monkeypatch, tmp_path, supports_tools, interval):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            return _reply(kwargs, text="Inspection complete.")

    runtime = _install_runtime(monkeypatch, Provider())
    runtime.capabilities = replace(runtime.capabilities, supports_tools=supports_tools)
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": str(interval)})
    options = RunOptions(config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    monkeypatch.setattr(environment, "_projected_request_tokens",
                        lambda _: environment._prompt_budget().soft_preflight_tokens + 1)
    try:
        asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert len(calls) == 1
        assert not calls[0]["messages"][0]["content"].startswith("Maintain working notes")
    finally:
        environment.close()


def test_cancel_during_maintenance_keeps_notes_and_retires_worker(monkeypatch, tmp_path):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk
    from test_native_product_runtime import _request, _install_runtime

    (tmp_path / "source.txt").write_text("source")
    entered, release = threading.Event(), threading.Event()

    class Provider:
        name = "offline"
        coding = 0

        def create_completion(self, _client, **kwargs):
            if kwargs["messages"][0]["content"].startswith("Maintain working notes"):
                entered.set()
                assert release.wait(5), "maintenance was not released"
                return _reply(kwargs, text='{"status": "replace", "memory": {"focus": [], "findings": [], "decisions": [], "lessons": []}}')
            self.coding += 1
            if self.coding == 1:
                return _reply(kwargs, calls=[_call(1, "update_scratchpad", {"section": 'focus', "entries": [{"summary": "KEEP_CONFIRMED_FACT", "details": ""}]})])
            return _reply(kwargs, calls=[_call(2, "read_file", {"path": "source.txt"})])

    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    snapshot = load_config_snapshot(tmp_path, environ={"NZ_WORKING_MEMORY_REMINDER_STEPS": "1"})
    options = RunOptions(config_snapshot=snapshot)
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            task.cancel()
        finally:
            release.set()
        result = await task
        assert result.status.value == "cancelled"

    try:
        asyncio.run(scenario())
        assert "KEEP_CONFIRMED_FACT" in environment._sp.read()
        assert environment._sp.completed_steps == 2
        assert provider.coding == 2
        assert runtime.inflight_status()["inflight_calls"] == 0
    finally:
        release.set()
        environment.close()
