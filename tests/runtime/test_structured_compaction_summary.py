"""摘要字段白名单、真实压缩入口及失败不安装边界。"""
from __future__ import annotations

import asyncio
import copy
import importlib
import json
from types import SimpleNamespace

import pytest

from nz_coder.protocol.public_error import PublicRuntimeError
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.sessions import scoped_session
from tests.fixtures.compaction_summary import summary_document, summary_json


def codec():
    return importlib.import_module("nz_coder.state.compaction_summary")


def test_known_fields_survive_and_nested_unknown_fields_are_removed():
    original = summary_document(
        "修复 parser，并保留公开 API",
        constraints=["禁止安装依赖"],
        progress={"done": ["已修改 src/parser.py"], "in_progress": [], "blocked": [],
                  "internal": "DROP_PROGRESS"},
        findings=[{"summary": "锁顺序是 A→B", "certainty": "confirmed", "basis": "src/parser.py:12",
                   "private": "DROP_FINDING"}],
        decisions=[{"decision": "只改解析入口", "reason": "保持接口", "extra": "DROP_DECISION"}],
        verification=[{"command": "pytest tests/test_parser.py", "result": "FAILED: expected 3, got 2\nTraceback: parser.py:21",
                       "extra": "DROP_VERIFICATION"}],
        next_steps=["修复后复测同一条命令"],
        relevant_files=[{"path": "src/parser.py", "reason": "当前修改中", "extra": "DROP_FILE"}],
        undocumented="DROP_TOP",
    )
    before = copy.deepcopy(original)
    normalized, removed = codec().parse_compaction_summary(json.dumps(original, ensure_ascii=False))
    rendered = codec().render_compaction_summary(normalized)

    assert removed == 6
    assert "DROP_" not in json.dumps(normalized)
    assert "DROP_" not in rendered
    for text in (original["goal"], "禁止安装依赖", "锁顺序是 A→B", "src/parser.py:12",
                 "pytest tests/test_parser.py", "expected 3, got 2", "Traceback: parser.py:21"):
        assert text in rendered
    assert original == before
    assert "## Verification" in rendered
    assert "[confirmed]" in rendered


@pytest.mark.parametrize("field", list(summary_document()))
def test_missing_required_top_level_field_is_rejected(field):
    document = summary_document()
    del document[field]
    with pytest.raises(PublicRuntimeError, match=field):
        codec().parse_compaction_summary(json.dumps(document))


@pytest.mark.parametrize("field,value", [
    ("goal", None), ("goal", " "), ("constraints", "keep API"),
    ("constraints", [5]), ("progress", []),
    ("progress", {"done": [], "in_progress": []}),
    ("progress", {"done": "done", "in_progress": [], "blocked": []}),
    ("findings", [{"summary": "finding", "certainty": "certain", "basis": ""}]),
    ("findings", [{"summary": "finding", "certainty": "hypothesis"}]),
    ("decisions", [{"decision": "decision", "reason": 5}]),
    ("verification", [{"command": "pytest", "result": ""}]),
    ("relevant_files", [{"path": "file.py"}]),
    ("next_steps", [False]),
])
def test_bad_known_field_is_not_hidden_by_discarding_unknown_fields(field, value):
    document = summary_document(**{field: value}, unused={"anything": "drop me"})
    with pytest.raises(PublicRuntimeError, match="summary"):
        codec().parse_compaction_summary(json.dumps(document))


@pytest.mark.parametrize("raw", [
    "", "## Goal\n- continue", "```json\n{}\n```", "[]", "null",
    '{"goal":"first","goal":"second"}',
    summary_json()[:-1] + ',"unknown":NaN}',
])
def test_non_object_or_ambiguous_json_is_rejected(raw):
    with pytest.raises(PublicRuntimeError, match="summary"):
        codec().parse_compaction_summary(raw)


def test_hypotheses_and_empty_evidence_remain_explicit_without_fact_inference():
    document = summary_document(findings=[
        {"summary": "可能存在取消竞态", "certainty": "hypothesis", "basis": ""},
        {"summary": "用户声称的结论", "certainty": "confirmed", "basis": ""},
    ])
    normalized, count = codec().parse_compaction_summary(json.dumps(document))
    assert normalized == document
    assert count == 0
    assert "[hypothesis]" in codec().render_compaction_summary(normalized)


