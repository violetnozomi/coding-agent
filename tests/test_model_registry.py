"""Tests for the models.dev-compatible capability registry."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
from types import SimpleNamespace

import pytest

from nz_coder.foundation import config
from nz_coder.foundation.user_paths import user_storage_layout
from nz_coder.providers import configured_model_capabilities
from nz_coder.providers.cli import models_main
from nz_coder.providers.registry import (
    load_registry_snapshot,
    registry_models,
    registry_runtime_model,
    registry_status,
    sync_model_registry,
)
from nz_coder.providers.models import save_model_selection
from nz_coder.runtime.process.workdir import scoped_workdir


def _registry_payload(context: int = 222_000) -> dict:
    return {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "models": {
                "registry-model": {
                    "id": "registry-model",
                    "name": "Registry Model",
                    "family": "registry-family",
                    "release_date": "2026-01-02",
                    "tool_call": False,
                    "reasoning": True,
                    "temperature": False,
                    "modalities": {"input": ["text", "image"], "output": ["text"]},
                    "limit": {"context": context, "output": 44_000},
                    "cost": {
                        "input": 1.0,
                        "output": 4.0,
                        "cache_read": 0.1,
                        "cache_write": 1.25,
                        "context_over_200k": {
                            "input": 2.0,
                            "output": 6.0,
                            "cache_read": 0.2,
                            "cache_write": 2.0,
                        },
                    },
                }
            },
        },
        "unsupported-cloud": {
            "name": "Ignored",
            "models": {
                "ignored": {
                    "limit": {"context": 1, "output": 1},
                }
            },
        },
    }


class _RegistryHandler(BaseHTTPRequestHandler):
    payload: object = _registry_payload()
    requests = 0

    def do_GET(self):
        type(self).requests += 1
        body = json.dumps(type(self).payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


@pytest.fixture
def registry_server():
    _RegistryHandler.requests = 0
    _RegistryHandler.payload = _registry_payload()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _RegistryHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api.json"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture(autouse=True)
def private_user_roots(tmp_path, monkeypatch):
    """Keep registry cache writes inside each test sandbox."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path.parent / f"{tmp_path.name}-state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path.parent / f"{tmp_path.name}-cache"))


def _registry_path(workspace):
    return user_storage_layout(workspace).workspace_cache / "models/registry.json"


def test_registry_sync_projects_exact_capabilities(tmp_path, registry_server, monkeypatch):
    monkeypatch.setattr(config, "MODEL_CATALOG_JSON", "")
    monkeypatch.setattr(config, "MODEL_CATALOG_PATH", "")
    result = sync_model_registry(registry_server, workspace=tmp_path)

    assert result.refreshed is True
    assert (result.provider_count, result.model_count) == (1, 1)
    target = _registry_path(tmp_path)
    assert os.stat(target).st_mode & 0o777 == 0o600
    persisted = json.loads(target.read_text(encoding="utf-8"))
    assert persisted["schema"] == "models.dev.normalized/v1"
    assert len(persisted["content_digest"]) == 64
    with scoped_workdir(tmp_path):
        capability = configured_model_capabilities("openai", "registry-model")
    assert capability.family == "registry-family"
    assert (capability.context_tokens, capability.output_tokens) == (222_000, 44_000)
    assert capability.supports_tools is False
    assert capability.supports_reasoning is True
    assert capability.supports_image_input is True
    assert capability.supports_temperature is False
    assert capability.source == "registry:openai/registry-model"
    runtime = registry_runtime_model("openai", "registry-model", tmp_path)
    assert runtime is not None and runtime.pricing is not None
    assert runtime.pricing.input == 1.0
    assert runtime.pricing.output == 4.0
    assert runtime.pricing.context_over_200k is not None
    assert runtime.pricing.context_over_200k.output == 6.0


def test_local_exact_catalog_overrides_registry(tmp_path, registry_server, monkeypatch):
    sync_model_registry(registry_server, workspace=tmp_path)
    monkeypatch.setattr(
        config,
        "MODEL_CATALOG_JSON",
        json.dumps({
            "models": {
                "openai/registry-model": {
                    "context_tokens": 333_000,
                    "supports_tools": True,
                }
            }
        }),
    )
    monkeypatch.setattr(config, "MODEL_CATALOG_PATH", "")

    with scoped_workdir(tmp_path):
        capability = configured_model_capabilities("openai", "registry-model")
    assert capability.context_tokens == 333_000
    assert capability.output_tokens == 44_000
    assert capability.supports_tools is True
    assert capability.supports_reasoning is True
    assert capability.source == "catalog:openai/registry-model"


def test_registry_freshness_and_force_refresh(tmp_path, registry_server):
    first = sync_model_registry(registry_server, workspace=tmp_path)
    cached = sync_model_registry(registry_server, workspace=tmp_path)
    forced = sync_model_registry(registry_server, workspace=tmp_path, force=True)

    assert first.refreshed is True
    assert cached.refreshed is False
    assert forced.refreshed is True
    assert _RegistryHandler.requests == 2
    assert registry_status(tmp_path)["fresh"] is True


