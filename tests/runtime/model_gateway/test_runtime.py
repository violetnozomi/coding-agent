"""Tests for resolved Provider/model runtime ownership."""
from __future__ import annotations

import asyncio

import pytest

from nz_coder.providers.capabilities import ModelCapabilities
from nz_coder.providers.registry import ModelPricing, RegistryModel
from nz_coder.runtime.model_gateway.runtime import (
    ModelSelectionRequest,
    resolve_model_runtime,
)
from nz_coder.runtime.model_gateway import ProductionModelGateway


class _Client:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _AsyncClient:
    def __init__(self) -> None:
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


class _FailOnceClient(_Client):
    def close(self) -> None:
        super().close()
        if self.close_calls == 1:
            raise RuntimeError("first close failed")


class _AsyncFailOnceClient(_AsyncClient):
    async def aclose(self) -> None:
        await super().aclose()
        if self.close_calls == 1:
            raise RuntimeError("first close failed")


class _Provider:
    name = "example"

    def __init__(self, client=None, *, base_url="https://one.example/v1") -> None:
        self.client = client or _Client()
        self.base_url = base_url
        self.create_calls = 0

    def create_client(self):
        self.create_calls += 1
        return self.client

    def capabilities(self, model_id: str) -> ModelCapabilities:
        return ModelCapabilities(
            provider=self.name,
            model_id=model_id,
            context_tokens=321_000,
        )


def _registry(provider: str, model_id: str, workspace=None):
    assert provider == "example"
    assert model_id == "logical-model"
    return RegistryModel(
        provider=provider,
        model_id=model_id,
        name="Logical model",
        release_date="",
        api_model_id="wire-model-2026-08",
        pricing=ModelPricing(input=1.0, output=2.0),
    )


def test_resolver_separates_logical_and_wire_identity() -> None:
    provider = _Provider()
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            variant="high",
            provider=provider,
            client=provider.client,
        ),
        registry_resolver=_registry,
    )

    assert runtime.provider_id == "example"
    assert runtime.model_id == "logical-model"
    assert runtime.request_model_id == "wire-model-2026-08"
    assert runtime.capabilities.context_tokens == 321_000
    assert runtime.pricing == ModelPricing(input=1.0, output=2.0)
    assert runtime.variant == "high"
    assert runtime.owns_client is False
    assert runtime.provider_instance_id.startswith("provider-instance-")


def test_provider_instance_identity_is_endpoint_bound_without_key_material() -> None:
    first = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(base_url="HTTPS://ONE.EXAMPLE:443/v1/"),
            credential_scope_id="account-primary",
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )
    equivalent = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(base_url="https://one.example/v1"),
            credential_scope_id="account-primary",
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )
    other_endpoint = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(base_url="https://two.example/v1"),
            credential_scope_id="account-primary",
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )
    other_scope = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(base_url="https://one.example/v1"),
            credential_scope_id="account-secondary",
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    assert first.provider_instance_id == equivalent.provider_instance_id
    assert first.provider_instance_id != other_endpoint.provider_instance_id
    assert first.provider_instance_id != other_scope.provider_instance_id
    assert "account-primary" not in first.provider_instance_id
    assert "one.example" not in first.provider_instance_id


def test_injected_client_is_never_closed_by_runtime() -> None:
    client = _Client()
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(client),
            client=client,
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    runtime.close()
    runtime.close()

    assert client.close_calls == 0


def test_created_client_is_owned_and_closed_once() -> None:
    provider = _Provider()
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=provider,
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    runtime.close()
    runtime.close()

    assert provider.create_calls == 1
    assert provider.client.close_calls == 1


def test_async_created_client_is_closed_once() -> None:
    client = _AsyncClient()
    provider = _Provider(client)
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=provider,
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())

    assert client.close_calls == 1


def test_created_provider_runtime_close_failure_is_retryable() -> None:
    client = _FailOnceClient()
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(client),
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="first close failed"):
        runtime.close()
    runtime.close()
    runtime.close()

    assert client.close_calls == 2


def test_async_created_provider_runtime_close_failure_is_retryable() -> None:
    client = _AsyncFailOnceClient()
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=_Provider(client),
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(RuntimeError, match="first close failed"):
        asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())
    asyncio.run(runtime.aclose())

    assert client.close_calls == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("context_tokens", float("inf")),
        ("output_tokens", float("nan")),
        ("default_temperature", float("nan")),
    ],
)
def test_resolver_rejects_nonfinite_capabilities_before_client_creation(
    field: str,
    value: float,
) -> None:
    """An invalid adapter snapshot must fail before allocating its SDK client."""
    class MalformedProvider(_Provider):
        def capabilities(self, model_id: str) -> ModelCapabilities:
            values = {field: value}
            return ModelCapabilities(
                provider=self.name,
                model_id=model_id,
                **values,
            )

    provider = MalformedProvider()

    with pytest.raises(ValueError, match=field):
        resolve_model_runtime(
            ModelSelectionRequest(
                provider_name="example",
                model_id="logical-model",
                provider=provider,
            ),
            registry_resolver=lambda *_args, **_kwargs: None,
        )

    assert provider.create_calls == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_retries": float("inf")},
        {"max_retries": True},
        {"poll_interval": float("nan")},
        {"backoff_base": float("inf")},
    ],
)
def test_gateway_rejects_malformed_runtime_policy(kwargs) -> None:
    provider = _Provider()
    runtime = resolve_model_runtime(
        ModelSelectionRequest(
            provider_name="example",
            model_id="logical-model",
            provider=provider,
            client=provider.client,
        ),
        registry_resolver=lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError):
        ProductionModelGateway(runtime, **kwargs)
