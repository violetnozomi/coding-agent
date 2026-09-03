"""Tests for provider model discovery, cache, and workspace selection."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from nz_coder.foundation import config
from nz_coder.providers.cli import models_main
from nz_coder.providers.models import (
    active_model_selection,
    cached_models,
    clear_model_selection,
    discover_models,
    save_model_selection,
)
from nz_coder.runtime.process.workdir import scoped_workdir


class _ModelsHandler(BaseHTTPRequestHandler):
    seen: list[tuple[str, dict[str, str]]] = []
    payload: dict = {"data": []}
    payloads: dict[str, dict] = {}

    def do_GET(self):
        type(self).seen.append((self.path, dict(self.headers)))
        payload = type(self).payloads.get(self.path, type(self).payload)
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def model_server():
    _ModelsHandler.seen = []
    _ModelsHandler.payloads = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_openai_discovery_caches_without_credentials(tmp_path, model_server):
    _ModelsHandler.payload = {
        "data": [
            {"id": "z-model", "owned_by": "vendor"},
            {"id": "a-model", "owned_by": "vendor"},
        ]
    }
    models = discover_models(
        "openai-compatible",
        api_key="super-secret",
        base_url=f"{model_server}/v1",
        workspace=tmp_path,
    )

    assert [item.model_id for item in models] == ["a-model", "z-model"]
    assert _ModelsHandler.seen[0][0] == "/v1/models"
    assert _ModelsHandler.seen[0][1]["Authorization"] == "Bearer super-secret"
    cache = tmp_path / ".nz-coder/models/catalog.json"
    assert "super-secret" not in cache.read_text()
    assert stat_mode(cache) == 0o600
    assert [item.model_id for item in cached_models(workspace=tmp_path)] == [
        "a-model",
        "z-model",
    ]


def test_gemini_discovery_filters_non_generation_models(tmp_path, model_server):
    _ModelsHandler.payload = {
        "models": [
            {
                "name": "models/gemini-code",
                "displayName": "Gemini Code",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/embedding-only",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }
    models = discover_models(
        "gemini",
        api_key="gem-key",
        base_url=model_server,
        workspace=tmp_path,
    )

    assert [(item.model_id, item.display_name) for item in models] == [
        ("gemini-code", "Gemini Code")
    ]
    headers = {key.lower(): value for key, value in _ModelsHandler.seen[0][1].items()}
    assert headers["x-goog-api-key"] == "gem-key"


def test_anthropic_discovery_follows_bounded_pagination(tmp_path, model_server):
    _ModelsHandler.payloads = {
        "/v1/models": {
            "data": [{"id": "claude-a", "display_name": "Claude A"}],
            "has_more": True,
            "last_id": "claude-a",
        },
        "/v1/models?after_id=claude-a": {
            "data": [{"id": "claude-b", "display_name": "Claude B"}],
            "has_more": False,
        },
    }
    models = discover_models(
        "anthropic",
        api_key="anthropic-key",
        base_url=model_server,
        workspace=tmp_path,
    )

    assert [item.model_id for item in models] == ["claude-a", "claude-b"]
    assert [path for path, _headers in _ModelsHandler.seen] == [
        "/v1/models",
        "/v1/models?after_id=claude-a",
    ]


def test_discovery_rejects_plaintext_remote_credentials():
    with pytest.raises(ValueError, match="HTTPS or a loopback"):
        discover_models(
            "openai-compatible",
            api_key="secret",
            base_url="http://models.example.test/v1",
        )


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "bad"])
def test_discovery_rejects_invalid_timeout_before_network(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        discover_models(
            "openai-compatible",
            api_key="key",
            base_url="http://127.0.0.1:9/v1",
            workspace=tmp_path,
            timeout_seconds=timeout,
        )


def test_model_selection_rejects_nonstandard_json_numbers(tmp_path):
    target = tmp_path / ".nz-coder/models/selection.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        '{"version":1,"provider":"gemini","model_id":"code","variant":NaN}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid model state"):
        active_model_selection(tmp_path)


def test_workspace_selection_round_trip_and_reset(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE",
        str(tmp_path.parent / f"{tmp_path.name}-workspace-trust.json"),
    )
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "fallback-model")
    selection = save_model_selection("gemini", "gemini-code", workspace=tmp_path)

    assert selection.source == "workspace"
    assert active_model_selection(tmp_path).model_id == "gemini-code"
    target = tmp_path / ".nz-coder/models/selection.json"
    assert stat_mode(target) == 0o600
    assert clear_model_selection(tmp_path) is True
    assert active_model_selection(tmp_path).model_id == "fallback-model"
    assert clear_model_selection(tmp_path) is False


def test_reset_revokes_previous_model_selection_generation(tmp_path, monkeypatch):
    trust_path = tmp_path.parent / f"{tmp_path.name}-workspace-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "fallback-model")
    save_model_selection("gemini", "gemini-code", workspace=tmp_path)
    payload = (tmp_path / ".nz-coder/models/selection.json").read_text(
        encoding="utf-8"
    )

    assert clear_model_selection(tmp_path) is True
    target = tmp_path / ".nz-coder/models/selection.json"
    target.write_text(payload, encoding="utf-8")

    assert active_model_selection(tmp_path).model_id == "fallback-model"


def test_untrusted_project_model_selection_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "safe-model")
    target = tmp_path / ".nz-coder/models/selection.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({
            "version": 1,
            "provider": "openai-compatible",
            "model_id": "repo-selected-expensive-model",
            "variant": "xhigh",
        }),
        encoding="utf-8",
    )

    selection = active_model_selection(tmp_path)

    assert selection.model_id == "safe-model"
    assert selection.source == "configuration"


def test_workspace_control_trust_does_not_authorize_model_selection(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import (
        WorkspaceTrustStore,
        load_config_snapshot,
    )

    trust_path = tmp_path.parent / f"{tmp_path.name}-workspace-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    monkeypatch.setattr(config, "MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setattr(config, "MODEL_ID", "fallback-model")
    target = tmp_path / ".nz-coder/models/selection.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({
            "version": 1,
            "provider": "gemini",
            "model_id": "gemini-code",
            "variant": None,
        }),
        encoding="utf-8",
    )
    snapshot = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path,
        "workspace-control",
        snapshot.control_fingerprint,
    )

    selection = active_model_selection(tmp_path)

    assert selection.provider == "openai-compatible"
    assert selection.model_id == "fallback-model"
    assert selection.source == "configuration"


def test_model_selection_does_not_revoke_workspace_control_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    trust_path = tmp_path.parent / f"{tmp_path.name}-selection-control-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions":{"deny":["bash"]}}', encoding="utf-8")
    before = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path, "workspace-control", before.control_fingerprint
    )

    save_model_selection("gemini", "gemini-code", workspace=tmp_path)

    after = load_config_snapshot(tmp_path)
    assert after.control_fingerprint == before.control_fingerprint
    assert after.control_plane_trusted is True


def test_external_model_selection_change_invalidates_dedicated_trust(
    tmp_path, monkeypatch,
):
    trust_path = tmp_path.parent / f"{tmp_path.name}-selection-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    monkeypatch.setattr(config, "MODEL_ID", "fallback-model")
    save_model_selection("gemini", "gemini-code", workspace=tmp_path)
    target = tmp_path / ".nz-coder" / "models" / "selection.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["model_id"] = "externally-changed"
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert active_model_selection(tmp_path).model_id == "fallback-model"


def test_model_selection_cannot_refresh_workspace_control_trust(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import load_config_snapshot

    trust_path = tmp_path.parent / f"{tmp_path.name}-no-control-trust.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    settings = tmp_path / ".nz-coder" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"permissions":{"allow":["bash"]}}', encoding="utf-8")
    assert load_config_snapshot(tmp_path).control_plane_trusted is False

    save_model_selection("gemini", "gemini-code", workspace=tmp_path)

    assert load_config_snapshot(tmp_path).control_plane_trusted is False


def test_selection_validates_exact_catalog_variant(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "MODEL_CATALOG_JSON",
        json.dumps({
            "models": {
                "openai-compatible/private": {
                    "variants": {"high": {"reasoning_effort": "high"}}
                }
            }
        }),
    )
    with scoped_workdir(tmp_path):
        selected = save_model_selection(
            "openai-compatible",
            "private",
            variant="high",
        )
        assert selected.variant == "high"
        with pytest.raises(ValueError, match="Unknown variant"):
            save_model_selection(
                "openai-compatible",
                "private",
                variant="maximum",
            )


def test_models_cli_select_current_and_reset(tmp_path, capsys):
    with scoped_workdir(tmp_path):
        assert models_main(["select", "gemini/gemini-code"]) == 0
        assert models_main(["current"]) == 0
        assert models_main(["reset"]) == 0

    output = capsys.readouterr().out
    assert "Selected gemini/gemini-code" in output
    assert "gemini/gemini-code (workspace)" in output
    assert "selection removed" in output


def test_models_cli_lists_cached_capability_details(
    tmp_path,
    model_server,
    capsys,
):
    _ModelsHandler.payload = {"data": [{"id": "qwen-plus"}]}
    discover_models(
        "openai-compatible",
        api_key="key",
        base_url=model_server,
        workspace=tmp_path,
    )
    with scoped_workdir(tmp_path):
        assert models_main(["list", "--details"]) == 0
    output = capsys.readouterr().out
    assert "openai-compatible/qwen-plus" in output
    assert "family=qwen" in output
    assert "cache openai-compatible:" in output


def test_agent_uses_workspace_model_selection(tmp_path, monkeypatch):
    import nz_coder.runtime.execution.loop as loop_module

    class Provider:
        name = "gemini"

        def create_client(self):
            return type("Client", (), {})()

        def capabilities(self, model_id):
            from nz_coder.providers import resolve_model_capabilities

            return resolve_model_capabilities(self.name, model_id)

    seen = []
    monkeypatch.setattr(
        loop_module,
        "create_provider",
        lambda name=None: seen.append(name) or Provider(),
    )
    with scoped_workdir(tmp_path):
        save_model_selection("gemini", "gemini-code")
        agent = loop_module.AgentLoop("base", trace_enabled=False)
    try:
        assert seen == ["gemini"]
        assert agent.model_id == "gemini-code"
        assert agent.model_capabilities.provider == "gemini"
    finally:
        agent.close()


def stat_mode(path: Path) -> int:
    """Return only permission bits for one state file."""
    return os.stat(path).st_mode & 0o777
