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
    extra = {"thinking": {"type": "enabled"}, "reasoning_effort": "high",
             **kwargs.pop("extra_body", {})}
    return client.chat.completions.create(model="deepseek-v4-flash",
                                          messages=[{"role": "user", "content": "private request text"}],
                                          max_tokens=100000, extra_body=extra, **kwargs)


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
        with pytest.raises(KeyboardInterrupt if kind == "cancelled" else Exception):
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


def valid_payload():
    return dict(id="offline", model="deepseek-v4-flash", choices=[], usage=dict(
        prompt_tokens=100, completion_tokens=20, total_tokens=120,
        prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60))


@pytest.mark.parametrize("kind,stage,category,status", [
    ("400", "http_response", "http_error", 400),
    ("500", "http_response", "http_error", 500),
    ("send_timeout", "transport_send", "write_timeout", None),
    ("connect_timeout", "transport_send", "connect_timeout", None),
    ("response_loss", "transport_send", "read_error", None),
    ("dynamic_error", "transport_send", "unknown_exception", None),
    ("read_timeout", "response_read", "read_timeout", 200),
    ("json", "json_parse", "invalid_json", 200),
    ("identity", "response_identity", "invalid_identity", 200),
    ("missing_usage", "usage_validation", "invalid_usage", 200),
    ("inconsistent", "usage_validation", "invalid_usage", 200),
    ("overrun", "usage_validation", "invalid_usage", 200),
])
def test_failures_have_safe_stages_and_stop_actual_sdk_sends(tmp_path, kind, stage, category, status):
    class ReadFailure(httpx.SyncByteStream):
        def __iter__(self):
            raise httpx.ReadTimeout("secret-sentinel-body")
            yield b""  # A streaming response, not an exception at construction.
    def respond(req):
        if kind == "dynamic_error":
            raise type("secret-sentinel-class", (Exception,), {})("secret-sentinel-message")
        if kind in {"400", "500"}:
            return httpx.Response(int(kind), json={"error": {
                "message": "secret-sentinel-body", "code": "secret-sentinel-code"}})
        errors = {"send_timeout": httpx.WriteTimeout, "connect_timeout": httpx.ConnectTimeout,
                  "response_loss": httpx.ReadError}
        if kind in errors:
            raise errors[kind]("secret-sentinel-url-key", request=req)
        if kind == "read_timeout":
            return httpx.Response(200, stream=ReadFailure())
        if kind == "json":
            return httpx.Response(200, content=b"secret-sentinel-invalid-json")
        payload = valid_payload()
        if kind == "identity":
            payload["model"] = "secret-sentinel-model"
        elif kind == "missing_usage":
            payload.pop("usage")
        elif kind == "inconsistent":
            payload["usage"]["prompt_cache_hit_tokens"] = 100
        elif kind == "overrun":
            payload["usage"].update(completion_tokens=8001, total_tokens=8101)
        return httpx.Response(200, json=payload)
    _, ledger, client, sent, events = fixture(tmp_path, responder=respond)
    with client:
        with pytest.raises(Exception):
            request(client)
        with pytest.raises(Exception):
            request(client)
    diag = ledger.rows[1]["diagnostic"]
    assert diag["failure_stage"] == stage
    assert diag["exception_category"] == category
    assert diag["http_status"] == status
    assert diag["settlement_state"] == "uncertain"
    assert diag["evidence_saved"]
    assert len(sent) == 1
    assert ledger.snapshot()["sdk_retry_entries"] >= 2
    assert ledger.snapshot()["sdk_retry_sends"] == 0
    assert Decimal(ledger.snapshot()["unresolved_reservation"]) == Decimal("3.217728")
    assert "secret-sentinel" not in json.dumps(events + [ledger.snapshot()])


@pytest.mark.parametrize("event,stage,sends", [
    ("reserved", "reservation_persistence", 0),
    ("dispatching", "dispatch_persistence", 0),
    ("settled", "settlement_persistence", 1),
])
def test_persistence_stage_retains_first_cause_and_hold(event, stage, sends):
    module = billing()
    sent = []
    def broken_record(row):
        if row["event"] in {event, "uncertain", "rejected"}:
            raise OSError("private-persistence-sentinel")
    policy = module.Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5))
    ledger = module.Ledger(policy, broken_record)
    def respond(req):
        sent.append(req)
        return httpx.Response(200, json=valid_payload())
    with module.make_client(policy, ledger, "fake", inner=httpx.MockTransport(respond)) as client:
        with pytest.raises(Exception):
            request(client)
        with pytest.raises(Exception):
            request(client)
    diag = ledger.rows[1]["diagnostic"]
    assert diag["failure_stage"] == stage
    assert diag["exception_category"] == "persistence_error"
    assert not diag["evidence_saved"]
    assert diag["secondary_failures"][0]["failure_stage"] == "diagnostic_persistence"
    assert len(sent) == sends and ledger.blocked
    assert Decimal(ledger.snapshot()["unresolved_reservation"]) == Decimal("3.217728")


