"""Product entrypoints own one immutable control epoch per top-level run."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading

import pytest


class _RetryingCloser:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def close(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("secret-value-must-not-escape")


def _cleanup_bundle(*, sidecar=None, mcp=None, providers=()):
    from nz_coder.runtime.execution.run_control import RunControlBundle

    return RunControlBundle(
        config_snapshot=None,
        permissions=None,
        plan_mode=None,
        skill_loader=None,
        hooks=None,
        mcp_runtime=mcp,
        model_runtime=None,
        provider_runtimes={str(index): item for index, item in enumerate(providers)},
        owns_provider_runtimes=True,
        sidecar_verifier=sidecar,
        owns_sidecar_verifier=sidecar is not None,
    )


def test_bundle_close_retries_only_incomplete_resources():
    sidecar = _RetryingCloser()
    mcp = _RetryingCloser(1)
    provider = _RetryingCloser()
    bundle = _cleanup_bundle(sidecar=sidecar, mcp=mcp, providers=(provider,))

    with pytest.raises(RuntimeError, match="resource cleanup failed"):
        bundle.close()
    bundle.close()

    assert (sidecar.calls, mcp.calls, provider.calls) == (1, 2, 1)
    assert bundle.completed is True


@pytest.mark.parametrize("failed_resource", ["sidecar", "mcp", "provider"])
def test_bundle_close_continues_after_resource_failure(failed_resource):
    resources = {
        name: _RetryingCloser(1 if name == failed_resource else 0)
        for name in ("sidecar", "mcp", "provider")
    }
    bundle = _cleanup_bundle(
        sidecar=resources["sidecar"],
        mcp=resources["mcp"],
        providers=(resources["provider"],),
    )

    with pytest.raises(RuntimeError):
        bundle.close()

    assert all(resource.calls == 1 for resource in resources.values())
    assert bundle.completed is False
    assert any(
        item == failed_resource or item.startswith(f"{failed_resource}:")
        for item in bundle.incomplete_resources
    )


def test_cleanup_failure_remains_secret_free():
    bundle = _cleanup_bundle(mcp=_RetryingCloser(1))
    with pytest.raises(RuntimeError) as captured:
        bundle.close()

    assert "secret-value-must-not-escape" not in str(captured.value)
    assert "secret-value-must-not-escape" not in repr(bundle.cleanup_failures)

    from nz_coder.runtime.execution.loop import AgentLoop

    recorded = []
    agent = AgentLoop.__new__(AgentLoop)
    agent._run_control_lock = threading.RLock()
    agent._pending_run_control_cleanup = []
    agent.tracer = type("Tracer", (), {
        "log": lambda self, event, **payload: recorded.append((event, payload)),
    })()
    retrying = _cleanup_bundle(mcp=_RetryingCloser(1))
    assert agent._attempt_run_control_cleanup(retrying, "test") is False
    assert recorded[0][0] == "run_control_cleanup_failed"
    assert "secret-value-must-not-escape" not in repr(recorded)


def test_cleanup_trace_failure_does_not_escape_or_lose_retry_ownership():
    from nz_coder.runtime.execution.loop import AgentLoop

    agent = AgentLoop.__new__(AgentLoop)
    agent._run_control_lock = threading.RLock()
    agent._pending_run_control_cleanup = []
    agent.tracer = type("Tracer", (), {
        "log": lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("trace unavailable")
        ),
    })()
    bundle = _cleanup_bundle(mcp=_RetryingCloser(1))

    assert agent._attempt_run_control_cleanup(bundle, "test") is False
    assert agent._pending_run_control_cleanup == [bundle]


class _LifecycleMCP(_RetryingCloser):
    @classmethod
    def configured(cls, **_kwargs):
        return cls()

    def set_change_handler(self, _handler):
        return None

    def start(self):
        return None

    def status_summary(self):
        return []

    def tool_bindings(self):
        return []


def _cleanup_agent(tmp_path, runtime_factory):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    scope = scoped_workdir(tmp_path)
    scope.__enter__()
    agent = AgentLoop(
        "test", permission_mode="auto", client=_client(), trace_enabled=False,
        sidecar_verifier=False,
    )
    agent._mcp_runtime_factory = runtime_factory
    return scope, agent


def test_previous_runtime_close_failure_does_not_orphan_active_bundle(tmp_path):
    old = _LifecycleMCP(failures=1)
    scope, agent = _cleanup_agent(tmp_path, _LifecycleMCP)
    agent._mcp_runtime = old
    try:
        bundle = agent.prepare_run_control()
        assert agent._active_run_control is bundle
        assert old.calls == 1
        agent.retire_run_control(bundle)
        assert agent._active_run_control is None
    finally:
        agent.close()
        scope.__exit__(None, None, None)


def test_cleanup_failure_does_not_mask_primary_run_error(tmp_path):
    class FailsOnceMCP(_LifecycleMCP):
        @classmethod
        def configured(cls, **_kwargs):
            return cls(failures=1)

    class ProviderError(RuntimeError):
        pass

    async def fail(*_args):
        raise ProviderError("provider-primary")

    scope, agent = _cleanup_agent(tmp_path, FailsOnceMCP)
    try:
        bundle = agent.prepare_run_control()
        with pytest.raises(ProviderError, match="provider-primary"):
            asyncio.run(agent.runtime_host.run(
                agent, [{"role": "user", "content": "work"}],
                execute=fail, run_control=bundle,
            ))
    finally:
        agent.close()
        scope.__exit__(None, None, None)


def test_cleanup_failure_does_not_turn_success_into_failed_run(tmp_path):
    class FailsOnceMCP(_LifecycleMCP):
        @classmethod
        def configured(cls, **_kwargs):
            return cls(failures=1)

    async def succeed(*_args):
        return {"status": "completed", "answer": "done"}

    scope, agent = _cleanup_agent(tmp_path, FailsOnceMCP)
    try:
        bundle = agent.prepare_run_control()
        result = asyncio.run(agent.runtime_host.run(
            agent, [{"role": "user", "content": "work"}],
            execute=succeed, run_control=bundle,
        ))
        assert result["status"] == "completed"
    finally:
        agent.close()
        scope.__exit__(None, None, None)


def _native_environment(*, result=None, primary_error=None):
    from nz_coder.runtime.core.result import RunResult, RunStatus, TokenUsage

    completed = result or RunResult(
        status=RunStatus.COMPLETED,
        final_text="done",
        messages=({"role": "assistant", "content": "done"},),
        usage=TokenUsage(),
        session_id="native-cleanup",
        active_agent="test",
    )

    class InnerRunner:
        async def run_result(self, _request, _options):
            if primary_error is not None:
                raise primary_error
            return completed

    class Host:
        async def run(self, _agent, _messages, *, execute, **_kwargs):
            return await execute(_agent, _messages)

    class Environment:
        def __init__(self):
            self.runner = InnerRunner()
            self.runtime_host = Host()
            self.model_runtime = type("ModelRuntime", (), {
                "provider_id": "offline", "model_id": "offline-model",
                "variant": None,
            })()
            self.change_tracker = type("Changes", (), {
                "current_changed_paths": lambda self: [],
            })()
            self.tracer = type("Tracer", (), {
                "events": [],
                "log": lambda self, event, **payload: self.events.append(
                    (event, payload)
                ),
            })()
            self._active_run_control = None

        def prepare_run_control(self, *_args, **_kwargs):
            self._active_run_control = object()
            return self._active_run_control

        def retire_run_control(self, bundle):
            if self._active_run_control is bundle:
                self._active_run_control = None

        def close(self):
            raise RuntimeError("cleanup-secret-must-not-escape")

    return Environment()


def test_native_cleanup_failure_does_not_turn_success_into_failed_run(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk

    environment = _native_environment()
    monkeypatch.setattr(
        native_sdk, "build_product_run_environment",
        lambda *_args, **_kwargs: environment,
    )
    result = asyncio.run(native_sdk.NativeSDKRunner().run_result(
        _native_request(tmp_path), RunOptions(),
    ))

    assert result.status.value == "completed"
    assert environment.tracer.events == [(
        "product_environment_cleanup_failed",
        {"failure_type": "RuntimeError"},
    )]


def test_native_cleanup_failure_does_not_mask_primary_run_error(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.core.request import RunOptions
    from nz_coder.runtime.execution import native_sdk

    class ProviderError(RuntimeError):
        pass

    environment = _native_environment(primary_error=ProviderError("primary"))
    monkeypatch.setattr(
        native_sdk, "build_product_run_environment",
        lambda *_args, **_kwargs: environment,
    )
    with pytest.raises(ProviderError, match="primary"):
        asyncio.run(native_sdk.NativeSDKRunner().run_result(
            _native_request(tmp_path), RunOptions(),
        ))
    assert "cleanup-secret-must-not-escape" not in repr(environment.tracer.events)


def _native_request(tmp_path):
    from nz_coder.runtime.core import MAIN_PROFILE
    from nz_coder.runtime.core.request import AgentDefinition, RunRequest

    return RunRequest(
        agent=AgentDefinition(name="test", instructions="test"),
        profile=MAIN_PROFILE,
        messages=({"role": "user", "content": "test"},),
        workspace=tmp_path,
        session_id="native-cleanup",
        provider="offline",
        model="offline-model",
        stream=False,
    )


def test_next_run_can_start_after_cleanup_failure(tmp_path):
    created = []

    class FailsOnceMCP(_LifecycleMCP):
        @classmethod
        def configured(cls, **_kwargs):
            runtime = cls(failures=1 if not created else 0)
            created.append(runtime)
            return runtime

    scope, agent = _cleanup_agent(tmp_path, FailsOnceMCP)
    try:
        first = agent.prepare_run_control()
        agent.retire_run_control(first)
        second = agent.prepare_run_control()
        assert agent._active_run_control is second
        assert created[0].calls == 2
        agent.retire_run_control(second)
    finally:
        agent.close()
        scope.__exit__(None, None, None)


def test_repeated_retire_does_not_double_close_completed_resources(tmp_path):
    scope, agent = _cleanup_agent(tmp_path, _LifecycleMCP)
    try:
        bundle = agent.prepare_run_control()
        runtime = bundle.mcp_runtime
        agent.retire_run_control(bundle)
        agent.retire_run_control(bundle)
        assert runtime.calls == 1
    finally:
        agent.close()
        scope.__exit__(None, None, None)


def test_prepare_failure_after_install_is_recoverable(tmp_path):
    created = []

    class RollbackMCP(_LifecycleMCP):
        @classmethod
        def configured(cls, **_kwargs):
            runtime = cls(failures=1 if not created else 0)
            created.append(runtime)
            return runtime

    scope, agent = _cleanup_agent(tmp_path, RollbackMCP)
    agent.agent_graph = object()
    agent.current_agent_name = "broken"
    original_activate = agent._activate_agent_runtime
    agent._activate_agent_runtime = lambda _name: (_ for _ in ()).throw(
        ValueError("role activation failed")
    )
    try:
        with pytest.raises(ValueError, match="role activation failed"):
            agent.prepare_run_control()
        assert agent._active_run_control is None
        assert created[0].calls == 1

        agent.agent_graph = None
        agent._activate_agent_runtime = original_activate
        bundle = agent.prepare_run_control()
        assert created[0].calls == 2
        assert agent._active_run_control is bundle
        agent.retire_run_control(bundle)
    finally:
        agent.close()
        scope.__exit__(None, None, None)


class _Message:
    content = "done"
    tool_calls = []
    reasoning_content = None


class _Completions:
    def __init__(self, callback=None):
        self.callback = callback

    def create(self, **_kwargs):
        if self.callback is not None:
            self.callback()
        choice = type("Choice", (), {"message": _Message(), "finish_reason": "stop"})()
        return type("Response", (), {"choices": [choice]})()


def _client(callback=None):
    return type("Client", (), {
        "chat": type("Chat", (), {"completions": _Completions(callback)})(),
    })()


def _trust_control(workspace: Path, trust_path: Path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    snapshot = load_config_snapshot(workspace)
    WorkspaceTrustStore(trust_path).trust(
        workspace, "workspace-control", snapshot.control_fingerprint,
    )
    return load_config_snapshot(workspace)


def test_terminal_next_submission_recaptures_project_control(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import current_config_snapshot
    from nz_coder.interface.session_controller import TerminalSessionController
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"permissions":{"deny":["bash"]}}', encoding="utf-8")
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    trusted = _trust_control(workspace, trust_path)
    seen = []
    with scoped_workdir(workspace):
        agent = AgentLoop(
            "test", permission_mode="auto",
            client=_client(lambda: seen.append(
                (
                    current_config_snapshot(workspace).control_plane_trusted,
                    current_config_snapshot(workspace).control_fingerprint,
                )
            )),
            trace_enabled=False,
            config_snapshot=trusted,
        )
        controller = TerminalSessionController(agent)
        try:
            asyncio.run(controller.run(
                [{"role": "user", "content": "first"}], stream=False,
            ))
            settings.write_text(
                '{"permissions":{"allow":["bash"]}}', encoding="utf-8",
            )
            asyncio.run(controller.run(
                [{"role": "user", "content": "second"}], stream=False,
            ))
        finally:
            controller.close()

    assert seen[0][0] is True
    assert seen[1][0] is False
    assert seen[0][1] != seen[1][1]


def test_http_next_run_recaptures_project_control(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nz_coder.http_service.manager import ManagedSession
    from nz_coder.protocol.session_events import SessionEventBus

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = workspace / ".nz-coder" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"permissions":{"deny":["bash"]}}', encoding="utf-8")
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    _trust_control(workspace, trust_path)
    seen = []

    class Agent:
        permissions = SimpleNamespace(mode="auto")

        def __init__(self):
            self.event_bus = SessionEventBus(session_id="http-run-control")

        def prepare_run_control(self):
            return None

        async def run(self, _messages, *, stream=True, config_snapshot=None):
            seen.append((
                config_snapshot.control_plane_trusted,
                config_snapshot.control_fingerprint,
            ))
            return {"status": "completed", "answer": "done"}

        def close(self):
            self.event_bus.close()

    session = ManagedSession(
        "http-run-control", "auto", Agent(), threading.Lock(), 1.0,
        workspace_id="workspace", workspace=workspace,
        event_bus=SessionEventBus(session_id="http-run-control-public"),
    )
    try:
        session.start_run("first")
        assert session.wait(5)
        settings.write_text(
            '{"permissions":{"allow":["bash"]}}', encoding="utf-8",
        )
        session.start_run("second")
        assert session.wait(5)
    finally:
        session.dispose(force=True)

    assert seen[0][0] is True
    assert seen[1][0] is False
    assert seen[0][1] != seen[1][1]


def test_sdk_reused_environment_recaptures_project_control(tmp_path):
    from nz_coder.foundation.workspace_trust import current_config_snapshot
    from nz_coder.interface.session_controller import TerminalSessionController
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env").write_text("LOG_LEVEL=ONE\n", encoding="utf-8")
    seen = []
    with scoped_workdir(workspace):
        environment = AgentLoop(
            "test", permission_mode="auto",
            client=_client(lambda: seen.append(
                current_config_snapshot(workspace).get("LOG_LEVEL")
            )),
            trace_enabled=False,
        )
        runner = TerminalSessionController(environment)
        try:
            asyncio.run(runner.run([{"role": "user", "content": "one"}], stream=False))
            (workspace / ".env").write_text("LOG_LEVEL=TWO\n", encoding="utf-8")
            asyncio.run(runner.run([{"role": "user", "content": "two"}], stream=False))
        finally:
            runner.close()
    assert seen == ["ONE", "TWO"]


def test_control_change_does_not_affect_inflight_run_and_requires_new_trust(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    settings = workspace / ".nz-coder" / "settings.json"
    skill = workspace / ".nz-coder" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: old skill\n---\nold body\n",
        encoding="utf-8",
    )
    settings.write_text(json.dumps({
        "permissions": {"deny": ["bash"]},
        "hooks": [{
            "id": "old-hook", "event": "turn_start",
            "action": {"type": "prompt", "message": "old"},
        }],
    }), encoding="utf-8")
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    trusted = _trust_control(workspace, trust_path)

    with scoped_workdir(workspace):
        agent = AgentLoop(
            "test", permission_mode="default", client=_client(), trace_enabled=False,
            config_snapshot=trusted,
        )
        bundle = agent.prepare_run_control(trusted)
        try:
            settings.write_text(json.dumps({
                "permissions": {"allow": ["bash"]},
                "hooks": [{
                    "id": "new-hook", "event": "turn_start",
                    "action": {"type": "prompt", "message": "new"},
                }],
            }), encoding="utf-8")
            skill.write_text(
                "---\nname: review\ndescription: new skill\n---\nnew body\n",
                encoding="utf-8",
            )
            assert bundle.permissions.check("bash", {"command": "pwd"})["behavior"] == "deny"
            assert "old body" in str(bundle.skill_loader.load("review"))
            assert [item.id for item in bundle.hooks.configured_hooks] == ["old-hook"]
        finally:
            agent.retire_run_control(bundle)

        next_bundle = agent.prepare_run_control()
        try:
            assert next_bundle.config_snapshot.control_plane_trusted is False
            assert "review" not in {
                item["name"] for item in next_bundle.skill_loader.list_skills()
                if item["source"] == "project"
            }
            assert next_bundle.hooks.configured_hooks == []
        finally:
            agent.retire_run_control(next_bundle)
            agent.close()


def test_failed_run_control_construction_does_not_pollute_session(tmp_path):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    class BrokenMCP:
        @staticmethod
        def configured(**_kwargs):
            raise RuntimeError("cannot build MCP")

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test", permission_mode="auto", client=_client(), trace_enabled=False,
        )
        before = agent.config_snapshot
        agent._mcp_runtime_factory = BrokenMCP
        try:
            try:
                agent.prepare_run_control()
            except RuntimeError as exc:
                assert str(exc) == "cannot build MCP"
            else:
                raise AssertionError("run-control construction must fail")
            assert agent.config_snapshot is before
            assert agent._active_run_control is None
        finally:
            agent.close()


def test_failed_role_activation_does_not_partially_install_run_control(tmp_path):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "base prompt", permission_mode="auto", client=_client(),
            trace_enabled=False,
        )
        previous = {
            "snapshot": agent.config_snapshot,
            "permissions": agent.permissions,
            "skills": agent._skill_loader,
            "hooks": agent.hooks,
            "mcp": agent._mcp_runtime,
            "prompt": agent.system_prompt,
        }
        agent.agent_graph = object()
        agent.current_agent_name = "broken"
        agent._activate_agent_runtime = lambda _name: (_ for _ in ()).throw(
            ValueError("invalid role")
        )
        try:
            with pytest.raises(ValueError, match="invalid role"):
                agent.prepare_run_control()
            assert agent.config_snapshot is previous["snapshot"]
            assert agent.permissions is previous["permissions"]
            assert agent._skill_loader is previous["skills"]
            assert agent.hooks is previous["hooks"]
            assert agent._mcp_runtime is previous["mcp"]
            assert agent.system_prompt == previous["prompt"]
            assert agent._active_run_control is None
        finally:
            agent.close()


def test_untrust_revokes_project_permission_on_next_run(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    settings = workspace / ".nz-coder" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"permissions": {"allow": ["bash"]}}), encoding="utf-8",
    )
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    trusted = _trust_control(workspace, trust_path)
    with scoped_workdir(workspace):
        agent = AgentLoop(
            "test", permission_mode="default", client=_client(), trace_enabled=False,
            config_snapshot=trusted,
        )
        first = agent.prepare_run_control(trusted)
        try:
            assert first.permissions.check(
                "bash", {"command": "rm -f result.txt"},
            )["behavior"] == "allow"
        finally:
            agent.retire_run_control(first)

        WorkspaceTrustStore(trust_path).remove(workspace, "workspace-control")
        second = agent.prepare_run_control()
        try:
            assert second.config_snapshot.control_plane_trusted is False
            assert second.permissions.check(
                "bash", {"command": "rm -f result.txt"},
            )["behavior"] != "allow"
        finally:
            agent.retire_run_control(second)
            agent.close()


def test_project_skill_is_pinned_during_run_and_rotated_next_run(
    tmp_path, monkeypatch,
):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    workspace = tmp_path / "workspace"
    skill = workspace / ".nz-coder" / "skills" / "review" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        "---\nname: review\ndescription: old description\n---\nold body\n",
        encoding="utf-8",
    )
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    first_snapshot = _trust_control(workspace, trust_path)

    with scoped_workdir(workspace):
        agent = AgentLoop(
            "base prompt", permission_mode="auto", client=_client(),
            trace_enabled=False, config_snapshot=first_snapshot,
        )
        first = agent.prepare_run_control(first_snapshot)
        try:
            assert "old description" in agent.system_prompt
            skill.write_text(
                "---\nname: review\ndescription: new description\n---\nnew body\n",
                encoding="utf-8",
            )
            assert "old description" in agent.system_prompt
            assert "new description" not in agent.system_prompt
        finally:
            agent.retire_run_control(first)

        second_snapshot = _trust_control(workspace, trust_path)
        second = agent.prepare_run_control(second_snapshot)
        try:
            assert "new description" in agent.system_prompt
            assert "old description" not in agent.system_prompt
        finally:
            agent.retire_run_control(second)
            agent.close()


def test_project_workflow_rotates_on_next_run(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from nz_coder.http_service.manager import ManagedSession
    from nz_coder.protocol.session_events import SessionEventBus
    from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager
    from nz_coder.runtime.workflows.workflow_capsule import create_workflow_capsule
    from nz_coder.runtime.workflows.workflow_library import save_workflow_capsule

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trust_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))

    def capsule(description: str) -> dict:
        manifest = {
            "name": "review",
            "description": description,
            "phases": ["final"],
            "read_only": True,
            "planned_agents": 1,
            "max_agents": 1,
            "max_concurrency": 1,
            "patterns": ["fan-out-and-synthesize"],
        }
        return create_workflow_capsule(
            manifest=manifest,
            plan={
                "phases": [{
                    "name": "final", "mode": "synthesize",
                    "from_phases": [], "rubric": description,
                    "artifact": "review",
                }],
            },
        )

    save_workflow_capsule("review", capsule("old"), workspace=workspace)
    trusted = _trust_control(workspace, trust_path)
    event_bus = SessionEventBus(session_id="workflow-rotation")
    background = BackgroundAgentManager(workspace, "workflow-rotation")
    agent = SimpleNamespace(
        event_bus=event_bus,
        background_agents=background,
        config_snapshot=trusted,
    )
    session = ManagedSession(
        "workflow-rotation", "default", agent, threading.Lock(), 1.0,
        workspace_id="workspace", workspace=workspace, event_bus=event_bus,
    )
    try:
        first = session.prepare_workflow("review", {})
        save_workflow_capsule(
            "review", capsule("new"), workspace=workspace, overwrite=True,
        )
        with pytest.raises(ValueError, match="not found"):
            session.prepare_workflow("review", {})
        _trust_control(workspace, trust_path)
        second = session.prepare_workflow("review", {})
        assert second["approval_digest"] != first["approval_digest"]
        assert second["summary"]["description"] == "new"
    finally:
        background.close()
        event_bus.close()


def test_provider_runtime_is_retired_after_each_run_epoch(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.execution import loop as loop_module
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.runtime.process.workdir import scoped_workdir

    closed = []

    class Client:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    def runtime(name):
        return ResolvedModelRuntime(
            provider_id="openai-compatible",
            model_id="model",
            request_model_id="model",
            variant=None,
            provider=type("Provider", (), {"name": "openai-compatible"})(),
            client=Client(name),
            owns_client=True,
            capabilities=ModelCapabilities(
                provider="openai-compatible", model_id="model",
            ),
        )

    snapshot = load_config_snapshot(tmp_path, environ={
        "MODEL_PROVIDER": "openai-compatible", "MODEL_ID": "model",
    })
    first_runtime = runtime("first")
    with scoped_workdir(tmp_path):
        agent = loop_module.AgentLoop(
            "test", model_runtime=first_runtime, manage_model_runtime=True,
            config_snapshot=snapshot, sidecar_verifier=False, trace_enabled=False,
        )
        first = agent.prepare_run_control(snapshot)
        agent.retire_run_control(first)
        assert closed == ["first"]

        second_runtime = runtime("second")
        original = loop_module.resolve_model_runtime
        loop_module.resolve_model_runtime = lambda _request: second_runtime
        try:
            second = agent.prepare_run_control(load_config_snapshot(tmp_path, environ={
                "MODEL_PROVIDER": "openai-compatible", "MODEL_ID": "model",
            }))
            agent.retire_run_control(second)
        finally:
            loop_module.resolve_model_runtime = original
            agent.close()
    assert closed == ["first", "second"]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("provider failed"), asyncio.CancelledError()],
    ids=["exception", "cancel"],
)
def test_run_owned_resources_close_on_exception_and_cancel(tmp_path, failure):
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.process.workdir import scoped_workdir

    closed = []

    class Runtime:
        @classmethod
        def configured(cls, **_kwargs):
            return cls()

        def set_change_handler(self, _handler):
            return None

        def start(self):
            return None

        def status_summary(self):
            return []

        def tool_bindings(self):
            return []

        def close(self):
            closed.append("mcp")

    async def fail(*_args):
        raise failure

    with scoped_workdir(tmp_path):
        agent = AgentLoop(
            "test", permission_mode="auto", client=_client(), trace_enabled=False,
        )
        agent._mcp_runtime_factory = Runtime
        bundle = agent.prepare_run_control()
        try:
            with pytest.raises(type(failure)):
                asyncio.run(agent.runtime_host.run(
                    agent,
                    [{"role": "user", "content": "test"}],
                    execute=fail,
                    run_control=bundle,
                ))
            assert closed == ["mcp"]
            assert agent._active_run_control is None
            assert agent._mcp_runtime is None
        finally:
            agent.close()
