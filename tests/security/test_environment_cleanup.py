"""Exhaustive, retryable ProductRunEnvironment cleanup contracts."""
from __future__ import annotations

import threading


class _Closer:
    def __init__(self, failures=0):
        self.failures = failures
        self.calls = 0
        self.events = []

    def close(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("cleanup-secret-must-not-escape")

    def log(self, event, **payload):
        self.events.append((event, payload))


def _environment():
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

    environment = ProductRunEnvironment.__new__(ProductRunEnvironment)
    environment._environment_cleanup_lock = threading.RLock()
    environment._environment_cleanup_completed = {
        "run-control", "stall-sidecar", "background-agents", "repo-intelligence",
    }
    environment._environment_cleanup_failures = {}
    environment._environment_provider_ids_closed = set()
    environment._mcp_runtime_lock = threading.RLock()
    environment._mcp_runtime = _Closer(failures=1)
    environment._sidecar_verifier_handle = _Closer()
    environment.image_describer = _Closer()
    failing_provider = _Closer(failures=1)
    successful_provider = _Closer()
    environment._provider_runtimes = {
        ("failing", "model"): failing_provider,
        ("successful", "model"): successful_provider,
    }
    environment._owns_event_bus = True
    environment.event_bus = _Closer()
    environment.tracer = _Closer()
    return environment, failing_provider, successful_provider


def test_environment_close_attempts_all_resources():
    environment, failing_provider, successful_provider = _environment()

    environment.close()

    assert environment._mcp_runtime.calls == 1
    assert environment._sidecar_verifier_handle.calls == 1
    assert environment.image_describer.calls == 1
    assert failing_provider.calls == 1
    assert successful_provider.calls == 1
    assert environment.event_bus.calls == 1
    assert environment.tracer.calls == 1
    assert environment.environment_cleanup_failures == {
        "mcp": "RuntimeError", "provider-runtimes": "RuntimeError",
    }
    assert "cleanup-secret-must-not-escape" not in repr(environment.tracer.events)


def test_environment_close_is_retryable():
    environment, failing_provider, successful_provider = _environment()
    mcp = environment._mcp_runtime

    environment.close()
    environment.close()

    assert mcp.calls == 2
    assert failing_provider.calls == 2
    assert successful_provider.calls == 1
    assert environment._sidecar_verifier_handle.calls == 1
    assert environment.image_describer.calls == 1
    assert environment.event_bus.calls == 1
    assert environment.tracer.calls == 1
    assert environment.environment_cleanup_failures == {}
    assert environment.environment_cleanup_complete is True
