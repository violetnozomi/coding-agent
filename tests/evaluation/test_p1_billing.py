"""Real OpenAI/httpx boundary contracts using only controlled, offline transport."""
from __future__ import annotations

from decimal import Decimal
import importlib
import importlib.util
import json

import httpx
import pytest


def billing():
    assert importlib.util.find_spec("evaluation.linux_baseline.billing") is not None, "P1 budget transport is missing"
    return importlib.import_module("evaluation.linux_baseline.billing")


def fixture(tmp_path, *, authorized=True, total="10", task="5", tokens=2000000, responder=None):
    module = billing()
    policy = module.Policy(authorized=authorized, total_budget=Decimal(total),
                           task_budget=Decimal(task), token_budget=tokens)
    events, sent = [], []
    ledger = module.Ledger(policy, lambda row: events.append(row))
    def respond(request):
        sent.append(json.loads(request.content))
        if responder:
            return responder(request)
        return httpx.Response(200, json={
            "id": "controlled", "object": "chat.completion", "created": 1,
            "model": "deepseek-v4-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "offline"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 40,
                      "prompt_cache_miss_tokens": 60, "completion_tokens": 20,
                      "completion_tokens_details": {"reasoning_tokens": 15}, "total_tokens": 120},
        })
    client = module.make_client(policy, ledger, "test-only-key", inner=httpx.MockTransport(respond))
    return policy, ledger, client, sent, events


def request(client, **kwargs):
    return client.chat.completions.create(model="deepseek-v4-flash",
                                          messages=[{"role": "user", "content": "private request text"}],
                                          max_tokens=100000, **kwargs)


@pytest.mark.parametrize("options", [{"authorized": False}, {"total": "0.01"}, {"task": "0.01"}, {"tokens": 100000}])
def test_no_transport_without_authorization_and_both_budgets(tmp_path, options):
    _, ledger, client, sent, _ = fixture(tmp_path, **options)
    with client, pytest.raises(Exception):
        request(client)
    assert sent == []
    assert ledger.snapshot()["requests_sent"] == 0


def test_wire_output_cap_and_usage_subsets_are_not_double_counted(tmp_path):
    policy, ledger, client, sent, events = fixture(tmp_path)
    with client:
        response = request(client)
    assert response.usage.total_tokens == 120
    assert sent[0]["max_tokens"] == 8000
    assert sent[0]["thinking"] == {"type": "enabled"}
    assert sent[0]["reasoning_effort"] == "high"
    state = ledger.snapshot()
    # 40*0.10 + 60*3.0 + 20*9.0 per million. Reasoning is within output.
    assert Decimal(state["usage_derived_cost"]) == Decimal("0.000364")
    assert state["usage"]["total_tokens"] == 120
    assert Decimal(state["unresolved_reservation"]) == 0
    assert Decimal(state["remaining_task_budget"]) == Decimal("4.999636")
    serialized = json.dumps(events)
    assert "test-only-key" not in serialized
    assert "private request text" not in serialized
    assert "Authorization" not in serialized


@pytest.mark.parametrize("kind", ["no_usage", "timeout", "read_loss", "cancelled", "500", "malformed", "bad_usage"])
def test_uncertainty_retains_reservation_and_stops_sdk_retries(tmp_path, kind):
    def response(req):
        if kind == "cancelled":
            raise KeyboardInterrupt("synthetic cancellation after dispatch")
        if kind == "timeout":
            raise httpx.ReadTimeout("private endpoint or key must not leak", request=req)
        if kind == "read_loss":
            raise httpx.ReadError("lost response", request=req)
        if kind == "500":
            return httpx.Response(500, json={"error": "private upstream detail"})
        if kind == "malformed":
            return httpx.Response(200, content=b"invalid JSON")
        if kind == "bad_usage":
            return httpx.Response(200, json={"usage": {"prompt_tokens": 1, "completion_tokens": -1}})
        return httpx.Response(200, json={"choices": []})
    _, ledger, client, sent, events = fixture(tmp_path, responder=response)
    with client:
        with pytest.raises(Exception):
            request(client)
        with pytest.raises(Exception):
            request(client)
    assert len(sent) == 1  # Even the real SDK's automatic retry may not send again.
    state = ledger.snapshot()
    assert state["blocked"]
    assert state["usage_complete"] is False
    assert Decimal(state["unresolved_reservation"]) == Decimal("3.217728")
    assert state["usage_derived_cost"] is None
    assert "private upstream detail" not in json.dumps(events)


