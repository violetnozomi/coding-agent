"""Production consumers must preserve evidence while retaining each Run owner."""
from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import replace

from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.swebench.orchestrator import _bind_attempt_evidence
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request


def test_swe_attempt_records_real_gateway_pairs_and_tool_result(monkeypatch, tmp_path):
    class Provider:
        name = "offline"
        calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            return _completion(tool="list_directory") if self.calls == 1 else _completion()

    provider = Provider()
    _install_runtime(monkeypatch, provider)
    request = _request(tmp_path)
    options = RunOptions(permission_asker=lambda *_args: True)
    environment = native_sdk.build_product_run_environment(request, options)
    try:
        with scoped_workdir(tmp_path):
            _bind_attempt_evidence(environment)
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request, options))
        assert result.status.value == "completed"
        assert provider.calls == 2
        facts = [json.loads(row) for row in (tmp_path / ".nz-coder-runs/execution-facts.jsonl").read_text().splitlines()]
        starts = [row for row in facts if row["event"] == "model_call_start"]
        finishes = [row for row in facts if row["event"] == "model_call_finish"]
        assert len(starts) == len(finishes) == 2
        assert all(row["session_id"] == request.session_id for row in facts)
        assert len({row["call_id"] for row in starts}) == 2
        assert all(row["call_id"] for row in starts)
        assert [row["call_id"] for row in starts] == [row["call_id"] for row in finishes]
        tools = [row for row in facts if row["event"] == "tool_execution_result"]
        assert len(tools) == 1
        assert tools[0]["result"]["name"] == "list_directory"
        assert tools[0]["result"]["executed"]
    finally:
        environment.close()


def test_swe_evidence_observer_keeps_captured_owner_across_reuse(monkeypatch, tmp_path):
    captured = {}

    class Provider:
        name = "offline"
        calls = 0

        def create_completion(self, _client, **_kwargs):
            self.calls += 1
            environment = captured["environment"]
            if self.calls == 1:
                captured["observer"] = environment._gateway().observer
            else:
                before = copy.deepcopy(environment.runtime_state.provider_usage)
                captured["observer"]("model_call_finish", {
                    "purpose": "memory", "usage": {"input": 1000, "output": 2000},
                })
                assert environment.runtime_state.provider_usage == before
            return _completion()

    _install_runtime(monkeypatch, Provider())
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    captured["environment"] = environment

    async def scenario():
        runner = native_sdk.NativeSDKRunner(environment)
        first = await runner.run_result(request)
        second = await runner.run_result(replace(request, messages=(*first.messages, {"role": "user", "content": "Reply again."})))
        assert second.status.value == "completed"
        assert second.usage.input_tokens == 2
        assert second.usage.output_tokens == 1

    try:
        with scoped_workdir(tmp_path):
            _bind_attempt_evidence(environment)
        asyncio.run(scenario())
    finally:
        environment.close()


def test_cancelled_sidecar_finish_cannot_account_to_next_run(monkeypatch, tmp_path):
    import threading

    import pytest

    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.verification import sidecar_verifier
    from nz_coder.runtime.verification.hooks import StopHookContext

    _install_runtime(monkeypatch, object())
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    cancellations, errors = [], []

    def paused_invoke(**kwargs):
        cancellations.append(kwargs["cancel_event"])
        entered.set()
        try:
            assert release.wait(5), "sidecar worker was not released"
            kwargs["observer"]("model_call_finish", {
                "purpose": "verifier", "usage": {"input": 1000, "output": 2000},
            })
            return sidecar_verifier.VerifierVerdict("accept", "offline", trace="verifier_ok")
        except BaseException as exc:
            errors.append(exc)
            raise
        finally:
            finished.set()

    monkeypatch.setattr(sidecar_verifier, "invoke_sidecar_verifier", paused_invoke)

    async def scenario():
        runtime = environment.runtime_services.session_runtime
        first = await runtime.open(request)
        execution1 = environment._native_execution_context(first, environment.runtime_services)
        execution1.lifecycle.initialize(first.transcript, False)
        hook = sidecar_verifier.create_sidecar_verifier_hook(
            environment,
            sidecar_verifier.ResolvedVerifierProvider(object(), object(), "offline", "offline", "inherit-main"),
            env={"KODAX_VERIFIER_ALWAYS": "1"},
        )
        task = asyncio.create_task(hook(StopHookContext(
            transcript=tuple(first.transcript), last_assistant_text="done", runtime_state={},
        )))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancellations[0].is_set()
        assert not finished.is_set()
        await execution1.lifecycle.settle_session_checkpoints()
        await runtime.finalize(first, RunStatus.CANCELLED)
        second = await runtime.open(replace(request, messages=(*first.transcript, {"role": "user", "content": "next task"})))
        execution2 = environment._native_execution_context(second, environment.runtime_services)
        execution2.lifecycle.initialize(second.transcript, False)
        before = copy.deepcopy(environment.runtime_state.provider_usage)
        release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        assert not errors
        assert environment.runtime_state.provider_usage == before
        assert second.usage.input_tokens == second.usage.output_tokens == 0
        # 同一个真实 hook 在当前 Run 仍能正常记账。
        await hook(StopHookContext(
            transcript=tuple(second.transcript), last_assistant_text="done", runtime_state={},
        ))
        assert second.usage.input_tokens == 1000
        assert second.usage.output_tokens == 2000
        await execution2.lifecycle.settle_session_checkpoints()
        await runtime.finalize(second, RunStatus.COMPLETED)

    try:
        with scoped_workdir(tmp_path):
            asyncio.run(scenario())
    finally:
        release.set()
        environment.close()


