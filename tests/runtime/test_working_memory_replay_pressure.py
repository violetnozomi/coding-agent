"""离线 Provider 验证回放压力下的真实 Runner 链路，不代表模型语义收益。"""
from __future__ import annotations

import asyncio
import copy
import json
import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.conversation.context_manager import replay_compaction_pressure
from nz_coder.runtime.conversation.working_memory import maintain_working_memory
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.model_gateway import ModelCallOutcome
from nz_coder.state.context import PromptBudget
from nz_coder.tools.scratchpad import Scratchpad
from tests.fixtures.compaction_summary import summary_json
from test_native_product_runtime import _install_runtime, _request
from test_working_memory_reminders import _call, _reply


SUMMARY = "Python 3.13: close active clients before waiting for server shutdown; recheck after API changes."
DETAILS = "Waiting first blocked until idle_timeout; reproduced with two connected clients."
BASIS = "close-path reproduction, active_clients=2, Python 3.13"
MEMORY = {
    "focus": [{"summary": "Retest shutdown with active clients.", "details": "The read-only investigation is complete."}],
    "findings": [{"summary": SUMMARY, "details": DETAILS, "certainty": "confirmed", "basis": BASIS}],
    "decisions": [{"summary": "Close clients first to release shutdown waiters.", "details": "Do not increase idle_timeout to hide the wait."}],
    "lessons": [{"summary": "A timeout-free idle shutdown does not verify shutdown with active clients.", "details": "Repeat the two-client reproduction after changing the close path."}],
}


def _kind(kwargs):
    system = kwargs["messages"][0]["content"]
    if system.startswith("Maintain working notes"):
        return "memory"
    if system.startswith("You are a context summarization specialist."):
        return "compaction"
    return "coding"


def _environment(monkeypatch, tmp_path, provider, *, stream=False, interval=25, replay=9000):
    runtime = _install_runtime(monkeypatch, provider)
    runtime.capabilities = replace(runtime.capabilities, supports_streaming=stream)
    snapshot = load_config_snapshot(tmp_path, environ={
        "NZ_CONTEXT_REPLAY_COMPACTION_TOKENS": str(replay),
        "NZ_WORKING_MEMORY_REMINDER_STEPS": str(interval),
        "MAX_AGENT_TURNS": "10",
    })
    options = RunOptions(permission_asker=lambda *_: True, config_snapshot=snapshot)
    request = replace(_request(tmp_path), stream=stream, session_id=tmp_path.name)
    env = native_sdk.build_product_run_environment(request, options, config_snapshot=snapshot)
    return env, request, options


@pytest.mark.parametrize("stream", [False, True])
def test_native_replay_pressure_preserves_conditional_notes_before_compaction(monkeypatch, tmp_path, stream):
    # 足够大的两个独立读取使旧证据超出近期尾部；摘要回复故意不复述该发现。
    for index in range(2):
        (tmp_path / f"evidence-{index}.txt").write_text(
            SUMMARY + "\n" + (f"observations for source {index}: " + "detailed evidence " * 50 + "\n") * 200,
        )
    requests = []

    class Provider:
        name = "offline"
        coding = 0

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            kind = _kind(kwargs)
            if kind == "memory":
                return _reply(kwargs, text=json.dumps({"status": "replace", "memory": MEMORY}))
            if kind == "compaction":
                return _reply(kwargs, text=summary_json("Continue read-only investigation."))
            self.coding += 1
            if self.coding == 1:
                return _reply(kwargs, calls=[
                    _call(10, "update_scratchpad", {"section": "focus", "entries": [
                        {"summary": "OLD_FOCUS", "details": "Preserve existing notes in the maintenance input."},
                    ]}),
                    *[_call(index, "read_file", {"path": f"evidence-{index}.txt"}) for index in range(2)],
                ])
            return _reply(kwargs, text="Inspection complete.")

    env, request, options = _environment(monkeypatch, tmp_path, Provider(), stream=stream)
    freezes = []
    freeze = env._freeze_api_request

    def capture_freeze(rows):
        frozen = freeze(rows)
        freezes.append(frozen)
        return frozen

    monkeypatch.setattr(env, "_freeze_api_request", capture_freeze)
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert [_kind(row) for row in requests] == ["coding", "memory", "compaction", "coding"]
        maintenance = requests[1]
        assert not maintenance.get("tools") and not maintenance.get("stream")
        evidence = json.loads(maintenance["messages"][1]["content"])
        assert SUMMARY in str(evidence["visible_transcript"])
        assert evidence["existing_memory"]["focus"][0]["summary"] == "OLD_FOCUS"
        dynamic = requests[-1]["messages"][-1]["content"]
        for section in MEMORY.values():
            for entry in section:
                assert entry["summary"] in dynamic
        assert DETAILS not in dynamic and BASIS not in dynamic
        assert "OLD_FOCUS" not in dynamic
        assert DETAILS in env._sp.read() and BASIS in env._sp.read()
        assert len(freezes) == 2
        assert freezes[-1].working_memory_revision == env._sp.revision == 2
        assert env._sp.completed_steps == 2
        # 本场景只提供编码与记忆模型，没有独立交付评审。
        verification = env.verification_runtime.domain.snapshot()
        assert not verification.reviews and verification.assess_completion("completed").review_required
        assert result.status.value == "completed_unverified"
        assert not any("detailed evidence" in str(row.get("content", ""))
                       for row in requests[-1]["messages"])
    finally:
        env.close()