def test_concurrent_sync_uses_one_cross_process_refresh(
    tmp_path,
    registry_server,
):
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda _index: sync_model_registry(
                    registry_server,
                    workspace=tmp_path,
                ),
                range(4),
            )
        )
    assert sum(result.refreshed for result in results) == 1
    assert _RegistryHandler.requests == 1


def test_invalid_refresh_preserves_previous_snapshot(
    tmp_path,
    registry_server,
):
    sync_model_registry(registry_server, workspace=tmp_path)
    before = load_registry_snapshot(tmp_path, strict=True)
    _RegistryHandler.payload = {"openai": {"models": {"broken": {}}}}

    with pytest.raises(ValueError, match="no supported provider models"):
        sync_model_registry(registry_server, workspace=tmp_path, force=True)
    assert load_registry_snapshot(tmp_path, strict=True) == before


@pytest.mark.parametrize(
    "url, message",
    [
        ("http://registry.example.test/api.json", "HTTPS or a loopback"),
        ("https://user:secret@models.example/api.json", "must not contain"),
        ("https://models.example/api.json?token=secret", "must not contain"),
    ],
)
def test_registry_rejects_unsafe_source_urls(tmp_path, url, message):
    with pytest.raises(ValueError, match=message):
        sync_model_registry(url, workspace=tmp_path)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), "bad"])
def test_registry_rejects_invalid_timeout_before_network(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        sync_model_registry(
            "http://127.0.0.1:9/api.json",
            workspace=tmp_path,
            timeout_seconds=timeout,
        )


def test_registry_strict_load_rejects_nonstandard_json_numbers(tmp_path):
    target = _registry_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(
        '{"version":1,"source":"local","providers":{},"extra":NaN}',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Invalid model registry cache"):
        load_registry_snapshot(tmp_path, strict=True)


def test_registry_path_must_stay_in_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MODEL_REGISTRY_PATH", "../registry.json")
    with pytest.raises(ValueError, match="escapes user cache"):
        load_registry_snapshot(tmp_path, strict=True)


def test_models_cli_sync_status_and_offline_list(
    tmp_path,
    registry_server,
    capsys,
):
    with scoped_workdir(tmp_path):
        assert models_main(["sync", "--url", registry_server]) == 0
        assert models_main(["registry-status"]) == 0
        assert models_main(["list", "--provider", "openai", "--details"]) == 0
    output = capsys.readouterr().out
    assert "1 provider(s), 1 model(s)" in output
    assert "fresh=true" in output
    assert "openai/registry-model" in output
    assert "context=222000 output=44000 tools=false" in output


def test_registry_preserves_logical_api_and_adapter_model_identities(
    tmp_path,
    registry_server,
):
    _RegistryHandler.payload = {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "api": "https://api.provider.test/v1",
            "npm": "@ai-sdk/openai",
            "models": {
                "friendly-code": {
                    "id": "deployments/team/code-model",
                    "name": "Friendly Code",
                    "provider": {
                        "api": "https://regional.provider.test/v1",
                        "npm": "@vendor/ai-sdk",
                    },
                    "limit": {"context": 123_000, "output": 12_000},
                }
            },
        }
    }

    sync_model_registry(registry_server, workspace=tmp_path)
    runtime = registry_runtime_model("openai", "friendly-code", tmp_path)
    listed = registry_models(tmp_path)

    assert runtime is not None
    assert runtime.model_id == "friendly-code"
    assert runtime.api_model_id == "deployments/team/code-model"
    assert runtime.adapter == "@vendor/ai-sdk"
    assert runtime.endpoint == "https://regional.provider.test/v1"
    assert listed == [runtime]


def test_agent_uses_api_model_id_but_keeps_logical_capability_identity(
    tmp_path,
    registry_server,
    monkeypatch,
):
    from nz_coder.runtime.execution import loop as loop_module

    _RegistryHandler.payload = {
        "openai": {
            "id": "openai",
            "name": "OpenAI",
            "models": {
                "friendly-code": {
                    "id": "deployments/team/code-model",
                    "name": "Friendly Code",
                    "family": "registry-family",
                    "limit": {"context": 123_000, "output": 12_000},
                }
            },
        }
    }
    requests = []

    class Provider:
        name = "openai"

        def create_client(self):
            return object()

        def capabilities(self, model_id):
            from nz_coder.providers import configured_model_capabilities

            return configured_model_capabilities(self.name, model_id)

        def create_completion(self, _client, **kwargs):
            requests.append(kwargs)
            message = SimpleNamespace(content="done", tool_calls=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="stop")],
                usage=None,
            )

    monkeypatch.setattr(loop_module, "create_provider", lambda _name=None: Provider())
    with scoped_workdir(tmp_path):
        sync_model_registry(registry_server)
        save_model_selection("openai", "friendly-code")
        agent = loop_module.AgentLoop("system", trace_enabled=False)
        result = agent._call_non_streaming([{"role": "user", "content": "hi"}])
    try:
        assert agent.model_id == "friendly-code"
        assert agent.request_model_id == "deployments/team/code-model"
        assert agent.model_capabilities.model_id == "friendly-code"
        assert agent.model_capabilities.context_tokens == 123_000
        assert requests[0]["model"] == "deployments/team/code-model"
        assert result.content == "done"
    finally:
        agent.close()
