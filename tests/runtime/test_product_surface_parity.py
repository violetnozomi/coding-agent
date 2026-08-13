"""Four-surface contract and composition parity tests."""
from __future__ import annotations

import inspect


def test_all_product_surfaces_declare_the_same_capability_fingerprint():
    from nz_coder.runtime.product_surfaces import ProductSurface, capability_fingerprint

    fingerprints = {capability_fingerprint(surface) for surface in ProductSurface}
    assert len(fingerprints) == 1
    fingerprint = next(iter(fingerprints))
    assert {
        "mcp", "skills", "memory", "tool_exposure", "verification",
        "snapshots", "media_preflight", "subagents", "workflows", "events",
        "repo_intelligence", "retrieval_policy", "process_service", "web_search",
    } <= fingerprint


def test_four_surface_execution_adapters_converge_on_agent_client():
    from nz_coder.http_service.manager import ManagedSession
    from nz_coder.interface import cli, headless
    from nz_coder.interface.session_controller import TerminalSessionController
    from nz_coder.sdk import AgentClient

    assert "AgentClient" in inspect.getsource(headless)
    assert "controller.run" in inspect.getsource(cli._run_cli_impl)
    assert "self._client.run" in inspect.getsource(TerminalSessionController.run)
    assert "self.client.run" in inspect.getsource(ManagedSession._run_agent)
    assert "build_native_sdk_runner" in inspect.getsource(AgentClient.run)


def test_product_resource_scope_binds_runtime_capabilities_once():
    from nz_coder.runtime.host import ProductionRuntimeHost

    source = inspect.getsource(ProductionRuntimeHost.run)
    for binding in (
        "scoped_mcp_runtime", "bind_skill_loader", "bind_memory_manager",
        "scoped_dynamic_tool_provider", "scoped_background_agent_manager",
        "scoped_question_asker", "scoped_workflow_approval_asker",
        "bind_tool_state",
    ):
        assert binding in source


def test_default_http_manager_has_no_agentloop_owner(monkeypatch, tmp_path):
    from nz_coder.http_service.manager import SessionManager

    monkeypatch.setattr(
        "nz_coder.runtime.loop.AgentLoop.__init__",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("HTTP must not construct AgentLoop")
        ),
    )
    manager = SessionManager(workspace_roots=[tmp_path], restore_saved=False)
    try:
        session = manager.get(manager.create("auto")["id"])
        assert session.agent is None
        assert session.client is not None
    finally:
        manager.close()