def _maintenance_model():
    calls = []

    async def complete(call):
        calls.append(call)
        return ModelCallOutcome.completed(content='{"status":"unchanged"}', finish_reason="stop")

    gateway = SimpleNamespace(complete=complete, runtime=SimpleNamespace(
        inflight_status=lambda: {"inflight_calls": 0, "tainted": False}))
    budget = PromptBudget(11000, 1000, 10000, 9000, 0, 0, 0, False)
    model = SimpleNamespace(prompt_budget=lambda: budget, capabilities=lambda: None,
                            gateway=lambda **_: gateway, trace=lambda *_a, **_k: None)
    return model, calls


async def _maintain(model, pad, *, under_pressure=None, projected=100):
    await maintain_working_memory(
        model, pad, task={"message_id": "task", "content": "inspect shutdown"},
        history=[{"role": "user", "content": "inspect shutdown"}],
        projected_tokens=projected, interval=25, superseded=lambda: False,
        under_pressure=under_pressure, current_facts=lambda: "",
    )


@pytest.mark.parametrize("explicit,projected,expected_calls", [
    (None, 100, 0), (None, 9001, 1), (True, 100, 1), (False, 9001, 0),
])
def test_explicit_pressure_and_default_soft_pressure_before_periodic_window(explicit, projected, expected_calls):
    model, calls = _maintenance_model()
    pad = Scratchpad(persistent=False)
    asyncio.run(_maintain(model, pad, under_pressure=explicit, projected=projected))
    assert len(calls) == expected_calls
    assert pad.completed_steps == 0 and pad.revision == 0


def test_pressure_window_reopens_after_settled_step_observes_relief():
    model, calls = _maintenance_model()
    pad = Scratchpad(persistent=False)

    async def scenario():
        await _maintain(model, pad, under_pressure=True)
        pad.complete_step(pad.revision, under_pressure=True)
        await _maintain(model, pad, under_pressure=True)
        assert len(calls) == 1
        pad.complete_step(pad.revision, under_pressure=False)
        await _maintain(model, pad, under_pressure=True)

    asyncio.run(scenario())
    assert len(calls) == 2


def test_replay_pressure_does_not_override_hard_input_budget():
    model, calls = _maintenance_model()
    pad = Scratchpad(persistent=False)
    asyncio.run(_maintain(model, pad, under_pressure=True, projected=10001))
    assert calls == [] and pad.reminder_reason(25, True) == "pressure"


@pytest.mark.parametrize("limit,measurement,expected", [
    (0, 1000, False), (100, 100, False), (100, 101, True),
    (100, None, False), (100, True, False), (100, -1, False), (100, "error", False),
])
def test_replay_pressure_uses_valid_history_estimate_and_existing_threshold(limit, measurement, expected):
    def project(_rows):
        if measurement == "error":
            raise ValueError("offline metric failure")
        return measurement

    context = SimpleNamespace(
        budget=SimpleNamespace(replay_compaction_tokens=limit),
        projected_replay_tokens=project, trace=lambda *_a, **_k: None,
    )
    assert replay_compaction_pressure(context, []) is expected


