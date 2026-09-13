"""Nested product runtimes inherit one private top-level config epoch."""
from __future__ import annotations

import asyncio
import pickle


def _snapshot(tmp_path, workspace, values):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    workspace.mkdir(parents=True, exist_ok=True)
    return load_config_snapshot(
        workspace,
        environ=values,
        user_config_path=tmp_path / "missing-user.env",
        trust_store=WorkspaceTrustStore(tmp_path / "workspace-trust.json"),
    )


class _CapturingRunner:
    def __init__(self):
        self.calls = []

    async def run_result(self, request, options=None):
        from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage

        self.calls.append((request, options))
        return RunResult(
            status=RunStatus.COMPLETED,
            final_text="done",
            messages=request.messages,
            usage=TokenUsage(),
            session_id=request.session_id,
            active_agent=request.agent.name,
        )


def _parent_request(workspace):
    from nz_coder.runtime.core import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest

    return RunRequest(
        agent=AgentDefinition(name="parent", instructions="work"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "work"},),
        workspace=workspace,
        session_id="parent-session",
        provider="openai-compatible",
        model="parent-model",
        stream=False,
    )


def test_sdk_child_inherits_parent_run_snapshot(tmp_path):
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.core.request import AgentDefinition
    from nz_coder.sdk import AgentClient

    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "API_KEY": "parent-secret",
        "API_BASE_URL": "https://parent.invalid/v1",
        "MODEL_ID": "parent-model",
    })
    runner = _CapturingRunner()
    with scoped_config_snapshot(snapshot):
        asyncio.run(AgentClient(runner=runner).run_child(
            parent=_parent_request(snapshot.workspace),
            agent=AgentDefinition(name="child", instructions="review"),
            prompt="review",
            session_id="child-session",
        ))

    request, options = runner.calls[0]
    assert options.config_snapshot is snapshot
    assert "parent-secret" not in repr(request.metadata)


def test_control_change_during_parent_does_not_affect_child(tmp_path):
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.core.request import AgentDefinition
    from nz_coder.sdk import AgentClient

    workspace = tmp_path / "target"
    first = _snapshot(tmp_path, workspace, {
        "API_BASE_URL": "https://b1.invalid/v1", "MODEL_ID": "model-b1",
    })
    workspace.joinpath(".env").write_text(
        "API_BASE_URL=https://b2.invalid/v1\nMODEL_ID=model-b2\n",
        encoding="utf-8",
    )
    runner = _CapturingRunner()
    with scoped_config_snapshot(first):
        asyncio.run(AgentClient(runner=runner).run_child(
            parent=_parent_request(workspace),
            agent=AgentDefinition(name="child", instructions="review"),
            prompt="review",
            session_id="child-session",
        ))

    assert runner.calls[0][1].config_snapshot.get("MODEL_ID") == "model-b1"
    assert runner.calls[0][1].config_snapshot.get("API_BASE_URL") == "https://b1.invalid/v1"


def test_subagent_model_fallback_uses_parent_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.agent import subagent

    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "MODEL_ID": "target-model",
        "SUBAGENT_EXPLORE_MODEL": "target-fast",
        "SUBAGENT_DEEP_MODEL": "target-deep",
    })
    monkeypatch.setattr(config, "MODEL_ID", "startup-model")
    monkeypatch.setattr(config, "SUBAGENT_EXPLORE_MODEL", "startup-fast")
    monkeypatch.setattr(config, "SUBAGENT_DEEP_MODEL", "startup-deep")

    with subagent.scoped_parent_context(
        model_id="target-model", config_snapshot=snapshot,
    ):
        fast, _ = subagent._resolve_subagent_route("explore", "fast")
        deep, _ = subagent._resolve_subagent_route("general-purpose", "deep")

    assert (fast, deep) == ("target-fast", "target-deep")


