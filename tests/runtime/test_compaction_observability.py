"""压缩观测不把内部重试或未知收费当成额外成功/零费用。"""
from __future__ import annotations


def test_report_deduplicates_requests_and_keeps_unknown_cost():
    from scripts.context_compaction_report import summarize
    selection = {"event": "compaction_selection", "operation_id": "op-1", "candidate_messages": 10, "selected_messages": 3}
    finish = {"event": "model_call_finish", "compaction_operation_id": "op-1", "compaction_request_id": "r-1",
              "attempts": 2, "cost": None, "usage": {"input": 0, "output": 0}, "compaction_usage_known": False}
    rows = [selection, finish, dict(finish), {
        "event": "compaction_candidate_installed", "operation_id": "op-1", "before_tokens": 200, "after_tokens": 80,
    }]
    result = summarize(rows)[0]
    assert result["operations"] == 1 and result["model_requests"] == 1
    assert result["model_attempts"] == 2
    assert result["actual_cost"] is None and result["usage"] is None
    assert result["estimated_tokens_saved"] == 120


def test_report_keeps_known_and_unknown_requests_separate():
    from scripts.context_compaction_report import summarize
    result = summarize([
        {"event": "model_call_finish", "compaction_operation_id": "op", "compaction_request_id": "a",
         "attempts": 1, "cost": 0.2, "cost_source": "provider", "usage": {"input": 100, "output": 5}, "compaction_usage_known": True},
        {"event": "model_call_finish", "compaction_operation_id": "op", "compaction_request_id": "b",
         "attempts": 1, "cost": None, "usage": None, "compaction_usage_known": False},
    ])[0]
    assert result["actual_cost"] is None
    assert result["known_cost_subtotal"] == 0.2
    assert result["unknown_cost_requests"] == 1
    assert result["usage"] is None


def test_registry_estimate_is_separate_from_actual_and_cache_zero_is_unknown():
    from scripts.context_compaction_report import summarize
    result = summarize([{"event": "model_call_finish", "compaction_operation_id": "op", "compaction_request_id": "a",
                         "cost": 0.1, "cost_source": "registry", "usage": {"input": 100, "output": 5, "cache_read": 0},
                         "compaction_usage_known": True}])[0]
    assert result["actual_cost"] is None
    assert result["estimated_cost"] == 0.1
    assert result["usage"]["cache_read"] is None


def test_missing_usage_with_registry_zero_does_not_become_free_request():
    from scripts.context_compaction_report import summarize
    result = summarize([{"event": "model_call_finish", "compaction_operation_id": "op", "compaction_request_id": "a",
                         "cost": 0.0, "cost_source": "registry", "usage": {"input": 0, "output": 0, "cache_read": 0},
                         "compaction_usage_known": False}])[0]
    assert result["actual_cost"] is None and result["estimated_cost"] is None
    assert result["unknown_cost_requests"] == 1


def test_before_and_after_coding_cache_buckets_keep_unknown_values():
    from scripts.context_compaction_report import summarize
    usage = {"input": 100, "output": 5, "cache_read": 0, "cache_write": 0}
    result = summarize([
        {"event": "compaction_selection", "operation_id": "op", "preceding_coding_usage": usage},
        {"event": "model_call_finish", "purpose": "coding", "preceding_compaction_operation_id": "op", "usage": usage},
    ])[0]
    assert result["selection"]["preceding_coding_usage"]["cache_read"] is None
    assert result["coding_usage_after"][0]["cache_read"] is None
    assert result["coding_usage_after"][0]["cache_write"] is None
    assert usage["cache_read"] == 0
