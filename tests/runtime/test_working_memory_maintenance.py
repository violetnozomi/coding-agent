"""专用整理请求的协议、费用入口、任务边界与原子提交。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nz_coder.runtime.conversation.working_memory import maintain_working_memory
from nz_coder.runtime.conversation.model_result import LLMResult, ModelCompletionError
from nz_coder.runtime.model_gateway import ModelCallOutcome, ModelCallPurpose
from nz_coder.state.context import PromptBudget
from nz_coder.tools.scratchpad import Scratchpad


def _completed(**kwargs):
    return ModelCallOutcome.completed(**{"finish_reason": "stop", **kwargs})


def _setup(tmp_path, reply, *, callback=lambda: None, hard=100000, soft=None, inflight=0):
    pad = Scratchpad(tmp_path / "notes.json")
    pad.update_section('focus', [{"summary": "OLD_FACT", "details": ""}])
    for _ in range(25):
        pad.complete_step(pad.revision)
    calls, events = [], []

    async def complete(call):
        calls.append(call)
        callback()
        return reply

    gateway = SimpleNamespace(complete=complete, runtime=SimpleNamespace(inflight_status=lambda: {
        "worker_still_running": inflight, "inflight_calls": inflight, "tainted": bool(inflight)}))
    model = SimpleNamespace(
        gateway=lambda **kw: gateway,
        prompt_budget=lambda: PromptBudget(hard + 262144, 262144, hard,
            max(0, hard - 100) if soft is None else soft, 0, 0, 0, False),
        capabilities=lambda: None,
        project_outcome=lambda _: LLMResult(aborted=True),
        trace=lambda event, **kw: events.append((event, kw)),
    )

    async def run(*, superseded=lambda: False, interval=25, history=None, current_facts=lambda: "", projected=100):
        await maintain_working_memory(model, pad, task={"message_id": "task-one", "content": "build"},
            history=history if history is not None else [{"role": "user", "content": "build"}], projected_tokens=projected,
            current_facts=current_facts,
            interval=interval, superseded=superseded)

    return pad, calls, events, run


@pytest.mark.parametrize("payload, expected", [
    ({"status": "replace", "memory": {'focus': [{"summary": "MODEL_FACT", "details": ""}], 'findings': [], 'decisions': [], 'lessons': []}}, "MODEL_FACT"),
    ({"status": "replace", "memory": {'focus': [], 'findings': [], 'decisions': [], 'lessons': []}}, "Scratchpad is empty."),
    ({"status": "unchanged"}, "OLD_FACT"),
    ({"status": "unable", "reason": "insufficient evidence"}, "OLD_FACT"),
])
def test_model_result_and_next_window(tmp_path, payload, expected):
    pad, calls, events, run = _setup(tmp_path, _completed(content=json.dumps(payload)))
    asyncio.run(run())
    assert expected in Scratchpad(tmp_path / "notes.json").read()
    assert pad.reminder_reason(25, False) == ""
    assert len(calls) == 1
    assert calls[0].purpose is ModelCallPurpose.MEMORY
    assert calls[0].tools == ()
    assert calls[0].max_output_tokens == 262144
    assert calls[0].metadata["operation"] == "working_memory_maintenance"
    assert events


@pytest.mark.parametrize("reply", [
    _completed(content="not JSON"),
    _completed(content='{"status":"replace"}'),
    _completed(content='{"status": "replace", "memory": {"focus": [], "findings": [], "decisions": [], "lessons": []}}', finish_reason="length"),
    _completed(content='{"status": "replace", "memory": {"focus": [], "findings": [], "decisions": [], "lessons": []}}', finish_reason="error"),
    _completed(content='{"status": "replace", "memory": {"focus": [], "findings": [], "decisions": [], "lessons": []}}', finish_reason="tool_calls"),
    _completed(content='{"status": "replace", "memory": {"focus": [], "findings": [], "decisions": [], "lessons": []}}', tool_calls=[{"id": "bad"}]),
])
def test_invalid_response_preserves_old_notes_without_immediate_retry(tmp_path, reply):
    pad, calls, _, run = _setup(tmp_path, reply)
    asyncio.run(run())
    asyncio.run(run())
    assert len(calls) == 1
    assert "OLD_FACT" in pad.read()


@pytest.mark.parametrize("hard,interval", [(1, 25), (100000, 0)])
def test_budget_and_disabled_checks_do_not_send(tmp_path, hard, interval):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'), hard=hard)
    asyncio.run(run(interval=interval))
    assert calls == []
    assert pad.reminder_reason(25, False) == "periodic"


def test_schedule_save_failure_never_sends(monkeypatch, tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'))
    monkeypatch.setattr(pad, "_persist_state", lambda *_: "disk full")
    asyncio.run(run())
    assert calls == []
    assert pad.reminder_reason(25, False) == "periodic"


def test_followup_during_call_does_not_commit(tmp_path):
    state = {"queued": False}
    pad, calls, _, run = _setup(tmp_path, _completed(content=json.dumps({"status": "replace", "memory": {'focus': [{"summary": "LATE", "details": ""}], 'findings': [], 'decisions': [], 'lessons': []}})),
        callback=lambda: state.update(queued=True))
    asyncio.run(run(superseded=lambda: state["queued"]))
    assert len(calls) == 1
    assert "OLD_FACT" in pad.read() and "LATE" not in pad.read()


def test_gateway_failure_is_not_softened(tmp_path):
    _, calls, _, run = _setup(tmp_path, ModelCallOutcome.aborted("budget stopped"))
    with pytest.raises(ModelCompletionError):
        asyncio.run(run())
    assert len(calls) == 1


def test_revision_changed_while_awaiting_keeps_latest_notes(tmp_path):
    pad, _, _, run = _setup(tmp_path, _completed(content=json.dumps({"status": "replace", "memory": {'focus': [{"summary": "STALE", "details": ""}], 'findings': [], 'decisions': [], 'lessons': []}})),
        callback=lambda: pad.update_section('focus', [{"summary": "NEWER", "details": ""}]))
    asyncio.run(run())
    assert "NEWER" in pad.read() and "STALE" not in pad.read()


@pytest.mark.parametrize("cancel_source", ["task", "run"])
def test_pending_cancel_before_commit_keeps_old_notes(tmp_path, cancel_source):
    from nz_coder.runtime.core.run_context import RunContext, scoped_run_context
    from nz_coder.runtime.session.model import Session
    from test_native_product_runtime import _request

    request = _request(tmp_path)
    owner = RunContext(request, Session.create(request.session_id, [], workspace=tmp_path), request.agent.name)

    def cancel():
        if cancel_source == "task":
            asyncio.current_task().cancel()
        else:
            owner.request_cancel()

    pad, calls, events, run = _setup(tmp_path, _completed(content=json.dumps({
        "status": "replace",
        "memory": {"focus": [{"summary": "LATE", "details": ""}], "findings": [], "decisions": [], "lessons": []},
    })), callback=cancel)
    before = pad.snapshot()
    with scoped_run_context(owner), pytest.raises(asyncio.CancelledError):
        asyncio.run(run())
    assert pad.snapshot() == Scratchpad(tmp_path / "notes.json").snapshot() == before
    assert len(calls) == 1
    assert [event["status"] for _, event in events] == ["started", "cancelled"]


def test_soft_pressure_runs_once_until_completed_step_observes_relief(tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'), soft=50)
    asyncio.run(run())
    pad.complete_step(pad.revision, under_pressure=True)
    asyncio.run(run())
    assert len(calls) == 1
    pad.complete_step(pad.revision, under_pressure=False)
    asyncio.run(run())
    assert len(calls) == 2


def test_full_notes_overflow_skips_without_slicing_or_consuming_window(tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'), hard=1000)
    pad.update_section('focus', [{"summary": "valuable material " * 2000, "details": ""}])
    for _ in range(25):
        pad.complete_step(pad.revision)
    before = pad.read()
    asyncio.run(run())
    assert calls == []
    assert pad.read() == before
    assert pad.reminder_reason(25, False) == "periodic"


def test_unsettled_provider_never_starts_second_request(tmp_path):
    from nz_coder.protocol.public_error import PublicRuntimeError

    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'), inflight=1)
    with pytest.raises(PublicRuntimeError, match="resources have not exited"):
        asyncio.run(run())
    assert calls == []
    assert pad.reminder_reason(25, False) == "periodic"


@pytest.mark.parametrize("case", json.loads((Path(__file__).parent / "fixtures" /
    "working_memory_actual_requests.json").read_text()), ids=lambda case: str(case["original_request_index"]))
def test_original_26_52_material_is_visible_and_model_result_persists(tmp_path, case):
    # 使用真实材料验证传递；预制回复仅验证链路，不证明模型会选取正确事实。
    note = f"MODEL_NOTE_FOR_ORIGINAL_STEP_{case['original_request_index']}"
    pad, calls, _, run = _setup(tmp_path, _completed(content=json.dumps({"status": "replace", "memory": {'focus': [{"summary": note, "details": ""}], 'findings': [], 'decisions': [], 'lessons': []}})))
    asyncio.run(run(history=case["messages"]))
    payload = json.loads(calls[0].messages[1]["content"])
    original_output = case["messages"][1]["content"]
    assert any(row.get("role") == "tool" and row.get("content") == original_output
               for row in payload["visible_transcript"])
    assert note in pad.read()
    assert note in Scratchpad(tmp_path / "notes.json").read()


def test_existing_memory_is_last_and_present_once_with_atomic_revision(tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'))
    asyncio.run(run())
    content = calls[0].messages[1]["content"]
    data = json.loads(content)
    assert list(data) == ["current_task", "current_facts", "visible_transcript", "existing_memory"]
    assert content.count("OLD_FACT") == 1
    assert data["existing_memory"] == pad.memory


def test_old_notes_response_is_not_a_memory_write(tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content=
        '{"status":"replace","notes":[{"category":"plan","content":"WRONG_FORMAT"}]}'))
    revision = pad.revision
    asyncio.run(run())
    assert len(calls) == 1
    assert pad.revision == revision
    assert "OLD_FACT" in pad.read() and "WRONG_FORMAT" not in pad.read()


def test_due_maintenance_receives_current_facts_once_without_rewriting_notes(tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'))
    before = pad.read()
    observed = []

    def facts():
        observed.append(True)
        return "CURRENT_GENERATION_9: previous test pass is historical; inspect handoff remains pending."

    asyncio.run(run(current_facts=facts))
    assert len(observed) == 1 and len(calls) == 1
    data = json.loads(calls[0].messages[1]["content"])
    assert data["current_facts"] == "CURRENT_GENERATION_9: previous test pass is historical; inspect handoff remains pending."
    assert calls[0].messages[1]["content"].count("CURRENT_GENERATION_9") == 1
    assert pad.read() == before


@pytest.mark.parametrize("early_exit", ["not_due", "disabled", "superseded", "main_budget"])
def test_maintenance_fast_exit_does_not_evaluate_current_facts(tmp_path, early_exit):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'))
    if early_exit == "not_due":
        pad.update_section("focus", [{"summary": "Recently refreshed reason", "details": ""}])
    observed = []
    asyncio.run(run(current_facts=lambda: observed.append(True) or "facts",
                    interval=0 if early_exit == "disabled" else 25,
                    superseded=lambda: early_exit == "superseded",
                    projected=100001 if early_exit == "main_budget" else 100))
    assert not observed and not calls


def test_current_facts_participate_in_complete_maintenance_budget(tmp_path):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'), hard=1000)
    before = pad.read()
    observed = []
    asyncio.run(run(current_facts=lambda: observed.append(True) or "事实" * 10000))
    assert observed == [True] and not calls
    assert pad.read() == before and pad.reminder_reason(25, False) == "periodic"


@pytest.mark.parametrize("interruption", ["followup", "cancel"])
def test_interruption_while_collecting_facts_does_not_send_or_replace(tmp_path, interruption):
    pad, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'))
    queued = []
    before = pad.read()

    def facts():
        if interruption == "followup":
            queued.append(True)
        else:
            asyncio.current_task().cancel()
        return "facts obtained before interruption"

    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run(current_facts=facts))
    else:
        asyncio.run(run(current_facts=facts, superseded=lambda: bool(queued)))
    assert not calls and pad.read() == before


def test_failed_maintenance_keeps_existing_handoff_invalidation(tmp_path):
    from test_execution_handoff import _case, _submit
    from nz_coder.runtime.conversation.continuation_context import build_current_facts

    state, messages, call, result = _case(tmp_path)
    assert not _submit(tmp_path, state, messages, call, result).dispatch_failed
    (tmp_path / "api.py").write_text("new external version")
    pad, calls, _, run = _setup(tmp_path, _completed(content="bad output"))
    before = pad.read()
    asyncio.run(run(current_facts=lambda: build_current_facts(state, workspace=tmp_path)))
    assert state.execution_handoff["status"] == "stale"
    assert len(calls) == 1 and pad.read() == before
    assert "<execution-handoff>" not in json.loads(calls[0].messages[1]["content"])["current_facts"]
    assert state.changed_files_verified is False


def test_already_cancelled_maintenance_does_not_collect_facts(tmp_path):
    _, calls, _, run = _setup(tmp_path, _completed(content='{"status":"unchanged"}'))
    observed = []

    async def cancel_then_run():
        asyncio.current_task().cancel()
        await run(current_facts=lambda: observed.append(True) or "facts")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(cancel_then_run())
    assert not observed and not calls
