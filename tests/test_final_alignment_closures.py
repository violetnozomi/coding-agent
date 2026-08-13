"""Regression coverage for the final terminal/Provider/Workflow closure chains."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


def test_attempt_controller_uses_single_pre_boundary_fallback_then_retries():
    from nz_coder.runtime.agent_resilience import ProviderAttemptController

    controller = ProviderAttemptController(max_retries=2)
    first = controller.decide(
        TimeoutError("stream stalled"),
        attempt=1,
        streaming=True,
        stable_boundary=False,
        retryable=True,
    )
    second = controller.decide(
        TimeoutError("stream stalled"),
        attempt=1,
        streaming=True,
        stable_boundary=False,
        retryable=True,
    )
    stable = ProviderAttemptController(max_retries=0).decide(
        TimeoutError("stream stalled"),
        attempt=1,
        streaming=True,
        stable_boundary=True,
        retryable=True,
    )

    assert first.action == "non_streaming_fallback"
    assert second.action == "retry"
    assert stable.action == "abort"


def test_stream_watchdog_detects_idle_and_observes_cancellation():
    import time
    from nz_coder.runtime.loop import _iter_completion_with_timeouts

    def stalled():
        time.sleep(0.1)
        yield "late"

    with pytest.raises(TimeoutError, match="Stream stalled"):
        list(_iter_completion_with_timeouts(
            stalled(), idle_timeout_seconds=0.01, hard_timeout_seconds=1
        ))

    assert list(_iter_completion_with_timeouts(
        stalled(),
        idle_timeout_seconds=1,
        hard_timeout_seconds=1,
        cancelled=lambda: True,
    )) == []


def test_agent_stream_failure_falls_back_once_to_buffered_provider(tmp_path):
    from nz_coder.providers.capabilities import resolve_model_capabilities
    from nz_coder.runtime.loop import AgentLoop
    from nz_coder.runtime.workdir import scoped_workdir

    class Provider:
        name = "fake"

        def __init__(self):
            self.requests = []

        def create_client(self):
            return object()

        def capabilities(self, model_id):
            return resolve_model_capabilities("fake", model_id)

        def create_completion(self, _client, **kwargs):
            self.requests.append(kwargs)
            if kwargs.get("stream"):
                def broken():
                    if False:
                        yield None
                    raise TimeoutError("stream stalled")
                return broken()
            message = SimpleNamespace(
                content="buffered recovery",
                tool_calls=[],
                reasoning_content="",
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=None,
            )

    provider = Provider()
    with scoped_workdir(tmp_path):
        agent = AgentLoop("system", provider=provider, trace_enabled=False)
    try:
        result = agent._call_llm(
            [{"role": "user", "content": "hello"}], stream=True
        )
    finally:
        agent.close()

    assert result.content == "buffered recovery"
    assert result.attempts == 2
    assert [bool(item.get("stream")) for item in provider.requests] == [True, False]


def test_provider_workflow_generation_repairs_and_times_out():
    from nz_coder.runtime.workflow_generation import generate_workflow_with_provider

    outputs = iter([
        "not-json",
        json.dumps({
            "action": "generate",
            "pattern": "fan-out-and-synthesize",
            "request": "inspect routing",
            "options": {"agents": 2},
            "approval_summary": "Two investigators and synthesis.",
        }),
    ])
    prompts = []

    def generate(_system, prompt):
        prompts.append(prompt)
        return next(outputs)

    result = generate_workflow_with_provider("inspect routing", generate)

    assert result["kind"] == "generated"
    assert result["attempts"] == 2
    assert "failed validation" in prompts[1]

    with pytest.raises(TimeoutError, match="timed out"):
        generate_workflow_with_provider(
            "slow",
            lambda _system, _prompt: __import__("time").sleep(0.2) or "{}",
            timeout_seconds=0.01,
        )


def test_workflow_sdk_publishes_first_started_and_terminal_result(
    tmp_path, monkeypatch
):
    from nz_coder.runtime.workflow_sdk import WorkflowHostSDK
    from tests.test_workflow_runtime import _install_fake_child, _manager

    _install_fake_child(monkeypatch, tmp_path)
    manager = _manager(tmp_path, monkeypatch, max_tasks=2, concurrency=1)
    plan = {
        "manifest": {
            "name": "sdk-run",
            "description": "SDK lifecycle test",
            "phases": ["inspect"],
            "read_only": True,
            "planned_agents": 1,
            "max_agents": 1,
            "max_concurrency": 1,
            "patterns": ["classify-and-act"],
        },
        "phases": [{
            "name": "inspect",
            "mode": "parallel",
            "tasks": [{"prompt": "inspect", "read_only": True}],
        }],
    }

    handle = WorkflowHostSDK(manager).start(
        plan=plan,
        approval_decision="approve",
    )
    started = handle.wait_started(timeout=2)
    result = handle.wait(timeout=3)

    assert started["run_id"] == handle.run_id
    assert started["status"] == "running"
    assert not str(result).startswith("Error:")
    assert manager.workflow_run_snapshots()[0]["status"] == "completed"


def test_manager_rehydrates_and_fails_orphaned_workflow_identity(
    tmp_path, monkeypatch
):
    from tests.test_workflow_runtime import _manager

    first = _manager(tmp_path, monkeypatch)
    first.begin_workflow_run("workflow-orphan123", "orphan")
    first.record_workflow_event(
        "workflow_run_started",
        data={"run_id": "workflow-orphan123", "name": "orphan"},
    )

    restored = _manager(tmp_path, monkeypatch)
    snapshot = next(
        item for item in restored.workflow_run_snapshots()
        if item["run_id"] == "workflow-orphan123"
    )

    assert snapshot["status"] == "failed"
    assert "process restart" in snapshot["error"]
    lifecycle = restored._workflow.workflow_run_lifecycles()
    assert next(item for item in lifecycle if item["run_id"] == "workflow-orphan123")["status"] == "failed"


def test_terminal_registry_and_safe_live_smoke_entrypoints(capsys):
    from nz_coder.interface.commands import default_command_registry
    from nz_coder.mcp.cli import mcp_main

    assert "workflow" in {
        item.name for item in default_command_registry.visible_commands()
    }
    assert mcp_main(["smoke", "example"]) == 0
    assert "Dry run only" in capsys.readouterr().out

    from nz_coder.runtime.workflow_contracts import workflow_contract

    contract = workflow_contract()
    assert contract["version"] == "1.6"
    assert contract["managed_run_semantics"]["sdk_first_started"] is True
    assert contract["generation_semantics"]["provider_orchestrated"] is True


def test_terminal_workflow_approval_renderer_returns_typed_decision():
    from nz_coder.interface.interactions import TerminalInteractionBridge

    class Terminal:
        async def select_async(self, **kwargs):
            assert kwargs["title"] == "Workflow approval"
            assert "Risk: may write files" in kwargs["text"]
            return "approve"

    class Renderer:
        def pause(self):
            return None

        def resume(self):
            return None

    async def exercise():
        import asyncio

        bridge = TerminalInteractionBridge(
            Terminal(), Renderer(), asyncio.get_running_loop()
        )
        return await bridge._ask_workflow_approval({
            "name": "review",
            "description": "Review changes",
            "phases": ["inspect", "verify"],
            "planned_agents": 2,
            "max_concurrency": 2,
            "writes_files": True,
        })

    import asyncio

    assert asyncio.run(exercise()) == "approve"


def test_swebench_manifest_is_secret_free_and_source_bound(tmp_path):
    from nz_coder.evaluation.reproducibility import (
        build_swebench_manifest,
        write_reproducibility_manifest,
    )

    manifest = build_swebench_manifest(
        run_id="run-1",
        dataset="princeton-nlp/SWE-bench_Lite",
        split="test",
        instance_ids=["repo__issue-1"],
        model_name="nz-coder",
        provider="fake",
        model_id="model",
        max_agent_turns=80,
        agent_timeout_seconds=900,
    )
    target = write_reproducibility_manifest(tmp_path / "run.manifest.json", manifest)
    loaded = json.loads(target.read_text(encoding="utf-8"))

    assert len(loaded["source_sha256"]) == 64
    assert loaded["instance_ids"] == ["repo__issue-1"]
    assert "api_key" not in json.dumps(loaded).lower()
