"""Effective configuration explanation and redaction contracts."""
from __future__ import annotations

import json

from nz_coder.foundation import config


def test_effective_config_reports_values_sources_and_no_credentials(
    monkeypatch, tmp_path,
):
    from nz_coder.interface.config_cli import collect_effective_config

    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "model-x")
    monkeypatch.setattr(config, "MODEL_VARIANT", "high")
    monkeypatch.setattr(config, "API_KEY", "must-never-appear")
    monkeypatch.setattr(config, "PERMISSION_MODE", "plan")
    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "missing-mcp.json"))
    monkeypatch.setattr(config, "MCP_PROJECT_CONFIG", ".nz-coder/mcp.json")
    monkeypatch.setenv("MODEL_ID", "model-x")
    monkeypatch.setenv("PERMISSION_MODE", "plan")

    result = collect_effective_config(tmp_path)

    assert result["provider"] == {"value": "openai-compatible", "source": "configuration"}
    assert result["model"] == {"value": "model-x", "source": "environment"}
    assert result["reasoning_effort"]["value"] == "high"
    assert result["permission_mode"] == {"value": "plan", "source": "environment"}
    assert result["semantic_status"]["value"] in {"disabled", "dependency-ready"}
    assert result["process_tier"]["value"] in {"pty", "pipe"}
    assert "must-never-appear" not in json.dumps(result)


def test_config_show_json_and_sources_are_machine_clean(monkeypatch, tmp_path, capsys):
    from nz_coder.interface.config_cli import config_main
    from nz_coder.runtime.process.workdir import scoped_workdir

    monkeypatch.setattr(config, "MCP_ENABLED", False)
    monkeypatch.setattr(config, "MCP_USER_CONFIG", str(tmp_path / "missing-mcp.json"))
    with scoped_workdir(tmp_path):
        assert config_main(["show", "--json"]) == 0
        flat = json.loads(capsys.readouterr().out)
        assert flat["model"]
        assert "source" not in json.dumps(flat["model"])

        assert config_main(["show", "--sources", "--json"]) == 0
        sourced = json.loads(capsys.readouterr().out)
        assert set(sourced["model"]) == {"value", "source"}


def test_top_level_cli_dispatches_config_show(monkeypatch):
    from nz_coder.interface import cli

    seen = []
    monkeypatch.setattr(
        "nz_coder.interface.config_cli.config_main",
        lambda args: seen.append(args) or 0,
    )
    assert cli.main(["config", "show", "--json"]) == 0
    assert seen == [["show", "--json"]]