def test_formal_compaction_filters_extra_fields_and_preserves_user_originals(tmp_path, monkeypatch):
    from nz_coder.state import context
    from tests.test_context_budget import _CompactionClient

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    observed = []
    messages = [{"role": "user", "content": "保留 API，并禁止安装依赖。", "_nz_message_id": "msg-original"},
                {"role": "assistant", "content": "long investigation " * 2000}]
    original = copy.deepcopy(messages)
    client = _CompactionClient(summary_json("修复 parser", unexpected={"content": "DROP_THIS_OUTPUT"}))
    with scoped_workdir(tmp_path), scoped_session("structured-summary"):
        result = context.auto_compact(messages, client, "fake-model", focus="unicode",
            current_facts="CURRENT_VERIFICATION_UNKNOWN",
            observer=lambda event, data: observed.append((event, data)))

    assert messages == original
    assert messages[0] in result
    assert "修复 parser" in result[0]["content"]
    assert "DROP_THIS_OUTPUT" not in str(result)
    validated = next(data for event, data in observed if event == "compaction_summary_validated")
    assert validated["discarded_fields"] == 1
    assert "JSON" in client.request["messages"][0]["content"]
    assert "CURRENT_VERIFICATION_UNKNOWN" in client.request["messages"][1]["content"]
    assert "unicode" in client.request["messages"][1]["content"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_invalid_summary_never_installs_or_stamps_a_runtime_candidate(tmp_path, monkeypatch, asynchronous):
    from nz_coder.state import context
    from nz_coder.runtime.conversation.context_manager import ProductionContextManager
    from tests.runtime.test_compaction_candidates import _context
    from tests.test_context_budget import _CompactionClient

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    messages = [{"role": "user", "content": "Fix parser"},
                {"role": "assistant", "content": "investigation " * 3000}]
    original = copy.deepcopy(messages)

    def compact(rows):
        return context.auto_compact(rows, _CompactionClient('{"goal":"partial"}'), "fake-model")

    runtime, frame, stamps = _context(tmp_path, messages, compact, usable=2000)
    with scoped_workdir(tmp_path), scoped_session("invalid-structured-summary"):
        with pytest.raises(PublicRuntimeError, match="summary"):
            if asynchronous:
                asyncio.run(ProductionContextManager().prepare_async(runtime, messages, request_snapshot=frame))
            else:
                ProductionContextManager().prepare_sync(runtime, messages, request_snapshot=frame)
    assert messages == original
    assert stamps == []


def test_failed_payload_retry_preserves_original_tool_evidence(tmp_path, monkeypatch):
    from nz_coder.state import context
    from tests.test_context_budget import _SequencedCompactionClient

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    messages = [{"role": "user", "content": "Keep the current API"},
                {"role": "tool", "tool_call_id": "old", "content": "original evidence " * 2000},
                {"role": "user", "content": "Continue"},
                {"role": "assistant", "content": "recent"},
                {"role": "user", "content": "Verify"}]
    original = copy.deepcopy(messages)
    client = _SequencedCompactionClient([
        RuntimeError("request entity too large"), RuntimeError("context window exceeded")])
    with scoped_workdir(tmp_path), scoped_session("structured-retry-failure"):
        with pytest.raises(RuntimeError, match="context window exceeded"):
            context.auto_compact(messages, client, "fake-model")
    assert len(client.requests) == 2
    assert messages == original


@pytest.mark.parametrize("finish,tool_calls", [("length", []), ("tool_calls", [
    {"id": "bad", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
])])
def test_valid_json_with_incomplete_or_tool_call_completion_is_not_installed(tmp_path, monkeypatch, finish, tool_calls):
    from nz_coder.state import context
    from tests.test_context_budget import _CompactionClient

    class IncompleteClient(_CompactionClient):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            response.choices[0].finish_reason = finish
            response.choices[0].message.tool_calls = tool_calls
            return response

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    messages = [{"role": "user", "content": "Keep API"}]
    original = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("structured-incomplete-response"):
        with pytest.raises(PublicRuntimeError, match="summary"):
            context.auto_compact(messages, IncompleteClient(summary_json()), "fake-model")
    assert messages == original


@pytest.mark.parametrize("entrypoint", ["command", "tool_hook"])
@pytest.mark.parametrize("valid", [False, True])
def test_manual_compaction_uses_same_validation_before_replacing_history(tmp_path, monkeypatch, entrypoint, valid):
    from nz_coder.state import context
    from nz_coder.interface.commands.handlers.core import handle_compact
    from nz_coder.runtime.verification.hooks import manual_compact_hook
    from tests.test_context_budget import _CompactionClient

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    messages = [{"role": "user", "content": "Keep the current API"}]
    before = copy.deepcopy(messages)
    response = summary_json("Continue API work") if valid else '{"goal":"missing required fields"}'
    retired = []

    def compact(rows, **_kwargs):
        return context.auto_compact(rows, _CompactionClient(response), "fake-model")

    if entrypoint == "command":
        ctx = SimpleNamespace(history=messages, args="", controller=None,
                              console=SimpleNamespace(print=lambda *_args: None),
                              agent=SimpleNamespace(_compact_messages=compact))
        def invoke():
            handle_compact(ctx)
    else:
        ctx = SimpleNamespace(messages=messages, manual_compact=True, on_text=None,
            loop=SimpleNamespace(_compact_messages=compact,
                tracer=SimpleNamespace(log=lambda *_args, **_kw: None),
                _on_context_compacted=lambda: retired.append("context")))
        def invoke():
            manual_compact_hook(ctx)
    with scoped_workdir(tmp_path), scoped_session("manual-structured-summary"):
        if valid:
            invoke()
            assert "Continue API work" in messages[0]["content"]
            assert before[0] in messages
            assert retired == (["context"] if entrypoint == "tool_hook" else [])
        else:
            with pytest.raises(PublicRuntimeError, match="summary"):
                invoke()
            assert messages == before
            assert retired == []


def test_formal_compaction_preserves_reasoning_tag_literals_inside_json_values(tmp_path, monkeypatch):
    from nz_coder.state import context
    from tests.test_context_budget import _CompactionClient

    class ExplicitStopClient(_CompactionClient):
        def create(self, **kwargs):
            response = super().create(**kwargs)
            response.choices[0].finish_reason = "stop"
            return response

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    goal = "修复 </think>、</thinking> 和 <think>literal</think> 的字面量解析"
    with scoped_workdir(tmp_path), scoped_session("summary-literal-tags"):
        result = context.auto_compact([{"role": "user", "content": "Repair the tag parser"}],
            ExplicitStopClient(summary_json(goal)), "fake-model")
    assert goal in result[0]["content"]


@pytest.mark.parametrize("finish", [None, "", "absent"])
def test_formal_compaction_requires_explicit_provider_completion_marker(tmp_path, monkeypatch, finish):
    from nz_coder.state import context
    from tests.test_context_budget import _CompactionClient

    class MissingFinishClient(_CompactionClient):
        def create(self, **kwargs):
            self.request = kwargs
            fields = {"message": SimpleNamespace(content=self.summary)}
            if finish != "absent":
                fields["finish_reason"] = finish
            return SimpleNamespace(choices=[SimpleNamespace(**fields)])

    monkeypatch.setattr(context, "_get_git_diff_summary", lambda: "")
    messages = [{"role": "user", "content": "Repair parser"}]
    before = copy.deepcopy(messages)
    with scoped_workdir(tmp_path), scoped_session("summary-missing-finish"):
        with pytest.raises(PublicRuntimeError, match="summary"):
            context.auto_compact(messages, MissingFinishClient(summary_json()), "fake-model")
    assert messages == before


@pytest.mark.parametrize("phase", ["coding", "memory"])
def test_compaction_protocol_does_not_change_other_call_normalization(phase):
    from nz_coder.runtime.model_gateway import ModelCall, ModelCallPurpose, ProductionModelGateway
    from tests.runtime.model_gateway.test_buffered_gateway import _Provider, _runtime

    raw = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content="<think>reason</think>visible"), finish_reason=None,
    )])
    provider = _Provider([raw])
    outcome = ProductionModelGateway(_runtime(provider)).complete_sync(ModelCall(
        purpose=ModelCallPurpose(phase), messages=[{"role": "user", "content": "test"}],
        max_output_tokens=100, timeout_seconds=1,
    ))
    assert outcome.content == "visible"
    assert outcome.reasoning == "reason"
    assert outcome.finish_reason == "stop"
