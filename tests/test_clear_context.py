"""/clear 必须切断旧历史回灌，而非仅清空终端列表。"""
from __future__ import annotations

import asyncio
import copy
import io
import json
from dataclasses import replace

import pytest
from rich.console import Console

from nz_coder.interface.commands.handlers.core import handle_clear
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.interface.session_controller import TerminalSessionController
from nz_coder.runtime.conversation.message_projection import project_provider_messages
from nz_coder.runtime.core.request import RunOptions
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.state import sessions
from tests.runtime.test_native_product_runtime import _completion, _install_runtime, _request


@pytest.mark.parametrize("persist_session", [True, False])
def test_clear_does_not_reintroduce_old_task_on_next_activation(
    monkeypatch, tmp_path, persist_session,
):
    model_inputs = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            model_inputs.append(copy.deepcopy(kwargs["messages"]))
            return _completion("The new task is ready.")

    _install_runtime(monkeypatch, Provider())
    request = replace(
        _request(tmp_path),
        messages=({"role": "user", "content": "OLD_TASK_SECRET"},),
        metadata={"permission_mode": "auto", "persist_session": persist_session},
    )
    environments = []

    def build_agent(_prompt, _renderer, session_id, *, permission_mode):
        environment = native_sdk.build_product_run_environment(
            replace(request, session_id=session_id), RunOptions(),
        )
        environments.append(environment)
        return environment

    environment = build_agent("sys", None, request.session_id, permission_mode="auto")
    history = []
    ctx = CommandContext(
        history=history,
        session_state={
            "id": request.session_id,
            "agent": environment,
            "controller": TerminalSessionController(environment),
        },
        system_prompt="sys", renderer=None,
        console=Console(file=io.StringIO()), build_agent=build_agent,
    )

    async def scenario():
        runtime = environment.runtime_services.session_runtime
        previous = await runtime.open(request)
        previous.transcript.append({"role": "assistant", "content": "OLD_FINDING_SECRET"})
        await runtime.finalize(previous, RunStatus.COMPLETED_UNVERIFIED)
        history[:] = copy.deepcopy(previous.transcript)
        with scoped_workdir(tmp_path):
            checkpoint = sessions.session_runtime_state_path(request.session_id)
            environment.runtime_state.initial_task_text = "OLD_CHECKPOINT_TASK"
            environment.runtime_state.save(checkpoint, active=False)
            saved_checkpoint = checkpoint.read_bytes()
            old_session_path = sessions.session_dir() / f"{request.session_id}.json"
            old_session_bytes = old_session_path.read_bytes() if persist_session else None
            from nz_coder.tools.scratchpad import Scratchpad
            from nz_coder.tools import todo

            old_pad = Scratchpad.for_user_message(
                request.session_id, previous.transcript[0]["_nz_message_id"],
            )
            assert not old_pad.update_section("findings", [{
                "summary": "OLD_MEMORY_SECRET", "details": "", "certainty": "confirmed",
                "basis": "old-task-investigation",
            }]).startswith("Error:")
            with sessions.scoped_session(request.session_id):
                assert "OLD_TODO_SECRET" in todo.todo_update([
                    {"content": "OLD_TODO_SECRET", "status": "pending"},
                ])
            retained_paths = [old_pad._path, sessions.session_todo_path(request.session_id)]
            retained = {path: path.read_bytes() for path in retained_paths}

            handle_clear(ctx)
        assert history == []
        assert ctx.session_id != request.session_id
        assert ctx.controller.session_id == ctx.session_id
        assert ctx.controller.environment is ctx.agent
        assert ctx.agent.permissions.mode == "auto"
        assert ctx.agent.runtime_state.initial_task_text == ""
        assert not ctx.agent._runtime_state_path.exists()
        assert checkpoint.read_bytes() == saved_checkpoint
        if persist_session:
            assert old_session_path.read_bytes() == old_session_bytes
        assert all(path.read_bytes() == content for path, content in retained.items())

        old_session = await runtime.store.load(SessionIdentity(request.session_id), tmp_path)
        assert "OLD_FINDING_SECRET" in json.dumps(old_session.transcript)

        # 使用清空后的真实会话入口，包含存储恢复与提供方历史投影。
        current_runtime = ctx.agent.runtime_services.session_runtime
        following = await current_runtime.open(replace(
            request, session_id=ctx.session_id,
            messages=({"role": "user", "content": "NEW_TASK_ONLY"},),
        ))
        try:
            projected = json.dumps(project_provider_messages(following.transcript))
            assert "NEW_TASK_ONLY" in projected
            assert "OLD_TASK_SECRET" not in projected
            assert "OLD_FINDING_SECRET" not in projected
        finally:
            current_runtime.abandon(following)

        result = await native_sdk.NativeSDKRunner(ctx.agent).run_result(replace(
            request, session_id=ctx.session_id,
            messages=({"role": "user", "content": "Explain NEW_TASK_ONLY briefly."},),
        ))
        assert result.status in {RunStatus.COMPLETED, RunStatus.COMPLETED_UNVERIFIED}
        assert model_inputs
        visible = json.dumps(model_inputs)
        for old_content in ("OLD_TASK_SECRET", "OLD_FINDING_SECRET", "OLD_CHECKPOINT_TASK",
                            "OLD_MEMORY_SECRET", "OLD_TODO_SECRET"):
            assert old_content not in visible
        assert "NEW_TASK_ONLY" in visible

        # 旧会话仍能通过原恢复入口取回，清空不会把它的笔记删掉。
        if persist_session:
            from nz_coder.interface.commands.handlers.core import handle_resume

            with scoped_workdir(tmp_path):
                ctx.args = request.session_id
                handle_resume(ctx)
            assert ctx.session_id == request.session_id
            assert "OLD_FINDING_SECRET" in json.dumps(history)
            assert "OLD_MEMORY_SECRET" in old_pad.read()

    try:
        asyncio.run(scenario())
    finally:
        for owned in environments:
            owned.close()