@pytest.mark.parametrize("interval,replay", [(0, 50), (25, 0)])
def test_native_disabled_memory_or_replay_does_not_add_maintenance(monkeypatch, tmp_path, interval, replay):
    (tmp_path / "evidence.txt").write_text("Independent observation " * 100)
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                return _reply(kwargs, calls=[_call(1, "read_file", {"path": "evidence.txt"})])
            return _reply(kwargs, text="Inspection complete.")

    env, request, options = _environment(monkeypatch, tmp_path, Provider(), interval=interval, replay=replay)
    monkeypatch.setattr(env, "_compact_messages", lambda rows: copy.deepcopy(rows))
    try:
        asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert [_kind(row) for row in requests] == ["coding", "coding"]
    finally:
        env.close()


@pytest.mark.parametrize("interruption", ["followup", "cancel"])
def test_native_replay_maintenance_interruption_never_installs_or_continues(monkeypatch, tmp_path, interruption):
    (tmp_path / "evidence.txt").write_text("Independent observation " * 100)
    entered, release = threading.Event(), threading.Event()
    queued = {"value": False}
    requests, compactions = [], []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            if _kind(kwargs) == "memory":
                if interruption == "followup":
                    queued["value"] = True
                else:
                    entered.set()
                    assert release.wait(5)
                return _reply(kwargs, text=json.dumps({"status": "replace", "memory": MEMORY}))
            return _reply(kwargs, calls=[_call(1, "read_file", {"path": "evidence.txt"})])

    env, request, options = _environment(monkeypatch, tmp_path, Provider(), replay=50)
    env.set_followup_pending(lambda: queued["value"])

    def compact(rows):
        compactions.append(1)
        return rows

    monkeypatch.setattr(env, "_compact_messages", compact)

    async def scenario():
        task = asyncio.create_task(native_sdk.NativeSDKRunner(env).run_result(request, options))
        if interruption == "cancel":
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
            finally:
                release.set()
        return await task

    try:
        result = asyncio.run(scenario())
        assert result.status.value == ("cancelled" if interruption == "cancel" else "interrupted")
        assert [_kind(row) for row in requests] == ["coding", "memory"]
        assert not compactions
        assert env._sp.read() == "Scratchpad is empty."
        assert env._sp.completed_steps == 1
    finally:
        release.set()
        env.close()


@pytest.mark.parametrize("reply", [
    '{"status":"unchanged"}', '{"status":"unable","reason":"insufficient evidence"}',
    'invalid JSON', 'save_failure',
])
def test_native_unrelieved_replay_pressure_does_not_maintain_every_step(monkeypatch, tmp_path, reply):
    for index in range(3):
        (tmp_path / f"evidence-{index}.txt").write_text("A separate observation " * 100)
    requests = []

    class Provider:
        name = "offline"
        coding = 0

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            if _kind(kwargs) == "memory":
                text = json.dumps({"status": "replace", "memory": MEMORY}) if reply == "save_failure" else reply
                return _reply(kwargs, text=text)
            self.coding += 1
            if self.coding <= 3:
                return _reply(kwargs, calls=[_call(self.coding, "read_file", {"path": f"evidence-{self.coding - 1}.txt"})])
            return _reply(kwargs, text="Inspection complete.")

    if reply == "save_failure":
        persist = Scratchpad._persist_state

        def fail_memory_save(self, session_id, state):
            return "disk full" if state.revision else persist(self, session_id, state)

        monkeypatch.setattr(Scratchpad, "_persist_state", fail_memory_save)
    env, request, options = _environment(monkeypatch, tmp_path, Provider(), replay=50)
    # 无节省候选走真实 accept_compaction 拒绝；只替代昂贵的摘要模型调用。
    monkeypatch.setattr(env, "_compact_messages", lambda rows: copy.deepcopy(rows))
    try:
        result = asyncio.run(native_sdk.NativeSDKRunner(env).run_result(request, options))
        assert [_kind(row) for row in requests].count("memory") == 1
        assert [_kind(row) for row in requests].count("coding") == 4
        assert env._sp.read() == "Scratchpad is empty."
        assert env._sp.completed_steps == 4
        assert env._sp.reminder_reason(25, True) == ""
        # 本场景只提供编码与记忆模型，没有独立交付评审。
        verification = env.verification_runtime.domain.snapshot()
        assert not verification.reviews and verification.assess_completion("completed").review_required
        assert result.status.value == "completed_unverified"
    finally:
        env.close()
