"""SDK production assembly must execute via focused owners, not host policy facades."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.execution.loop import ProductRunEnvironment
from nz_coder.runtime.model_gateway import ResolvedModelRuntime
from nz_coder.runtime.tool_runtime.pipeline import ProductionToolRuntime
from nz_coder.runtime.execution.tool_effects import (
    ToolResultRecorder,
    ToolTransaction,
    CodingWriteEffects,
)
from nz_coder.runtime.session.tool_progress import ToolSessionBoundary
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.session.model import SessionIdentity
from nz_coder.sdk import AgentClient
from nz_coder.state.tool_ledger import ToolLedger


def test_real_sdk_tools_never_use_migrated_private_host_rules(monkeypatch, tmp_path):
    calls_seen = []
    contexts = []
    active = set()

    class FakeProvider:
        name = "offline"

        def __init__(self, workspace):
            self.workspace = workspace
            self.calls = 0

        def create_completion(self, _client, **kwargs):
            self.calls += 1
            calls_seen.append(self.workspace)
            if self.calls == 1:
                tools = [
                    SimpleNamespace(
                        id="call-native-write",
                        type="function",
                        function=SimpleNamespace(
                            name="write_file",
                            arguments=json.dumps(
                                {"path": "result.txt", "content": self.workspace.name}
                            ),
                        ),
                    )
                ]
            else:
                tools = []
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content="" if tools else "File created.", tool_calls=tools
                        ),
                        finish_reason="tool_calls" if tools else "stop",
                    )
                ],
                usage=SimpleNamespace(
                    prompt_tokens=3, completion_tokens=2, total_tokens=5
                ),
            )

    def resolve(request):
        return ResolvedModelRuntime(
            provider_id="offline",
            model_id="offline-model",
            request_model_id="offline-model",
            variant=None,
            provider=FakeProvider(request.workspace),
            client=object(),
            capabilities=ModelCapabilities(
                provider="offline", model_id="offline-model", supports_streaming=False
            ),
        )

    monkeypatch.setattr(
        "nz_coder.runtime.execution.native_sdk.resolve_model_runtime", resolve
    )
    original_factory = ProductRunEnvironment.tool_execution_context

    def factory(environment, *args, **kwargs):
        context = original_factory(environment, *args, **kwargs)
        recorder = context.projection.record_result.__self__
        effects = context.lifecycle.observer._post_write.__self__
        assert recorder.run_evidence is environment.run_evidence
        assert recorder.facts is environment.tool_run_facts
        assert effects.change_tracker is environment.change_tracker
        return context

    monkeypatch.setattr(ProductRunEnvironment, "tool_execution_context", factory)
    original_batch = ProductionToolRuntime.execute_batch_async

    async def batch(runtime, context, *args, **kwargs):
        assert isinstance(context.lifecycle.transaction, ToolTransaction)
        assert isinstance(context.lifecycle.checkpoint.__self__, ToolSessionBoundary)
        recorder = context.projection.record_result.__self__
        assert isinstance(recorder, ToolResultRecorder)
        assert isinstance(
            context.lifecycle.observer._post_write.__self__, CodingWriteEffects
        )
        assert context.run is not None
        assert context.lifecycle.dispatch_override_async is None
        assert context.lifecycle.dispatch_override_sync is None
        assert context.lifecycle.consume_override is None
        contexts.append(context)
        active.add(context.run.interaction_run_id)
        try:
            return await original_batch(runtime, context, *args, **kwargs)
        finally:
            active.remove(context.run.interaction_run_id)

    monkeypatch.setattr(ProductionToolRuntime, "execute_batch_async", batch)
    migrated = (
        "_record_tool_result",
        "_finish_tool_transaction",
        "_refresh_patch_risk",
        "_refresh_code_index",
        "_attach_lsp_write_diagnostics",
        "_trace_tool_result",
        "_execute_tool_call_with_hooks",
        "_strict_verification_completed",
        "_apply_pending_plan_mode",
        "_record_step_patch",
        "_processor_for_latest_assistant",
        "_tool_metadata_callback",
        "_question_lifecycle_callback",
    )
    for name in migrated:
        previous = getattr(ProductRunEnvironment, name)

        def forbidden(owner, *args, _name=name, _previous=previous, **kwargs):
            assert not active, f"Native tools called migrated host rule {_name}"
            return _previous(owner, *args, **kwargs)

        monkeypatch.setattr(ProductRunEnvironment, name, forbidden)

    workspaces = [tmp_path / "first", tmp_path / "second"]
    for workspace in workspaces:
        workspace.mkdir()

    async def run(workspace):
        return await AgentClient().run(
            RunRequest(
                agent=AgentDefinition(
                    "coder",
                    "Create the requested text file. Do not run verification for this text-only request.",
                    allowed_tools=("write_file",),
                ),
                profile=MAIN_PROFILE,
                workspace=workspace,
                session_id=f"session-{workspace.name}",
                messages=(
                    {
                        "role": "user",
                        "content": "Create result.txt with the workspace name.",
                    },
                ),
                stream=False,
                metadata={"permission_mode": "auto"},
            )
        )

    async def both():
        return await asyncio.gather(*(run(workspace) for workspace in workspaces))

    results = asyncio.run(both())
    assert len(contexts) == 2, [(r.status, r.error) for r in results]
    assert contexts[0].run.interaction_run_id != contexts[1].run.interaction_run_id
    assert contexts[0].policy is not contexts[1].policy
    assert all(
        context.lifecycle.checkpoint.__self__._pending == [] for context in contexts
    )
    assert (
        contexts[0].projection.record_result.__self__.facts
        is not contexts[1].projection.record_result.__self__.facts
    )
    for workspace in workspaces:
        assert (workspace / "result.txt").read_text(encoding="utf-8") == workspace.name
        ledger = ToolLedger(workspace)
        (row,) = ledger.executions(f"session-{workspace.name}")
        assert row["execution_state"] == "succeeded"
        assert row["preview_admitted"] == 1
        (mutation,) = ledger.mutations(f"session-{workspace.name}")
        assert mutation["disposition"] == "committed"
        stored = asyncio.run(
            LegacyJsonSessionStore().load(
                SessionIdentity(f"session-{workspace.name}"), workspace
            )
        )
        assert stored is not None
        assert any(m.get("tool_call_id") == row["call_id"] for m in stored.transcript)
    assert len(calls_seen) == 4
