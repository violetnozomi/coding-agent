"""Main compatibility facade delegates one-way into the native Runner API."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from nz_coder.runtime.core.request import RunOptions, RunRequest
from nz_coder.runtime.execution.loop import AgentLoop


class _Runner:
    def __init__(self) -> None:
        self.call = None

    async def run(self, request, *, options):
        self.call = (request, options)
        return {"status": "completed", "content": "done"}


class _RuntimeHost:
    async def run(self, owner, messages, *, execute, **kwargs):
        return await execute(
            owner,
            messages,
            kwargs.get("on_tool"),
            kwargs.get("on_text"),
            kwargs.get("on_token"),
            kwargs.get("stream", True),
        )


def test_main_facade_builds_native_request_before_entering_runner(tmp_path: Path):
    owner = object.__new__(AgentLoop)
    owner.runner = _Runner()
    owner.runtime_host = _RuntimeHost()
    owner.runtime_services = object()
    owner.runtime_profile = "coding"
    owner.current_agent_name = "coder"
    owner.agent_id = "coder"
    owner.system_prompt = "Fix the repository."
    owner.tool_allowlist = None
    owner.permissions = type("Permissions", (), {"mode": "default"})()
    owner.parent_session_id = None
    owner.provider_id = "test"
    owner.model_id = "test-model"
    owner.model_variant = None
    owner.workdir = tmp_path
    owner.session_id = "main-native"
    owner.active_run_context = SimpleNamespace(
        transcript=[{"role": "assistant", "content": "done"}],
    )
    messages = [{"role": "user", "content": "fix it"}]

    result = asyncio.run(AgentLoop.run(owner, messages, stream=False))

    assert result["status"] == "completed"
    request, options = owner.runner.call
    assert isinstance(request, RunRequest)
    assert isinstance(options, RunOptions)
    assert request.session_id == "main-native"
    assert request.messages[0]["content"] == "fix it"
    assert options.stream is False
    assert messages == [{"role": "assistant", "content": "done"}]
    assert owner.active_run_context is None
