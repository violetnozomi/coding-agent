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
                if request.get("stream"):
                    data = ("data: " + json.dumps({**body, "usage": None}) + "\n\n"
                            + "data: " + json.dumps(body) + "\n\n"
                            + "data: " + json.dumps(body) + "\n\n")
                    if fake.mode == "decreasing_usage":
                        data += "data: " + json.dumps({**body, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}) + "\n\n"
                    if fake.mode != "cut_stream":
                        data += "data: [DONE]\n\n"
                    raw_response, content_type = data.encode(), "text/event-stream"
                else:
                    raw_response, content_type = json.dumps(body).encode(), "application/json"
                self.send_response(500 if fake.mode == "error" else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw_response)))
                self.end_headers()
                try:
                    self.wfile.write(raw_response)
                except OSError:
                    pass

        return Handler


@contextmanager
def running(tmp_path, *, mode="normal", limits=None, cap=2, output=64000, counter=fake_count, deadline=None):
    fake = FakeService(mode)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), fake.handler())
    fake.server_port = upstream.server_port
    upstream.daemon_threads = True
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    ledger = RelayLedger(tmp_path / "ledger.jsonl", limits or RelayLimits())
    binding = RelayBinding("local-test", "autonomous", "r1", "main", cap, "local-model", output,
                           deadline or time.monotonic() + 5)
    path = tmp_path / "relay.sock"
    relay = RelayServer(path, ledger=ledger, binding=binding,
                        upstream=f"http://127.0.0.1:{upstream.server_port}/v1/chat/completions", counter=counter)
    relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
    relay_thread.start()
    try:
        yield fake, ledger, binding, path
    finally:
        fake.release.set()
        ledger.cancel(binding.run)
        relay.shutdown()
        relay.server_close()
        relay_thread.join(2)
        upstream.shutdown()
        upstream.server_close()
        thread.join(2)
        ledger.close()


def post(path, *, nonce=None, model="local-model", output=64000, stream=False, extra=None, headers=None):
    payload = {"model": model, "messages": [{"role": "user", "content": "local"}],
               "max_tokens": output, "stream": stream, **(extra or {})}
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
