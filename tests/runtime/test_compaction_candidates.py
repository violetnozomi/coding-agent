"""A summary is installed only if it improves the frozen outbound request."""
from __future__ import annotations

import asyncio
import copy
import threading
from pathlib import Path

import pytest

from nz_coder.runtime.conversation.context_manager import (
    CompactionAttemptState,
    ProductionContextManager,
)
from nz_coder.runtime.conversation.request_snapshot import FrozenRequestSnapshot, message_version
from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.state.context import PromptBudget


def _frame(messages, system=""):
    return FrozenRequestSnapshot(system, "", "", message_version(messages), copy.deepcopy, ())


def _context(tmp_path: Path, messages, compact, *, usable=100, replay=0, system=""):
    frame = _frame(messages, system)
    stamped = []
    context = ContextExecutionContext(
        workspace=tmp_path,
        budget=PromptBudget(usable + 20, 20, usable, usable * 8 // 10, 0, 0, 0, False,
                            replay_compaction_tokens=replay),
        projected_tokens=frame.estimate,
        projected_replay_tokens=frame.estimate,
        compact=compact,
        stamp_auto_compaction=lambda value: stamped.append(copy.deepcopy(value)),
        trace=lambda *_args, **_kwargs: None,
        freeze_request=_frame,
    )
    return context, frame, stamped


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("candidate", [
    [{"role": "user", "content": "old source " * 100}],
    [{"role": "user", "content": "bigger source " * 200}],
    [{"role": "user", "content": "still too long " * 60}],
])
def test_physical_recovery_rejects_no_shrink_or_still_over_budget(tmp_path, asynchronous, candidate):
    messages = [{"role": "user", "content": "Fix the issue."},
                {"role": "assistant", "content": "old source " * 100}]
    original = copy.deepcopy(messages)
    context, frame, stamped = _context(tmp_path, messages, lambda _rows: copy.deepcopy(candidate))
    manager = ProductionContextManager()

    with pytest.raises(RuntimeError, match="cannot.*(shrink|fit)|cannot.*reduc"):
        if asynchronous:
            asyncio.run(manager.prepare_async(context, messages, request_snapshot=frame))
        else:
            manager.prepare_sync(context, messages, request_snapshot=frame)
    assert messages == original
    assert stamped == []


def test_candidate_must_fit_with_fixed_instructions_included(tmp_path):
    messages = [{"role": "user", "content": "Fix the issue."},
                {"role": "assistant", "content": "old source " * 100}]
    context, frame, stamped = _context(
        tmp_path, messages, lambda _rows: [{"role": "user", "content": "small"}],
        system="Fixed instructions " * 100,
    )
    with pytest.raises(RuntimeError, match="cannot.*(fit|reduc)"):
        ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)
    assert stamped == []


def test_replay_candidate_without_savings_keeps_history_and_is_suppressed(tmp_path):
    messages = [{"role": "user", "content": "source " * 100}]
    original = copy.deepcopy(messages)
    calls = []

    def compact(rows):
        calls.append(1)
        return rows

    context, frame, stamped = _context(tmp_path, messages, compact, usable=1000, replay=50)
    state = CompactionAttemptState()
    manager = ProductionContextManager()
    assert manager.prepare_sync(context, messages, attempt_state=state, request_snapshot=frame) is False
    assert manager.prepare_sync(context, messages, attempt_state=state, request_snapshot=frame) is False
    assert messages == original
    assert calls == [1]
    assert stamped == []


def test_candidate_is_not_installed_after_source_changes(tmp_path):
    messages = [{"role": "user", "content": "source " * 100}]

    def compact(rows):
        assert rows is not messages
        messages.append({"role": "user", "content": "New constraint: keep public APIs."})
        return [{"role": "user", "content": "small"}]

    context, frame, stamped = _context(tmp_path, messages, compact)
    with pytest.raises(RuntimeError, match="changed"):
        ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)
    assert messages[-1]["content"] == "New constraint: keep public APIs."
    assert stamped == []


def test_cancellation_waits_for_summary_but_never_installs_it(tmp_path):
    from dataclasses import replace

    messages = [{"role": "user", "content": "Fix the issue."},
                {"role": "assistant", "content": "old observations " * 100}]
    original = copy.deepcopy(messages)
    started = threading.Event()
    released = threading.Event()

    def compact(_rows):
        started.set()
        assert released.wait(2)
        return [{"role": "user", "content": "small"}]

    context, frame, stamped = _context(tmp_path, messages, compact)
    context = replace(context, cancel_compaction=released.set)

    async def exercise():
        task = asyncio.create_task(ProductionContextManager().prepare_async(
            context, messages, request_snapshot=frame,
        ))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert messages == original
    assert stamped == []


