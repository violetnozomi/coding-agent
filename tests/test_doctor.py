"""Tests for offline terminal-product diagnostics and Provider readiness."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import stat

from rich.console import Console

from nz_coder.foundation import config
from nz_coder.interface.setup.doctor import (
    collect_doctor_checks,
    collect_repo_intelligence_checks,
    doctor_main,
    _check_private_state_security,
    _check_credential_file_security,
)
from nz_coder.foundation.private_paths import PrivatePathSecurity
from nz_coder.interface.setup.initializer import init_main
from nz_coder.providers.configuration import provider_connection


def _by_name(checks):
    return {check.name: check for check in checks}


def test_provider_connection_uses_provider_specific_credentials(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "compatible-secret")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "openai-secret")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "gemini-secret")

    assert provider_connection("openai-compatible").api_key == "compatible-secret"
    assert provider_connection("openai-responses").api_key == "openai-secret"
    assert provider_connection("anthropic").api_key == "anthropic-secret"
    assert provider_connection("gemini").api_key == "gemini-secret"


def test_doctor_collects_ready_configuration_without_network(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "test-model")
    monkeypatch.setattr(config, "API_KEY", "secret-not-for-output")
    monkeypatch.setattr(config, "API_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "missing-user.json"))
    monkeypatch.setattr(config, "MCP_PROJECT_CONFIG", ".nz-coder/mcp.json")
    monkeypatch.setattr(config, "LSP_ENABLED", False)

    checks = _by_name(collect_doctor_checks(tmp_path))

    assert checks["python"].status == "pass"
    assert checks["workspace"].status == "pass"
    assert checks["model"].status == "pass"
    assert checks["credential"].status == "pass"
    assert "secret-not-for-output" not in repr(checks)
    assert checks["provider-endpoint"].status == "pass"
    assert checks["web-search"].status == "pass"
    assert "live network not probed" in checks["web-search"].detail
    assert checks["credential"].category == "required"
    assert checks["lsp"].category == "optional"
    assert checks["repo-semantic-retrieval"].category == "experimental"


def test_doctor_reports_missing_credential_and_invalid_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "test-model")
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "API_BASE_URL", "http://public.example/v1")
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "missing-user.json"))
    monkeypatch.setattr(config, "LSP_ENABLED", False)

    checks = _by_name(collect_doctor_checks(tmp_path))

    assert checks["credential"].status == "fail"
    assert "API_KEY" in checks["credential"].action
    assert checks["provider-endpoint"].status == "fail"


def test_doctor_reports_invalid_and_disabled_web_search(monkeypatch, tmp_path):
    monkeypatch.setenv("NZ_CODER_WEB_SEARCH_PROVIDER", "unsupported")
    assert _by_name(collect_doctor_checks(tmp_path))["web-search"].status == "fail"

    monkeypatch.setenv("NZ_CODER_WEB_SEARCH_PROVIDER", "off")
    check = _by_name(collect_doctor_checks(tmp_path))["web-search"]
    assert check.status == "pass"
    assert "disabled" in check.detail


def test_doctor_json_is_secret_free_and_returns_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "test-model")
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "API_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "missing-user.json"))
    monkeypatch.setattr(config, "LSP_ENABLED", False)

    result = doctor_main(["--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 1
    assert payload["workspace"] == str(tmp_path)
    assert next(item for item in payload["checks"] if item["name"] == "credential")[
        "status"
    ] == "fail"
    assert all(item["category"] in {"required", "optional", "experimental"} for item in payload["checks"])


def test_doctor_accepts_headless_cwd_and_workspace_alias(
    monkeypatch, tmp_path, capsys,
):
    """Installation diagnostics must target a workspace without shell cd."""
    import nz_coder.interface.setup.doctor as doctor

    observed = []

    def collect(workspace=None):
        observed.append(workspace)
        return []

    monkeypatch.setattr(doctor, "collect_doctor_checks", collect)
    for option in ("--cwd", "--workspace"):
        result = doctor_main([option, str(tmp_path), "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert result == 0
        assert observed[-1] == tmp_path.resolve()
        assert payload["workspace"] == str(tmp_path.resolve())


def test_doctor_rich_table_contains_actions(monkeypatch, tmp_path):
    output = Console(record=True, force_terminal=False, width=140)
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "test-model")
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "API_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "missing-user.json"))
    monkeypatch.setattr(config, "LSP_ENABLED", False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)

    result = doctor_main([], output_console=output)
    rendered = output.export_text()

    assert result == 1
    assert "NZ-Coder doctor" in rendered
    assert "credential" in rendered
    assert "workspace .env" in rendered


def test_repo_intelligence_doctor_is_independent_of_provider(monkeypatch, tmp_path, capsys):
    import nz_coder.interface.setup.doctor as doctor

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(
        doctor,
        "_check_model",
        lambda _root: (_ for _ in ()).throw(AssertionError("provider probe ran")),
    )

    checks = _by_name(collect_repo_intelligence_checks(tmp_path))
    result = doctor_main(["--repo-intelligence-only", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert checks["repo-parser-python"].status == "pass"
    assert {item["name"] for item in payload["checks"]} == {
        "repo-parser-python",
        "repo-parser-ts-js",
        "repo-parser-go",
        "repo-watcher",
        "repo-lsp-augmentation",
        "repo-semantic-retrieval",
    }


def test_untrusted_workspace_dotenv_cannot_select_model_and_shell_wins(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / ".env").write_text(
        "API_KEY=workspace-key\nMODEL_ID=workspace-model\n",
        encoding="utf-8",
    )
    code = (
        "import json; from nz_coder.foundation import config; "
        "print(json.dumps([config.API_KEY, config.MODEL_ID]))"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root)
    environment.pop("API_KEY", None)
    environment.pop("MODEL_ID", None)
    loaded = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    environment["API_KEY"] = "shell-key"
    overridden = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )

    assert json.loads(loaded.stdout) == ["", "deepseek-v4-flash"]
    assert json.loads(overridden.stdout) == ["shell-key", "deepseek-v4-flash"]


def test_init_creates_private_config_and_refuses_overwrite(tmp_path, capsys, monkeypatch):
    import nz_coder.interface.setup.initializer as initializer

    hardened = []
    monkeypatch.setattr(
        initializer,
        "harden_private_path",
        lambda path: (
            hardened.append(Path(path))
            or PrivatePathSecurity(str(path), True, "A", "test")
        ),
    )
    assert init_main(["--directory", str(tmp_path)]) == 0
    target = tmp_path / ".env"
    original = target.read_text(encoding="utf-8")

    assert "API_KEY=" in original
    assert "MODEL_PROVIDER=openai-compatible" in original
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target in hardened
    assert init_main(["--directory", str(tmp_path)]) == 1
    assert target.read_text(encoding="utf-8") == original
    output = capsys.readouterr().out
    assert "owner-private" in output
    assert "refusing to overwrite" in output


def test_bundled_skill_uses_package_owned_path():
    skill = Path(config.SKILLS_DIR) / "code-review" / "SKILL.md"

    assert skill.is_file()
    assert "Code Review Skill" in skill.read_text(encoding="utf-8")


def test_windows_doctor_reports_verified_state_acl_and_unavailable_fallback(tmp_path):
    class API:
        def __init__(self, available, private):
            self.available = available
            self.private = private

        def is_available(self):
            return self.available

        def inspect(self, _path):
            return self.private

    state = tmp_path / ".nz-coder"
    state.mkdir()

    secure = _check_private_state_security(
        tmp_path, os_name="nt", windows_api=API(True, True),
    )
    unavailable = _check_private_state_security(
        tmp_path, os_name="nt", windows_api=API(False, False),
    )

    assert secure.status == "pass"
    assert "protected" in secure.detail
    assert unavailable.status == "warn"
    assert "Tier B" in unavailable.detail


def test_windows_doctor_verifies_persisted_credential_acl(tmp_path):
    class API:
        def is_available(self):
            return True

        def inspect(self, _path):
            return False

    (tmp_path / ".env").write_text("API_KEY=redacted\n", encoding="utf-8")

    check = _check_credential_file_security(
        tmp_path,
        os_name="nt",
        windows_api=API(),
    )

    assert check.status == "warn"
    assert "Tier B" in check.detail
    assert "redacted" not in repr(check)


def test_init_does_not_claim_owner_private_when_hardening_fails(
    tmp_path, capsys, monkeypatch,
):
    import nz_coder.interface.setup.initializer as initializer

    monkeypatch.setattr(
        initializer,
        "harden_private_path",
        lambda path: PrivatePathSecurity(str(path), False, "B", "denied"),
    )

    assert init_main(["--directory", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "best-effort" in output
    assert "nz-coder doctor" in output
    assert "owner-private permissions" not in output
