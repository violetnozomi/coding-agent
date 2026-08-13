"""Behavior tests for the focused Context Runtime input boundary."""
from __future__ import annotations

from pathlib import Path

from nz_coder.runtime.adapters.context import context_from_legacy_host
from nz_coder.runtime.core.context import ContextExecutionContext
from nz_coder.state.context import prompt_budget


class _Tracer:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, event: str, **payload) -> None:
        self.events.append((event, payload))


class _LegacyHost:
    def __init__(self, workspace: Path) -> None:
        self.workdir = workspace
        self.tracer = _Tracer()
        self.compacted: list[list[dict]] = []
        self.stamped: list[list[dict]] = []

    def _prompt_budget(self):
        return prompt_budget(32_000, 4_000)

    def _projected_request_tokens(self, messages: list[dict]) -> int:
        return len(messages) * 17

    def _compact_messages(self, messages: list[dict]) -> list[dict]:
        compacted = [{"role": "system", "content": "summary"}]
        self.compacted.append(messages)
        return compacted

    def _stamp_auto_compaction(self, messages: list[dict]) -> None:
        self.stamped.append(messages)


def test_context_from_legacy_host_exposes_only_focused_operations(tmp_path) -> None:
    """Removing any adapter delegate would break a Context Runtime operation."""
    host = _LegacyHost(tmp_path)
    context = context_from_legacy_host(host)
    messages = [{"role": "user", "content": "inspect"}]

    assert isinstance(context, ContextExecutionContext)
    assert context.workspace == tmp_path.resolve()
    assert context.budget == prompt_budget(32_000, 4_000)
    assert context.projected_tokens(messages) == 17
    assert context.compact(messages) == [{"role": "system", "content": "summary"}]
    context.stamp_auto_compaction(messages)
    context.trace("context_event", count=1)

    assert host.compacted == [messages]
    assert host.stamped == [messages]
    assert host.tracer.events == [("context_event", {"count": 1})]


def test_context_execution_context_resolves_workspace_and_rejects_bad_callbacks(
    tmp_path,
) -> None:
    """An invalid capability must fail at composition instead of mid-run."""
    context = ContextExecutionContext(
        workspace=tmp_path / ".." / tmp_path.name,
        budget=prompt_budget(16_000, 2_000),
        projected_tokens=lambda _messages: 0,
        compact=lambda messages: messages,
        stamp_auto_compaction=lambda _messages: None,
        trace=lambda _event, **_payload: None,
    )
    assert context.workspace == tmp_path.resolve()

    try:
        ContextExecutionContext(
            workspace=tmp_path,
            budget=prompt_budget(16_000, 2_000),
            projected_tokens=None,  # type: ignore[arg-type]
            compact=lambda messages: messages,
            stamp_auto_compaction=lambda _messages: None,
            trace=lambda _event, **_payload: None,
        )
    except TypeError as exc:
        assert "projected_tokens" in str(exc)
    else:
        raise AssertionError("invalid projected_tokens callback was accepted")
