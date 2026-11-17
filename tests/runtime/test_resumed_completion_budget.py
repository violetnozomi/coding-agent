"""新用户激活重新获得收尾指导，同时保留任务事实和累计用量。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import threading
import time
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.http_service import NZCoderClient, SessionHTTPService, SessionManager
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.runtime.verification.domain.checkpoint import restore_domain
from nz_coder.runtime.verification.domain.requirements import Requirement, TaskContract
from tests.runtime.test_native_product_runtime import _install_runtime, _request, _runtime
from tests.verification_scenarios import VerificationScenario


@pytest.mark.parametrize("active", [False, True])
def test_new_activation_preserves_failed_execution_and_usage_while_reopening_guidance(tmp_path, active):
    (tmp_path / "module.py").write_text("def broken(:\n")
    contract = TaskContract(objective="Repair module and write docs", requirements=(
        Requirement("R1", "Write README.md", "docs", ("README.md",), "deterministic"),),
        acceptance_commands=("python -m py_compile module.py",))
    scenario = VerificationScenario(contract, workspace=tmp_path, plan={"stages": []})
    scenario.mutate(13, ["module.py"])
    scenario.settle_shell("python -m py_compile module.py")
    before = scenario.domain.snapshot()
    assert len(before.obligations) == 1 and not before.obligations[0].resolved
    failure_ids = before.obligations[0].failure_execution_ids
    state = scenario.state
    state.completion_gate_prompts = 2
    state.completion_gate_signature = "13|R1"
    state.provider_calls = 7
    state.provider_attempts = 8
    state.provider_cost_usd = 1.25
    state.provider_usage = {"input": 100, "output": 20, "total": 120,
                            "reasoning": 0, "cache_read": 0, "cache_write": 0}
    checkpoint = tmp_path / "runtime.json"
    state.save(checkpoint, active=active)

    resumed = RuntimeState()
    assert resumed.load(checkpoint, allow_inactive=True)
    assert resumed.completion_gate_prompts == 2
    resumed.begin_resumed_activation(max_turns=80, timeout_seconds=90)

    assert resumed.completion_gate_prompts == 0
    assert resumed.completion_gate_signature == ""
    assert resumed.changed_files == ["module.py"]
    assert resumed.mutation_generation == 13
    assert resumed.provider_calls == 7 and resumed.provider_attempts == 8
    assert resumed.provider_cost_usd == 1.25
    assert resumed.provider_usage == {"input": 100, "output": 20, "total": 120,
                                      "reasoning": 0, "cache_read": 0, "cache_write": 0}
    domain = restore_domain(resumed._restored_verification_domain, lock=threading.RLock(),
        scope=replace(scenario.domain.scope, interaction_run_id="resumed"), version=before.version)
    after = domain.snapshot()
    assert after.obligations[0].failure_execution_ids == failure_ids
    assert not after.obligations[0].resolved
    assert after.decision(after.obligations[0].check_id).status == "failed"
    assert after.requirements[0].requirement == contract.requirements[0]
    assert not after.ready_for_completion


@pytest.mark.parametrize("restart_service", [False, True])
def test_http_continue_can_repair_after_previous_guidance_budget_exhausted(tmp_path, monkeypatch, restart_service):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    for setting in ("PLANNING_ENABLED", "REFLECTION_ENABLED"):
        monkeypatch.setattr(config, setting, False)
    workspace = tmp_path / "project"
    workspace.mkdir()

    class Provider:
        name = "offline"
        phase = 0
        calls = [0, 0]

        def create_completion(self, _client, **kwargs):
            names = [item["function"]["name"] for item in kwargs.get("tools", [])]
            tool = None
            content = "The documentation is complete."
            if names == ["emit_sidecar_verdict"]:
                tool = ("emit_sidecar_verdict", {"verdict": "accept", "reason": "Controlled offline review"})
            else:
                self.calls[self.phase] += 1
                assert self.calls[self.phase] <= 3, "unexpected extra coding request"
                if self.phase == 1 and self.calls[1] == 2:
                    tool = ("write_file", {"path": "README.md", "content": "# Project\nLocal usage instructions.\n"})
            calls = [] if tool is None else [SimpleNamespace(id=f"call-{self.phase}-{self.calls[self.phase]}",
                type="function", function=SimpleNamespace(name=tool[0], arguments=json.dumps(tool[1])))]
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                content="" if calls else content, tool_calls=calls), finish_reason="tool_calls" if calls else "stop")],
                usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3))

    provider = Provider()
    runtime = _install_runtime(monkeypatch, provider)
    monkeypatch.setattr("nz_coder.runtime.model_gateway.resolve_model_runtime", lambda *_a, **_k: runtime)

    def start(restore):
        manager = SessionManager(workspace_roots=[workspace], restore_saved=restore)
        service = SessionHTTPService(port=0, manager=manager, token="resume-budget-offline")
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        return service, thread, NZCoderClient(service.base_url, service.token, timeout=3)

    def run_to_settlement(service, client, session_id, prompt):
        session = service.manager.get(session_id)
        events = session.event_bus.subscribe()
        client.run(session_id, prompt, model="offline/offline-model")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            event = events.get(timeout=max(.01, deadline - time.monotonic()))
            if event.type == "permission.asked":
                assert event.properties["permission"] == "write_file"
                assert event.properties["tool_input"]["path"] == "README.md"
                assert client.reply_permission(session_id, event.properties["id"], "once")
            if event.type == "session.run.settled":
                break
        assert session.wait(1)
        return client.get_session(session_id)

    service, thread, client = start(False)
    try:
        wid = next(item["id"] for item in client.list_workspaces() if item["path"] == str(workspace))
        sid = client.create_session("default", workspace_id=wid)["id"]
        first = run_to_settlement(service, client, sid, "Create README.md documenting local project usage.")
        assert first["status"] == "blocked"
        assert provider.calls[0] == 3
        assert not (workspace / "README.md").exists()
        if restart_service:
            service.shutdown()
            thread.join(5)
            assert not thread.is_alive()
            service, thread, client = start(True)
        provider.phase = 1
        second = run_to_settlement(service, client, sid, "Continue the unfinished task.")
        # 该场景只写文档，没有已采信的验证执行；续跑成功不等于验证通过。
        assert second["status"] == "completed_unverified", second["last_result"]
        assert provider.calls[1] == 3
        assert (workspace / "README.md").read_text() == "# Project\nLocal usage instructions.\n"
    finally:
        service.shutdown()
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.parametrize("new_user_message", [False, True])
def test_lifecycle_refreshes_guidance_only_for_unconsumed_user_message(tmp_path, monkeypatch, new_user_message):
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.core.execution_context import command_activation
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.state import sessions

    monkeypatch.setattr(native_sdk, "resolve_model_runtime", lambda *_: _runtime())
    messages = [
        {"role": "user", "content": "Create README.md"},
        {"role": "assistant", "content": "stopped", "_nz_continuation": {
            "status": "blocked", "summary": "## Latest User Instruction\nCreate README.md"}},
        {"role": "user", "content": "Continue the unfinished task."},
        {"role": "assistant", "content": "README still needs work."},
        {"role": "user", "content": "<requirement-completion-gate>Finish README.</requirement-completion-gate>",
         "_nz_synthetic": True, "_nz_completion_gate": True},
    ]
    if new_user_message:
        messages.append({"role": "user", "content": "继续"})
    request = replace(_request(tmp_path), messages=tuple(messages),
                      metadata={"permission_mode": "auto", "persist_session": True})
    environment = native_sdk.build_product_run_environment(request, native_sdk.RunOptions())
    try:
        context = asyncio.run(environment.runtime_services.session_runtime.open(request))
        environment._native_execution_context(context, environment.runtime_services)
        state = environment.runtime_state
        state.completion_gate_prompts = 2
        state.completion_gate_signature = "5|R1"
        state.provider_calls = 7
        state.provider_cost_usd = 1.25
        with scoped_workdir(tmp_path), sessions.scoped_session(request.session_id):
            state.save(sessions.session_runtime_state_path(request.session_id), active=True)
            environment._init_run(messages, False)
        assert state.completion_gate_prompts == (0 if new_user_message else 2)
        assert state.completion_gate_signature == ("" if new_user_message else "5|R1")
        assert state.provider_calls == 7 and state.provider_cost_usd == 1.25
        assert command_activation(messages).resume_activation is new_user_message
    finally:
        environment.close()
