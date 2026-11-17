"""Contracts for revocable cross-run capabilities and credential delegation."""
from __future__ import annotations


def test_untrust_reports_active_leases(tmp_path, monkeypatch, capsys):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.interface.config_cli import config_main

    workspace = tmp_path / "repo"
    workspace.mkdir()
    lease = capability_leases().create(
        kind="persistent-process",
        resource_id="proc-test",
        workspace=workspace,
        control_fingerprint="control-a",
        run_id="run-a",
        interaction_id="interaction-a",
        owner_session="session-a",
        revoke=lambda: None,
    )
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json")
    )
    try:
        assert config_main(["untrust", "--workspace", str(workspace)]) == 0
        output = " ".join(capsys.readouterr().out.split())
        assert "1 active capability lease" in output
        assert "next Run only" in output
    finally:
        capability_leases().release(lease.lease_id)


def test_untrust_revoke_active_stops_owned_processes(tmp_path, monkeypatch, capsys):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.interface.config_cli import config_main

    workspace = tmp_path / "repo"
    workspace.mkdir()
    revoked: list[str] = []
    lease = capability_leases().create(
        kind="background-child",
        resource_id="child-test",
        workspace=workspace,
        control_fingerprint="control-a",
        run_id="run-a",
        interaction_id="interaction-a",
        owner_session="session-a",
        revoke=lambda: revoked.append("child-test"),
    )
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path / "trust.json")
    )

    assert config_main([
        "untrust", "--workspace", str(workspace), "--revoke-active",
    ]) == 0
    assert revoked == ["child-test"]
    assert capability_leases().get(lease.lease_id) is None
    assert "revoked 1 active capability lease" in capsys.readouterr().out


def test_workspace_endpoint_requires_credential_delegation(tmp_path, monkeypatch):
    import pytest

    from nz_coder.foundation.workspace_trust import (
        ConfigValidationError,
        WorkspaceTrustStore,
        load_config_snapshot,
    )
    from nz_coder.providers.configuration import provider_connection

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "API_BASE_URL=https://workspace-proxy.example/v1\n", encoding="utf-8"
    )
    store_path = tmp_path / "trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(store_path))
    store = WorkspaceTrustStore(store_path)
    initial = load_config_snapshot(
        workspace, environ={"API_KEY": "owner-secret"}, trust_store=store,
    )
    store.trust(workspace, "workspace-config", initial.workspace_fingerprint)
    trusted = load_config_snapshot(
        workspace, environ={"API_KEY": "owner-secret"}, trust_store=store,
    )

    with pytest.raises(ConfigValidationError, match="credential delegation"):
        provider_connection("openai-compatible", config_snapshot=trusted)

    from nz_coder.providers.configuration import trust_provider_endpoint

    trust_provider_endpoint("openai-compatible", config_snapshot=trusted, trust_store=store)
    connection = provider_connection("openai-compatible", config_snapshot=trusted)
    assert connection.credential_source == "environment"
    assert connection.endpoint_source == "trusted-workspace"


def test_revoke_pending_cleanup_keeps_lease_until_resources_done(tmp_path, monkeypatch):
    from nz_coder.foundation.capability_lease import capability_leases
    from nz_coder.runtime.process import process_service as processes
    from tests.fixtures.process_lifecycle import ControlledBackend

    backend = ControlledBackend()
    monkeypatch.setattr(processes, "create_process_backend", lambda *a, **kw: backend)
    service = processes.ProcessService(tmp_path, kill_grace_seconds=0)
    handle = service.start("owned", cwd=tmp_path, owner_session_id="session-a")
    record = service._records[handle.process_id]
    lease_id = record.capability_lease_id
    try:
        result = capability_leases().revoke_workspace(tmp_path)
        assert result.failed == 1
        assert result.revoked == 0
        assert capability_leases().get(lease_id) is not None
        assert record.waiter.is_alive()
        backend.recover()
        assert record.cleanup_done.wait(2)
        assert capability_leases().get(lease_id) is None
    finally:
        backend.recover()
        service.close()
