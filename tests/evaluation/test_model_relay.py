"""真实Unix转发与本地假上游的预算契约，不调用外部模型。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import hashlib
import socket
import threading
import time
import uuid

import httpx
import pytest

from nz_coder.evaluation.model_relay import (
    InputAccounting, RelayBinding, RelayLedger, RelayLimits, RelayServer,
)


def fake_count(raw, _payload):
    # 假服务的token定义就是JSON字节数；不得用于DeepSeek或线上效率结论。
    return InputAccounting(len(raw), "local-fake-json-byte-tokens", trusted=True, exact=True, evidence_level="exact",
                           contract_id="local-fake-v1", payload_sha256=hashlib.sha256(raw).hexdigest())


class FakeService:
    def __init__(self, mode="normal", completion=7):
        self.mode, self.completion = mode, completion
        self.requests = []
        self.started = threading.Event()
        self.release = threading.Event()

    def handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                request = json.loads(raw)
                fake.requests.append(request)
                fake.started.set()
                if fake.mode == "hold":
                    fake.release.wait(3)
                if fake.mode == "lost":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                usage = {"prompt_tokens": len(raw), "completion_tokens": fake.completion,
                         "total_tokens": len(raw) + fake.completion,
                         "prompt_cache_hit_tokens": 3,
                         "completion_tokens_details": {"reasoning_tokens": 2}}
                if fake.mode == "missing":
                    usage = None
                if fake.mode == "invalid":
                    usage["completion_tokens"] = -1
                if fake.mode == "malformed_details":
                    usage["completion_tokens_details"] = "invalid"
                if fake.mode == "float_total":
                    usage["total_tokens"] = float(usage["total_tokens"])
                if fake.mode == "over_input":
                    usage.update(prompt_tokens=150, completion_tokens=7, total_tokens=157)
                if fake.mode == "over_output":
                    usage.update(completion_tokens=64001, total_tokens=len(raw) + 64001)
                if fake.mode == "conflicting_over":
                    usage.update(prompt_tokens=150, total_tokens=0)
                if fake.mode == "cache_conflict":
                    usage["prompt_tokens_details"] = {"cached_tokens": 4}
                if fake.mode == "scalar_usage":
                    usage = "invalid"
                if fake.mode == "length":
                    body_finish = "length"
                elif fake.mode == "aborted":
                    body_finish = "aborted"
                else:
                    body_finish = "stop"
                body = {"id": "local", "object": "chat.completion", "model": request["model"],
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "local"},
                                     "finish_reason": body_finish}], "usage": usage}
                if fake.mode == "invalid_choices":
                    body["choices"] = fake.response_choices
                if request.get("stream"):
                    data = ("data: " + json.dumps({**body, "usage": None}) + "\n\n"
                            + "data: " + json.dumps(body) + "\n\n"
                            + "data: " + json.dumps(body) + "\n\n")
                    if fake.mode == "decreasing_usage":
                        data += "data: " + json.dumps({**body, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}) + "\n\n"
                    if fake.mode != "cut_stream":
                        data += "data: [DONE]\n\n"
                    if fake.mode == "invalid_choices":
                        data = "data: " + json.dumps(body) + "\n\ndata: [DONE]\n\n"
                    if fake.mode in {"four_report_truncated", "four_report_timeout", "crossed_report_truncated"}:
                        reports = [(100, 1000), (50, 1), (9000, 70000), (1, 0)]
                        if fake.mode == "crossed_report_truncated":
                            reports = [(100, 1000), (50, 1), (9000, 2), (1, 70000), (1, 0)]
                        data = "".join("data: " + json.dumps({
                            "model": "observed-provider-model", "system_fingerprint": "observed-fingerprint",
                            "choices": [{"finish_reason": "length"}],
                            "usage": {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p+c},
                        }) + "\n\n" for p, c in reports)
                    raw_response, content_type = data.encode(), "text/event-stream"
                else:
                    raw_response, content_type = json.dumps(body).encode(), "application/json"
                self.send_response(500 if fake.mode == "error" else 200)
                self.send_header("Content-Type", content_type)
                if fake.mode != "done_hold":
                    self.send_header("Content-Length", str(len(raw_response) + (100 if fake.mode in {"four_report_truncated", "four_report_timeout", "crossed_report_truncated"} else 0)))
                self.end_headers()
                try:
                    self.wfile.write(raw_response)
                    if fake.mode in {"four_report_truncated", "four_report_timeout", "crossed_report_truncated"}:
                        self.wfile.flush()
                        if fake.mode == "four_report_timeout":
                            fake.release.wait(3)
                        self.close_connection = True
                    if fake.mode == "done_hold":
                        self.wfile.flush()
                        fake.release.wait(3)
                except OSError:
                    pass

        return Handler


@contextmanager
def running(tmp_path, *, mode="normal", limits=None, cap=2, output=64000, counter=fake_count, deadline=None, authorization=None, exact_output=True, stop_on_rejection=False, trace_directory=None):
    fake = FakeService(mode)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    fake.server_port = upstream.server_port
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    ledger = RelayLedger(tmp_path / "ledger.jsonl", limits or RelayLimits(output_total=1000000 if authorization else 100000), empirical_authorization=authorization,
                         stop_on_rejection=stop_on_rejection)
    binding = RelayBinding(authorization["purpose"] if authorization else "local-test",
                           authorization["scope"][0] if authorization else "autonomous", "r1", "main", cap,
                           authorization["model"] if authorization else "local-model", output,
                           deadline or time.monotonic() + 5, exact_output)
    path = tmp_path / "relay.sock"
    relay = RelayServer(path, ledger=ledger, binding=binding,
                        upstream=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions", counter=counter,
                        trace_directory=trace_directory)
    fake.relay = relay
    relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
    relay_thread.start()
    try:
        yield fake, ledger, binding, path
    finally:
        fake.release.set()
        if ledger.stopped(binding) != "journal_fault":
            ledger.cancel(binding.run)
        relay.shutdown()
        relay.server_close()
        relay_thread.join(2)
        assert relay.wait_handlers(time.monotonic() + 2), "测试退出前必须确认账本写入者已退出"
        upstream.shutdown()
        upstream.server_close()
        thread.join(2)
        if ledger.stopped(binding) == "journal_fault":
            with pytest.raises(RuntimeError, match="journal_fault"):
                ledger.close()
        else:
            ledger.close()


def post(path, *, nonce=None, model="local-model", output=64000, stream=False, extra=None, headers=None, omit_output=False):
    payload = {"model": model, "messages": [{"role": "user", "content": "local"}],
               "max_tokens": output, "stream": stream, **(extra or {})}
    if omit_output:
        payload.pop("max_tokens")
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), trust_env=False, timeout=4) as client:
        return client.post("http://localhost/v1/chat/completions", json=payload,
                           headers={"X-NZ-Call-Id": nonce or uuid.uuid4().hex, **(headers or {})})


def test_n_plus_one_and_client_role_forgery_do_not_add_budget(tmp_path):
    with running(tmp_path, cap=2) as (fake, ledger, binding, path):
        assert post(path).status_code == 200
        assert post(path, headers={"X-NZ-Role": "review", "X-NZ-Run": "other"}).status_code == 200
        assert post(path).status_code == 429
        assert len(fake.requests) == len(ledger.attempts) == 2
        assert set(ledger.counts) == {binding.bucket}


def test_client_close_after_done_keeps_complete_stream_settlement(tmp_path):
    with running(tmp_path, mode="done_hold") as (_fake, ledger, _binding, path):
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), trust_env=False, timeout=4) as client:
            with client.stream("POST", "http://localhost/v1/chat/completions",
                json={"model": "local-model", "messages": [{"role": "user", "content": "local"}],
                      "max_tokens": 64000, "stream": True},
                headers={"X-NZ-Call-Id": uuid.uuid4().hex}) as response:
                assert response.status_code == 200
                assert any(line == "data: [DONE]" for line in response.iter_lines())
        deadline = time.monotonic()+2
        while not any(e["event"] == "settled" for e in ledger.events) and time.monotonic() < deadline:
            time.sleep(.01)
        settled = next(e for e in ledger.events if e["event"] == "settled")
        assert settled["status"] == "complete"
        assert settled["usage"]["completion_tokens"] == 7
        assert settled["finish_reason"] == "stop"
        assert not settled["ambiguous"] and not settled["reservation_retained"]
        assert not ledger.failed_contracts


def provider_output_grant(reservation=393216):
    return {"purpose": "swebench-pro-v2", "model": "deepseek-flash", "account": "offline-test",
            "scope": ["instance_example__project-" + "a" * 40], "instance_manifest_sha256": "a" * 64,
            "authorization_text": "offline fixture", "input_mode": "empirical", "empirical_risk_accepted": True,
            "contract_id": "local-fake-v1", "cost_currency": "CNY", "cost_limit": 50,
            "valid_from": time.time() - 1, "valid_until": time.time() + 60,
            "prices_cny_per_million": {"input": 2, "cached_input": 0.04, "output": 8},
            "provider_output_reservation_tokens": reservation}


def reference_count(raw, _payload):
    return InputAccounting(None, "local-byte-reference", contract_id="local-fake-v1",
                           payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=len(raw))


def test_pro_grant_accepts_frozen_official_nan_version_id(tmp_path):
    authorization = provider_output_grant()
    authorization["scope"] = ["instance_element-hq__element-web-459df4583e01e4744a52d45446e34183385442d6-vnan"]
    ledger = RelayLedger(tmp_path / "ledger.jsonl", RelayLimits(), empirical_authorization=authorization)
    ledger.close()
    authorization["scope"] = [authorization["scope"][0].replace("-vnan", "-vunknown")]
    with pytest.raises(ValueError, match="invalid_empirical_authorization"):
        RelayLedger(tmp_path / "invalid.jsonl", RelayLimits(), empirical_authorization=authorization)


def test_provider_default_output_uses_fee_reservation_without_setting_wire_limit(tmp_path):
    with running(tmp_path, mode="length", output=393216, counter=reference_count,
                 authorization=provider_output_grant(), exact_output=False) as (fake, ledger, binding, path):
        fake.completion = 8001
        assert post(path, model=binding.model, omit_output=True).status_code == 200
        row = next(iter(ledger.attempts.values()))
        assert row["output_reserved"] == 393216
        assert row["output_requested"] is None
        assert row["output_over_request_limit"] is None
        assert row["output_over_reservation"] == 0
        assert row["usage"]["completion_tokens"] == ledger.output_occupied == 8001
        assert ledger.cost_occupied_cny == pytest.approx(
            ((row["usage"]["prompt_tokens"] - 3) * 2 + 3 * .04 + 8001 * 8) / 1_000_000)
        assert not ledger.failed_contracts
        fake.completion = 64000
        assert post(path, model=binding.model, omit_output=True).status_code == 200
        assert all("max_tokens" not in p for p in fake.requests)
        assert post(path, model=binding.model, output=393217).status_code == 429
        assert len(fake.requests) == 2


def test_report_above_requested_limit_within_paid_reservation_does_not_stop(tmp_path):
    with running(tmp_path, output=8000, counter=reference_count,
                 authorization=provider_output_grant()) as (fake, ledger, binding, path):
        fake.completion = 8001
        assert post(path, model=binding.model, output=8000).status_code == 200
        assert post(path, model=binding.model, output=8000).status_code == 200
        assert len(fake.requests) == 2 and not ledger.failed_contracts
        assert next(iter(ledger.attempts.values()))["output_over_request_limit"] == 1


@pytest.mark.parametrize("boundary", ["output", "fee"])
def test_provider_capacity_reservation_counts_against_total_budget(tmp_path, boundary):
    authorization = provider_output_grant()
    if boundary == "fee":
        authorization["cost_limit"] = .065
    limits = RelayLimits(output_total=64000 if boundary == "output" else 1000000)
    with running(tmp_path, output=393216, limits=limits, counter=reference_count,
                 authorization=authorization, exact_output=False) as (fake, ledger, binding, path):
        assert post(path, model=binding.model, omit_output=True).status_code == 429
        assert not fake.requests and not ledger.attempts


@pytest.mark.parametrize("reservation", [0, -1, True, 1.5, "393216", None])
def test_invalid_provider_reservation_cannot_open_an_empirical_ledger(tmp_path, reservation):
    with pytest.raises(ValueError, match="invalid_empirical_authorization"):
        RelayLedger(tmp_path / "ledger", RelayLimits(), empirical_authorization=provider_output_grant(reservation))


@pytest.mark.parametrize("mode", ["missing", "above_capacity"])
def test_provider_default_never_relaxes_unknown_usage_or_unreserved_cost(tmp_path, mode):
    def counter(raw, p):
        return InputAccounting(None, "local-reference", contract_id="local-fake-v1",
            payload_sha256=hashlib.sha256(raw).hexdigest(), reference_count=100)
    with running(tmp_path, mode="normal" if mode == "above_capacity" else mode, output=393216,
                 counter=counter, authorization=provider_output_grant(), exact_output=False) as (fake, ledger, binding, path):
        fake.completion = 393217 if mode == "above_capacity" else 7
        assert post(path, model=binding.model, omit_output=True).status_code == 200
        assert post(path, model=binding.model, omit_output=True).status_code == 429
        assert len(fake.requests) == 1
        row = next(iter(ledger.attempts.values()))
        assert row["contract_failed"]
        if mode == "missing":
            assert row["usage"] is None
            assert ledger.output_occupied == 393216
        else:
            assert row["output_over_reservation"] == 1


@pytest.mark.parametrize("lim,reason", [(RelayLimits(requests=1), "physical_request"),
                                      (RelayLimits(), "output_reservation")])
def test_concurrent_last_request_and_64000_output_reservation(tmp_path, lim, reason):
    with running(tmp_path, mode="hold", limits=lim) as (fake, ledger, _binding, path):
        with ThreadPoolExecutor() as pool:
            first = pool.submit(post, path)
            assert fake.started.wait(2)
            second = post(path)
            assert second.status_code == 429 and reason in second.text
            assert ledger.output_occupied == 64000 and len(fake.requests) == 1
            fake.release.set()
            assert first.result(3).status_code == 200
        assert ledger.output_occupied == 7


@pytest.mark.parametrize("budget,second_status", [(6, 429), (7, 200)])
def test_two_task_relays_share_inflight_fee_and_cancel_only_their_run(tmp_path, budget, second_status):
    """并行题目共享费用预留；已完成题目的取消不终止另一道请求。"""
    authorization = provider_output_grant()
    authorization["cost_limit"] = budget
    with running(tmp_path, mode="hold", output=393216, counter=reference_count,
                 authorization=authorization, exact_output=False) as (fake, ledger, first_binding, first_path):
        from dataclasses import replace
        second_fake = FakeService("hold")
        second_upstream = ThreadingHTTPServer(("127.0.0.1", 0), second_fake.handler())
        second_http_thread = threading.Thread(target=second_upstream.serve_forever, daemon=True)
        second_http_thread.start()
        second_binding = replace(first_binding, run="r2")
        second_path = tmp_path / "second.sock"
        second_relay = RelayServer(second_path, ledger=ledger, binding=second_binding,
            upstream=f"http://127.0.0.1:{second_upstream.server_port}/v1/chat/completions", counter=reference_count)
        second_thread = threading.Thread(target=second_relay.serve_forever, daemon=True)
        second_thread.start()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(post, first_path, model=first_binding.model, omit_output=True)
                assert fake.started.wait(2)
                if second_status == 429:
                    denied = post(second_path, model=second_binding.model, omit_output=True)
                    assert denied.status_code == 429 and "estimated_fee" in denied.text
                    assert not second_fake.requests and len(ledger.attempts) == 1
                    fake.release.set()
                    assert first.result(3).status_code == 200
                else:
                    second = pool.submit(post, second_path, model=second_binding.model, omit_output=True)
                    assert second_fake.started.wait(2)
                    assert len(ledger.attempts) == 2
                    assert 6 < ledger.cost_occupied_cny <= budget
                    assert all(row["status"] == "reserved" for row in ledger.attempts.values())
                    fake.release.set()
                    assert first.result(3).status_code == 200
                    ledger.cancel(first_binding.run)
                    assert "r2" not in ledger.cancelled and not second.done()
                    second_fake.release.set()
                    assert second.result(3).status_code == 200
                    assert {row["run"] for row in ledger.attempts.values()} == {"r1", "r2"}
                    assert all(row["usage"] is not None for row in ledger.attempts.values())
            assert ledger.cost_occupied_cny <= budget
        finally:
            fake.release.set()
            second_fake.release.set()
            second_relay.shutdown()
            second_relay.server_close()
            second_thread.join(2)
            second_upstream.shutdown()
            second_upstream.server_close()
            second_http_thread.join(2)


def test_duplicate_nonce_does_not_resend_even_after_settlement(tmp_path):
    with running(tmp_path) as (fake, ledger, _binding, path):
        assert post(path, nonce="same").status_code == 200
        assert post(path, nonce="same").status_code == 429
        assert len(fake.requests) == len(ledger.attempts) == 1


def test_upstream_failure_is_counted_and_forwarder_never_retries(tmp_path):
    with running(tmp_path, mode="error") as (fake, ledger, _binding, path):
        assert post(path).status_code == 500
        assert len(fake.requests) == 1
        assert post(path).status_code == 429  # 64000预留未归还，不能再容纳一次。
        assert len(ledger.attempts) == 1 and ledger.output_occupied == 64000


def test_response_loss_is_ambiguous_and_blocks_retry(tmp_path):
    with running(tmp_path, mode="lost") as (fake, ledger, binding, path):
        assert post(path).status_code == 502
        assert binding.run in ledger.ambiguous
        assert post(path).status_code == 429
        assert len(fake.requests) == 1 and ledger.output_occupied == 64000


@pytest.mark.parametrize("mode", ["missing", "invalid", "malformed_details", "float_total", "scalar_usage"])
def test_missing_or_invalid_usage_retains_input_and_output(tmp_path, mode):
    with running(tmp_path, mode=mode) as (fake, ledger, _binding, path):
        assert post(path).status_code == 200
        row = next(iter(ledger.attempts.values()))
        assert row["usage"] is None
        assert row["status"] != "reserved", "invalid usage must settle unknown, not crash the handler"
        assert ledger.output_occupied == row["output_reserved"] == 64000
        assert ledger.input_occupied == row["input_reserved"] > 0
        assert len(fake.requests) == 1


def test_sse_duplicate_cumulative_usage_is_settled_once(tmp_path):
    with running(tmp_path) as (fake, ledger, _binding, path):
        response = post(path, stream=True)
        assert response.status_code == 200 and "[DONE]" in response.text
        row = next(iter(ledger.attempts.values()))
        assert row["usage"]["completion_tokens"] == ledger.output_occupied == 7
        assert row["usage"]["prompt_tokens"] == ledger.input_occupied
        assert len(fake.requests) == len(ledger.attempts) == 1


def test_cut_stream_does_not_fabricate_usage_or_retry(tmp_path):
    with running(tmp_path, mode="cut_stream") as (fake, ledger, binding, path):
        response = post(path, stream=True)
        assert '"error"' in response.text and "[DONE]" not in response.text
        assert binding.run in ledger.ambiguous
        assert next(iter(ledger.attempts.values()))["usage"] is None
        assert ledger.output_occupied == 64000 and len(fake.requests) == 1


def test_request_timeout_cancels_transport_and_keeps_reservation(tmp_path):
    with running(tmp_path, mode="hold", limits=RelayLimits(request_seconds=0.08)) as (fake, ledger, binding, path):
        response = post(path)
        assert response.status_code == 502 and "request_timeout" in response.text
        assert binding.run in ledger.ambiguous
        assert ledger.output_occupied == 64000 and len(fake.requests) == 1


def test_cancel_waiting_admission_and_expired_run_never_forward(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def counter(raw, payload):
        entered.set()
        assert release.wait(2)
        return fake_count(raw, payload)

    with running(tmp_path, counter=counter) as (fake, ledger, binding, path):
        with ThreadPoolExecutor() as pool:
            future = pool.submit(post, path)
            assert entered.wait(2)
            ledger.cancel(binding.run)
            release.set()
            assert future.result(2).status_code == 429
        assert not fake.requests and not ledger.attempts
    other = tmp_path / "expired"
    other.mkdir()
    with running(other, deadline=time.monotonic() - 1) as (fake, ledger, _binding, path):
        assert post(path).status_code == 429
        assert not fake.requests and not ledger.attempts


@pytest.mark.parametrize("case", ["schema", "unknown", "wrong_model", "target", "change_output"])
def test_final_request_validation_rejects_before_upstream(tmp_path, case):
    counter = (lambda *_args: InputAccounting(None, "unknown")) if case == "unknown" else fake_count
    with running(tmp_path, counter=counter, limits=RelayLimits(input_per_request=1000)) as (fake, ledger, _binding, path):
        kwargs = {"schema": {"extra": {"tools": [{"parameters": {"description": "x" * 2000}}]}},
                  "unknown": {}, "wrong_model": {"model": "other"},
                  "target": {"extra": {"base_url": "http://127.0.0.1:1"}},
                  "change_output": {"output": 1024}}[case]
        assert post(path, **kwargs).status_code == 429
        assert not fake.requests and not ledger.attempts


def test_restarting_existing_ledger_cannot_reset_budget(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with running(tmp_path) as (_fake, ledger, _binding, socket_path):
        assert post(socket_path).status_code == 200
        before = path.read_bytes()
        with pytest.raises(FileExistsError):
            RelayLedger(path, RelayLimits())
        assert path.read_bytes() == before and len(ledger.attempts) == 1


def test_unapproved_remote_target_and_non_forwarding_routes(tmp_path):
    ledger = RelayLedger(tmp_path / "ledger.jsonl", RelayLimits())
    binding = RelayBinding("x", "u", "r", "review", 2, "m", 1024, time.monotonic() + 1)
    try:
        with pytest.raises(PermissionError):
            RelayServer(tmp_path / "s.sock", ledger=ledger, binding=binding,
                        upstream="https://api.deepseek.com/v1/chat/completions")
        assert not ledger.attempts
    finally:
        ledger.close()


@pytest.mark.parametrize("mode,physical", [("error", 2), ("lost", 1)])
def test_real_sdk_and_gateway_retries_each_cross_admission(tmp_path, mode, physical):
    from openai import OpenAI
    from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
    from nz_coder.runtime.model_gateway import (
        ModelCall, ModelCallPurpose, ModelCallStatus, ProductionModelGateway, ResolvedModelRuntime,
    )
    with running(tmp_path, mode=mode, output=1024) as (fake, ledger, _binding, path):
        provider = OpenAICompatibleProvider(api_key="local-only", base_url="http://localhost/v1")
        client = OpenAI(api_key="local-only", base_url="http://localhost/v1", max_retries=0,
                        http_client=httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), trust_env=False,
                            event_hooks={"request": [lambda req: req.headers.__setitem__("X-NZ-Call-Id", uuid.uuid4().hex)]}))
        runtime = ResolvedModelRuntime(provider.name, "local-model", "local-model", None,
                                       provider, client, provider.capabilities("local-model"))
        try:
            result = ProductionModelGateway(runtime, max_retries=1, wait=lambda _s: None).complete_sync(
                ModelCall(ModelCallPurpose.VERIFIER, [{"role": "user", "content": "local"}], 1024))
        finally:
            client.close()
        assert result.status is not ModelCallStatus.COMPLETED
        assert len(fake.requests) == len(ledger.attempts) == physical
        assert all(a["usage"] is None for a in ledger.attempts.values())


def test_review_buckets_and_autonomous_auxiliary_have_separate_caps(tmp_path):
    with running(tmp_path, cap=12, output=1024) as (fake, ledger, binding, main_socket):
        servers, threads, sockets = [], [], [main_socket]
        try:
            bindings = [RelayBinding(binding.experiment, "autonomous", "r1", "auxiliary", 6,
                                     binding.model, 1024, binding.deadline)] + [
                RelayBinding(binding.experiment, f"u{i}", f"review{i}", "review", 2,
                             binding.model, 1024, binding.deadline) for i in range(4)]
            for i, extra in enumerate(bindings):
                path = tmp_path / f"extra{i}.sock"
                server = RelayServer(path, ledger=ledger, binding=extra,
                    upstream=f"http://127.0.0.1:{fake.server_port}/v1/chat/completions", counter=fake_count)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                servers.append(server)
                threads.append(thread)
                sockets.append(path)
            for path, cap in zip(sockets, (12, 6, 2, 2, 2, 2)):
                for _ in range(cap):
                    assert post(path, output=1024).status_code == 200
                assert post(path, output=1024).status_code == 429
            assert len(fake.requests) == len(ledger.attempts) == 26
            assert sorted(ledger.counts.values()) == [2, 2, 2, 2, 6, 12]
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(2)


def test_total_input_includes_full_requests_and_never_silently_lowers_output(tmp_path):
    with running(tmp_path, limits=RelayLimits(input_total=200)) as (fake, ledger, _binding, path):
        assert post(path).status_code == 200
        response = post(path)
        assert response.status_code == 429 and "input_reservation_exhausted" in response.text
        assert len(fake.requests) == 1 and fake.requests[0]["max_tokens"] == 64000
        assert ledger.input_occupied == next(iter(ledger.attempts.values()))["usage"]["prompt_tokens"]


def test_waiting_request_expires_before_admission(tmp_path):
    entered, release = threading.Event(), threading.Event()
    now = [time.monotonic()]

    def counter(raw, payload):
        entered.set()
        assert release.wait(2)
        return fake_count(raw, payload)

    with running(tmp_path, counter=counter, deadline=now[0] + 1) as (fake, ledger, binding, path):
        ledger.clock = lambda: now[0]
        with ThreadPoolExecutor() as pool:
            future = pool.submit(post, path)
            assert entered.wait(2)
            now[0] = binding.deadline
            release.set()
            assert future.result(2).status_code == 429
        assert not fake.requests and not ledger.attempts


def test_active_cancel_stops_forwarding_and_keeps_unknown_usage(tmp_path):
    with running(tmp_path, mode="hold") as (fake, ledger, binding, path):
        with ThreadPoolExecutor() as pool:
            future = pool.submit(post, path)
            assert fake.started.wait(2)
            ledger.cancel(binding.run)
            assert future.result(2).status_code == 502
        assert len(fake.requests) == 1
        assert post(path).status_code == 429
        row = next(iter(ledger.attempts.values()))
        assert row["usage"] is None and row["status"] == "cancelled_or_disconnected"
        assert ledger.output_occupied == 64000


def test_client_disconnect_does_not_refund_or_allow_resend(tmp_path):
    with running(tmp_path, mode="hold") as (fake, ledger, binding, path):
        original = ledger.settle
        settled = threading.Event()

        def capture(*args, **kwargs):
            original(*args, **kwargs)
            settled.set()

        ledger.settle = capture
        raw = json.dumps({"model": "local-model", "messages": [], "max_tokens": 64000}).encode()
        with socket.socket(socket.AF_UNIX) as channel:
            channel.connect(str(path))
            channel.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nX-NZ-Call-Id: disconnect\r\nContent-Length: "
                            + str(len(raw)).encode() + b"\r\n\r\n" + raw)
            assert fake.started.wait(2)
        assert settled.wait(2), "disconnect must settle through the real forwarder"
        assert post(path, nonce="disconnect").status_code == 429
        assert len(fake.requests) == 1 and ledger.output_occupied == 64000


def test_default_unknown_counter_and_unexposed_routes_never_forward(tmp_path):
    from nz_coder.evaluation.model_relay import unknown_input

    with running(tmp_path, counter=unknown_input) as (fake, ledger, _binding, path):
        assert post(path).status_code == 429
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(path)), trust_env=False) as client:
            assert client.post("http://localhost/reset", json={}).status_code == 404
            assert client.get("http://localhost/v1/chat/completions").status_code == 501
        assert not fake.requests and not ledger.attempts


def test_journal_failure_never_forwards_and_restart_refuses_partial_record(tmp_path, monkeypatch):
    import nz_coder.evaluation.model_relay as module

    with running(tmp_path) as (fake, ledger, _binding, path):
        def fail_fsync(_fd):
            raise OSError("local durable-journal probe")

        with monkeypatch.context() as patch:
            patch.setattr(module.os, "fsync", fail_fsync)
            with pytest.raises(httpx.HTTPError):
                post(path)
        assert not fake.requests and not ledger.attempts
        with pytest.raises(FileExistsError):
            RelayLedger(ledger.path, RelayLimits())


def test_online_entry_and_help_remain_offline_without_authorization(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    command = [sys.executable, str(root / "tests/evaluation/fixtures/offline_exec.py"),
               sys.executable, str(root / "scripts/review_effects_execution.py")]
    environment = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
    for action, expected in (("status", 0), ("online", 2)):
        result = subprocess.run(command + [action, "--artifact-root", str(tmp_path)],
                                env=environment, capture_output=True, text=True, timeout=10)
        assert result.returncode == expected, result.stderr
        ready = json.loads(result.stdout)
        assert not ready["paid_authorization_valid"] and not ready["technical_ready"]
        assert ready["online_not_run"] and not ready["isolation_verified"]
    assert subprocess.run(command + ["--help"], env=environment, capture_output=True, timeout=10).returncode == 0


def test_reported_input_over_reservation_stops_followup_before_upstream(tmp_path):
    def under_count(raw, _payload):
        return InputAccounting(100, "local-invalid-bound-v1", trusted=True, exact=True, evidence_level="exact",
                           contract_id="local-fake-v1", payload_sha256=hashlib.sha256(raw).hexdigest())

    with running(tmp_path, mode="over_input", output=1024, limits=RelayLimits(input_total=250), counter=under_count) as (fake, ledger, _binding, path):
        assert post(path, output=1024).status_code == 200
        response = post(path, output=1024)
        assert response.status_code == 429, "150 reported against 100 reserved invalidates the counting contract"
        assert len(fake.requests) == 1
        row = next(iter(ledger.attempts.values()))
        assert row["reported_usage"]["prompt_tokens"] == 150
        assert row["input_over_reservation"] == 50
        assert ledger.input_occupied == 150


@pytest.mark.parametrize('mode', ['over_input', 'over_output', 'conflicting_over'])
def test_failed_contract_blocks_other_role_and_counter_waiter(tmp_path, mode):
    entered, release = threading.Event(), threading.Event()
    def under_count(raw, payload):
        return InputAccounting(100, 'local-test-under-reserve', trusted=True, exact=True, evidence_level='exact',
                               contract_id='local-fake-v1', payload_sha256=hashlib.sha256(raw).hexdigest())
    def waiting_count(raw, payload):
        entered.set()
        assert release.wait(3)
        return under_count(raw, payload)
    with running(tmp_path, mode=mode, output=1024, counter=under_count) as (fake, ledger, _binding, path):
        other = RelayBinding('local-test', 'u02', 'other-run', 'auxiliary', 2, 'local-model', 1024, time.monotonic()+5)
        relay = RelayServer(tmp_path/'other.sock', ledger=ledger, binding=other,
                            upstream=f'http://127.0.0.1:{fake.server_port}/v1/chat/completions', counter=waiting_count)
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        try:
            with ThreadPoolExecutor() as pool:
                waiting = pool.submit(post, tmp_path/'other.sock', output=1024)
                assert entered.wait(2)
                assert post(path, output=1024).status_code == 200
                release.set()
                response = waiting.result(3)
                assert response.status_code == 429 and 'accounting_contract_failed' in response.text
            assert len(fake.requests) == 1 and len(ledger.attempts) == 1
            row = next(iter(ledger.attempts.values()))
            assert row['reported_usage'] is not None and row['contract_failed']
            assert row['usage_valid'] is (mode != 'conflicting_over')
            assert row['usage'] is None if mode == 'conflicting_over' else row['usage'] is not None
            assert any(e['event'] == 'accounting_contract_failed' and e['attempt_id'] == row['attempt_id'] for e in ledger.events)
            with pytest.raises(FileExistsError):
                RelayLedger(tmp_path/'ledger.jsonl', RelayLimits())
            assert len(fake.requests) == 1
        finally:
            release.set()
            relay.shutdown()
            relay.server_close()
            thread.join(2)


@pytest.mark.parametrize('mode', ['cache_conflict', 'length', 'aborted'])
def test_usage_conflict_and_finish_reason_are_not_zero_or_recounted(tmp_path, mode):
    with running(tmp_path, mode=mode) as (fake, ledger, _binding, path):
        assert post(path).status_code == 200
        row = next(iter(ledger.attempts.values()))
        assert len(fake.requests) == 1 and row['reported_usage']['completion_tokens'] == 7
        if mode == 'length':
            assert row['finish_reason'] == 'length' and ledger.output_occupied == 7
        else:
            assert row['usage'] is None and ledger.output_occupied == 64000


def test_accounting_payload_hash_and_empirical_label_cannot_bypass_strict_admission(tmp_path):
    from dataclasses import replace
    for change, reason in (({'payload_sha256':'wrong'}, 'counted_payload_mismatch'),
                           ({'evidence_level':'empirically_calibrated'}, 'input_token_bound_unknown')):
        case = tmp_path/reason
        case.mkdir()
        def counter(raw, payload):
            return replace(fake_count(raw,payload), **change)
        with running(case, counter=counter) as (fake, _ledger, _binding, path):
            response = post(path)
            assert response.status_code == 429 and reason in response.text
            assert not fake.requests


def test_conflicting_cumulative_sse_preserves_reports_and_stops_contract(tmp_path):
    with running(tmp_path, mode='decreasing_usage', output=1024) as (fake, ledger, _binding, path):
        assert post(path, output=1024, stream=True).status_code == 200
        row = next(iter(ledger.attempts.values()))
        assert row['usage'] is None and row['usage_conflict'] and row['contract_failed']
        assert row['conflicting_reports'][0]['prompt_tokens'] > row['reported_usage']['prompt_tokens'] == 1
        assert ledger.output_occupied == 1024
        assert post(path, output=1024).status_code == 429
        assert len(fake.requests) == 1


def test_contract_failure_keeps_already_sent_other_role_truthful(tmp_path):
    def under_count(raw, _payload):
        return InputAccounting(100, 'test-under-bound', trusted=True, exact=True, evidence_level='exact',
                               contract_id='local-fake-v1', payload_sha256=hashlib.sha256(raw).hexdigest())
    with running(tmp_path, mode='over_input', output=1024, counter=under_count) as (first, ledger, _binding, path):
        held = FakeService('hold')
        upstream = ThreadingHTTPServer(('127.0.0.1',0), held.handler())
        upstream.daemon_threads = True
        http_thread = threading.Thread(target=upstream.serve_forever,daemon=True)
        http_thread.start()
        binding = RelayBinding('local-test','u02','active-other','auxiliary',2,'local-model',1024,time.monotonic()+5)
        relay = RelayServer(tmp_path/'active.sock',ledger=ledger,binding=binding,
                            upstream=f'http://127.0.0.1:{upstream.server_port}/v1/chat/completions',counter=under_count)
        thread = threading.Thread(target=relay.serve_forever,daemon=True)
        thread.start()
        try:
            with ThreadPoolExecutor() as pool:
                active = pool.submit(post,tmp_path/'active.sock',output=1024)
                assert held.started.wait(2), 'the second role really reached the upstream before failure'
                assert post(path,output=1024).status_code==200
                assert active.result(3).status_code==502
            assert len(first.requests)==len(held.requests)==1 and len(ledger.attempts)==2
            row = next(r for r in ledger.attempts.values() if r['run']=='active-other')
            assert row['status']=='cancelled_or_disconnected' and row['usage'] is None
            assert row['reservation_retained'] and row['input_reserved']==100
            assert ledger.input_occupied==250
            assert post(tmp_path/'active.sock',output=1024).status_code==429
            assert len(held.requests)==1
        finally:
            held.release.set()
            relay.shutdown()
            relay.server_close()
            thread.join(2)
            upstream.shutdown()
            upstream.server_close()
            http_thread.join(2)


def test_authorized_forty_pro_instances_use_one_host_ledger(tmp_path):
    grant = provider_output_grant()
    grant['scope'] = ['instance_example__project-' + f'{i:040x}' for i in range(40)]
    with running(tmp_path, authorization=grant, counter=reference_count, output=393216, exact_output=False) as (fake, ledger, binding, path):
        assert post(path, model=binding.model, omit_output=True).status_code == 200
        assert len(fake.requests) == len(ledger.attempts) == 1
        assert ledger.empirical_authorization['cost_limit'] == 50
    grant['scope'].append('instance_example__project-' + f'{40:040x}')
    with pytest.raises(ValueError, match='invalid_empirical_authorization'):
        RelayLedger(tmp_path/'forty-one.jsonl', RelayLimits(), empirical_authorization=grant)


@pytest.mark.parametrize("stop_on_rejection,later_status", [(False, 200), (True, 429)])
def test_budget_rejection_policy_does_not_reopen_after_cohort_settles(tmp_path, stop_on_rejection, later_status):
    with running(tmp_path, mode="hold", cap=5, stop_on_rejection=stop_on_rejection) as (fake, ledger, binding, path):
        with ThreadPoolExecutor() as pool:
            first = pool.submit(post, path)
            assert fake.started.wait(2)
            assert post(path).status_code == 429
            assert ledger.stopped(binding) == "", "预算软封门不能取消已准入请求"
            fake.release.set()
            assert first.result(3).status_code == 200
        assert post(path).status_code == later_status
        assert len(fake.requests) == (1 if stop_on_rejection else 2)
        if stop_on_rejection:
            assert ledger.admission_closed_reason == "output_reservation_exhausted"
            closed = [e for e in ledger.events if e["event"] == "admissions_closed"]
            assert len(closed) == 1
            ledger.close_admissions("another_reason")
            assert ledger.admission_closed_reason == closed[0]["reason"]
            assert len([e for e in ledger.events if e["event"] == "admissions_closed"]) == 1


def ledger_request(tmp_path, **kwargs):
    ledger = RelayLedger(tmp_path / "ledger", RelayLimits(), **kwargs)
    binding = RelayBinding("local-test", "u", "run", "main", 3, "local-model", 1024, time.monotonic() + 5)
    payload = {"model": binding.model, "max_tokens": 1024, "messages": []}
    raw = json.dumps(payload).encode()
    return ledger, binding, raw, payload, fake_count(raw, payload)


def test_explicit_deny_closes_all_roles_when_policy_selected(tmp_path):
    from dataclasses import replace
    from nz_coder.evaluation.model_relay import AdmissionDenied

    ledger, binding, raw, payload, accounting = ledger_request(tmp_path, stop_on_rejection=True)
    try:
        ledger.deny(binding, "counter_failed")
        assert ledger.stopped(binding) == ""
        with pytest.raises(AdmissionDenied, match="counter_failed"):
            ledger.reserve(replace(binding, unit="other", run="other", role="review"), "new", raw, payload, accounting)
        assert not ledger.attempts
    finally:
        ledger.close()


@pytest.mark.parametrize("operation", ["write", "fsync"])
def test_journal_fault_latches_hard_stop_after_storage_recovers(tmp_path, monkeypatch, operation):
    from nz_coder.evaluation.model_relay import AdmissionDenied
    import nz_coder.evaluation.model_relay as module

    ledger, binding, raw, payload, accounting = ledger_request(tmp_path)
    failure = OSError("journal unavailable")
    def fail(*_args):
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(module.os, operation, fail)
        with pytest.raises(OSError) as caught:
            ledger.reserve(binding, "first", raw, payload, accounting)
    assert caught.value is failure
    assert ledger.journal_fault
    assert ledger.stopped(binding) == "journal_fault"
    with pytest.raises(AdmissionDenied, match="journal_fault"):
        ledger.reserve(binding, "second", raw, payload, accounting)
    assert not ledger.attempts
    with pytest.raises(RuntimeError, match="journal_fault"):
        ledger.close()


def test_cancel_is_effective_even_when_its_journal_write_fails(tmp_path, monkeypatch):
    import nz_coder.evaluation.model_relay as module

    ledger, binding, *_ = ledger_request(tmp_path)
    def fail(_fd):
        raise OSError("cancel journal unavailable")
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", fail)
        with pytest.raises(OSError, match="cancel journal"):
            ledger.cancel(binding.run)
    assert binding.run in ledger.cancelled
    assert ledger.stopped(binding)
    with pytest.raises(RuntimeError, match="journal_fault"):
        ledger.close()


def test_ledger_close_refuses_unsettled_attempt_and_remains_usable_for_settlement(tmp_path):
    ledger, binding, raw, payload, accounting = ledger_request(tmp_path)
    attempt = ledger.reserve(binding, "first", raw, payload, accounting)
    with pytest.raises(RuntimeError, match="reserved"):
        ledger.close()
    ledger.settle(attempt, status="cancelled_or_disconnected", ambiguous=True)
    assert ledger.attempts[attempt]["reservation_retained"]
    ledger.close()


def test_incomplete_usage_increases_token_and_fee_occupancy_without_unconfirmed_cache_discount(tmp_path):
    from dataclasses import replace

    grant = provider_output_grant(1024)
    ledger = RelayLedger(tmp_path / "ledger", RelayLimits(), empirical_authorization=grant)
    binding = RelayBinding(grant["purpose"], grant["scope"][0], "run", "main", 2, grant["model"], 1024, time.monotonic()+5)
    payload = {"model": binding.model, "max_tokens": 1024, "messages": []}
    raw = json.dumps(payload).encode()
    accounting = replace(reference_count(raw, payload), reference_count=100)
    attempt = ledger.reserve(binding, "first", raw, payload, accounting)
    try:
        ledger.settle(attempt, status="stream_incomplete", ambiguous=True,
                      usage={"prompt_tokens": 2000, "completion_tokens": 2048, "total_tokens": 4048,
                             "prompt_cache_hit_tokens": 2000})
        row = ledger.attempts[attempt]
        assert (ledger.input_occupied, ledger.output_occupied) == (2000, 2048)
        assert ledger.cost_occupied_cny == pytest.approx(.020384)
        assert row["estimated_cost_occupied_cny"] == pytest.approx(.020384)
        assert row["usage"] is None and row["estimated_cost_reported_cny"] is None
        assert row["reservation_retained"]
    finally:
        ledger.close()


@pytest.mark.parametrize("mode,expected_status", [("four_report_truncated", "transport_error"),
                                                 ("crossed_report_truncated", "transport_error"),
                                                 ("four_report_timeout", "request_timeout")])
def test_all_usage_peaks_and_metadata_survive_transport_failure(tmp_path, mode, expected_status):
    with running(tmp_path, mode=mode, output=1024, counter=reference_count,
                 authorization=provider_output_grant(1024), limits=RelayLimits(request_seconds=.3)) as (_fake, ledger, binding, path):
        assert post(path, model=binding.model, output=1024, stream=True).status_code == 200
        row = next(iter(ledger.attempts.values()))
        assert row["status"] == expected_status
        assert row["reported_usage"] == {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}
        assert row["usage_conflict"] and row["conflicting_reports"][0]["completion_tokens"] == 1000
        assert row["usage_high_water"] == {"prompt_tokens": 9000, "completion_tokens": 70000}
        assert (ledger.input_occupied, ledger.output_occupied) == (9000, 70000)
        assert ledger.cost_occupied_cny == pytest.approx(.578)
        assert row["estimated_cost_occupied_cny"] == pytest.approx(.578)
        assert row["usage"] is None and row["estimated_cost_reported_cny"] is None
        assert row["provider_model"] == "observed-provider-model"
        assert row["system_fingerprint"] == "observed-fingerprint" and row["finish_reason"] == "length"
        assert len([e for e in ledger.events if e["event"] == "settled"]) == 1


def test_settlement_journal_failure_keeps_observation_and_occupancy_in_memory(tmp_path, monkeypatch):
    import nz_coder.evaluation.model_relay as module

    ledger, binding, raw, payload, accounting = ledger_request(tmp_path)
    attempt = ledger.reserve(binding, "first", raw, payload, accounting)
    failure = OSError("settlement durable write failed")
    def fail(_fd):
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", fail)
        with pytest.raises(OSError) as caught:
            ledger.settle(attempt, status="stream_incomplete", usage={"prompt_tokens": 9000, "completion_tokens": 70000},
                          ambiguous=True, provider_model="observed-model")
    assert caught.value is failure
    row = ledger.attempts[attempt]
    assert row["reported_usage"] == {"prompt_tokens": 9000, "completion_tokens": 70000}
    assert row["provider_model"] == "observed-model" and not row["settlement_persisted"]
    assert (ledger.input_occupied, ledger.output_occupied) == (9000, 70000)
    assert binding.experiment in ledger.failed_contracts and binding.run in ledger.ambiguous
    assert not any(e["event"] == "settled" for e in ledger.events)
    written = [json.loads(line) for line in ledger.path.read_text().splitlines()]
    assert not any(e.get("settlement_persisted") is True for e in written), "写出但未fsync的行不能宣称持久结算"
    with pytest.raises(RuntimeError, match="already settled"):
        ledger.settle(attempt, status="complete")
    with pytest.raises(RuntimeError, match="journal_fault"):
        ledger.close()


@pytest.mark.parametrize("boundary", ["complete", "cancel", "timeout"])
def test_budget_closed_cohort_survives_downstream_eof_until_original_boundary(tmp_path, boundary):
    limits = RelayLimits(request_seconds=.3 if boundary == "timeout" else 3)
    with running(tmp_path, mode="hold", cap=5, stop_on_rejection=True, limits=limits) as (fake, ledger, binding, path):
        settled = threading.Event()
        original = ledger.settle
        def capture(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            finally:
                settled.set()
        ledger.settle = capture
        raw = json.dumps({"model": binding.model, "messages": [], "max_tokens": 64000}).encode()
        with socket.socket(socket.AF_UNIX) as channel:
            channel.connect(str(path))
            channel.sendall(b"POST /v1/chat/completions HTTP/1.0\r\nX-NZ-Call-Id: cohort\r\nContent-Length: "
                            + str(len(raw)).encode() + b"\r\n\r\n" + raw)
            assert fake.started.wait(2)
            assert post(path).status_code == 429
        if boundary == "complete":
            assert not settled.wait(.05), "预算软封门后的EOF不能取消原上游"
            fake.release.set()
        elif boundary == "cancel":
            ledger.cancel(binding.run)
        assert settled.wait(1), "原超时或显式取消必须仍能结束排空"
        row = next(iter(ledger.attempts.values()))
        if boundary == "complete":
            assert row["status"] == "complete" and row["usage"]["completion_tokens"] == 7
            assert ledger.output_occupied == 7 and not row["reservation_retained"]
        else:
            assert row["status"] == ("request_timeout" if boundary == "timeout" else "cancelled_or_disconnected")
            assert row["usage"] is None and row["reservation_retained"]
            assert ledger.output_occupied == 64000
        assert len(fake.requests) == len(ledger.attempts) == 1


def test_handler_wait_includes_counter_and_actual_native_thread_exit(tmp_path):
    entered, counter_release = threading.Event(), threading.Event()
    tail_entered, tail_release = threading.Event(), threading.Event()
    ledger, binding, *_ = ledger_request(tmp_path)
    def counter(raw, payload):
        entered.set()
        assert counter_release.wait(3)
        return fake_count(raw, payload)
    path = tmp_path / "wait.sock"
    relay = RelayServer(path, ledger=ledger, binding=binding, upstream="http://127.0.0.1:9/v1/chat/completions", counter=counter)
    original = relay.process_request_thread
    def delayed_native_exit(*args):
        original(*args)
        tail_entered.set()
        assert tail_release.wait(3)
    relay.process_request_thread = delayed_native_exit
    thread = threading.Thread(target=relay.serve_forever, daemon=True)
    thread.start()
    try:
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(post, path, output=1024)
            assert entered.wait(2)
            relay.shutdown()
            relay.server_close()
            thread.join(2)
            assert not thread.is_alive() and not ledger.attempts
            assert relay.active_handlers == 1
            assert not relay.wait_handlers(time.monotonic() + .03)
            ledger.close_admissions("budget_stop")
            counter_release.set()
            assert pending.result(2).status_code == 429
            assert tail_entered.wait(2)
            assert relay.active_handlers == 1
            assert not relay.wait_handlers(time.monotonic() + .03)
            tail_release.set()
            assert relay.wait_handlers(time.monotonic() + 1)
            assert relay.active_handlers == 0 and not ledger.attempts
    finally:
        counter_release.set()
        tail_release.set()
        relay.shutdown()
        relay.server_close()
        thread.join(2)
        ledger.close()


@pytest.mark.parametrize("reason", ["budget_stop", ""])
def test_explicit_admission_close_is_irreversible_for_every_reason(tmp_path, reason):
    from nz_coder.evaluation.model_relay import AdmissionDenied

    ledger, binding, raw, payload, accounting = ledger_request(tmp_path)
    try:
        ledger.close_admissions(reason)
        with pytest.raises(AdmissionDenied):
            ledger.reserve(binding, "later", raw, payload, accounting)
        assert ledger.admission_closed_reason == reason and not ledger.attempts
    finally:
        for attempt_id, row in ledger.attempts.items():
            if row["status"] == "reserved":
                ledger.settle(attempt_id, status="cancelled_or_disconnected", ambiguous=True)
        ledger.close()


def test_fd_close_failure_keeps_admissions_closed_and_allows_close_retry(tmp_path, monkeypatch):
    from nz_coder.evaluation.model_relay import AdmissionDenied
    import nz_coder.evaluation.model_relay as module

    ledger, binding, raw, payload, accounting = ledger_request(tmp_path)
    original_close = module.os.close
    def fail(fd):
        if fd == ledger.fd:
            raise OSError("fd close unavailable")
        return original_close(fd)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "close", fail)
            with pytest.raises(OSError, match="fd close unavailable"):
                ledger.close()
        with pytest.raises(AdmissionDenied):
            ledger.reserve(binding, "later", raw, payload, accounting)
    finally:
        for attempt_id, row in ledger.attempts.items():
            if row["status"] == "reserved":
                ledger.settle(attempt_id, status="cancelled_or_disconnected", ambiguous=True)
        ledger.close()


def test_response_trace_write_failure_preserves_already_received_usage(tmp_path, monkeypatch):
    from pathlib import Path

    original_open = Path.open
    class FailingTrace:
        def __init__(self, handle):
            self.handle = handle
        def __getattr__(self, name):
            return getattr(self.handle, name)
        def write(self, _chunk):
            raise OSError("trace capture unavailable")
    def fail_trace(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return FailingTrace(handle) if path.name.endswith(".response.txt") else handle
    monkeypatch.setattr(Path, "open", fail_trace)
    with running(tmp_path, trace_directory=tmp_path / "trace") as (_fake, ledger, binding, path):
        assert post(path).status_code == 200
        row = next(iter(ledger.attempts.values()))
        assert row["status"] == "transport_error" and row["usage"] is None
        assert row["reported_usage"]["completion_tokens"] == 7
        assert row["provider_model"] == binding.model
        assert row["reservation_retained"] and row["settlement_persisted"]


def test_budget_closed_cohort_survives_broken_pipe_when_writing_headers(tmp_path, monkeypatch):
    import nz_coder.evaluation.model_relay as module

    with running(tmp_path, mode="hold", cap=5, stop_on_rejection=True) as (fake, ledger, _binding, path):
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(post, path)
            assert fake.started.wait(2)
            assert post(path).status_code == 429
            def broken_headers(_self):
                raise BrokenPipeError("downstream write probe")
            monkeypatch.setattr(module._Handler, "end_headers", broken_headers)
            fake.release.set()
            with pytest.raises(httpx.HTTPError):
                pending.result(2)
        row = next(iter(ledger.attempts.values()))
        assert row["status"] == "complete" and row["usage"]["completion_tokens"] == 7
        assert row["settlement_persisted"] and ledger.output_occupied == 7


@pytest.mark.parametrize("boundary", ["cancel", "transport", "timeout"])
def test_unparsed_buffered_transport_loss_does_not_invent_cross_run_usage_conflict(tmp_path, boundary):
    from dataclasses import replace

    with running(tmp_path, mode="lost" if boundary == "transport" else "hold", output=1024,
                 limits=RelayLimits(request_seconds=.2 if boundary == "timeout" else 3)) as (fake, ledger, binding, path):
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(post, path, output=1024)
            assert fake.started.wait(2)
            if boundary == "cancel":
                ledger.cancel(binding.run)
            assert pending.result(2).status_code == 502
        row = next(iter(ledger.attempts.values()))
        assert row["usage"] is None and row["reservation_retained"]
        assert not row["usage_conflict"] and not ledger.failed_contracts
        payload = {"model": binding.model, "max_tokens": 1024, "messages": []}
        raw = json.dumps(payload).encode()
        other = ledger.reserve(replace(binding, run="another-run"), "other", raw, payload, fake_count(raw, payload))
        ledger.settle(other, status="cancelled_or_disconnected", ambiguous=True)


@pytest.mark.parametrize("operation", ["write", "fsync"])
def test_constructor_journal_failure_closes_its_unowned_fd(tmp_path, monkeypatch, operation):
    from contextlib import suppress
    import errno
    import nz_coder.evaluation.model_relay as module

    real_open = module.os.open
    opened = []
    failure = OSError("initial journal unavailable")
    def capture_open(*args):
        fd = real_open(*args)
        opened.append(fd)
        return fd
    def fail(*_args):
        raise failure
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "open", capture_open)
        patch.setattr(module.os, operation, fail)
        with pytest.raises(OSError) as caught:
            RelayLedger(tmp_path / "ledger", RelayLimits())
    assert caught.value is failure
    try:
        with pytest.raises(OSError) as closed:
            module.os.fstat(opened[0])
        assert closed.value.errno == errno.EBADF
    finally:
        with suppress(OSError):
            module.os.close(opened[0])


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("choices", [[None], None, {}, [1], [{"finish_reason": []}]])
def test_malformed_response_choices_settle_once_and_preserve_received_usage(tmp_path, stream, choices):
    import errno
    import os

    with running(tmp_path, mode="invalid_choices", output=1024) as (fake, ledger, binding, path):
        fake.response_choices = choices
        assert post(path, output=1024, stream=stream).status_code == 200
        assert fake.relay.wait_handlers(time.monotonic() + 1)
        row = next(iter(ledger.attempts.values()))
        assert row["status"] == "transport_error", "预期协议结构错误仍须走唯一结算"
        assert row["usage"] is None and row["reservation_retained"] and row["ambiguous"]
        assert row["reported_usage"]["completion_tokens"] == 7
        assert row["reported_usage"]["prompt_tokens"] == row["input_reserved"]
        assert row["provider_model"] == binding.model and row["settlement_persisted"]
        assert ledger.output_occupied == 1024
        assert len([e for e in ledger.events if e["event"] == "settled"]) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(ledger.fd)
    assert closed.value.errno == errno.EBADF


def test_complete_buffered_body_keeps_trusted_usage_when_cancel_arrives_at_client_exit(tmp_path, monkeypatch):
    import asyncio
    import nz_coder.evaluation.model_relay as module

    real_client = module.httpx.AsyncClient
    transport_closed = threading.Event()
    with running(tmp_path, output=1024) as (fake, ledger, binding, path):
        class CancelAtClientExit(real_client):
            async def __aexit__(self, exc_type, exc_value, exc_tb):
                result = await super().__aexit__(exc_type, exc_value, exc_tb)
                if exc_type is None:
                    assert self.is_closed
                    transport_closed.set()
                    ledger.cancel(binding.run)
                    # 已完整读取且真实 HTTP client 已关闭，让既有取消观察器在同一收尾轮完成。
                    watchers = [task for task in asyncio.all_tasks()
                                if task.get_coro().__qualname__.endswith("._forward.<locals>.interruption")]
                    assert len(watchers) == 1
                    await watchers[0]
                return result
        monkeypatch.setattr(module.httpx, "AsyncClient", CancelAtClientExit)
        assert post(path, output=1024).status_code == 200
        assert fake.relay.wait_handlers(time.monotonic() + 1)
        assert transport_closed.is_set() and binding.run in ledger.cancelled
        row = next(iter(ledger.attempts.values()))
        assert row["status"] == "complete" and row["usage"]["completion_tokens"] == 7
        assert not row["reservation_retained"] and not row["ambiguous"]
        assert ledger.output_occupied == 7 and binding.run not in ledger.ambiguous
        assert len(fake.requests) == len([e for e in ledger.events if e["event"] == "settled"]) == 1