def test_subagent_provider_endpoint_uses_parent_snapshot(tmp_path, monkeypatch):
    from nz_coder.providers import create_provider as create_real_provider
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.process.workdir import scoped_workdir

    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "API_KEY": "target-secret",
        "API_BASE_URL": "https://target.invalid/v1",
        "MODEL_PROVIDER": "openai-compatible",
        "MODEL_ID": "target-model",
        "SUBAGENT_WORKTREE_ENABLED": "0",
    })
    seen = {}

    def capture_provider(*_args, **kwargs):
        seen["snapshot"] = kwargs.get("config_snapshot")
        provider = create_real_provider(*_args, **kwargs)
        seen["endpoint"] = provider.base_url
        raise RuntimeError("stop after provider selection")

    monkeypatch.setattr(subagent, "create_provider", capture_provider)
    with scoped_workdir(snapshot.workspace), subagent.scoped_parent_context(
        session_id="parent", model_id="target-model", config_snapshot=snapshot,
    ):
        try:
            subagent.run_subagent("inspect", agent_type="explore")
        except RuntimeError as exc:
            assert str(exc) == "stop after provider selection"

    assert seen["snapshot"] is snapshot
    assert seen["endpoint"] == "https://target.invalid/v1"


def test_subagent_does_not_inherit_startup_workspace_endpoint(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.providers import create_provider
    from nz_coder.runtime.agent import subagent

    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "API_KEY": "target-secret",
        "API_BASE_URL": "https://target.invalid/v1",
        "MODEL_PROVIDER": "openai-compatible",
    })
    monkeypatch.setattr(config, "API_BASE_URL", "https://startup.invalid/v1")

    with subagent.scoped_parent_context(config_snapshot=snapshot):
        selected = create_provider(
            "openai-compatible",
            config_snapshot=subagent._parent_config_snapshot(snapshot.workspace),
        )

    assert selected.base_url == "https://target.invalid/v1"


def test_child_snapshot_metadata_does_not_expose_secrets(tmp_path):
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.core.request import AgentDefinition
    from nz_coder.sdk import AgentClient

    secret = "child-metadata-secret"
    snapshot = _snapshot(tmp_path, tmp_path / "target", {"API_KEY": secret})
    runner = _CapturingRunner()
    with scoped_config_snapshot(snapshot):
        asyncio.run(AgentClient(runner=runner).run_child(
            parent=_parent_request(snapshot.workspace),
            agent=AgentDefinition(name="child", instructions="review"),
            prompt="review",
            session_id="child-session",
        ))
    request, options = runner.calls[0]
    assert secret not in repr(request)
    assert secret not in repr(options)
    assert "config_snapshot" not in request.metadata


def test_child_snapshot_survives_private_spawn_transport(tmp_path):
    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "API_KEY": "spawn-private-secret",
        "MODEL_ID": "spawn-model",
    })

    restored = pickle.loads(pickle.dumps(snapshot))

    assert restored.workspace == snapshot.workspace
    assert restored.get("MODEL_ID") == "spawn-model"
    assert restored.get("API_KEY") == "spawn-private-secret"
    assert "spawn-private-secret" not in repr(restored)
    assert type(restored.project_control.files).__name__ == "mappingproxy"


def test_background_child_inherits_parent_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "MODEL_ID": "background-model",
    })
    seen = []
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", True)

    def child(*_args, **_kwargs):
        seen.append(subagent._parent_config_snapshot(snapshot.workspace))
        return "done"

    monkeypatch.setattr(subagent, "run_subagent", child)
    manager = BackgroundAgentManager(snapshot.workspace, "parent-background")
    try:
        with scoped_config_snapshot(snapshot), subagent.scoped_parent_context(
            session_id="parent-background", config_snapshot=snapshot,
        ):
            started = manager.start([{
                "name": "inspect", "prompt": "inspect", "read_only": True,
            }])
        task_id = started.metadata["task_ids"][0]
        manager.wait([task_id], timeout_ms=5000)
    finally:
        manager.close()

    assert seen == [snapshot]