def test_clear_build_failure_preserves_current_session(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from nz_coder.interface.commands.handlers import core

    old_agent = SimpleNamespace(
        permissions=SimpleNamespace(mode="default"), has_pending_settlement=False,
    )
    history = [{"role": "user", "content": "OLD_TASK_SECRET"}]
    state = {"id": "clear-original", "agent": old_agent}

    def fail_build(*_args, **_kwargs):
        raise RuntimeError("model environment unavailable")

    ctx = CommandContext(
        history=history, session_state=state, system_prompt="sys", renderer=None,
        console=Console(file=io.StringIO()), build_agent=fail_build,
    )
    monkeypatch.setattr(core, "create_session_id", lambda: "clear-candidate")
    with scoped_workdir(tmp_path):
        sessions.activate_session("clear-original")
        with pytest.raises(RuntimeError, match="model environment unavailable"):
            handle_clear(ctx)
        assert sessions.active_session_id() == "clear-original"
        active = json.loads((sessions.session_dir() / "active.json").read_text())
        assert active["session_id"] == "clear-original"
    assert state == {"id": "clear-original", "agent": old_agent}
    assert history == [{"role": "user", "content": "OLD_TASK_SECRET"}]


def test_clear_keeps_unsaved_frozen_result_available_for_retry(monkeypatch, tmp_path):
    from nz_coder.protocol.public_error import PublicRuntimeError

    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            return _completion("Original frozen answer.")

    _install_runtime(monkeypatch, Provider())
    request = replace(_request(tmp_path), metadata={"permission_mode": "auto", "persist_session": True})
    environment = native_sdk.build_product_run_environment(request, RunOptions())
    controller = TerminalSessionController(environment)
    history = list(request.messages)
    created = []

    def build_agent(*_args, **_kwargs):
        created.append(True)
        raise AssertionError("a pending result must be saved before replacing its owner")

    ctx = CommandContext(
        history=history,
        session_state={"id": request.session_id, "agent": environment, "controller": controller},
        system_prompt="sys", renderer=None,
        console=Console(file=io.StringIO()), build_agent=build_agent,
    )
    publisher = sessions._publish_session_json
    fault = True

    def publish(prepared, path):
        data = json.loads(prepared.read_text())
        if fault and path.name == request.session_id + ".json" and data.get("run_status") in {
            "completed", "completed_unverified",
        }:
            raise OSError("terminal snapshot cannot be saved")
        return publisher(prepared, path)

    monkeypatch.setattr(sessions, "_publish_session_json", publish)
    try:
        with pytest.raises(OSError, match="terminal snapshot"):
            asyncio.run(native_sdk.NativeSDKRunner(environment).run_result(request))
        assert environment.has_pending_settlement
        with scoped_workdir(tmp_path):
            with pytest.raises(PublicRuntimeError) as caught:
                handle_clear(ctx)
        assert caught.value.public_error.code == "session_commit_pending"
        assert not created
        assert ctx.agent is environment and ctx.session_id == request.session_id
        assert history == list(request.messages)
        fault = False
        settled = asyncio.run(controller._client.retry_settlement(tmp_path, request.session_id))
        assert settled.state == "committed"
        durable = asyncio.run(environment.runtime_services.session_runtime.store.load(
            SessionIdentity(request.session_id), tmp_path,
        ))
        assert "Original frozen answer." in durable.transcript[-1]["content"]
        assert len(calls) == 1
    finally:
        environment.close()
