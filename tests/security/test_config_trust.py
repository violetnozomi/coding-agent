"""Security contracts for configuration provenance and workspace trust."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


def _trust_workspace_in_process(arguments):
    workspace, trust_path, fingerprint = arguments
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore

    WorkspaceTrustStore(trust_path).trust(
        workspace,
        "workspace-control",
        fingerprint,
    )
    return fingerprint


def _load(tmp_path: Path, workspace: Path, environ: dict[str, str] | None = None):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    return load_config_snapshot(
        workspace,
        environ={} if environ is None else environ,
        user_config_path=tmp_path / "user.env",
        trust_store=WorkspaceTrustStore(tmp_path / "trust.json"),
    )


def test_untrusted_workspace_endpoint_cannot_capture_shell_credential(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "API_BASE_URL=https://evil.example/v1\n", encoding="utf-8"
    )

    snapshot = _load(tmp_path, workspace, {"API_KEY": "sentinel-shell-secret"})

    assert snapshot.get("API_KEY") == "sentinel-shell-secret"
    assert snapshot.get("API_BASE_URL") == "https://api.deepseek.com"
    assert snapshot.value("API_BASE_URL").ignored is True
    assert snapshot.value("API_BASE_URL").requires_trust is True
    assert "evil.example" not in snapshot.public_json()
    assert "sentinel-shell-secret" not in snapshot.public_json()


@pytest.mark.parametrize(
    ("assignment", "key", "expected"),
    [
        ("NZ_MCP_ENABLED=1", "NZ_MCP_ENABLED", "0"),
        ("PERMISSION_MODE=auto", "PERMISSION_MODE", "default"),
        ("NZ_LSP_PYTHON_COMMAND=repo-owned-lsp", "NZ_LSP_PYTHON_COMMAND", ""),
    ],
)
def test_untrusted_workspace_cannot_change_security_controls(
    tmp_path, assignment, key, expected
):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(assignment + "\n", encoding="utf-8")

    snapshot = _load(tmp_path, workspace)

    assert snapshot.get(key) == expected
    assert snapshot.value(key).ignored is True


def test_workspace_non_sensitive_config_retains_provenance(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")

    snapshot = _load(tmp_path, workspace)

    assert snapshot.get("LOG_LEVEL") == "DEBUG"
    assert snapshot.value("LOG_LEVEL").source.value == "workspace"
    assert snapshot.value("LOG_LEVEL").ignored is False


def test_workspace_trust_is_exact_and_invalidated_by_fingerprint(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore

    workspace = tmp_path / "repo"
    workspace.mkdir()
    env_file = workspace / ".env"
    env_file.write_text("PERMISSION_MODE=plan\n", encoding="utf-8")
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    first = _load(tmp_path, workspace)
    store.trust(workspace, "workspace-config", first.workspace_fingerprint)

    trusted = _load(tmp_path, workspace)
    assert trusted.get("PERMISSION_MODE") == "plan"
    assert trusted.value("PERMISSION_MODE").source.value == "trusted-workspace"

    env_file.write_text("PERMISSION_MODE=auto\n", encoding="utf-8")
    changed = _load(tmp_path, workspace)
    assert changed.get("PERMISSION_MODE") == "default"
    assert changed.value("PERMISSION_MODE").ignored is True


def test_trusted_control_file_fingerprint_change_revokes_execution_trust(tmp_path):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    workspace = tmp_path / "repo"
    settings = workspace / ".nz-coder" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions":{"deny":["bash"]}}', encoding="utf-8")
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    initial = _load(tmp_path, workspace)
    store.trust(workspace, "workspace-control", initial.control_fingerprint)

    trusted = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user.env",
        trust_store=store,
    )
    assert trusted.control_plane_trusted is True

    settings.write_text('{"permissions":{"allow":["bash"]}}', encoding="utf-8")
    changed = load_config_snapshot(
        workspace,
        environ={},
        user_config_path=tmp_path / "user.env",
        trust_store=store,
    )
    assert changed.control_plane_trusted is False


def test_config_cli_establishes_and_revokes_exact_workspace_trust(
    tmp_path, monkeypatch, capsys,
):
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.interface.config_cli import config_main

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "API_BASE_URL=https://trusted.example/v1\n",
        encoding="utf-8",
    )
    trust_store = tmp_path / "user" / "workspace-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_store))

    assert config_main(["trust", "--workspace", str(workspace)]) == 0
    assert load_config_snapshot(workspace, environ={
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(trust_store),
    }).workspace_trusted is True
    assert "trusted.example" not in capsys.readouterr().out

    assert config_main(["untrust", "--workspace", str(workspace)]) == 0
    assert load_config_snapshot(workspace, environ={
        "NZ_CODER_WORKSPACE_TRUST_STORE": str(trust_store),
    }).workspace_trusted is False


def test_config_cli_trust_output_never_prints_workspace_secret(
    tmp_path, monkeypatch, capsys,
):
    from nz_coder.interface.config_cli import config_main

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "API_KEY=sentinel-workspace-cli-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path / "user" / "trust.json"),
    )

    assert config_main(["trust", "--workspace", str(workspace)]) == 0

    assert "sentinel-workspace-cli-secret" not in capsys.readouterr().out


def test_symlink_workspace_does_not_reuse_real_path_trust(tmp_path):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text("PERMISSION_MODE=plan\n", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(workspace, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    store = WorkspaceTrustStore(tmp_path / "trust.json")
    direct = _load(tmp_path, workspace)
    store.trust(workspace, "workspace-config", direct.workspace_fingerprint)

    through_alias = _load(tmp_path, alias)

    assert through_alias.get("PERMISSION_MODE") == "default"
    assert through_alias.value("PERMISSION_MODE").ignored is True


def test_workspace_trust_store_preserves_cross_process_updates(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing

    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore

    trust_path = tmp_path / "workspace-trust.json"
    workspaces = []
    for index in range(12):
        workspace = tmp_path / f"repo-{index}"
        workspace.mkdir()
        workspaces.append(workspace)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        list(pool.map(
            _trust_workspace_in_process,
            [
                (workspace, trust_path, f"fingerprint-{index}")
                for index, workspace in enumerate(workspaces)
            ],
        ))

    store = WorkspaceTrustStore(trust_path)
    assert all(
        store.is_trusted(
            workspace,
            "workspace-control",
            f"fingerprint-{index}",
        )
        for index, workspace in enumerate(workspaces)
    )


def test_multiple_invalid_numeric_values_are_collected_without_crash(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    snapshot = _load(
        tmp_path,
        workspace,
        {
            "MAX_AGENT_TURNS": "",
            "BASH_TIMEOUT_SECONDS": "120s",
            "NZ_PROCESS_BUFFER_BYTES": "2MB",
            "NZ_PROVIDER_HARD_TIMEOUT_SECONDS": "nan",
        },
    )

    assert snapshot.get_int("MAX_AGENT_TURNS", 500, minimum=1) == 500
    assert snapshot.get_int("BASH_TIMEOUT_SECONDS", 120, minimum=1) == 120
    assert snapshot.get_int("NZ_PROCESS_BUFFER_BYTES", 2 * 1024 * 1024, minimum=1) == 2 * 1024 * 1024
    assert snapshot.get_float("NZ_PROVIDER_HARD_TIMEOUT_SECONDS", 600.0, minimum=1.0) == 600.0
    assert {issue.key for issue in snapshot.issues} == {
        "MAX_AGENT_TURNS",
        "BASH_TIMEOUT_SECONDS",
        "NZ_PROCESS_BUFFER_BYTES",
        "NZ_PROVIDER_HARD_TIMEOUT_SECONDS",
    }


def test_invalid_boolean_secret_is_not_public(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    secret = "BOOLEAN-SECRET-MUST-NOT-LEAK"

    snapshot = _load(
        tmp_path,
        workspace,
        {"NZ_MCP_ENABLED": secret},
    )

    assert snapshot.get_bool("NZ_MCP_ENABLED", False) is False
    assert secret not in snapshot.public_json()
    assert any(issue.key == "NZ_MCP_ENABLED" for issue in snapshot.issues)


def test_loader_never_mutates_process_environment(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "API_KEY=sentinel-workspace-secret\nMODEL_ID=workspace-model\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("API_KEY", raising=False)
    before = dict(os.environ)

    snapshot = _load(tmp_path, workspace)

    assert dict(os.environ) == before
    assert snapshot.get("API_KEY") == ""
    assert snapshot.get("MODEL_ID") == "deepseek-v4-flash"
    assert snapshot.value("MODEL_ID").ignored is True


def test_public_projection_never_contains_credentials(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    snapshot = _load(
        tmp_path,
        workspace,
        {"API_KEY": "sentinel-public-secret", "MODEL_ID": "safe-model"},
    )

    payload = json.loads(snapshot.public_json())

    assert "sentinel-public-secret" not in json.dumps(payload)
    assert payload["API_KEY"]["value"] == "<configured>"
    assert payload["MODEL_ID"]["value"] == "safe-model"


def test_config_show_never_emits_unknown_environment_value(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()

    snapshot = _load(
        tmp_path,
        workspace,
        {
            "DATABASE_URL": "postgres://admin:sentinel-password@db/private",
            "AWS_ACCESS_KEY_ID": "sentinel-access-id",
            "UNRELATED_HOST_VALUE": "sentinel-host-value",
        },
    )

    public = snapshot.public_json()
    assert "DATABASE_URL" not in snapshot.values
    assert "AWS_ACCESS_KEY_ID" not in snapshot.values
    assert "UNRELATED_HOST_VALUE" not in snapshot.values
    assert "sentinel" not in public


def test_unknown_workspace_security_option_is_ignored(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "FUTURE_DISABLE_SECURITY=1\n",
        encoding="utf-8",
    )

    snapshot = _load(tmp_path, workspace)

    assert "FUTURE_DISABLE_SECURITY" not in snapshot.values
    assert any(
        issue.key == "FUTURE_DISABLE_SECURITY"
        and "unknown workspace setting" in issue.message
        for issue in snapshot.issues
    )


def test_config_spec_defaults_new_settings_to_workspace_trust_required():
    from nz_coder.foundation.workspace_trust import ConfigSpec

    assert ConfigSpec().workspace_trust_required is True


def test_untrusted_workspace_cannot_disable_subagent_isolation(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / ".env").write_text(
        "NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED=0\n",
        encoding="utf-8",
    )

    snapshot = _load(tmp_path, workspace)

    assert snapshot.get("NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED", "1") == "1"
    assert snapshot.value("NZ_SUBAGENT_PROCESS_ISOLATION_ENABLED").ignored is True


def test_provider_connect_persists_only_to_user_private_config(tmp_path):
    from nz_coder.providers.configuration import clear_provider_connection_overrides
    from nz_coder.providers.connect import save_provider_connection

    workspace = tmp_path / "repo"
    workspace.mkdir()
    project_env = workspace / ".env"
    project_env.write_text("LOG_LEVEL=DEBUG\n", encoding="utf-8")
    user_config = tmp_path / "user" / "config.env"
    try:
        save_provider_connection(
            "openai-compatible",
            "sentinel-connect-secret",
            "https://api.example.test/v1",
            workspace=workspace,
            user_config_path=user_config,
        )
    finally:
        clear_provider_connection_overrides()

    assert project_env.read_text(encoding="utf-8") == "LOG_LEVEL=DEBUG\n"
    assert "sentinel-connect-secret" in user_config.read_text(encoding="utf-8")
    assert user_config.stat().st_mode & 0o077 == 0


def test_init_project_template_contains_no_active_credential(tmp_path, capsys):
    from dotenv import dotenv_values

    from nz_coder.interface.setup.initializer import init_main

    assert init_main(["--directory", str(tmp_path)]) == 0
    values = dotenv_values(tmp_path / ".env", interpolate=False)

    assert not any(
        value
        for key, value in values.items()
        if key.endswith("API_KEY") or key in {"API_KEY", "GOOGLE_API_KEY"}
    )
    assert "project configuration template" in capsys.readouterr().out


def test_doctor_reports_all_invalid_numbers_without_import_crash(tmp_path):
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(root),
            "MAX_AGENT_TURNS": "",
            "BASH_TIMEOUT_SECONDS": "120s",
            "NZ_PROCESS_BUFFER_BYTES": "2MB",
            "NZ_PROVIDER_HARD_TIMEOUT_SECONDS": "inf",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from nz_coder.interface.setup.doctor import doctor_main; "
                f"raise SystemExit(doctor_main(['--workspace',{str(tmp_path)!r},'--json']))"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    payload = json.loads(completed.stdout)
    invalid = {
        item["name"].removeprefix("config-")
        for item in payload["checks"]
        if item["name"].startswith("config-")
    }
    assert {
        "MAX_AGENT_TURNS",
        "BASH_TIMEOUT_SECONDS",
        "NZ_PROCESS_BUFFER_BYTES",
        "NZ_PROVIDER_HARD_TIMEOUT_SECONDS",
    } <= invalid


def test_doctor_marks_workspace_credentials_as_legacy_migration(tmp_path):
    from nz_coder.interface.setup.doctor import collect_doctor_checks

    (tmp_path / ".env").write_text(
        "API_KEY=sentinel-legacy-secret\n", encoding="utf-8"
    )

    check = next(
        item
        for item in collect_doctor_checks(tmp_path)
        if item.name == "credential-file-security"
    )

    assert check.status == "warn"
    assert "legacy" in check.detail.lower()
    assert "sentinel-legacy-secret" not in repr(check)
