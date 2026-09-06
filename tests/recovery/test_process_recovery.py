"""Kill a real file-tool worker and capture a fresh process's first request."""
from __future__ import annotations

import asyncio
import copy
import html
import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace

import pytest


def _write_until_boundary(root, confirmed, channel):
    from nz_coder.runtime.execution.tool_executor import ToolExecutor
    from nz_coder.runtime.process.checkpoint_runtime import recovery_run, register_batch
    from nz_coder.state.sessions import save_session
    from nz_coder.state.workdir import scoped_workdir
    from nz_coder.tools import files  # noqa: F401

    class Permissions:
        def check(self, name, arguments):
            return {"behavior": "allow"}

    root = Path(root)
    user = {"role": "user", "content": "Write marker.txt, then verify its content.", "_nz_message_id": "msg-user"}
    call = {"id": "call-crash", "type": "function", "function": {
        "name": "write_file", "arguments": {"path": "marker.txt", "content": "written"}}}
    with scoped_workdir(root), recovery_run(root, "session-crash") as run:
        save_session([user], session_id="session-crash", activate=False)
        messages = [user, {"role": "assistant", "content": "", "_nz_message_id": "msg-step"}]
        run.attach("interaction-before-death", messages)
        register_batch(messages, [call])
        original = run.ledger.confirm_mutation

        def pause(operation, after):
            if confirmed:
                original(operation, after)
            channel.send("file-published")
            channel.recv()

        run.ledger.confirm_mutation = pause
        ToolExecutor(Permissions()).execute_one(call, 0)


def _resume_and_capture(root, channel):
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.core.profiles import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.sdk import AgentClient

    captured = []

    class OfflineProvider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            captured.append(copy.deepcopy(kwargs["messages"]))
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Execution is uncertain. Verification remains outstanding.", tool_calls=[]),
                finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5))

    runtime = ResolvedModelRuntime(provider_id="offline", model_id="offline-model", request_model_id="offline-model",
                                   variant=None, provider=OfflineProvider(), client=object(),
                                   capabilities=ModelCapabilities(provider="offline", model_id="offline-model", supports_streaming=False))
    native_sdk.resolve_model_runtime = lambda _request: runtime
    request = RunRequest(agent=AgentDefinition("recovery", "Describe recovery facts.", allowed_tools=()),
                         profile=MAIN_PROFILE, messages=({"role": "user", "content": "Continue; first report recovery facts."},),
                         workspace=Path(root), session_id="session-crash", stream=False,
                         metadata={"permission_mode": "auto"})
    asyncio.run(AgentClient().run(request))
    channel.send(captured[0])


@pytest.mark.parametrize("confirmed", [False, True])
def test_killed_real_tool_reaches_first_request_in_new_process(tmp_path, confirmed):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    writer = context.Process(target=_write_until_boundary, args=(str(tmp_path), confirmed, child))
    writer.start()
    try:
        assert parent.poll(20), "writer did not reach explicit publish boundary"
        assert parent.recv() == "file-published"
        writer.kill()
        writer.join(10)
        assert writer.exitcode != 0
    finally:
        if writer.is_alive():
            writer.kill()
            writer.join(10)
        parent.close()
        child.close()
    assert (tmp_path / "marker.txt").read_text() == "written"
    parent, child = context.Pipe()
    reader = context.Process(target=_resume_and_capture, args=(str(tmp_path), child))
    reader.start()
    try:
        assert parent.poll(30), "fresh runtime did not capture its first Provider request"
        messages = parent.recv()
        reader.join(10)
        assert reader.exitcode == 0
    finally:
        if reader.is_alive():
            reader.kill()
            reader.join(10)
        parent.close()
        child.close()
    text = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
    facts = [json.loads(html.unescape(line)) for line in text.splitlines() if line.startswith('{"')]
    fact = next(item for item in facts if item.get("call_id") == "call-crash")
    assert fact["execution_state"] == "uncertain" and fact["terminal_cause"] == "process_lost"
    assert fact["side_effect_state"] == ("committed" if confirmed else "unknown")
    assert fact["files"][0]["path"] == "marker.txt"
    assert bool(fact["files"][0].get("after_ref")) == confirmed
    assert "verify" in text.lower() and "result_summary" not in fact
    assert not any(m.get("role") == "tool" for m in messages)