def test_primary_http_error_survives_close_and_diagnostic_failure(tmp_path):
    class BrokenClose(httpx.Response):
        def close(self):
            raise RuntimeError("close-secret-sentinel")
    _, ledger, client, sent, events = fixture(tmp_path, responder=lambda req: BrokenClose(503, stream=httpx.ByteStream(b"{}")))
    def record(row):
        if row["event"] in {"uncertain", "rejected"}:
            raise OSError("journal-secret-sentinel")
        events.append(row)
    ledger.record = record
    with client, pytest.raises(Exception):
        request(client)
    diag = ledger.rows[1]["diagnostic"]
    assert diag["failure_stage"] == "http_response" and diag["http_status"] == 503
    assert {x["failure_stage"] for x in diag["secondary_failures"]} == {
        "response_close", "diagnostic_persistence"}
    assert ledger.blocked and len(sent) == 1
    assert "secret-sentinel" not in json.dumps(ledger.snapshot())


def test_pre_send_refusal_is_recorded_without_fake_usage(tmp_path):
    _, ledger, client, sent, events = fixture(tmp_path, authorized=False)
    with client, pytest.raises(Exception):
        request(client)
    assert sent == []
    assert events[0]["event"] == "rejected"
    assert events[0]["diagnostic"]["failure_stage"] == "authorization"
    assert events[0]["diagnostic"]["settlement_state"] == "not_dispatched"
    assert events[0]["usage"] is None
    assert ledger.snapshot()["unresolved_reservation"] == "0"


def test_legacy_unknown_is_historically_unrecorded_not_reconstructed():
    module = billing()
    row = dict(event="uncertain", request_id=14, dispatched=True, reserved_cost="3.154944000",
        reserved_tokens=1049600, usage=None, usage_derived_cost=None)
    summary = module.summarize([row], module.Policy())
    assert summary["request_diagnostics"][0]["recording_status"] == "historical_not_recorded"
    assert summary["request_diagnostics"][0]["http_status"] is None
    assert summary["unresolved_reservation"] == "3.154944000"


@pytest.mark.parametrize("during", ["response_close", "diagnostic_persistence"])
def test_cleanup_cancellation_propagates_without_replacing_http_diagnostic(tmp_path, during):
    class CancelClose(httpx.Response):
        def close(self):
            raise KeyboardInterrupt("private-cancel-sentinel")
    def respond(req):
        cls = CancelClose if during == "response_close" else httpx.Response
        return cls(503, stream=httpx.ByteStream(b"{}"))
    _, ledger, client, sent, events = fixture(tmp_path, responder=respond)
    if during == "diagnostic_persistence":
        def record(row):
            if row["event"] == "uncertain":
                raise KeyboardInterrupt("private-cancel-sentinel")
            events.append(row)
        ledger.record = record
    with client, pytest.raises(KeyboardInterrupt):
        request(client)
    diag = ledger.rows[1]["diagnostic"]
    assert diag["failure_stage"] == "http_response"
    assert diag["exception_category"] == "http_error" and diag["http_status"] == 503
    assert diag["secondary_failures"][0]["exception_category"] == "cancelled"
    assert len(sent) == 1 and ledger.blocked


@pytest.mark.parametrize("http_status", [200, 503])
def test_client_teardown_is_safe_and_keeps_known_cost_or_primary_failure(http_status):
    module = billing()
    class BrokenTransport(httpx.MockTransport):
        def close(self):
            raise RuntimeError("private-close-sentinel")
    policy = module.Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5))
    ledger = module.Ledger(policy, lambda row: None)
    with pytest.raises(Exception) as caught:
        with module.make_client(policy, ledger, "fake", inner=BrokenTransport(
                lambda req: httpx.Response(http_status, json=valid_payload()))) as client:
            request(client)
    assert "private-close-sentinel" not in str(caught.value)
    diag = ledger.rows[1]["diagnostic"]
    assert ledger.blocked
    if http_status == 503:
        assert diag["failure_stage"] == "http_response" and diag["http_status"] == 503
        assert diag["secondary_failures"][-1]["failure_stage"] == "transport_close"
        assert ledger.snapshot()["unresolved_reservation"] == "3.217728000"
    else:
        assert diag["failure_stage"] == "transport_close"
        assert ledger.snapshot()["usage_derived_cost"] == "0.000364000"
        assert ledger.snapshot()["usage_complete"]


@pytest.mark.parametrize("during", ["transport_close", "diagnostic_persistence"])
def test_client_teardown_preserves_already_unwinding_cancellation(during):
    module = billing()
    primary, secondary = KeyboardInterrupt("offline-first"), KeyboardInterrupt("offline-second")
    class CancelTransport(httpx.MockTransport):
        def close(self):
            raise secondary if during == "transport_close" else OSError("offline-close")
    policy = module.Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5))
    def record(row):
        if during == "diagnostic_persistence" and row.get("diagnostic", {}).get("secondary_failures"):
            raise secondary
    ledger = module.Ledger(policy, record)
    def respond(req):
        raise primary
    with pytest.raises(KeyboardInterrupt) as caught:
        with module.make_client(policy, ledger, "fake", inner=CancelTransport(respond)) as client:
            request(client)
    assert caught.value is primary
    assert ledger.rows[1]["diagnostic"]["failure_stage"] == "transport_send"
    assert ledger.blocked