def test_swe_attempt_records_default_image_gateway_evidence(monkeypatch, tmp_path):
    from nz_coder.capabilities import vision
    from nz_coder.protocol.attachments import make_image_attachment
    from nz_coder.protocol.message_schema import attach_file_parts, attach_message_identity

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            return _completion("done")

    monkeypatch.setenv("NZ_IMAGE_DESCRIBE_PROVIDER", "offline")
    monkeypatch.setenv("NZ_IMAGE_DESCRIBE_MODEL", "offline-vision")
    runtime = _install_runtime(monkeypatch, Provider())
    image_runtime = replace(runtime, capabilities=replace(runtime.capabilities, supports_image_input=True))
    monkeypatch.setattr(vision, "create_provider", lambda *_args, **_kwargs: runtime.provider)
    monkeypatch.setattr(vision, "resolve_model_runtime", lambda _request: image_runtime)
    request = _request(tmp_path)
    user = {"role": "user", "content": "Describe this screenshot."}
    attach_message_identity(user, session_id=request.session_id)
    attach_file_parts(user, [make_image_attachment(b"\x89PNG\r\n\x1a\noffline image", "image/png", filename="test.png")])
    request = replace(request, messages=(user,))
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    try:
        with scoped_workdir(tmp_path):
            _bind_attempt_evidence(environment)
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert result.status.value == "completed"
        assert result.usage.input_tokens == 4 and result.usage.output_tokens == 2
        facts = [json.loads(row) for row in (tmp_path / ".nz-coder-runs/execution-facts.jsonl").read_text().splitlines()]
        starts = [row for row in facts if row["event"] == "model_call_start"]
        finishes = [row for row in facts if row["event"] == "model_call_finish"]
        assert [row["purpose"] for row in starts] == ["vision", "coding"]
        assert [row["call_id"] for row in starts] == [row["call_id"] for row in finishes]
    finally:
        environment.close()


def test_default_image_observer_factory_failure_restores_previous_owner(monkeypatch, tmp_path):
    import pytest

    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.session.session_processor import SessionProcessor

    _install_runtime(monkeypatch, object())
    request = _request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    run_control = environment.prepare_run_control()
    attempted = []

    async def scenario():
        runtime = environment.runtime_services.session_runtime
        first = await runtime.open(request)
        execution1 = environment._native_execution_context(first, environment.runtime_services)
        assistant = {"role": "assistant", "content": "old processor"}
        attach_message_identity(assistant, session_id=request.session_id)
        first.transcript.append(assistant)
        processor = SessionProcessor(assistant)
        execution1.messages.bind_active_processor(processor, first.transcript)
        await execution1.lifecycle.settle_session_checkpoints()
        environment._abort_run_preparation(first)
        runtime.abandon(first)
        previous = environment._session_checkpoint_writer
        original_factory = environment._model_gateway_observer_for_run
        original_image_observer = environment.image_describer.observer

        def broken_factory():
            attempted.append(environment._session_checkpoint_writer)
            assert environment._active_session_processor is None
            assert environment._active_processor_messages is None
            raise ValueError("image observer factory failed")

        second = await runtime.open(request)
        monkeypatch.setattr(environment, "_model_gateway_observer_for_run", broken_factory)
        with pytest.raises(ValueError, match="image observer factory failed"):
            environment._native_execution_context(second, environment.runtime_services)
        assert len(attempted) == 1
        assert attempted[0] is not previous and attempted[0].retired
        assert environment._session_checkpoint_writer is previous
        assert environment._active_session_processor is processor
        assert environment._active_processor_messages is first.transcript
        assert environment.image_describer.observer is original_image_observer
        monkeypatch.setattr(environment, "_model_gateway_observer_for_run", original_factory)
        execution2 = environment._native_execution_context(second, environment.runtime_services)
        before = copy.deepcopy(assistant)
        environment.image_describer.observer("model_call_retry", {"purpose": "vision", "attempt": 2, "error": "new image retry"})
        assert assistant == before
        await execution2.lifecycle.settle_session_checkpoints()
        environment._abort_run_preparation(second)
        runtime.abandon(second)

    try:
        asyncio.run(scenario())
    finally:
        environment.retire_run_control(run_control)
        environment.close()
