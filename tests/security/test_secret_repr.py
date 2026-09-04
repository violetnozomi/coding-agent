"""Regression tests for credential-bearing runtime object representations."""
from __future__ import annotations

from dataclasses import asdict, replace

import pytest


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
    # ConfigSnapshot intentionally contains immutable mapping proxies, so this
    # isolates the credential-bearing RunSettings projection itself.
    projected = asdict(replace(settings, snapshot=None))
    assert secret not in repr(projected)
    assert projected["image_api_key"] == "<redacted>"


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
    projected = asdict(connection)
    assert secret not in repr(projected)
    assert projected["api_key"] == "<redacted>"


def test_image_connection_repr_never_contains_secret(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.providers.configuration import image_provider_connection

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "SENTINEL-IMAGE-CONNECTION"
    snapshot = load_config_snapshot(workspace, environ={
        "API_KEY": "shared",
        "NZ_IMAGE_DESCRIBE_API_KEY": secret,
    })

    connection = image_provider_connection("deepseek", config_snapshot=snapshot)

    assert secret not in repr(connection)
    assert secret not in repr(asdict(connection))


def test_mcp_config_repr_hides_environment_and_headers(tmp_path):
    from nz_coder.mcp.config import MCPServerConfig

    secret = "SENTINEL-MCP-SECRET"
    config = MCPServerConfig(
        name="secret",
        environment=(("TOKEN", secret),),
        headers=(("Authorization", secret),),
    )

    assert secret not in repr(config)
    projected = asdict(config)
    assert secret not in repr(projected)
    assert projected["environment"] == (("TOKEN", "<redacted>"),)
    assert projected["headers"] == (("Authorization", "<redacted>"),)


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


def test_secret_sentinel_never_appears_in_assertion_diagnostics(tmp_path):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.providers.configuration import provider_connection

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    secret = "SENTINEL-ASSERTION-SECRET"
    connection = provider_connection(
        "deepseek",
        config_snapshot=load_config_snapshot(workspace, environ={"API_KEY": secret}),
    )

    with pytest.raises(AssertionError) as caught:
        assert connection == "an intentionally different value"

    assert secret not in str(caught.value)
