"""P1-only DeepSeek Chat HTTP budget boundary; no Agent loop or global billing service."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx
from openai import OpenAI


class BillingStopped(RuntimeError):
    """Secret-free fail-closed request admission/settlement error."""


def money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000000001"), rounding=ROUND_CEILING)


@dataclass(frozen=True)
class Policy:
    authorized: bool = False
    total_budget: Decimal = Decimal(0)
    task_budget: Decimal = Decimal(0)
    token_budget: int = 2000000
    endpoint: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    currency: str = "CNY"
    input_ceiling: int = 1048576
    output_limit: int = 8000
    hit_rate: Decimal = Decimal("0.10")
    miss_rate: Decimal = Decimal("3.0")
    output_rate: Decimal = Decimal("9.0")
    effort: str = "high"

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != "https" or url.netloc != "api.deepseek.com" or
                url.path not in {"", "/", "/v1", "/v1/"} or url.query or url.fragment):
            raise ValueError("P1 supports only the credential-free official DeepSeek endpoint")
        if (self.model != "deepseek-v4-flash" or self.currency != "CNY" or self.effort != "high"
                or type(self.authorized) is not bool):
            raise ValueError("Unsupported P1 model/currency/effort/authorization")
        for value in (self.total_budget, self.task_budget, self.hit_rate, self.miss_rate, self.output_rate):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError("Budget and rates must be finite nonnegative Decimal values")
        if (self.input_ceiling < 1048576 or not 1 <= self.output_limit <= 8000 or
                type(self.token_budget) is not int or self.token_budget < 1 or
                self.hit_rate > self.miss_rate):
            raise ValueError("Invalid P1 token bounds or price ordering")


def usage_cost(policy: Policy, payload: dict) -> tuple[dict, Decimal]:
    usage = payload["usage"]
    names = ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    values = {name: usage[name] for name in names}
    if any(type(value) is not int or value < 0 for value in values.values()):
        raise ValueError("Unknown or invalid usage")
    if (values["prompt_tokens"] != values["prompt_cache_hit_tokens"] + values["prompt_cache_miss_tokens"]
            or values["total_tokens"] != values["prompt_tokens"] + values["completion_tokens"]):
        raise ValueError("Inconsistent usage subsets")
    reasoning = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
    if reasoning is not None and (type(reasoning) is not int or not 0 <= reasoning <= values["completion_tokens"]):
        raise ValueError("Invalid reasoning subset")
    values["reasoning_tokens"] = reasoning
    cost = money((values["prompt_cache_hit_tokens"] * policy.hit_rate
                  + values["prompt_cache_miss_tokens"] * policy.miss_rate
                  + values["completion_tokens"] * policy.output_rate) / Decimal(1000000))
    return values, cost


class Ledger:
    """Shared per-task thread-safe reservations; batch remainder is passed by the serial parent."""

    def __init__(self, policy: Policy, record: Callable[[dict], None]):
        self.policy, self.record = policy, record
        self.lock = threading.RLock()
        self.rows: dict[int, dict] = {}
        self.blocked = False
        self.deadline = time.monotonic() + 600

    def _save(self, row: dict) -> None:
        try:
            self.record(row)
        except BaseException:
            self.blocked = True
            raise BillingStopped("Billing evidence persistence failed") from None

    def reserve(self, request_hash: str, output_limit: int, *, request_facts: dict | None = None) -> int:
        with self.lock:
            p = self.policy
            if not p.authorized or self.blocked or time.monotonic() >= self.deadline:
                raise BillingStopped("Request not authorized or billing stopped")
            total_tokens = p.input_ceiling + output_limit
            cost = money((p.input_ceiling * p.miss_rate + output_limit * p.output_rate) / Decimal(1000000))
            state = self.snapshot()
            if (cost > Decimal(state["remaining_total_budget"]) or cost > Decimal(state["remaining_task_budget"])
                    or total_tokens > state["remaining_tokens"]):
                raise BillingStopped("Request reservation exceeds remaining budget")
            number = len(self.rows) + 1
            row = dict(event="reserved", request_id=number, request_hash=request_hash,
                       reserved_cost=str(cost), reserved_tokens=total_tokens,
                       output_limit=output_limit, dispatched=False, usage=None, usage_derived_cost=None)
            row["request_facts"] = dict(request_facts or {})
            self.rows[number] = row
            self._save(dict(row))  # Durable before touching the transport.
            return number

    def dispatching(self, number: int) -> None:
        with self.lock:
            if self.blocked or time.monotonic() >= self.deadline:
                raise BillingStopped("Request stopped before dispatch")
            row = {**self.rows[number], "event": "dispatching", "dispatched": True}
            self._save(row)
            self.rows[number] = row

    def settle(self, number: int, payload: dict) -> None:
        with self.lock:
            original = self.rows[number]
            if (payload.get("model") != self.policy.model or not isinstance(payload.get("id"), str)
                    or not payload["id"]):
                raise BillingStopped("Unverified response identity")
            usage, cost = usage_cost(self.policy, payload)
            if (usage["prompt_tokens"] > self.policy.input_ceiling or
                    usage["completion_tokens"] > original["output_limit"] or
                    cost > Decimal(original["reserved_cost"])):
                raise BillingStopped("Provider usage exceeded the declared reservation")
            row = {**original, "event": "settled", "usage": usage, "usage_derived_cost": str(cost),
                   "response_model": payload["model"],
                   "response_id_sha256": hashlib.sha256(payload["id"].encode()).hexdigest()}
            self._save(row)  # A failed settlement write must not release reservations.
            self.rows[number] = row

    def uncertain(self, number: int) -> None:
        with self.lock:
            self.blocked = True
            # Do not release even if the final journal append itself fails.
            self.rows[number] = {**self.rows[number], "event": "uncertain"}
            self._save(dict(self.rows[number]))

    def snapshot(self) -> dict:
        with self.lock:
            return summarize(list(self.rows.values()), self.policy, blocked=self.blocked)


def summarize(rows: list[dict], policy: Policy, *, blocked: bool = False) -> dict:
    latest = {row["request_id"]: row for row in rows}
    settled = [row for row in latest.values() if row["event"] == "settled"]
    unknown = [row for row in latest.values() if row["event"] != "settled"]
    known_cost = sum((Decimal(row["usage_derived_cost"]) for row in settled), Decimal(0))
    unresolved = sum((Decimal(row["reserved_cost"]) for row in unknown), Decimal(0))
    tokens = sum(row["usage"]["total_tokens"] for row in settled)
    held_tokens = sum(row["reserved_tokens"] for row in unknown)
    usage = {key: sum(row["usage"][key] for row in settled)
             for key in ("prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")}
    return dict(
        currency=policy.currency, cost_basis="configured token rates; not an independently verified account invoice",
        requests_sent=sum(bool(row["dispatched"]) for row in latest.values()),
        send_count_semantics="durable dispatch intent; crash before socket send can overcount",
        sdk_retry_sends=sum(bool(row["dispatched"]) and row.get("request_facts", {}).get("sdk_retry_index", 0) > 0
                            for row in latest.values()),
        reserved_cost=str(sum((Decimal(row["reserved_cost"]) for row in latest.values()), Decimal(0))),
        usage_derived_cost=str(known_cost) if settled else None,
        provider_reported_cost=None, unresolved_reservation=str(unresolved),
        remaining_total_budget=str(policy.total_budget - known_cost - unresolved),
        remaining_task_budget=str(policy.task_budget - known_cost - unresolved),
        remaining_tokens=policy.token_budget - tokens - held_tokens,
        usage=usage if settled else None, usage_complete=not unknown,
        blocked=blocked or bool(unknown), unknown_requests=len(unknown),
    )


class BudgetTransport(httpx.BaseTransport):
    """Below the OpenAI SDK retry loop: every HTTP attempt re-enters admission."""

    def __init__(self, policy: Policy, ledger: Ledger, inner: httpx.BaseTransport):
        self.policy, self.ledger, self.inner = policy, ledger, inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        p = self.policy
        expected_path = urlsplit(p.endpoint).path.rstrip("/") + "/chat/completions"
        if (str(request.url.copy_with(path="/", query=None)) != "https://api.deepseek.com/"
                or request.url.path != expected_path or request.url.query or request.method != "POST"):
            raise BillingStopped("Uncovered P1 HTTP route")
        try:
            body = json.loads(request.content)
            if body.get("model") != p.model or body.get("stream") or body.get("n", 1) != 1:
                raise ValueError("Unsupported model or request mode")
            # Do not let an SDK extra_body alias compete with the only output
            # bound reserved below. Its precedence is not part of this pilot.
            if "max_completion_tokens" in body:
                raise ValueError("Unsupported alternate output bound")
            requested = body.get("max_tokens", p.output_limit)
            if type(requested) is not int or requested <= 0:
                raise ValueError("Invalid output bound")
            body["max_tokens"] = min(requested, p.output_limit)
            body["thinking"] = {"type": "enabled"}
            body["reasoning_effort"] = p.effort
            encoded = json.dumps(body, ensure_ascii=False).encode()
        except Exception:
            raise BillingStopped("Unsupported request shape") from None
        system = [m for m in body.get("messages", []) if m.get("role") in {"system", "developer"}]
        retry = request.headers.get("x-stainless-retry-count", "0")
        facts = dict(model=p.model, effort=p.effort, thinking="enabled",
                     system_sha256=hashlib.sha256(json.dumps(system, sort_keys=True).encode()).hexdigest(),
                     tools_sha256=hashlib.sha256(json.dumps(body.get("tools", []), sort_keys=True).encode()).hexdigest(),
                     sdk_retry_index=int(retry) if retry.isdigit() and len(retry) < 5 else 0)
        number = self.ledger.reserve(hashlib.sha256(encoded).hexdigest(), body["max_tokens"], request_facts=facts)
        headers = dict(request.headers)
        headers.pop("content-length", None)
        wire = httpx.Request(request.method, request.url, headers=headers, content=encoded,
                             extensions=request.extensions)
        response = None
        try:
            # Serialize paid round trips, not tools. A queued auxiliary cannot
            # cross the boundary after another request makes billing uncertain.
            with self.ledger.lock:
                try:
                    self.ledger.dispatching(number)
                    response = self.inner.handle_request(wire)
                    response.read()
                    if response.status_code != 200:
                        raise ValueError("HTTP outcome lacks verified billing")
                    self.ledger.settle(number, response.json())
                except BaseException:
                    self.ledger.uncertain(number)
                    raise
            return response
        except BaseException:
            if response is not None:
                response.close()
            raise BillingStopped("Billing uncertain; further requests stopped") from None

    def close(self) -> None:
        self.inner.close()


def make_client(policy: Policy, ledger: Ledger, api_key: str, *, inner=None) -> OpenAI:
    transport = BudgetTransport(policy, ledger, inner if inner is not None else httpx.HTTPTransport(retries=0, trust_env=False))
    return OpenAI(api_key=api_key, base_url=policy.endpoint, max_retries=2,
                  http_client=httpx.Client(transport=transport, follow_redirects=False, trust_env=False, timeout=60))


def journal(path: Path) -> Callable[[dict], None]:
    def append(row: dict) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return append


def claim_attempt(root: Path, task: str) -> Path:
    if task not in {"T01", "T04"}:
        raise ValueError("P1 allows T01 and T04 only")
    path = root / task
    path.mkdir(mode=0o700)  # Atomic refusal; even failed attempts keep their identity.
    return path
