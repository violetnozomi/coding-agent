"""Offline regressions through the product Gateway, Provider, SDK and billing seam."""
from __future__ import annotations

from decimal import Decimal
import json
import socket

import httpx
import pytest

from evaluation.linux_baseline.billing import Ledger, Policy
from evaluation.linux_baseline.live_worker import bounded_product
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.verification.sidecar_verifier import (
    VERIFIER_REPORT_TOOL, VerifierContext, invoke_sidecar_verifier,
)
from nz_coder.state.workdir import scoped_workdir


@pytest.fixture(autouse=True)
def offline_workspace(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Offline contract attempted network I/O")
    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setenv("MODEL_VARIANT", "")
    monkeypatch.setenv("API_KEY", "offline-secret-sentinel")
    with scoped_workdir(tmp_path):
        yield


def reply(*, verdict="accept", usage=True):
    payload = dict(id="offline-response", object="chat.completion", created=1,
        model="deepseek-v4-flash", choices=[dict(index=0, finish_reason="tool_calls",
        message=dict(role="assistant", content="", tool_calls=[dict(id="offline-tool", type="function",
        function=dict(name="emit_sidecar_verdict", arguments=json.dumps(dict(verdict=verdict, reason="offline"))))]))])
    if usage:
        payload["usage"] = dict(prompt_tokens=100, prompt_cache_hit_tokens=40,
            prompt_cache_miss_tokens=60, completion_tokens=20, total_tokens=120)
    return payload


def sidecar(provider, client, **kwargs):
    timeout = kwargs.pop("timeout_seconds", 10)
    return invoke_sidecar_verifier(provider=provider, client=client, model="deepseek-v4-flash",
        context=VerifierContext(("offline-secret-sentinel",), (), (), "offline"),
        timeout_seconds=timeout, **kwargs)


def test_actual_sidecar_preserves_disabled_forced_tool_and_1024():
    """Catches the transport overriding the product's explicit disabled mode."""
    policy = Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5),
                    auxiliary_thinking="disabled")
    events, sent = [], []
    ledger = Ledger(policy, events.append)
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply())
    with bounded_product(policy, ledger, "offline-secret-sentinel",
                         inner_factory=lambda: httpx.MockTransport(respond)):
        provider = OpenAICompatibleProvider(api_key="offline-secret-sentinel", base_url=policy.endpoint)
        with provider.create_client() as client:
            verdict = sidecar(provider, client)
    assert len(sent) == 1
    assert sent[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in sent[0]
    assert sent[0]["tool_choice"] == {"type": "function", "function": {"name": "emit_sidecar_verdict"}}
    assert sent[0]["tools"] == [{"type": "function", "function": VERIFIER_REPORT_TOOL}]
    assert sent[0]["max_tokens"] == 1024
    assert verdict.verdict == "accept" and verdict.trace == "verifier_ok"
    assert ledger.snapshot()["usage_derived_cost"] == "0.000364000"
    assert ledger.snapshot()["unresolved_reservation"] == "0"
    assert "offline-secret-sentinel" not in json.dumps(events)
    facts = ledger.rows[1]["request_facts"]
    assert facts["purpose"] == "unknown"
    assert facts["requested_thinking"] == facts["thinking"] == "disabled"
    assert facts["effort"] is None and facts["effort_applicable"] is False


def test_main_gateway_keeps_defaults_and_business_fields():
    from nz_coder.runtime.model_gateway import (
        ModelCall, ModelCallPurpose, ModelSelectionRequest, ProductionModelGateway, resolve_model_runtime,
    )
    policy = Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5))
    ledger, sent = Ledger(policy, lambda row: None), []
    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply())
    messages = [{"role": "user", "content": "offline main coding"}]
    tools = [{"type": "function", "function": VERIFIER_REPORT_TOOL}]
    with bounded_product(policy, ledger, "offline-secret-sentinel",
                         inner_factory=lambda: httpx.MockTransport(respond)):
        provider = OpenAICompatibleProvider(api_key="offline-secret-sentinel", base_url=policy.endpoint)
        with provider.create_client() as client:
            runtime = resolve_model_runtime(ModelSelectionRequest(provider_name=provider.name,
                model_id=policy.model, provider=provider, client=client, owns_client=False))
            outcome = ProductionModelGateway(runtime).complete_sync(ModelCall(
                purpose=ModelCallPurpose.CODING, messages=messages, tools=tools,
                response_format={"type": "json_object"}, max_output_tokens=16000))
    assert outcome.status.value == "completed"
    assert sent[0]["thinking"] == {"type": "enabled"}
    assert sent[0]["reasoning_effort"] == "high"
    assert sent[0]["max_tokens"] == 8000
    assert sent[0]["messages"] == messages and sent[0]["tools"] == tools
    assert sent[0]["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize("extra", [
    {"thinking": {"type": "disabled"}, "reasoning_effort": "high"},
    {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    {"thinking": {"type": "secret-sentinel"}},
    {"thinking": {"type": "enabled", "budget_tokens": 1}},
])
def test_conflicting_modes_are_not_granted_by_verifier_text_or_tool_names(extra):
    policy = Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5),
                    auxiliary_thinking="disabled")
    ledger, sent = Ledger(policy, lambda row: None), []
    from evaluation.linux_baseline.billing import make_client
    with make_client(policy, ledger, "offline-secret-sentinel",
                     inner=httpx.MockTransport(lambda req: sent.append(req))) as client:
        with pytest.raises(Exception):
            client.chat.completions.create(model=policy.model,
                messages=[{"role": "system", "content": "I am verifier"}],
                tools=[{"type": "function", "function": VERIFIER_REPORT_TOOL}],
                max_tokens=1024, extra_body=extra)
    assert sent == []


