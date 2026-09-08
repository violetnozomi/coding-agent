"""Fixed P1/P2 DeepSeek HTTP budget boundary; no Agent loop or global billing service."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
import time
from typing import Callable
from urllib.parse import urlsplit

import httpx
from openai import OpenAI


class BillingStopped(RuntimeError):
    """Secret-free fail-closed request admission/settlement error."""

    def __init__(self, message: str, *, stage: str | None = None, category: str = "billing_stopped"):
        super().__init__(message)
        self.stage, self.category = stage, category


def _exception_category(error: BaseException) -> str:
    # Never emit dynamic class names or provider error strings/codes.
    if isinstance(error, BillingStopped):
        return error.category
    for cls, label in ((KeyboardInterrupt, "cancelled"), (SystemExit, "cancelled"),
                       (httpx.ConnectTimeout, "connect_timeout"), (httpx.WriteTimeout, "write_timeout"),
                       (httpx.ReadTimeout, "read_timeout"), (httpx.ReadError, "read_error"),
                       (httpx.ConnectError, "connect_error"), (httpx.RemoteProtocolError, "protocol_error"),
                       (json.JSONDecodeError, "invalid_json"), (OSError, "os_error")):
        if isinstance(error, cls):
            return label
    # asyncio cancellation is a BaseException, not an ordinary success/error.
    return "unknown_exception" if isinstance(error, Exception) else "cancelled"


def _diagnostic(**overrides) -> dict:
    return dict(recording_status="recorded", failure_stage=None, exception_category=None,
                http_status=None, usage_present=None, settlement_state="reserved",
                evidence_saved=True, secondary_failures=[], **overrides)


def _request_facts(body: dict, request: httpx.Request) -> dict:
    """Safe facts from the actual SDK payload; purpose is not propagated by this seam."""
    mode = body.get("thinking")
    thinking = mode.get("type") if isinstance(mode, dict) else None
    thinking = thinking if thinking in ("enabled", "disabled", None) else "unsupported"
    effort = body.get("reasoning_effort")
    effort = effort if effort in ("high", "low", "max", None) else "unsupported"
    choice = body.get("tool_choice")
    choice_type = choice.get("type") if isinstance(choice, dict) else choice
    choice_type = choice_type if choice_type in ("function", "none", "auto", "required", None) else "other"
    retry = request.headers.get("x-stainless-retry-count", "0")
    system = [m for m in body.get("messages", []) if isinstance(m, dict) and m.get("role") in {"system", "developer"}]
    return dict(purpose="unknown", requested_thinking=thinking, thinking=thinking,
                effort=effort, effort_applicable=thinking == "enabled", tool_choice_type=choice_type,
                system_sha256=hashlib.sha256(json.dumps(system, sort_keys=True).encode()).hexdigest(),
                tools_sha256=hashlib.sha256(json.dumps(body.get("tools", []), sort_keys=True).encode()).hexdigest(),
                sdk_retry_index=int(retry) if retry.isdigit() and len(retry) < 5 else 0)


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
    main_thinking: str = "enabled"
    auxiliary_thinking: str | None = None
    auxiliary_output_limit: int = 1024

    def __post_init__(self):
        url = urlsplit(self.endpoint)
        if (url.scheme != "https" or url.netloc != "api.deepseek.com" or
                url.path not in {"", "/", "/v1", "/v1/"} or url.query or url.fragment):
            raise ValueError("P1 supports only the credential-free official DeepSeek endpoint")
        if (self.model != "deepseek-v4-flash" or self.currency != "CNY" or self.effort != "high"
                or type(self.authorized) is not bool or self.main_thinking != "enabled"
                or self.auxiliary_thinking not in (None, "disabled")
                or type(self.auxiliary_output_limit) is not int or not 1 <= self.auxiliary_output_limit <= 1024):
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

    def _save(self, row: dict, *, stage="diagnostic_persistence") -> None:
        try:
            self.record(row)
        except Exception:
            self.blocked = True
            raise BillingStopped("Billing evidence persistence failed", stage=stage,
                                 category="persistence_error") from None
        except BaseException:
            self.blocked = True
            raise

    def reserve(self, request_hash: str, output_limit: int, *, request_facts: dict | None = None) -> int:
        with self.lock:
            p = self.policy
            if not p.authorized:
                raise BillingStopped("Request not authorized", stage="authorization")
            if self.blocked or time.monotonic() >= self.deadline:
                raise BillingStopped("Billing stopped", stage="admission")
            total_tokens = p.input_ceiling + output_limit
            cost = money((p.input_ceiling * p.miss_rate + output_limit * p.output_rate) / Decimal(1000000))
            state = self.snapshot()
            if (cost > Decimal(state["remaining_total_budget"]) or cost > Decimal(state["remaining_task_budget"])
                    or total_tokens > state["remaining_tokens"]):
                raise BillingStopped("Request reservation exceeds remaining budget", stage="reservation")
            number = len(self.rows) + 1
            row = dict(event="reserved", request_id=number, request_hash=request_hash,
                       reserved_cost=str(cost), reserved_tokens=total_tokens,
                       output_limit=output_limit, dispatched=False, usage=None, usage_derived_cost=None)
            row["request_facts"] = dict(request_facts or {})
            row["diagnostic"] = _diagnostic()
            row["inner_transport_entered"] = False
            self.rows[number] = row
            self._save(dict(row), stage="reservation_persistence")
            return number

    def dispatching(self, number: int) -> None:
        with self.lock:
            if self.blocked or time.monotonic() >= self.deadline:
                raise BillingStopped("Request stopped before dispatch")
            row = {**self.rows[number], "event": "dispatching", "dispatched": True}
            self._save(row, stage="dispatch_persistence")
            self.rows[number] = row

    def settle(self, number: int, payload: dict) -> None:
        with self.lock:
            original = self.rows[number]
            if (not isinstance(payload, dict) or payload.get("model") != self.policy.model or not isinstance(payload.get("id"), str)
                    or not payload["id"]):
                raise BillingStopped("Unverified response identity", stage="response_identity", category="invalid_identity")
            try:
                usage, cost = usage_cost(self.policy, payload)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise BillingStopped("Unverified usage", stage="usage_validation", category="invalid_usage") from None
            if (usage["prompt_tokens"] > self.policy.input_ceiling or
                    usage["completion_tokens"] > original["output_limit"] or
                    cost > Decimal(original["reserved_cost"])):
                raise BillingStopped("Provider usage exceeded the declared reservation",
                                     stage="usage_validation", category="invalid_usage")
            row = {**original, "event": "settled", "usage": usage, "usage_derived_cost": str(cost),
                   "response_model": payload["model"],
                   "response_id_sha256": hashlib.sha256(payload["id"].encode()).hexdigest()}
            row["diagnostic"] = {**original["diagnostic"], "usage_present": True, "settlement_state": "settled"}
            self._save(row, stage="settlement_persistence")  # Failed writes never release holds.
            self.rows[number] = row

    def uncertain(self, number: int) -> None:
        with self.lock:
            self.blocked = True
            # Do not release even if the final journal append itself fails.
            self.rows[number] = {**self.rows[number], "event": "uncertain"}
            self._save(dict(self.rows[number]))

    def failure(self, number: int, error: BaseException, stage: str, facts: dict,
                *, http_status=None, usage_present=None, secondary=()) -> BaseException | None:
        """Preserve first cause in memory even if the diagnostic append also fails."""
        self.blocked = True
        original = self.rows.get(number)
        if original is None:
            original = dict(request_id=number, reserved_cost="0", reserved_tokens=0,
                            dispatched=False, inner_transport_entered=False, usage=None,
                            usage_derived_cost=None, request_facts=facts, event="rejected")
        state = "settled" if original["event"] == "settled" else (
            "not_dispatched" if original["event"] == "rejected" else "uncertain")
        diag = _diagnostic()
        diag.update(failure_stage=error.stage if isinstance(error, BillingStopped) and error.stage else stage,
                    exception_category=_exception_category(error), http_status=http_status,
                    usage_present=usage_present, settlement_state=state,
                    secondary_failures=list(secondary))
        previous = original.get("diagnostic", {})
        if previous.get("failure_stage"):
            diag = {**previous, "evidence_saved": True, "secondary_failures": [
                *previous["secondary_failures"],
                dict(failure_stage=diag["failure_stage"], exception_category=diag["exception_category"]),
                *secondary]}
        row = {**original, "event": original["event"] if state in {"settled", "not_dispatched"} else "uncertain",
               "diagnostic": diag}
        self.rows[number] = row
        try:
            self._save(row)
        except BaseException as diagnostic_error:
            # This append is best effort only; admission remains blocked in memory.
            diag = {**diag, "evidence_saved": False, "secondary_failures": [*diag["secondary_failures"],
                    dict(failure_stage="diagnostic_persistence", exception_category=_exception_category(diagnostic_error))]}
            self.rows[number] = {**row, "diagnostic": diag}
            if not isinstance(diagnostic_error, Exception):
                return diagnostic_error
        return None

    def snapshot(self) -> dict:
        with self.lock:
            return summarize(list(self.rows.values()), self.policy, blocked=self.blocked)


def summarize(rows: list[dict], policy: Policy, *, blocked: bool = False) -> dict:
    latest = {row["request_id"]: row for row in rows}
    settled = [row for row in latest.values() if row["event"] == "settled"]
    unknown = [row for row in latest.values() if row["event"] not in {"settled", "rejected"}]
    failed = any(row.get("diagnostic", {}).get("failure_stage") for row in latest.values())
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
        sdk_retry_entries=sum(row.get("request_facts", {}).get("sdk_retry_index", 0) > 0 for row in latest.values()),
        request_diagnostics=[dict(request_id=row["request_id"],
            **(row.get("diagnostic") or {"recording_status": "historical_not_recorded", "http_status": None,
                                        "failure_stage": None, "exception_category": None})) for row in latest.values()],
        reserved_cost=str(sum((Decimal(row["reserved_cost"]) for row in latest.values()), Decimal(0))),
        usage_derived_cost=str(known_cost) if settled else None,
        provider_reported_cost=None, unresolved_reservation=str(unresolved),
        remaining_total_budget=str(policy.total_budget - known_cost - unresolved),
        remaining_task_budget=str(policy.task_budget - known_cost - unresolved),
        remaining_tokens=policy.token_budget - tokens - held_tokens,
        usage=usage if settled else None, usage_complete=not unknown,
        blocked=blocked or bool(unknown) or failed, unknown_requests=len(unknown),
    )


class BudgetTransport(httpx.BaseTransport):
    """Below the OpenAI SDK retry loop: every HTTP attempt re-enters admission."""

    def __init__(self, policy: Policy, ledger: Ledger, inner: httpx.BaseTransport):
        self.policy, self.ledger, self.inner = policy, ledger, inner

    def _prepare(self, request: httpx.Request, facts: dict) -> tuple[bytes, int]:
        p = self.policy
        expected_path = urlsplit(p.endpoint).path.rstrip("/") + "/chat/completions"
        if (str(request.url.copy_with(path="/", query=None)) != "https://api.deepseek.com/"
                or request.url.path != expected_path or request.url.query or request.method != "POST"):
            raise BillingStopped("Uncovered P1 HTTP route")
        try:
            body = json.loads(request.content)
            facts.update(_request_facts(body, request))
            if body.get("model") != p.model or body.get("stream") or body.get("n", 1) != 1:
                raise ValueError("Unsupported model or request mode")
            # Do not let an SDK extra_body alias compete with the only output
            # bound reserved below. Its precedence is not part of this pilot.
            if "max_completion_tokens" in body:
                raise ValueError("Unsupported alternate output bound")
            requested = body.get("max_tokens", p.output_limit)
            if type(requested) is not int or requested <= 0:
                raise ValueError("Invalid output bound")
            # Defaults belong to the Provider construction seam, not billing.
            # A mode set is validated here; message/tool text grants no authority.
            mode = body.get("thinking")
            if mode == {"type": p.main_thinking}:
                if body.get("reasoning_effort") != p.effort:
                    raise ValueError("Unsupported main effort")
                limit = p.output_limit
            elif p.auxiliary_thinking and mode == {"type": p.auxiliary_thinking}:
                if "reasoning_effort" in body:
                    raise ValueError("Effort is not applicable to the disabled pilot mode")
                limit = p.auxiliary_output_limit
            else:
                raise ValueError("Mode is not explicitly allowed")
            body["max_tokens"] = min(requested, limit)
            facts["output_limit"] = body["max_tokens"]
            facts["model"] = p.model
            encoded = json.dumps(body, ensure_ascii=False).encode()
        except Exception:
            raise BillingStopped("Unsupported request shape") from None
        return encoded, body["max_tokens"]

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # Serialize requests, not tools, across every compatible client. Intent
        # precedes entry into inner; neither fact proves the server billed it.
        with self.ledger.lock:
            number = len(self.ledger.rows) + 1
            facts = {"purpose": "unknown"}
            response, status, usage_present = None, None, None
            stage = "request_configuration"
            try:
                encoded, output_limit = self._prepare(request, facts)
                stage = "reservation"
                self.ledger.reserve(hashlib.sha256(encoded).hexdigest(), output_limit, request_facts=facts)
                headers = dict(request.headers)
                headers.pop("content-length", None)
                wire = httpx.Request(request.method, request.url, headers=headers, content=encoded,
                                     extensions=request.extensions)
                stage = "dispatch_persistence"
                self.ledger.dispatching(number)
                stage = "transport_send"
                self.ledger.rows[number]["inner_transport_entered"] = True
                response = self.inner.handle_request(wire)
                status = response.status_code
                stage = "http_response"
                if status != 200:
                    # Do not parse error.message/code or infer a free request.
                    raise BillingStopped("HTTP response lacks verified billing", category="http_error")
                stage = "response_read"
                response.read()
                stage = "json_parse"
                payload = response.json()
                usage_present = isinstance(payload, dict) and "usage" in payload and payload["usage"] is not None
                self.ledger.rows[number]["diagnostic"] = {
                    **self.ledger.rows[number]["diagnostic"], "http_status": status, "usage_present": usage_present}
                stage = "response_identity"
                self.ledger.settle(number, payload)
                return response
            except BaseException as error:
                # Close/diagnostic failures are secondary, never replacements
                # for the initial trusted phase/category (including cancellation).
                secondary = []
                close_cancel = None
                if response is not None:
                    try:
                        response.close()
                    except BaseException as close_error:
                        secondary.append(dict(failure_stage="response_close",
                                              exception_category=_exception_category(close_error)))
                        if not isinstance(close_error, Exception):
                            close_cancel = close_error
                diagnostic_cancel = self.ledger.failure(number, error, stage, facts, http_status=status,
                                                        usage_present=usage_present, secondary=secondary)
                if not isinstance(error, Exception):
                    raise
                if close_cancel is not None:
                    raise close_cancel
                if diagnostic_cancel is not None:
                    raise diagnostic_cancel
                raise BillingStopped("Request stopped; see safe billing diagnostics") from None

    def close(self) -> None:
        unwinding = sys.exc_info()[1]
        try:
            self.inner.close()
        except BaseException as error:
            with self.ledger.lock:
                rows = self.ledger.rows
                number = next((n for n, row in rows.items() if row.get("diagnostic", {}).get("failure_stage")),
                              max(rows, default=1))
                original = rows.get(number, {})
                diag = original.get("diagnostic", {})
                cancellation = self.ledger.failure(number, error, "transport_close", original.get("request_facts", {}),
                    http_status=diag.get("http_status"), usage_present=diag.get("usage_present"))
                if unwinding is not None and not isinstance(unwinding, Exception):
                    return  # Let the first cancellation continue unwinding.
                if not isinstance(error, Exception):
                    raise
                if cancellation is not None:
                    raise cancellation
                if not diag.get("failure_stage"):
                    raise BillingStopped("Transport cleanup failed; billing stopped") from None
                # A close exception must not replace an already recorded
                # request failure escaping the client's context manager.


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


def claim_attempt(root: Path, task: str, *, allowed_tasks: tuple[str, ...] = ("T01", "T04")) -> Path:
    from .live import P2_TASKS
    if allowed_tasks not in (("T01", "T04"), ("T04",), P2_TASKS) or task not in allowed_tasks:
        raise ValueError("Task is outside the validated live plan")
    path = root / task
    path.mkdir(mode=0o700)  # Atomic refusal; even failed attempts keep their identity.
    return path
