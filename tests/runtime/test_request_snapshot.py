"""Candidates must reuse one request's instructions without consuming state."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from nz_coder.runtime.conversation.prompt_builder import ProductionPromptBuilder


def _host(monkeypatch):
    instructions = SimpleNamespace(
        reminder="Repository rule: preserve APIs.", source_count=1, included_count=1,
        truncated_count=0, per_file_truncated_count=0, total_truncated_count=0,
        omitted_count=0, included_bytes=32, paths=["AGENTS.md"], disabled_count=0,
        warnings=[],
    )
    monkeypatch.setattr(
        "nz_coder.runtime.conversation.prompt_builder.load_instruction_context",
        lambda _path: instructions,
    )
    hooks = ["One-time note: retain the failing assertion."]
    state = SimpleNamespace(
        strict_progress_nudges=0, investigation_calls_since_edit=12,
        mutation_generation=0, task_assessment_state={}, plan_revision=0,
    )

    def render_state(strict=False):
        state.strict_progress_nudges += 1
        return "Current state: implement the parser fix."

    state.build_prompt_block = render_state
    tools = [{"type": "function", "function": {"name": "read_file",
              "parameters": {"type": "object", "properties": {}}}}]
    host = SimpleNamespace(
        _structured_output_active_repair="", runtime_profile="main",
        _sp=SimpleNamespace(build_prompt_block=lambda: ""),
        _memory_block=lambda _query: "", runtime_state=state,
        _project_profile_block=lambda: "", _hook_prompt_block=lambda: hooks.pop() if hooks else "",
        _lineage_recovery_block=lambda _messages: "", _implementation_bundle_block=lambda _query: "",
        _repo_retrieval_block=lambda _query: "", system_prompt="Stable rules.",
        _plan_mode_prompt_block=lambda: "", _sanitize_messages=copy.deepcopy,
        _active_tool_specs=lambda: tools,
        tracer=SimpleNamespace(log=lambda *_args, **_kwargs: None),
    )
    return host, tools, hooks


def test_candidate_projection_keeps_frozen_layers_and_tool_definitions(monkeypatch):
    """Changing source history cannot rebuild or lose a consumed dynamic note."""
    host, tools, hooks = _host(monkeypatch)
    history = [{"role": "user", "content": "Fix the parser."}]
    frame = ProductionPromptBuilder().freeze(host, history)
    assert hooks == []
    assert host.runtime_state.strict_progress_nudges == 1

    tools[0]["function"]["name"] = "changed_after_snapshot"
    host.system_prompt = "changed rules"
    candidate = [{"role": "user", "content": "Continue the parser fix."}]
    for _ in range(3):
        wire = frame.project(candidate)
        assert wire[0] == {"role": "system", "content": "Stable rules."}
        assert wire[1]["content"] == "Repository rule: preserve APIs.\n\nContinue the parser fix."
        assert "One-time note: retain the failing assertion." in wire[-1]["content"]
        assert "Current state: implement the parser fix." in wire[-1]["content"]
        assert frame.tools[0]["function"]["name"] == "read_file"
    assert host.runtime_state.strict_progress_nudges == 1
    assert history == [{"role": "user", "content": "Fix the parser."}]
    assert candidate == [{"role": "user", "content": "Continue the parser fix."}]


def test_snapshot_estimate_counts_frozen_tool_schema_and_attachment(monkeypatch):
    """Final budget comparisons must include schemas and visible media payloads."""
    host, _tools, _hooks = _host(monkeypatch)
    small = [{"role": "user", "content": "Inspect this."}]
    frame = ProductionPromptBuilder().freeze(host, small)
    large = [{"role": "user", "content": [
        {"type": "text", "text": "Inspect this."},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 8000}},
    ]}]
    assert frame.estimate(large) > frame.estimate(small) + 1000


def test_snapshot_detects_history_changes_during_candidate_generation(monkeypatch):
    host, _tools, _hooks = _host(monkeypatch)
    history = [{"role": "user", "content": "Fix the parser."}]
    frame = ProductionPromptBuilder().freeze(host, history)
    assert frame.matches_source(history)
    history.append({"role": "user", "content": "Preserve the public API."})
    assert not frame.matches_source(history)


def test_model_runtime_uses_frozen_tools_when_host_context_is_recreated():
    from nz_coder.runtime.core.model_context import scoped_request_tools
    from nz_coder.runtime.execution.services import ProductionTurnModelRuntime

    frozen = [{"type": "function", "function": {"name": "read_file"}}]
    context = SimpleNamespace(
        capabilities=lambda: None,
        active_tool_specs=lambda: [{"type": "function", "function": {"name": "unexpected"}}],
    )
    with scoped_request_tools(frozen):
        frozen.clear()
        assert ProductionTurnModelRuntime._tools(context) == [
            {"type": "function", "function": {"name": "read_file"}},
        ]
    assert ProductionTurnModelRuntime._tools(context)[0]["function"]["name"] == "unexpected"


def test_streaming_exit_uses_the_same_frozen_tools_as_preflight():
    from nz_coder.runtime.core.model_context import scoped_request_tools
    from nz_coder.runtime.execution.provider_stream import _active_tools

    host = SimpleNamespace(model_capabilities=None, _active_tool_specs=lambda: [
        {"type": "function", "function": {"name": "changed_after_freeze"}},
    ])
    frozen = [{"type": "function", "function": {"name": "frozen"}}]
    with scoped_request_tools(frozen):
        assert _active_tools(host) == frozen
    assert _active_tools(host)[0]["function"]["name"] == "changed_after_freeze"


def test_candidate_keeps_current_processor_message_identity(monkeypatch):
    """Installing a copied tail must not detach the active processor owner."""
    from dataclasses import replace

    host, _tools, _hooks = _host(monkeypatch)
    owner = {"role": "assistant", "content": "", "_nz_message_id": "active-step"}
    frame = replace(ProductionPromptBuilder().freeze(host, [owner]), retained=(owner,))
    candidate = [{"role": "user", "content": "summary"}, copy.deepcopy(owner)]
    restored = frame.restore_retained(candidate)
    assert restored[-1] is owner
    owner["content"] = "Visible final result."
    assert restored[-1]["content"] == "Visible final result."
    assert candidate[-1]["content"] == ""


def test_summary_retry_does_not_mutate_the_active_coding_source(monkeypatch):
    """Auxiliary retry bookkeeping must not invalidate an otherwise fresh summary."""
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.runtime.session.session_processor import SessionProcessor

    host, _tools, _hooks = _host(monkeypatch)
    owner = {"role": "assistant", "content": "", "_nz_message_id": "msg-active-coding"}
    processor = SessionProcessor(owner)
    processor.start_step()
    messages = [{"role": "user", "content": "Fix the parser."}, owner]
    host._active_session_processor = processor
    host._active_processor_messages = messages
    host.provider_id = "test"
    import threading
    host._session_checkpoint_lock = threading.RLock()
    host._session_checkpoint_writer = None
    host._session_checkpoint_retired = False
    host._model_gateway_observer_for_run = lambda: AgentLoop._model_gateway_observer_for_run(host)
    host._observe_model_gateway = lambda *args: AgentLoop._observe_model_gateway(host, *args)
    host._checkpoint_messages = lambda *_args: None
    frame = ProductionPromptBuilder().freeze(host, messages)

    AgentLoop._model_gateway_observer(host, "model_call_retry", {
        "purpose": "compaction", "attempt": 2, "wait_seconds": 0,
        "error": "transient transport failure",
    })
    assert frame.matches_source(messages)
