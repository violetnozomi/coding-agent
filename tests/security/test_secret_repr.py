"""Regression tests for credential-bearing runtime object representations."""
from __future__ import annotations


def test_run_settings_repr_hides_image_api_key(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.run_settings import RunSettings

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "SENTINEL-IMAGE-KEY"
    settings = RunSettings.from_snapshot(load_config_snapshot(
        workspace, environ={"NZ_IMAGE_DESCRIBE_API_KEY": secret},
    ))

    assert secret not in repr(settings)


def test_provider_connection_repr_hides_api_key(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.providers.configuration import provider_connection

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "SENTINEL-PROVIDER-KEY"
    connection = provider_connection(
        "deepseek",
        config_snapshot=load_config_snapshot(workspace, environ={"API_KEY": secret}),
    )

    assert secret not in repr(connection)


def test_mcp_config_repr_hides_environment_and_headers(tmp_path):
    from nz_coder.mcp.config import MCPServerConfig

    secret = "SENTINEL-MCP-SECRET"
    config = MCPServerConfig(
        name="secret",
        environment=(("TOKEN", secret),),
        headers=(("Authorization", secret),),
    )

    assert secret not in repr(config)


def test_run_control_repr_does_not_contain_nested_secret(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.runtime.core.run_settings import RunSettings
    from nz_coder.runtime.execution.run_control import RunControlBundle

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "SENTINEL-NESTED-SECRET"
    snapshot = load_config_snapshot(
        workspace, environ={"NZ_IMAGE_DESCRIBE_API_KEY": secret},
    )
    bundle = RunControlBundle(
        config_snapshot=snapshot,
        permissions=None,
        plan_mode=None,
        skill_loader=None,
        hooks=None,
        mcp_runtime=None,
        model_runtime=None,
        provider_runtimes={},
        owns_provider_runtimes=False,
        run_settings=RunSettings.from_snapshot(snapshot),
    )

    assert secret not in repr(bundle)
