"""官方V4.1资源的参考编码及严格拒绝；资源由显式离线准备提供。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter, validate_text_request
from nz_coder.evaluation.model_relay import AdmissionDenied, RelayBinding, RelayLedger, RelayLimits


@pytest.fixture
def counter():
    path = os.environ.get("NZ_DEEPSEEK_V41_TOKENIZER")
    if path is None:
        pytest.skip("official V4.1 resource not prepared: set NZ_DEEPSEEK_V41_TOKENIZER and audited recipe PYTHONPATH; no automatic download")
    return DeepSeekV41Counter(Path(path))


def payload(**fields):
    return {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": '中文\nprint("\\u2603") ☃'}],
            "max_tokens": 64000, "stream": False, **fields}


def count(counter, request):
    return counter(json.dumps(request, ensure_ascii=False).encode(), request)


@pytest.mark.parametrize("fields", [{"unknown_provider_field": True},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://invalid"}}]}]},
    {"messages": [{"role": "user", "content": "x", "file_id": "private"}]},
    {"reasoning_effort": "medium"}, {"model": "deepseek-v4-pro"}])
def test_unsupported_fields_reject_instead_of_disappearing(fields):
    with pytest.raises(ValueError):
        validate_text_request(payload(**fields))


def test_official_encoder_counts_roles_schema_history_reasoning_and_prefix(counter, tmp_path):
    base = payload(thinking={"type": "disabled"})
    short = count(counter, base)
    tool = {"type": "function", "function": {"name": "read_file", "description": "Read a bounded workspace file.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
    request = payload(tools=[tool], thinking={"type": "enabled"}, reasoning_effort="high", tool_choice="auto", messages=[
        {"role": "system", "content": "Use bounded local tools."}, *base["messages"],
        {"role": "assistant", "content": None, "reasoning_content": "Read relevant source before editing.",
         "tool_calls": [{"id": "r1", "type": "function", "function": {"name": "read_file", "arguments": '{"path":"a.py"}'}}]},
        {"role": "tool", "tool_call_id": "r1", "content": "value = 1\n"}])
    result = count(counter, request)
    assert result.reference_count > short.reference_count > 0
    assert not result.trusted and result.evidence_level == "unknown" and result.upper_bound is None
    assert result.payload_sha256 == hashlib.sha256(json.dumps(request, ensure_ascii=False).encode()).hexdigest()
    longer = json.loads(json.dumps(request))
    longer["messages"][2]["reasoning_content"] += " Extra diagnosis." * 30
    assert count(counter, longer).reference_count > result.reference_count
    expanded_schema = json.loads(json.dumps(request))
    expanded_schema["tools"][0]["function"]["description"] += " Unicode code evidence 中文." * 30
    assert count(counter, expanded_schema).reference_count > result.reference_count
    prefix = payload(thinking={"type": "disabled"}, messages=[{"role": "user", "content": ""},
                     {"role": "assistant", "content": "prefix=", "prefix": True}])
    assert count(counter, prefix).reference_count > 0
    raw_ascii = json.dumps(request, ensure_ascii=True).encode()
    assert counter(raw_ascii, request).reference_count == result.reference_count
    ledger = RelayLedger(tmp_path/'ledger.jsonl', RelayLimits())
    try:
        binding = RelayBinding('study','u1','r1','main',12,'deepseek-v4-flash',64000,ledger.clock()+5)
        with pytest.raises(AdmissionDenied, match='input_token_bound_unknown'):
            ledger.reserve(binding,'n',json.dumps(request,ensure_ascii=False).encode(),request,result)
        assert not ledger.attempts
    finally:
        ledger.close()


def test_same_payload_and_resource_identity_are_required(counter, tmp_path):
    request = payload()
    assert 'rejected' in counter(b'{}', request).method
    bad = tmp_path/'tokenizer.json'
    bad.write_text('{}')
    with pytest.raises(ValueError,match='tokenizer_hash_mismatch'):
        DeepSeekV41Counter(bad)
