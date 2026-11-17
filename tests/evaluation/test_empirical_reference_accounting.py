"""经验参考偏差可以结算；真实用量、费用和未知状态仍控制后续准入。"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import time

import pytest

from nz_coder.evaluation.model_relay import RelayBinding, RelayLedger, RelayLimits
from test_model_relay import post, provider_output_grant, reference_count, running


@pytest.mark.parametrize("reference,delta", [(100, 50), (200, -50)])
@pytest.mark.parametrize("stream", [False, True])
def test_empirical_reference_drift_settles_and_allows_next_request(tmp_path, reference, delta, stream):
    def counter(raw, payload):
        return replace(reference_count(raw, payload), reference_count=reference)

    with running(tmp_path, mode="over_input", output=1024, counter=counter,
                 authorization=provider_output_grant()) as (fake, ledger, binding, path):
        for _ in range(2):
            assert post(path, model=binding.model, output=1024, stream=stream).status_code == 200
        assert len(fake.requests) == len(ledger.attempts) == 2
        assert ledger.input_occupied == 300
        assert ledger.cost_occupied_cny == pytest.approx(0.00070024)
        assert not ledger.failed_contracts
        for row in ledger.attempts.values():
            assert row["usage"]["prompt_tokens"] == 150
            assert row["input_reference_delta"] == delta
            assert row["input_over_reservation"] == max(0, delta)
            assert not row["reservation_retained"] and not row["contract_failed"]
        assert sum(e["event"] == "input_reference_drift" for e in ledger.events) == 2


@pytest.mark.parametrize("boundary", ["single_input", "total_input", "fee"])
def test_actual_empirical_usage_crossing_limit_stops_experiment(tmp_path, boundary):
    authorization = provider_output_grant(reservation=7)
    authorization["cost_limit"] = 0.0003 if boundary == "fee" else 50
    limits = RelayLimits(input_per_request=149 if boundary == "single_input" else 1000,
                         input_total=149 if boundary == "total_input" else 1000)

    def counter(raw, payload):
        return replace(reference_count(raw, payload), reference_count=100)

    with running(tmp_path, mode="over_input", output=7, counter=counter, limits=limits,
                 authorization=authorization) as (fake, ledger, binding, path):
        assert post(path, model=binding.model, output=7).status_code == 200
        response = post(path, model=binding.model, output=7)
        assert response.status_code == 429
        assert "accounting_contract_failed" in response.text
        assert len(fake.requests) == len(ledger.attempts) == 1
        row = next(iter(ledger.attempts.values()))
        assert row["contract_failed"] and row["usage_valid"]
        assert row["usage"]["prompt_tokens"] == ledger.input_occupied == 150
        assert not row["reservation_retained"]
        assert ledger.cost_occupied_cny == pytest.approx(0.00035012)


def test_empirical_followup_reserves_against_actual_settled_input(tmp_path):
    def counter(raw, payload):
        return replace(reference_count(raw, payload), reference_count=100)

    with running(tmp_path, mode="over_input", output=1024, counter=counter,
                 limits=RelayLimits(input_total=249, output_total=1000000),
                 authorization=provider_output_grant()) as (fake, ledger, binding, path):
        assert post(path, model=binding.model, output=1024).status_code == 200
        response = post(path, model=binding.model, output=1024)
        assert response.status_code == 429 and "input_reservation_exhausted" in response.text
        assert ledger.input_occupied == 150 and not ledger.failed_contracts
        assert len(fake.requests) == len(ledger.attempts) == 1


@pytest.mark.parametrize("mode", ["missing", "conflicting_over", "aborted"])
def test_reference_mode_keeps_unknown_or_invalid_usage_stopped(tmp_path, mode):
    def counter(raw, payload):
        return replace(reference_count(raw, payload), reference_count=100)

    with running(tmp_path, mode=mode, output=1024, counter=counter,
                 authorization=provider_output_grant()) as (fake, ledger, binding, path):
        assert post(path, model=binding.model, output=1024).status_code == 200
        assert post(path, model=binding.model, output=1024).status_code == 429
        assert len(fake.requests) == len(ledger.attempts) == 1
        row = next(iter(ledger.attempts.values()))
        assert row["usage"] is None and row["reservation_retained"] and row["contract_failed"]


@pytest.mark.parametrize("prompt,completion,input_total,cost_limit,occupied_cost,stops", [
    (150, 7, 220, 50, 0.008748, True),
    (3000, 500, 100000, 0.017, 0.018392, True),
    (150, 1, 100000, 0.017, 0.0087, False),
])
def test_empirical_settlement_keeps_other_in_flight_reservation(
    tmp_path, prompt, completion, input_total, cost_limit, occupied_cost, stops,
):
    grant = provider_output_grant(reservation=1024)
    grant["cost_limit"] = cost_limit
    payload = {"model": grant["model"], "max_tokens": 1024, "messages": []}
    raw = json.dumps(payload).encode()
    accounting = replace(reference_count(raw, payload), reference_count=100,
                         payload_sha256=hashlib.sha256(raw).hexdigest())
    binding = RelayBinding(grant["purpose"], grant["scope"][0], "local-reservations", "main",
                           2, grant["model"], 1024, time.monotonic() + 10)
    ledger = RelayLedger(tmp_path / "ledger", RelayLimits(input_total=input_total),
                         empirical_authorization=grant)
    try:
        first = ledger.reserve(binding, "first", raw, payload, accounting)
        second = ledger.reserve(binding, "second", raw, payload, accounting)
        ledger.settle(first, status="complete", finish_reason="stop",
                      usage={"prompt_tokens": prompt, "completion_tokens": completion,
                             "total_tokens": prompt + completion})
        assert ledger.input_occupied == prompt + 100
        assert ledger.cost_occupied_cny == pytest.approx(occupied_cost)
        assert bool(ledger.failed_contracts) is stops
        assert ledger.attempts[second]["status"] == "reserved"
        assert ledger.attempts[second]["usage"] is None
    finally:
        for attempt_id, row in ledger.attempts.items():
            if row["status"] == "reserved":
                ledger.settle(attempt_id, status="cancelled_or_disconnected", ambiguous=True)
        ledger.close()
