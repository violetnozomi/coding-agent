"""Controlled evaluation scenarios must use the current request pipeline."""
from __future__ import annotations

import json
from pathlib import Path

from nz_coder.evaluation.native_scenario import run_native_agent_scenario
from nz_coder.runtime.conversation.model_result import LLMResult


class _PromptEchoModel:
    async def complete_turn(self, _context, messages, **_kwargs):
        return LLMResult(content=json.dumps(messages), finish_reason="stop")


class _NoTools:
    async def execute_batch_async(self, *_args, **_kwargs):
        raise AssertionError("prompt-only scenario must not execute tools")


def test_native_scenario_projects_declared_instructions_and_clean_history(tmp_path: Path):
    run = run_native_agent_scenario(
        tmp_path,
        prompt="Inspect the refresh path",
        model=_PromptEchoModel(),
        tools=_NoTools(),
        max_turns=2,
        tool_names=(),
        session_id="native-prompt-boundary",
    )

    assert run["result"]["status"] == "completed_unverified"
    assert json.loads(run["result"]["content"]) == [
        {"role": "system", "content": "solve the repository task"},
        {"role": "user", "content": "Inspect the refresh path"},
    ]