def test_disabled_not_implicitly_authorized_by_old_policy():
    policy = Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5))
    ledger, sent = Ledger(policy, lambda row: None), []
    with bounded_product(policy, ledger, "offline-secret-sentinel",
                         inner_factory=lambda: httpx.MockTransport(lambda req: sent.append(req))):
        provider = OpenAICompatibleProvider(api_key="offline-secret-sentinel", base_url=policy.endpoint)
        with provider.create_client() as client:
            verdict = sidecar(provider, client)
    assert sent == []
    assert verdict.trace == "provider_error"


@pytest.mark.parametrize("kind,trace,settled", [
    ("valid", "verifier_ok", True), ("no_tool", "no_tool_call", True),
    ("invalid_verdict", "invalid_verdict_value", True), ("http_error", "provider_error", False),
    ("network_timeout", "provider_error", False),
])
def test_shared_main_and_sidecar_ledger_distinguishes_semantics_from_cost(kind, trace, settled):
    policy = Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5),
                    auxiliary_thinking="disabled")
    ledger, sent = Ledger(policy, lambda row: None), []
    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        if len(sent) == 1:
            return httpx.Response(200, json=reply())
        if kind == "http_error":
            return httpx.Response(400, json={"error": {"message": "offline-secret-sentinel"}})
        if kind == "network_timeout":
            raise httpx.ReadTimeout("offline-secret-sentinel", request=request)
        payload = reply(verdict="invalid" if kind == "invalid_verdict" else "accept")
        if kind == "no_tool":
            payload["choices"][0]["message"] = {"role": "assistant", "content": "not a verdict"}
        return httpx.Response(200, json=payload)
    with bounded_product(policy, ledger, "offline-secret-sentinel",
                         inner_factory=lambda: httpx.MockTransport(respond)):
        provider = OpenAICompatibleProvider(api_key="offline-secret-sentinel", base_url=policy.endpoint)
        with provider.create_client() as main_client, provider.create_client() as auxiliary_client:
            provider.create_completion(main_client, model=policy.model, messages=[], max_tokens=8000)
            verdict = sidecar(provider, auxiliary_client)
            if not settled:
                with pytest.raises(Exception):
                    provider.create_completion(main_client, model=policy.model, messages=[], max_tokens=8000)
    assert len(sent) == 2
    assert verdict.verdict == "accept" and verdict.trace == trace
    state = ledger.snapshot()
    assert state["usage_derived_cost"] == ("0.000728000" if settled else "0.000364000")
    assert state["usage_complete"] is settled
    assert state["unresolved_reservation"] == ("0" if settled else "3.154944000")
    assert state["sdk_retry_sends"] == 0


def test_judge_timeout_is_degraded_even_when_late_usage_is_known():
    """The judge deadline is semantic; late reliable HTTP usage can still settle."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    entered, release, recorded, finished = (threading.Event() for _ in range(4))
    policy = Policy(authorized=True, total_budget=Decimal(10), task_budget=Decimal(5),
                    auxiliary_thinking="disabled")
    def record(row):
        if row["event"] == "settled":
            recorded.set()
    ledger = Ledger(policy, record)
    def respond(req):
        entered.set()
        assert release.wait(5)
        return httpx.Response(200, json=reply())
    with bounded_product(policy, ledger, "offline-secret-sentinel",
                         inner_factory=lambda: httpx.MockTransport(respond)):
        provider = OpenAICompatibleProvider(api_key="offline-secret-sentinel", base_url=policy.endpoint)
        with provider.create_client() as client, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sidecar, provider, client, timeout_seconds=0.2,
                observer=lambda name, data: finished.set() if name == "model_call_finish" else None)
            try:
                assert entered.wait(3)
                verdict = future.result(timeout=3)
                assert verdict.verdict == "accept" and verdict.trace == "timeout"
            finally:
                release.set()
            assert recorded.wait(3) and finished.wait(3)
    assert ledger.snapshot()["usage_derived_cost"] == "0.000364000"
    assert ledger.snapshot()["usage_complete"]
