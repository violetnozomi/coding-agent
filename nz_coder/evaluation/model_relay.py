"""有限实验的宿主 Unix 模型转发；不进入 Agent Core，不自动重试。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from http.server import BaseHTTPRequestHandler
import ipaddress
import json
import math
import os
import re
from pathlib import Path
import select
from socketserver import ThreadingMixIn, UnixStreamServer
import threading
import time
from typing import Callable
from urllib.parse import urlsplit
import uuid

import httpx


@dataclass(frozen=True)
class RelayLimits:
    requests: int = 26
    input_per_request: int = 100000
    input_total: int = 500000
    output_total: int = 100000
    request_seconds: float = 180.0


@dataclass(frozen=True)
class RelayBinding:
    experiment: str
    unit: str
    run: str
    role: str
    request_cap: int
    model: str
    output_limit: int
    deadline: float
    exact_output_limit: bool = True

    @property
    def bucket(self) -> str:
        return f"{self.unit}/{self.run}/{self.role}"


@dataclass(frozen=True)
class InputAccounting:
    upper_bound: int | None
    method: str
    trusted: bool = False
    exact: bool = False
    evidence_level: str = "unknown"
    contract_id: str = ""
    payload_sha256: str = ""
    reference_count: int | None = None


class AdmissionDenied(Exception):
    """本地拒绝不是一次 Provider 请求，也没有 Provider usage。"""


def unknown_input(_raw: bytes, _payload: dict) -> InputAccounting:
    return InputAccounting(None, "no-validated-provider-tokenizer-or-upper-bound")


def empirical_authorization_valid(grant) -> bool:
    """仅识别本轮宿主授权；不把经验计数升级为严格上界。"""
    if not isinstance(grant, dict):
        return False
    prices = grant.get("prices_cny_per_million", {})
    numbers = [grant.get("cost_limit"), grant.get("valid_from"), grant.get("valid_until")]
    numbers += [prices.get(k) for k in ("input", "cached_input", "output")]
    scope = grant.get("scope")
    if not isinstance(scope, list) or not scope or not all(isinstance(unit, str) for unit in scope):
        return False
    purpose = grant.get("purpose")
    allowed_scope = (purpose == "review-effects-formal" and set(scope) <= {"u01", "u02", "u03", "u04", "a01"})
    if purpose == "swebench-verified":
        # 仍由宿主签发同一账本授权，仅增加冻结实例范围，不借用旧审查单元。
        allowed_scope = (len(scope) <= 12 and len(set(scope)) == len(scope)
                         and all(re.fullmatch(r"[\w-]+__[\w-]+-\d+", unit) for unit in scope)
                         and bool(re.fullmatch(r"[0-9a-f]{64}", str(grant.get("instance_manifest_sha256", "")))))
    return (allowed_scope and grant.get("input_mode") == "empirical"
            and grant.get("empirical_risk_accepted") is True and grant.get("cost_currency") == "CNY"
            and bool(grant.get("account")) and bool(grant.get("authorization_text"))
            and bool(grant.get("contract_id")) and grant.get("model") == "deepseek-v4-flash"
            and all(type(n) in {int, float} and math.isfinite(n) and n > 0 for n in numbers)
            and prices["cached_input"] <= prices["input"]
            and grant["valid_from"] <= time.time() < grant["valid_until"])


class RelayLedger:
    """原子预留与追加记录；已有账本拒绝重开，避免重启清零。"""

    def __init__(self, path: Path, limits: RelayLimits, *, clock=time.monotonic, empirical_authorization=None):
        if empirical_authorization is not None and not empirical_authorization_valid(empirical_authorization):
            raise ValueError("invalid_empirical_authorization")
        self.empirical_authorization = dict(empirical_authorization) if empirical_authorization is not None else None
        self.cost_occupied_cny = 0.0
        self.path, self.limits, self.clock = path, limits, clock
        self.lock = threading.RLock()
        self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.counts: dict[str, int] = {}
        self.attempts: dict[str, dict] = {}
        self.seen: set[tuple[str, str]] = set()
        self.cancelled: set[str] = set()
        self.ambiguous: set[str] = set()
        self.failed_contracts: dict[str, dict] = {}
        self.input_occupied = self.output_occupied = 0
        self.events: list[dict] = []
        self._record("ledger_created", limits=vars(limits), input_admission_mode="empirical" if self.empirical_authorization else "strict",
                     fee_authorization=self.empirical_authorization)

    def _estimated_cost(self, prompt, completion, cached=0):
        if self.empirical_authorization is None:
            return 0.0
        prices = self.empirical_authorization["prices_cny_per_million"]
        return ((prompt-cached)*prices["input"] + cached*prices["cached_input"] + completion*prices["output"]) / 1_000_000

    def _record(self, event, **fields):
        row = {"event": event, "monotonic": self.clock(), "time_epoch": time.time(), **fields}
        data = (json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode()
        # 账目落盘成功才可转发；失败向上抛出，不能有一次未记账的请求。
        offset = 0
        while offset < len(data):
            offset += os.write(self.fd, data[offset:])
        os.fsync(self.fd)
        self.events.append(row)

    def deny(self, binding, reason):
        with self.lock:
            self._record("local_rejection", bucket=binding.bucket, reason=reason, usage=None)

    def stopped(self, binding) -> str:
        with self.lock:
            if binding.experiment in self.failed_contracts:
                return "experiment_accounting_contract_failed"
            if binding.run in self.cancelled:
                return "run_cancelled"
            if binding.run in self.ambiguous:
                return "prior_attempt_ambiguous"
            if self.clock() >= binding.deadline:
                return "run_deadline_exhausted"
            if self.empirical_authorization and not empirical_authorization_valid(self.empirical_authorization):
                return "empirical_authorization_expired_or_invalid"
            return ""

    def reserve(self, binding, nonce, raw, payload, accounting):
        with self.lock:
            reason = self.stopped(binding)
            if not reason and (not nonce or len(nonce) > 128):
                reason = "missing_or_invalid_request_nonce"
            key = (binding.bucket, nonce)
            if not reason and key in self.seen:
                reason = "duplicate_request_no_resend"
            output = payload.get("max_tokens")
            choices = payload.get("n", 1)
            if not reason and (payload.get("model") != binding.model or type(choices) is not int or choices != 1):
                reason = "model_or_choice_count_not_allowed"
            if not reason and (not isinstance(output, int) or isinstance(output, bool) or output < 1
                               or output > binding.output_limit
                               or (binding.exact_output_limit and output != binding.output_limit)):
                reason = "frozen_output_limit_mismatch"
            if not reason and any(k in payload for k in ("url", "base_url", "endpoint", "max_completion_tokens")):
                reason = "client_target_override_not_allowed"
            empirical = self.empirical_authorization is not None
            count = accounting.reference_count if empirical else accounting.upper_bound
            digest = hashlib.sha256(raw).hexdigest()
            if not reason and empirical and (binding.experiment != self.empirical_authorization["purpose"]
                    or binding.unit not in self.empirical_authorization["scope"]
                    or binding.model != self.empirical_authorization["model"]
                    or accounting.contract_id != self.empirical_authorization["contract_id"]):
                reason = "empirical_authorization_scope_mismatch"
            if not reason and ((not empirical and (not accounting.trusted or accounting.evidence_level not in {"exact", "proven_upper_bound"}))
                               or not accounting.contract_id or not isinstance(count, int) or isinstance(count, bool) or count < 0):
                reason = "input_token_bound_unknown"
            if not reason and (accounting.payload_sha256 != digest or json.loads(raw) != payload):
                reason = "counted_payload_mismatch"
            if not reason and count > self.limits.input_per_request:
                reason = "single_request_input_exhausted"
            if not reason and (len(self.attempts) >= self.limits.requests or self.counts.get(binding.bucket, 0) >= binding.request_cap):
                reason = "physical_request_cap_exhausted"
            if not reason and self.input_occupied + count > self.limits.input_total:
                reason = "input_reservation_exhausted"
            if not reason and self.output_occupied + output > self.limits.output_total:
                reason = "output_reservation_exhausted"
            cost = self._estimated_cost(count, output) if not reason else 0.0
            if not reason and empirical and self.cost_occupied_cny + cost > self.empirical_authorization["cost_limit"]:
                reason = "estimated_fee_reservation_exhausted"
            if reason:
                self._record("local_rejection", bucket=binding.bucket, reason=reason, usage=None)
                raise AdmissionDenied(reason)
            attempt_id = uuid.uuid4().hex
            row = {"attempt_id": attempt_id, "experiment": binding.experiment, "bucket": binding.bucket,
                   "unit": binding.unit, "run": binding.run, "role": binding.role,
                   "input_reserved": count, "output_reserved": output,
                   "input_method": accounting.method, "input_exact": accounting.exact,
                   "evidence_level": accounting.evidence_level, "contract_id": accounting.contract_id,
                   "payload_sha256": digest, "status": "reserved", "usage": None}
            row.update(input_admission_mode="empirical" if empirical else "strict",
                       estimated_cost_reserved_cny=cost if empirical else None)
            self._record("admitted", **row)
            self.seen.add(key)
            self.attempts[attempt_id] = row
            self.counts[binding.bucket] = self.counts.get(binding.bucket, 0) + 1
            self.input_occupied += count
            self.output_occupied += output
            self.cost_occupied_cny += cost
            return attempt_id

    def settle(self, attempt_id, *, status, usage=None, ambiguous=False, usage_conflict=False,
               finish_reason=None, provider_model=None, system_fingerprint=None, conflicting_reports=None):
        with self.lock:
            row = self.attempts[attempt_id]
            if row["status"] != "reserved":
                raise RuntimeError("attempt already settled")
            valid = isinstance(usage, dict) and not usage_conflict
            if valid:
                prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
                valid = all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (prompt, completion))
                total = usage.get("total_tokens")
                valid = valid and (total is None or (type(total) is int and total == prompt + completion))
                cached = usage.get("prompt_cache_hit_tokens", 0)
                details = usage.get("completion_tokens_details")
                details = {} if details is None else details
                valid = valid and isinstance(details, dict)
                reasoning = details.get("reasoning_tokens", 0) if isinstance(details, dict) else None
                valid = valid and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (cached, reasoning))
                valid = valid and cached <= prompt and reasoning <= completion
                miss = usage.get("prompt_cache_miss_tokens")
                if miss is not None:
                    valid = valid and type(miss) is int and 0 <= miss <= prompt
                    if "prompt_cache_hit_tokens" in usage:
                        valid = valid and cached + miss == prompt
                prompt_details = usage.get("prompt_tokens_details")
                if prompt_details is not None:
                    valid = valid and isinstance(prompt_details, dict)
                    alias = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
                    if alias is not None:
                        valid = valid and type(alias) is int and 0 <= alias <= prompt
                        if "prompt_cache_hit_tokens" in usage:
                            valid = valid and alias == cached
            trusted_usage = usage if valid and status == "complete" and finish_reason not in {"aborted", "error"} else None
            prompt_claim = usage.get("prompt_tokens") if isinstance(usage, dict) else None
            completion_claim = usage.get("completion_tokens") if isinstance(usage, dict) else None
            over_input = max(0, prompt_claim - row["input_reserved"]) if type(prompt_claim) is int and prompt_claim >= 0 else None
            over_output = max(0, completion_claim - row["output_reserved"]) if type(completion_claim) is int and completion_claim >= 0 else None
            if conflicting_reports:
                for report in conflicting_reports:
                    if not isinstance(report, dict):
                        continue
                    value = report.get("prompt_tokens")
                    if type(value) is int and value >= 0:
                        over_input = max(over_input or 0, value - row["input_reserved"])
                    value = report.get("completion_tokens")
                    if type(value) is int and value >= 0:
                        over_output = max(over_output or 0, value - row["output_reserved"])
            # 有效但超预留是契约被否证，不能伪装成缺失usage并继续发请求。
            empirical = self.empirical_authorization is not None
            contract_failed = bool(over_input or over_output or usage_conflict or (empirical and trusted_usage is None))
            actual_cost = self._estimated_cost(prompt, completion, cached) if trusted_usage is not None and empirical else None
            fields = dict(status=status, usage=trusted_usage, reported_usage=usage,
                          usage_valid=valid, usage_conflict=usage_conflict,
                          conflicting_reports=conflicting_reports,
                          usage_unknown_reason=None if trusted_usage is not None else ("missing_usage" if usage is None else "invalid_or_unsettled_usage"),
                          input_over_reservation=over_input, output_over_reservation=over_output,
                          reservation_retained=trusted_usage is None, ambiguous=ambiguous,
                          contract_failed=contract_failed, contract_id=row["contract_id"],
                          finish_reason=finish_reason, provider_model=provider_model, system_fingerprint=system_fingerprint)
            fields["estimated_cost_reported_cny"] = actual_cost
            self._record("settled", attempt_id=attempt_id, **fields)
            row.update(fields)
            if trusted_usage is not None:
                self.input_occupied -= row["input_reserved"] - usage["prompt_tokens"]
                self.output_occupied -= row["output_reserved"] - usage["completion_tokens"]
                if empirical:
                    self.cost_occupied_cny += actual_cost - row["estimated_cost_reserved_cny"]
            else:
                # 未完成请求不得释放预留；已报告的超额仍须如实增加占用。
                self.input_occupied += over_input or 0
                self.output_occupied += over_output or 0
            if contract_failed:
                self.failed_contracts[row["experiment"]] = {"attempt_id": attempt_id, "contract_id": row["contract_id"]}
                self._record("accounting_contract_failed", experiment=row["experiment"], attempt_id=attempt_id,
                             contract_id=row["contract_id"], input_over_reservation=over_input,
                             output_over_reservation=over_output)
            if ambiguous:
                self.ambiguous.add(row["run"])

    def cancel(self, run):
        with self.lock:
            self._record("run_cancelled", run=run)
            self.cancelled.add(run)

    def close(self):
        os.close(self.fd)


class _Usage:
    def __init__(self, streaming):
        self.streaming = streaming
        self.buffer = b""
        self.last = None
        self.invalid = False
        self.conflicting_reports = None
        self.done = not streaming
        self.finish_reason = self.provider_model = self.system_fingerprint = None

    def feed(self, chunk):
        self.buffer += chunk
        if len(self.buffer) > 2_000_000:
            raise ValueError("response frame exceeds bounded buffer")
        if not self.streaming:
            return
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if line.startswith(b"data: "):
                value = line[6:].strip()
                if value == b"[DONE]":
                    self.done = True
                else:
                    self._frame(json.loads(value, parse_constant=lambda _v: None))

    def _frame(self, data):
        if not isinstance(data, dict):
            raise ValueError("invalid response object")
        self.provider_model = data.get("model", self.provider_model)
        self.system_fingerprint = data.get("system_fingerprint", self.system_fingerprint)
        for choice in data.get("choices", []):
            if choice.get("finish_reason") is not None:
                self.finish_reason = choice["finish_reason"]
        usage = data.get("usage")
        if usage is None:
            return
        if not isinstance(usage, dict):
            self.invalid = True
            self.last = usage
            return
        if self.last is not None:
            if not isinstance(self.last, dict):
                self.invalid = True
                self.conflicting_reports = [self.last, usage]
                self.last = usage
                return
            for name in ("prompt_tokens", "completion_tokens"):
                before, after = self.last.get(name), usage.get(name)
                if not isinstance(before, int) or not isinstance(after, int) or after < before:
                    self.invalid = True
                    if self.conflicting_reports is None:
                        self.conflicting_reports = [self.last, usage]
        self.last = usage

    def finish(self):
        if not self.streaming:
            self._frame(json.loads(self.buffer, parse_constant=lambda _v: None))
        return self.last


class RelayServer(ThreadingMixIn, UnixStreamServer):
    """每个socket绑定宿主的一个单元/角色；不相信客户端归属头。"""

    daemon_threads = True

    def __init__(self, socket_path, *, ledger: RelayLedger, binding: RelayBinding,
                 upstream: str, counter: Callable = unknown_input,
                 remote_authorized=False, api_key=None, trace_directory=None):
        target = urlsplit(upstream)
        try:
            local = ipaddress.ip_address(target.hostname or "").is_loopback
        except ValueError:
            local = False
        if target.path != "/v1/chat/completions" or target.query or target.username or target.fragment:
            raise ValueError("one fixed upstream chat-completions target required")
        if not local and (not remote_authorized or target.scheme != "https"):
            raise PermissionError("remote model forwarding is not authorized")
        if local and target.scheme != "http":
            raise ValueError("offline upstream must be literal loopback HTTP")
        if not math.isfinite(binding.deadline):
            raise ValueError("bounded monotonic run deadline required")
        self.ledger, self.binding, self.upstream = ledger, binding, upstream
        self.counter, self.api_key = counter, api_key
        self.trace_directory = trace_directory
        if trace_directory is not None:
            trace_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handler_slots = threading.BoundedSemaphore(8)
        super().__init__(str(socket_path), _Handler)
        os.chmod(socket_path, 0o600)

    def process_request(self, request, client_address):
        # 单元内连接也有上限；未准入连接不能无限创建宿主线程。
        if not self._handler_slots.acquire(blocking=False):
            self.ledger.deny(self.binding, "relay_handler_capacity")
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._handler_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._handler_slots.release()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _error(self, code, reason):
        body = json.dumps({"error": {"type": "experiment_boundary", "message": reason}}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        server = self.server
        if self.path != "/v1/chat/completions":
            self._error(404, "only_chat_completions_allowed")
            return
        self.connection.settimeout(1)
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size <= 0 or size > 2_000_000:
                raise ValueError("invalid bounded request body")
            raw = self.rfile.read(size)
            payload = json.loads(raw, parse_constant=lambda _v: None)
            if not isinstance(payload, dict) or len(raw) != size:
                raise ValueError("invalid request body")
            accounting = server.counter(raw, payload)
            nonce = self.headers.get("X-NZ-Call-Id", "")
            attempt = server.ledger.reserve(server.binding, nonce, raw, payload, accounting)
        except AdmissionDenied as exc:
            self._error(429, str(exc))
            return
        except (ValueError, TypeError, OSError) as exc:
            server.ledger.deny(server.binding, type(exc).__name__)
            self._error(400, "invalid_local_request")
            return
        self.connection.settimeout(0.25)
        status, usage, ambiguous, observation = "transport_error", None, True, {}
        self._headers_sent = False
        remaining = min(server.ledger.limits.request_seconds, server.binding.deadline - time.monotonic())
        try:
            if server.trace_directory is not None:
                path = server.trace_directory / (attempt + ".request.json")
                with path.open("xb") as handle:
                    os.chmod(path, 0o600)
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            status, usage, ambiguous, observation = asyncio.run(asyncio.wait_for(self._forward(raw, payload, attempt), max(0.001, remaining)))
        except TimeoutError:
            status = "request_timeout"
        except (httpx.HTTPError, ConnectionError, OSError, ValueError):
            status = "transport_error"
        finally:
            server.ledger.settle(attempt, status=status, usage=usage, ambiguous=ambiguous, **observation)
        if status not in {"complete", "upstream_error"}:
            if not self._headers_sent:
                self._error(502, status)
            elif payload.get("stream"):
                try:
                    self.wfile.write(b'data: {"error":{"type":"experiment_boundary","message":"forward interrupted"}}\n\n')
                    self.wfile.flush()
                except OSError:
                    pass
        self.close_connection = True

    async def _forward(self, raw, payload, attempt):
        server = self.server
        usage = _Usage(bool(payload.get("stream")))
        status_code = None

        async def copy_response():
            nonlocal status_code
            if server.ledger.stopped(server.binding):
                return
            headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
            if server.api_key is not None:
                headers["Authorization"] = "Bearer " + server.api_key
            async with httpx.AsyncClient(trust_env=False, timeout=server.ledger.limits.request_seconds) as client:
                async with client.stream("POST", server.upstream, content=raw, headers=headers) as response:
                    status_code = response.status_code
                    self.send_response(status_code)
                    self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
                    self.end_headers()
                    self._headers_sent = True
                    path = server.trace_directory / (attempt + ".response.txt") if server.trace_directory else None
                    handle = path.open("xb") if path else None
                    if path:
                        os.chmod(path, 0o600)
                    recorded_bytes = 0
                    try:
                        async for chunk in response.aiter_bytes():
                            recorded_bytes += len(chunk)
                            if recorded_bytes > 32 * 1024 * 1024:
                                raise ValueError("bounded_response_capture_exhausted")
                            if handle:
                                handle.write(chunk)
                                handle.flush()
                            usage.feed(chunk)
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    finally:
                        if handle:
                            os.fsync(handle.fileno())
                            handle.close()

        async def interruption():
            while True:
                if server.ledger.stopped(server.binding):
                    return
                readable, _, _ = select.select([self.connection], [], [], 0)
                if readable and not self.connection.recv(1, 2):  # MSG_PEEK，不消费新请求。
                    return
                await asyncio.sleep(0.01)

        copy_task = asyncio.create_task(copy_response())
        cancel_task = asyncio.create_task(interruption())
        try:
            done, _ = await asyncio.wait((copy_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done:
                return "cancelled_or_disconnected", usage.last, True, {"usage_conflict": usage.invalid,
                                                                       "conflicting_reports": usage.conflicting_reports}
            await copy_task
            if status_code != 200:
                return "upstream_error", usage.finish(), False, {}
            actual = usage.finish()
            observation = {"usage_conflict": usage.invalid, "finish_reason": usage.finish_reason,
                           "conflicting_reports": usage.conflicting_reports,
                           "provider_model": usage.provider_model, "system_fingerprint": usage.system_fingerprint}
            return ("complete", actual, False, observation) if usage.done else ("stream_incomplete", actual, True, observation)
        finally:
            for task in (copy_task, cancel_task):
                task.cancel()
            await asyncio.gather(copy_task, cancel_task, return_exceptions=True)