def test_pending_processor_does_not_hide_latest_provider_usage(tmp_path):
    from dataclasses import replace

    owner = {"role": "assistant", "content": "", "_nz_message_id": "msg-active"}
    messages = [
        {"role": "user", "content": "Fix the issue."},
        {"role": "assistant", "content": "prior findings " * 20,
         "_nz_usage": {"input": 1200, "output": 10}},
        owner,
    ]
    context, frame, stamped = _context(
        tmp_path, messages, lambda _rows: [{"role": "user", "content": "summary"}],
        usable=1000,
    )
    frame = replace(frame, retained=(owner,))
    assert ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)
    assert messages[-1] is owner
    assert len(stamped) == 1


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("failure", ["invalid_json", "length", "request", "budget"])
def test_cost_only_summary_failure_keeps_history_and_suppresses_retry(
    tmp_path, monkeypatch, asynchronous, failure,
):
    from dataclasses import replace
    from nz_coder.state import context as context_module
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state.sessions import scoped_session
    from tests.fixtures.compaction_summary import summary_json
    from tests.test_context_budget import _CompactionClient

    class Client(_CompactionClient):
        calls = 0

        def create(self, **kwargs):
            self.calls += 1
            if failure == "request":
                raise RuntimeError("request entity too large; private provider detail")
            response = super().create(**kwargs)
            response.choices[0].finish_reason = "length" if failure == "length" else "stop"
            return response

    monkeypatch.setattr(context_module, "_get_git_diff_summary", lambda: "")
    client = Client('{"goal":' if failure == "invalid_json" else summary_json())
    messages = [{"role": "user", "content": "Fix the parser"},
                {"role": "assistant", "content": "investigation " * 100}]
    original = copy.deepcopy(messages)
    events = []

    def compact(rows):
        return context_module.auto_compact(rows, client, "fake-model",
            budget=context_module.prompt_budget(2000, 1000),
            current_facts="necessary fact " * 2000 if failure == "budget" else "")

    context, frame, stamped = _context(tmp_path, messages, compact, usable=10_000, replay=50)
    context = replace(context, trace=lambda name, **data: events.append((name, data)))
    state = CompactionAttemptState()
    manager = ProductionContextManager()
    with scoped_workdir(tmp_path), scoped_session("optional-summary-failure"):
        for _ in range(2):
            if asynchronous:
                result = asyncio.run(manager.prepare_async(context, messages, attempt_state=state, request_snapshot=frame))
            else:
                result = manager.prepare_sync(context, messages, attempt_state=state, request_snapshot=frame)
            assert result is False
    assert client.calls == (0 if failure == "budget" else 1)
    assert messages == original
    assert stamped == []
    assert state.suppressed_replay_version == message_version(original)
    assert sum(name == "compaction_generation_failed" for name, _ in events) == 1
    assert "private provider detail" not in str(events)


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("error", [OSError("archive write failed"), RuntimeError("unexpected host bug"),
                                    asyncio.CancelledError()])
def test_cost_only_compaction_does_not_swallow_storage_bugs_or_cancellation(tmp_path, asynchronous, error):
    messages = [{"role": "user", "content": "source " * 100}]

    def compact(_rows):
        raise error

    context, frame, _stamped = _context(tmp_path, messages, compact, usable=1000, replay=50)
    with pytest.raises(type(error)):
        if asynchronous:
            asyncio.run(ProductionContextManager().prepare_async(context, messages, request_snapshot=frame))
        else:
            ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_failed_cost_summary_still_reports_concurrent_source_change(tmp_path, asynchronous):
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError

    messages = [{"role": "user", "content": "source " * 100}]

    def compact(_rows):
        messages.append({"role": "user", "content": "new constraint"})
        raise PublicRuntimeError(PublicError("invalid_compaction_summary", "invalid summary"))

    context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    with pytest.raises(PublicRuntimeError) as caught:
        if asynchronous:
            asyncio.run(ProductionContextManager().prepare_async(context, messages, request_snapshot=frame))
        else:
            ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)
    assert caught.value.public_error.code == "context_changed"


def test_failed_cost_summary_does_not_hide_run_cancellation(tmp_path):
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
    from nz_coder.runtime.core.run_context import RunContext, scoped_run_context
    from nz_coder.runtime.session.model import Session, SessionIdentity
    from tests.runtime.test_native_product_runtime import _request

    messages = [{"role": "user", "content": "source " * 100}]
    run = RunContext(_request(tmp_path), Session(SessionIdentity("summary-cancel"), tmp_path, []), "product")
    def compact(_rows):
        run.request_cancel()
        raise PublicRuntimeError(PublicError("invalid_compaction_summary", "invalid summary"))

    context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    with scoped_run_context(run), pytest.raises(asyncio.CancelledError):
        ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)


def test_provider_reported_overflow_cannot_use_cost_failure_fallback(tmp_path):
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError

    messages = [{"role": "user", "content": "source"},
                {"role": "assistant", "content": "small", "_nz_usage": {"input": 2000, "output": 1}}]
    def compact(_rows):
        raise PublicRuntimeError(PublicError("invalid_compaction_summary", "invalid summary"))

    context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=1)
    with pytest.raises(PublicRuntimeError, match="invalid summary"):
        ProductionContextManager().prepare_sync(context, messages, request_snapshot=frame)


def test_usable_coding_response_renews_attempts_after_optional_failure(tmp_path):
    from nz_coder.protocol.public_error import PublicError, PublicRuntimeError
    from nz_coder.runtime.conversation.model_result import LLMResult

    messages = [{"role": "user", "content": "source " * 100}]
    def compact(_rows):
        raise PublicRuntimeError(PublicError("invalid_compaction_summary", "invalid summary"))

    context, frame, _ = _context(tmp_path, messages, compact, usable=1000, replay=50)
    state = CompactionAttemptState()
    assert ProductionContextManager().prepare_sync(context, messages, attempt_state=state, request_snapshot=frame) is False
    assert state.without_response == 1
    state.observe_response(LLMResult(content="new evidence", finish_reason="stop"))
    assert state.without_response == 0
    assert state.suppressed_replay_version == ""
    assert state.attempts == 1
