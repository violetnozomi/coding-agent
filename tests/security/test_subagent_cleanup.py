"""Subagent single-owner cleanup and result-preservation contracts."""
from __future__ import annotations

import pytest


class _Closer:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    def close(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("cleanup-secret-must-not-escape")


class _Tracer:
    def __init__(self):
        self.events = []

    def log(self, event, **payload):
        self.events.append((event, payload))


def test_subagent_runtime_has_single_owner():
    from nz_coder.runtime.agent.subagent import _cleanup_subagent_resources

    environment = _Closer()
    runtime = _Closer()
    _cleanup_subagent_resources(environment, runtime, _Tracer())

    assert environment.calls == 1
    assert runtime.calls == 0


def test_subagent_model_close_failure_preserves_success():
    from nz_coder.runtime.agent.subagent import _cleanup_subagent_resources

    environment = _Closer(fail=True)
    runtime = _Closer(fail=True)
    tracer = _Tracer()
    result = {"status": "completed", "answer": "kept"}

    _cleanup_subagent_resources(environment, runtime, tracer)

    assert result == {"status": "completed", "answer": "kept"}
    assert environment.calls == 1
    assert runtime.calls == 1
    assert tracer.events == [(
        "subagent_cleanup_failed",
        {"resource": "environment", "failure_type": "RuntimeError"},
    )]
    assert "cleanup-secret-must-not-escape" not in repr(tracer.events)


def test_subagent_model_close_failure_preserves_provider_error():
    from nz_coder.runtime.agent.subagent import _cleanup_subagent_resources

    primary = RuntimeError("provider failed")
    try:
        raise primary
    except RuntimeError as caught:
        _cleanup_subagent_resources(None, _Closer(fail=True), _Tracer())
        preserved = caught

    assert preserved is primary


def test_subagent_agent_close_failure_always_resets_parent_context(monkeypatch):
    from nz_coder.runtime.agent import subagent

    outer = {"agent_id": "parent"}
    outer_token = subagent._PARENT_CONTEXT.set(outer)
    child_token = subagent._PARENT_CONTEXT.set({"agent_id": "child"})
    monkeypatch.setattr(
        subagent,
        "_cleanup_subagent_resources",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    try:
        with pytest.raises(RuntimeError, match="close failed"):
            subagent._cleanup_subagent_scope(None, None, None, child_token)
        assert subagent._PARENT_CONTEXT.get() == outer
    finally:
        subagent._PARENT_CONTEXT.reset(outer_token)