def test_other_model_auxiliary_call_cannot_bypass_same_transport(tmp_path):
    _, ledger, client, sent, _ = fixture(tmp_path)
    with client, pytest.raises(Exception):
        client.chat.completions.create(model="deepseek-v4-pro", messages=[], max_tokens=100)
    assert not sent
    assert ledger.snapshot()["requests_sent"] == 0


def test_record_failure_before_send_and_after_response(tmp_path):
    module = billing()
    for fail_event, expected_sends in (("reserved", 0), ("settled", 1)):
        policy = module.Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5))
        events, sent = [], []
        def record(event):
            if event["event"] == fail_event:
                raise OSError("synthetic storage failure")
            events.append(event)
        def response(req):
            sent.append(req)
            return httpx.Response(200, json={"id": "offline", "model": policy.model, "usage": {"prompt_tokens": 1, "prompt_cache_hit_tokens": 0,
                                                       "prompt_cache_miss_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
        ledger = module.Ledger(policy, record)
        with module.make_client(policy, ledger, "test-key", inner=httpx.MockTransport(response)) as client:
            with pytest.raises(Exception):
                request(client)
        assert len(sent) == expected_sends
        assert ledger.snapshot()["blocked"]
        assert Decimal(ledger.snapshot()["unresolved_reservation"]) > 0


def test_parallel_reservations_share_task_limit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    module = billing()
    ledger = module.Ledger(module.Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5)), lambda _e: None)
    def reserve(_):
        try:
            ledger.reserve("bounded-request", 8000)
            return 1
        except module.BillingStopped:
            return 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(reserve, range(4))) == 1


def test_bad_endpoint_never_reaches_transport(tmp_path):
    module = billing()
    with pytest.raises(ValueError):
        module.Policy(endpoint="https://user:secret@api.deepseek.com/?key=secret")


def test_attempt_claim_is_exclusive_and_never_overwrites(tmp_path):
    module = billing()
    module.claim_attempt(tmp_path, "T01")
    with pytest.raises(FileExistsError):
        module.claim_attempt(tmp_path, "T01")
    with pytest.raises(ValueError):
        module.claim_attempt(tmp_path, "T02")


def test_reserved_concurrent_request_cannot_dispatch_after_uncertainty():
    module = billing()
    ledger = module.Ledger(module.Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(10), token_budget=3000000), lambda _row: None)
    first, second = ledger.reserve("one", 8000), ledger.reserve("two", 8000)
    ledger.dispatching(first)
    ledger.uncertain(first)
    with pytest.raises(module.BillingStopped):
        ledger.dispatching(second)


def test_mismatched_response_model_preserves_uncertainty(tmp_path):
    def respond(req):
        return httpx.Response(200, json={"id": "different-model", "model": "deepseek-v4-pro", "usage": {
            "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2,
            "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 1}})
    _, ledger, client, sent, _ = fixture(tmp_path, responder=respond)
    with client, pytest.raises(Exception):
        request(client)
    assert len(sent) == 1
    assert not ledger.snapshot()["usage_complete"]


def test_alternate_output_limit_cannot_escape_wire_reservation(tmp_path):
    """An SDK extra_body limit must not coexist with the reserved max_tokens."""
    _, ledger, client, sent, _ = fixture(tmp_path)
    with client, pytest.raises(Exception):
        request(client, extra_body={"max_completion_tokens": 100000})
    assert sent == []
    assert ledger.snapshot()["requests_sent"] == 0
