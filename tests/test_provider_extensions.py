"""Tests for installed Provider adapters and their Agent runtime consumer."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from nz_coder.providers import create_provider
from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.extensions import (
    ENTRY_POINT_GROUP,
    create_extension_provider,
    installed_provider_extensions,
)
from nz_coder.providers.models import save_model_selection
from nz_coder.providers.registry import _normalize_registry
from nz_coder.runtime.execution.loop import AgentLoop
from nz_coder.runtime.process.workdir import scoped_workdir


@dataclass
class _Distribution:
    name: str


class _EntryPoint:
    def __init__(self, name, value, loaded, *, distribution="test-package"):
        self.name = name
        self.value = value
        self.group = ENTRY_POINT_GROUP
        self.dist = _Distribution(distribution)
        self._loaded = loaded
        self.load_count = 0

    def load(self):
        self.load_count += 1
        if isinstance(self._loaded, BaseException):
            raise self._loaded
        return self._loaded


class _EntryPoints(list):
    def select(self, **kwargs):
        group = kwargs.get("group")
        return _EntryPoints(item for item in self if item.group == group)


class _Provider:
    name = "acme"

    def __init__(self, inputs):
        self.inputs = inputs
        self.client = object()
        self.create_count = 0

    def create_client(self):
        self.create_count += 1
        return self.client

    def create_completion(self, client, **kwargs):
        return (client, kwargs)

    def capabilities(self, model_id):
        return ModelCapabilities(provider=self.name, model_id=model_id)


def _install(monkeypatch, *entry_points):
    monkeypatch.setattr(
        "nz_coder.providers.extensions.importlib_metadata.entry_points",
        lambda: _EntryPoints(entry_points),
    )


def test_discovery_does_not_import_installed_adapter(monkeypatch):
    entry_point = _EntryPoint("Acme", "acme_provider:create", AssertionError("loaded"))
    _install(monkeypatch, entry_point)

    extensions = installed_provider_extensions()
    assert len(extensions) == 1
    extension = extensions[0]
    assert (extension.provider, extension.target, extension.distribution) == (
        "acme",
        "acme_provider:create",
        "test-package",
    )
    assert entry_point.load_count == 0


def test_create_provider_loads_selected_extension_with_connection(monkeypatch):
    calls = []

    def factory(**kwargs):
        calls.append(kwargs)
        return _Provider(kwargs)

    _install(monkeypatch, _EntryPoint("acme", "acme_provider:create", factory))
    client_factory = object()
    provider = create_provider(
        "acme",
        api_key="secret",
        base_url="https://provider.test/v1",
        client_factory=client_factory,
    )

    assert provider.name == "acme"
    assert calls == [{
        "provider_name": "acme",
        "api_key": "secret",
        "base_url": "https://provider.test/v1",
        "client_factory": client_factory,
    }]


def test_builtin_provider_cannot_be_shadowed_by_extension(monkeypatch):
    entry_point = _EntryPoint("openai", "shadow:create", AssertionError("loaded"))
    _install(monkeypatch, entry_point)

    provider = create_provider(
        "openai",
        api_key="key",
        base_url="https://provider.test/v1",
        client_factory=lambda **kwargs: kwargs,
    )

    assert provider.name == "openai"
    assert entry_point.load_count == 0


def test_duplicate_extension_owner_is_rejected_before_import(monkeypatch):
    first = _EntryPoint("acme", "one:create", lambda **_kwargs: None)
    second = _EntryPoint("ACME", "two:create", lambda **_kwargs: None)
    _install(monkeypatch, first, second)

    with pytest.raises(ValueError, match="Multiple installed Provider adapters"):
        create_extension_provider("acme", api_key="", base_url="")
    assert first.load_count == second.load_count == 0


@pytest.mark.parametrize(
    ("loaded", "message"),
    [
        (RuntimeError("broken import"), "Failed to import"),
        (object(), "not callable"),
        (lambda **_kwargs: object(), "must expose a non-empty name"),
    ],
)
def test_extension_load_failures_are_attributed(monkeypatch, loaded, message):
    _install(monkeypatch, _EntryPoint("acme", "broken:create", loaded))

    with pytest.raises((RuntimeError, TypeError), match=message):
        create_extension_provider("acme", api_key="", base_url="")


def test_extension_api_version_mismatch_is_rejected(monkeypatch):
    def factory(**kwargs):
        return _Provider(kwargs)

    factory.nz_coder_provider_api_version = 2
    _install(monkeypatch, _EntryPoint("acme", "future:create", factory))

    with pytest.raises(RuntimeError, match="API version 2"):
        create_extension_provider("acme", api_key="", base_url="")


def test_workspace_selection_drives_extension_into_agent_loop(monkeypatch, tmp_path):
    created = []

    def factory(**kwargs):
        provider = _Provider(kwargs)
        created.append(provider)
        return provider

    _install(monkeypatch, _EntryPoint("acme", "acme_provider:create", factory))
    with scoped_workdir(tmp_path):
        selection = save_model_selection("acme", "code-model")
        agent = AgentLoop("system", permission_mode="auto", trace_enabled=False)

    assert selection.provider == "acme"
    assert agent.provider is created[0]
    assert agent.client is created[0].client
    assert created[0].create_count == 1
    assert agent.model_id == "code-model"


def test_registry_accepts_installed_extension_provider_without_loading_it(monkeypatch):
    entry_point = _EntryPoint("acme", "acme_provider:create", AssertionError("loaded"))
    _install(monkeypatch, entry_point)
    snapshot = _normalize_registry(
        {
            "acme": {
                "id": "acme",
                "name": "Acme",
                "models": {
                    "friendly": {
                        "id": "deployments/acme-code",
                        "limit": {"context": 64_000, "output": 8_000},
                    }
                },
            }
        },
        "https://models.example.test/api.json",
    )

    model = snapshot["providers"]["acme"]["models"]["friendly"]
    assert model["api_model_id"] == "deployments/acme-code"
    assert entry_point.load_count == 0