def test_background_execution_switches_use_parent_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager

    workspace = tmp_path / "target"
    disabled_worktree = _snapshot(tmp_path, workspace, {
        "SUBAGENT_WORKTREE_ENABLED": "0",
    })
    snapshot = _snapshot(tmp_path, workspace, {
        "SUBAGENT_WORKTREE_ENABLED": "1",
        "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED": "0",
        "SUBAGENT_BACKGROUND_MAX_TASKS": "1",
        "SUBAGENT_BACKGROUND_MAX_CONCURRENT": "10",
    })
    monkeypatch.setattr(config, "SUBAGENT_WORKTREE_ENABLED", True)
    monkeypatch.setattr(config, "SUBAGENT_PROCESS_ISOLATION_ENABLED", True)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_TASKS", 20)
    monkeypatch.setattr(config, "SUBAGENT_BACKGROUND_MAX_CONCURRENT", 20)
    manager = BackgroundAgentManager(workspace, "parent-switches")
    try:
        with scoped_config_snapshot(disabled_worktree):
            worktree = manager.start([{
                "name": "write", "prompt": "write", "target_paths": ["src"],
            }])
        with scoped_config_snapshot(snapshot):
            assert manager.concurrency_cap == 1
            process = manager.start([{
                "name": "inspect", "prompt": "inspect", "read_only": True,
                "isolation": "process",
            }])
            overflow = manager.start([
                {"name": "one", "prompt": "one", "read_only": True},
                {"name": "two", "prompt": "two", "read_only": True},
            ])

        assert "SUBAGENT_WORKTREE_ENABLED=1" in str(worktree)
        assert "process isolation is disabled" in str(process)
        assert "lifetime cap (1)" in str(overflow)
    finally:
        manager.close()


def test_workflow_child_inherits_approved_snapshot(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import (
        active_config_snapshot,
        scoped_config_snapshot,
    )
    from nz_coder.runtime.workflows import workflow_sdk

    snapshot = _snapshot(tmp_path, tmp_path / "target", {
        "MODEL_ID": "workflow-model",
    })
    seen = []

    class Manager:
        workspace = snapshot.workspace

    def workflow_run(**_kwargs):
        seen.append(active_config_snapshot(snapshot.workspace))
        return "done"

    monkeypatch.setattr(workflow_sdk, "workflow_run", workflow_run)
    with scoped_config_snapshot(snapshot):
        handle = workflow_sdk.WorkflowHostSDK(Manager()).start(plan={})
        assert handle.wait(timeout=5) == "done"

    assert seen == [snapshot]


def test_next_top_level_run_recaptures_after_child_epoch(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot, scoped_config_snapshot
    from nz_coder.runtime.core.request import AgentDefinition
    from nz_coder.sdk import AgentClient

    workspace = tmp_path / "target"
    workspace.mkdir()
    workspace.joinpath(".env").write_text("LOG_LEVEL=ONE\n", encoding="utf-8")
    first = load_config_snapshot(workspace, environ={})
    runner = _CapturingRunner()
    with scoped_config_snapshot(first):
        asyncio.run(AgentClient(runner=runner).run_child(
            parent=_parent_request(workspace),
            agent=AgentDefinition(name="child", instructions="review"),
            prompt="review", session_id="child-session",
        ))
    workspace.joinpath(".env").write_text("LOG_LEVEL=TWO\n", encoding="utf-8")
    second = load_config_snapshot(workspace, environ={})

    assert runner.calls[0][1].config_snapshot.get("LOG_LEVEL") == "ONE"
    assert second.get("LOG_LEVEL") == "TWO"


def test_child_prepare_uses_inherited_active_snapshot(tmp_path):
    from nz_coder.foundation.workspace_trust import scoped_config_snapshot
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "target"
    first = _snapshot(tmp_path, workspace, {"MODEL_ID": "model-b1"})
    workspace.joinpath(".env").write_text("MODEL_ID=model-b2\n", encoding="utf-8")
    with scoped_workdir(workspace):
        agent = AgentLoop(
            "child", client=type("Client", (), {})(), config_snapshot=first,
            trace_enabled=False, sidecar_verifier=False,
        )
        with scoped_config_snapshot(first):
            bundle = agent.prepare_run_control()
        try:
            assert bundle.config_snapshot is first
            assert bundle.config_snapshot.get("MODEL_ID") == "model-b1"
        finally:
            agent.retire_run_control(bundle)
            agent.close()
