"""Real offline Run ownership and terminal writer ordering regressions."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from contextvars import copy_context
from dataclasses import replace

import pytest

from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _offline_accept_review, _request


def _durable_request(tmp_path):
    return replace(_request(tmp_path), metadata={"permission_mode": "auto", "persist_session": True})


def _payload(tmp_path, request):
    with scoped_workdir(tmp_path):
        return json.loads((sessions.session_dir() / f"{request.session_id}.json").read_text())


def test_completed_run_rejects_captured_retry(monkeypatch, tmp_path):
    """A returned Run must reject old callbacks even while its environment stays open."""
    captured = {}

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            if review := _offline_accept_review(kwargs, report_kind="ordinary"):
                return review
            environment = captured["environment"]
            captured["retry"] = environment._gateway().observer
            captured["processor"] = environment._active_session_processor
            return _completion()

    _install_runtime(monkeypatch, Provider(), with_delivery_review=True)
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    captured["environment"] = environment

    async def scenario():
        result = await native_sdk.NativeSDKRunner(environment).run_result(request)
        assert result.status.value == "completed"
        before = _payload(tmp_path, request)
        message_before = copy.deepcopy(captured["processor"].message)
        with scoped_workdir(tmp_path):
            captured["retry"]("model_call_retry", {
                "purpose": "coding", "attempt": 1, "wait_seconds": 0, "error": "LATE_RETRY",
            })
        assert _payload(tmp_path, request) == before
        assert captured["processor"].message == message_before

    try:
        asyncio.run(scenario())
    finally:
        environment.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_terminal_waits_for_real_checkpoint_without_blocking_loop(monkeypatch, tmp_path, cancel):
    """A real legacy JSON save must settle before terminal projection and canonical save."""
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    from nz_coder.runtime.adapters.runner import _LifecycleService
    from nz_coder.runtime.session.runtime import SessionRuntime

    entered, release, terminal_started = threading.Event(), threading.Event(), threading.Event()
    worker_finished, canonical_started = threading.Event(), threading.Event()
    original_canonical_finalize = SessionRuntime.finalize
    start_checkpoint, focused_started = threading.Event(), threading.Event()
    original_focused_finalize = _LifecycleService.finalize
    captured, errors = {}, []
    request = _durable_request(tmp_path)
    original_write = sessions._publish_session_json
    original_finalize = ProductionRunLifecycle.finalize

    def write(prepared, path):
        if threading.current_thread().name == "entered-run-checkpoint" and path.name == f"{request.session_id}.json":
            entered.set()
            assert release.wait(5), "checkpoint not released"
        return original_write(prepared, path)

    async def focused_finalize(*args, **kwargs):
        if not focused_started.is_set():
            focused_started.set()
            start_checkpoint.set()
            assert await asyncio.to_thread(entered.wait, 3)
        return await original_focused_finalize(*args, **kwargs)

    async def canonical_finalize(*args, **kwargs):
        canonical_started.set()
        return await original_canonical_finalize(*args, **kwargs)

    async def finalize(*args, **kwargs):
        terminal_started.set()
        return await original_finalize(*args, **kwargs)

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            observer = captured["environment"]._gateway().observer
            scope = copy_context()

            def checkpoint():
                try:
                    assert start_checkpoint.wait(3)
                    scope.run(observer, "model_call_retry", {
                        "purpose": "coding", "attempt": 1, "wait_seconds": 0, "error": "ENTERED_RETRY",
                    })
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    worker_finished.set()

            captured["worker"] = threading.Thread(target=checkpoint, name="entered-run-checkpoint")
            captured["worker"].start()
            return _completion()

    _install_runtime(monkeypatch, Provider())
    original_build = native_sdk.build_product_run_environment

    def build(*args, **kwargs):
        environment = original_build(*args, **kwargs)
        captured["environment"] = environment
        return environment

    monkeypatch.setattr(native_sdk, "build_product_run_environment", build)
    monkeypatch.setattr(sessions, "_publish_session_json", write)
    monkeypatch.setattr(ProductionRunLifecycle, "finalize", finalize)
    monkeypatch.setattr(_LifecycleService, "finalize", focused_finalize)
    monkeypatch.setattr(SessionRuntime, "finalize", canonical_finalize)

    async def scenario():
        expected_status = "cancelled" if cancel else "completed_unverified"
        task = asyncio.create_task(native_sdk.NativeSDKRunner().run_result(request))
        # 仅防失败实现把事件循环堵死；成功路径由测试显式放行。
        rescue = threading.Timer(2, release.set)
        rescue.start()
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            for _ in range(10):
                await asyncio.sleep(0)
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
            await asyncio.sleep(0.02)
            assert focused_started.is_set()
            assert not release.is_set(), "event loop was blocked by synchronous terminal checkpoint"
            assert not canonical_started.is_set()
            assert not terminal_started.is_set(), "terminal projection overtook the entered writer"
            assert not task.done(), "Run returned before its writer settled"
        finally:
            release.set()
            rescue.cancel()
            try:
                result = await asyncio.wait_for(task, 5)
            except asyncio.CancelledError:
                assert cancel
            else:
                assert result.status.value == expected_status
            await asyncio.to_thread(captured["worker"].join, 3)
        assert worker_finished.is_set()
        assert not errors
        assert _payload(tmp_path, request)["run_status"] == expected_status
        context = captured["environment"].active_run_context
        assert context.usage.input_tokens == 2 and context.usage.output_tokens == 1
        restored = await captured["environment"].runtime_services.session_runtime.store.load(context.session.identity, tmp_path)
        assert restored.status.value == expected_status

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        if "environment" in captured:
            captured["environment"].close()


def test_old_gateway_cannot_mutate_next_run_or_account_its_usage(monkeypatch, tmp_path):
    """Reset reuses RuntimeState, so callback ownership cannot follow that mutable object."""
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
                processor = environment._active_session_processor
                before = copy.deepcopy(processor.message)
                usage_before = copy.deepcopy(environment.runtime_state.provider_usage)
                captured["observer"]("model_call_retry", {
                    "purpose": "coding", "attempt": 7, "error": "OLD_RUN_RETRY",
                })
                captured["observer"]("model_call_finish", {
                    "purpose": "memory", "usage": {"input": 1000, "output": 2000},
                })
                assert processor.message == before, "old observer mutated the next Run processor"
                assert environment.runtime_state.provider_usage == usage_before
            return _completion()

    _install_runtime(monkeypatch, Provider())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    captured["environment"] = environment

    async def scenario():
        runner = native_sdk.NativeSDKRunner(environment)
        first = await runner.run_result(request)
        second_request = replace(request, messages=(*first.messages, {"role": "user", "content": "Reply again."}))
        result = await runner.run_result(second_request)
        assert result.status.value == "completed_unverified"
        assert result.usage.input_tokens < 1000

    try:
        asyncio.run(scenario())
    finally:
        environment.close()


def test_raw_subagent_and_question_callbacks_keep_their_run_owner(monkeypatch, tmp_path):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.agent.subagent import _report_subagent_progress
    from nz_coder.runtime.core.result import RunStatus
    from nz_coder.runtime.session.session_processor import SessionProcessor
    from nz_coder.tools import scoped_tool_call, scoped_tool_metadata_reporter

    _install_runtime(monkeypatch, object())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())

    def bind(context):
        execution = environment._native_execution_context(context, environment.runtime_services)
        assistant = {"role": "assistant", "content": ""}
        attach_message_identity(assistant, session_id=request.session_id)
        context.transcript.append(assistant)
        processor = SessionProcessor(assistant)
        processor.register_tool_calls([{
            "id": "parent-task", "type": "function",
            "function": {"name": "task", "arguments": "{}"},
        }])
        execution.messages.bind_active_processor(processor, context.transcript)
        with scoped_tool_call("parent-task"), scoped_tool_metadata_reporter(
            environment._tool_metadata_callback(processor, context.transcript),
        ):
            scope = copy_context()
        return execution, processor, scope

    async def scenario():
        runtime = environment.runtime_services.session_runtime
        first = await runtime.open(request)
        execution1, processor1, scope1 = bind(first)
        question1 = environment._question_lifecycle_callback(processor1, first.transcript)
        await execution1.lifecycle.settle_session_checkpoints()
        first_before = copy.deepcopy(processor1.message)
        # 此时 pending tool 尚未被 canonical finalize 清理；退休必须先拒绝 mutation。
        scope1.run(_report_subagent_progress, {"session_id": "old-child"}, status="OLD_PROGRESS")
        question1("pending", {"tool_call_id": "parent-task", "request_id": "OLD_QUESTION", "questions": []})
        assert processor1.message == first_before
        await runtime.finalize(first, RunStatus.CANCELLED)
        second = await runtime.open(replace(request, messages=(*first.transcript, {"role": "user", "content": "Another task."})))
        execution2, processor2, scope2 = bind(second)
        await runtime.checkpoint(second, "running")
        before = _payload(tmp_path, request)
        message_before = copy.deepcopy(processor2.message)
        scope1.run(_report_subagent_progress, {"session_id": "old-child"}, status="LATE_PROGRESS")
        question1("pending", {"tool_call_id": "parent-task", "request_id": "LATE_QUESTION", "questions": []})
        assert processor2.message == message_before
        assert _payload(tmp_path, request) == before
        scope2.run(_report_subagent_progress, {"session_id": "new-child"}, status="NEW_PROGRESS", tool_count=2)
        saved = _payload(tmp_path, request)
        assert saved["run_status"] == "running"
        assert any(part.get("state", {}).get("metadata", {}).get("child_status") == "NEW_PROGRESS"
                   for part in saved["messages"][-1]["_nz_parts"])
        await execution2.lifecycle.settle_session_checkpoints()
        await runtime.finalize(second, RunStatus.COMPLETED)

    try:
        with scoped_workdir(tmp_path):
            asyncio.run(scenario())
    finally:
        environment.close()


def test_terminal_learning_accounts_after_writer_retirement(monkeypatch, tmp_path):
    captured = {}

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            return _completion()

    _install_runtime(monkeypatch, Provider())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())

    async def learn(memory_context, _messages, _status):
        writer = environment._session_checkpoint_writer
        assert writer.retired
        assert not writer.owner.finalized
        captured["memory"] = memory_context.observer
        assert environment.image_describer.observer is not None
        for observer in (memory_context.observer, environment.image_describer.observer):
            observer("model_call_finish", {"purpose": "memory", "usage": {"input": 11, "output": 7}})

    monkeypatch.setattr(environment.runtime_services.memory, "finalize", learn)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert result.status.value == "completed_unverified"
        assert result.usage.input_tokens == 24
        assert result.usage.output_tokens == 15
        assert environment.runtime_state.provider_usage_by_purpose["memory"]["input"] == 22
        before = copy.deepcopy(environment.runtime_state.provider_usage)
        captured["memory"]("model_call_finish", {"purpose": "memory", "usage": {"input": 1000}})
        assert environment.runtime_state.provider_usage == before
    finally:
        environment.close()


@pytest.mark.parametrize("execution_error", [False, True])
def test_finalsave_failure_keeps_writer_retired_and_original_error(monkeypatch, tmp_path, execution_error):
    captured = {}
    request = _durable_request(tmp_path)

    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            captured["observer"] = captured["environment"]._gateway().observer
            return _completion()

    _install_runtime(monkeypatch, Provider())
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    captured["environment"] = environment
    original_write = sessions._publish_session_json
    expected_status = "error" if execution_error else "completed_unverified"
    failed_save = threading.Event()

    def fail_terminal(prepared, path):
        payload = json.loads(prepared.read_text())
        if path.name == f"{request.session_id}.json" and payload.get("run_status") == expected_status:
            failed_save.set()
            raise OSError("terminal storage failed")
        return original_write(prepared, path)

    async def before_run(_context):
        captured["observer"] = environment._gateway().observer
        raise ValueError("primary execution failed")

    if execution_error:
        monkeypatch.setattr(environment.runner._runtime_events, "before_run", before_run)
    monkeypatch.setattr(sessions, "_publish_session_json", fail_terminal)
    try:
        with pytest.raises(ValueError if execution_error else OSError, match="primary execution failed" if execution_error else "terminal storage failed"):
            asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert failed_save.is_set()
        writer = environment._session_checkpoint_writer
        assert writer.retired
        assert not writer.owner.finalized
        before = _payload(tmp_path, request) if (not execution_error) else None
        captured["observer"]("model_call_retry", {"purpose": "coding", "attempt": 3, "error": "late"})
        if before is not None:
            assert _payload(tmp_path, request) == before
    finally:
        environment.close()


def test_before_run_failure_retires_owner_and_environment_can_be_reused(monkeypatch, tmp_path):
    class Provider:
        name = "offline"

        def create_completion(self, _client, **_kwargs):
            return _completion()

    _install_runtime(monkeypatch, Provider())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    original_before = environment.runner._runtime_events.before_run

    async def fail_before(_context):
        raise ValueError("before_run failed")

    monkeypatch.setattr(environment.runner._runtime_events, "before_run", fail_before)
    try:
        with pytest.raises(ValueError, match="before_run failed"):
            asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        previous = environment._session_checkpoint_writer
        assert previous.retired and previous.owner.finalized
        monkeypatch.setattr(environment.runner._runtime_events, "before_run", original_before)
        result = asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert result.status.value == "completed_unverified"
        assert environment._session_checkpoint_writer is not previous
    finally:
        environment.close()


def test_factory_failure_does_not_activate_owner_and_closed_environment_stays_closed(monkeypatch, tmp_path):
    from nz_coder.runtime.adapters import runner as adapter

    _install_runtime(monkeypatch, object())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    original_snapshot = adapter.LegacySnapshotRuntime

    def broken_snapshot(_host):
        raise ValueError("focused port construction failed")

    async def scenario():
        context = await environment.runtime_services.session_runtime.open(request)
        monkeypatch.setattr(adapter, "LegacySnapshotRuntime", broken_snapshot)
        with pytest.raises(ValueError, match="focused port construction failed"):
            environment._native_execution_context(context, environment.runtime_services)
        assert environment._session_checkpoint_writer is None
        monkeypatch.setattr(adapter, "LegacySnapshotRuntime", original_snapshot)
        execution = environment._native_execution_context(context, environment.runtime_services)
        await execution.lifecycle.settle_session_checkpoints()
        environment._abort_run_preparation(context)
        environment.runtime_services.session_runtime.abandon(context)
        environment.close()
        successor = await environment.runtime_services.session_runtime.open(request)
        with pytest.raises(RuntimeError, match="closed environment"):
            environment._native_execution_context(successor, environment.runtime_services)
        environment.runtime_services.session_runtime.abandon(successor)

    try:
        asyncio.run(scenario())
    finally:
        environment.close()


@pytest.mark.parametrize("execution_error", [False, True])
def test_real_legacy_override_waits_for_its_writer(monkeypatch, tmp_path, execution_error):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.execution.runner import AgentRunner
    from nz_coder.runtime.session.session_processor import SessionProcessor

    _install_runtime(monkeypatch, object())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    entered, release = threading.Event(), threading.Event()
    original_write = sessions._publish_session_json
    captured = {}

    def write(prepared, path):
        if threading.current_thread().name == "legacy-override-writer" and path.name == f"{request.session_id}.json":
            entered.set()
            assert release.wait(5)
        return original_write(prepared, path)

    async def override(messages, *_args):
        assistant = {"role": "assistant", "content": "legacy answer"}
        attach_message_identity(assistant, session_id=request.session_id)
        messages.append(assistant)
        environment._active_session_processor = SessionProcessor(assistant)
        environment._active_processor_messages = messages
        observer = environment._model_gateway_observer_for_run()
        scope = copy_context()
        worker = threading.Thread(target=lambda: scope.run(observer, "model_call_retry", {
            "purpose": "coding", "attempt": 1, "error": "legacy retry",
        }), name="legacy-override-writer")
        captured["worker"] = worker
        worker.start()
        assert await asyncio.to_thread(entered.wait, 3)
        if execution_error:
            raise ValueError("legacy primary failure")
        return {"status": "completed", "content": "legacy answer"}

    environment._run = override
    monkeypatch.setattr(sessions, "_publish_session_json", write)

    async def scenario():
        task = asyncio.create_task(AgentRunner().run(environment, list(request.messages), stream=False))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            await asyncio.sleep(0.02)
            assert not task.done()
        finally:
            release.set()
            if execution_error:
                with pytest.raises(ValueError, match="legacy primary failure"):
                    await task
            else:
                assert (await task)["status"] == "completed"
            await asyncio.to_thread(captured["worker"].join, 3)
        assert environment._session_checkpoint_writer.retired
        assert _payload(tmp_path, request)["run_status"] == ("error" if execution_error else "completed")

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        environment.close()


def test_legacy_override_default_image_usage_has_its_owner(monkeypatch, tmp_path):
    from nz_coder.runtime.execution.runner import AgentRunner

    _install_runtime(monkeypatch, object())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())

    async def override(_messages, *_args):
        assert environment.image_describer.observer is not None
        environment.image_describer.observer("model_call_finish", {
            "purpose": "vision", "usage": {"input": 13, "output": 5},
        })
        return {"status": "completed", "content": "legacy image answer"}

    environment._run = override
    try:
        result = asyncio.run(AgentRunner().run(environment, list(request.messages), stream=False))
        assert result["status"] == "completed"
        assert environment._session_checkpoint_writer.owner.usage.input_tokens == 13
        assert environment._session_checkpoint_writer.owner.usage.output_tokens == 5
    finally:
        environment.close()


def test_new_run_retry_before_first_processor_cannot_reuse_old_processor(monkeypatch, tmp_path):
    from nz_coder.protocol.message_schema import attach_message_identity
    from nz_coder.runtime.session.session_processor import SessionProcessor

    _install_runtime(monkeypatch, object())
    request = _durable_request(tmp_path)
    environment = native_sdk.build_product_run_environment(request, RunOptions())

    async def scenario():
        runtime = environment.runtime_services.session_runtime
        first = await runtime.open(request)
        execution = environment._native_execution_context(first, environment.runtime_services)
        assistant = {"role": "assistant", "content": "old processor"}
        attach_message_identity(assistant, session_id=request.session_id)
        first.transcript.append(assistant)
        processor = SessionProcessor(assistant)
        execution.messages.bind_active_processor(processor, first.transcript)
        await execution.lifecycle.settle_session_checkpoints()
        environment._abort_run_preparation(first)
        runtime.abandon(first)
        second = await runtime.open(request)
        next_execution = environment._native_execution_context(second, environment.runtime_services)
        before = copy.deepcopy(assistant)
        environment._gateway().observer("model_call_retry", {
            "purpose": "coding", "attempt": 9, "error": "new preflight retry",
        })
        assert assistant == before
        await next_execution.lifecycle.settle_session_checkpoints()
        environment._abort_run_preparation(second)
        runtime.abandon(second)

    try:
        asyncio.run(scenario())
    finally:
        environment.close()
