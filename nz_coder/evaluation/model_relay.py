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


class AdmissionDenied(Exception):
    """本地拒绝不是一次 Provider 请求，也没有 Provider usage。"""


def unknown_input(_raw: bytes, _payload: dict) -> InputAccounting:
    return InputAccounting(None, "no-validated-provider-tokenizer-or-upper-bound")


class RelayLedger:
    """原子预留与追加记录；已有账本拒绝重开，避免重启清零。"""

    def __init__(self, path: Path, limits: RelayLimits, *, clock=time.monotonic):
        self.path, self.limits, self.clock = path, limits, clock
        self.lock = threading.RLock()
        self.fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.counts: dict[str, int] = {}
        self.attempts: dict[str, dict] = {}
        self.seen: set[tuple[str, str]] = set()
        self.cancelled: set[str] = set()
        self.ambiguous: set[str] = set()
        self.input_occupied = self.output_occupied = 0
        self.events: list[dict] = []
        self._record("ledger_created", limits=vars(limits))

    def _record(self, event, **fields):
        row = {"event": event, "monotonic": self.clock(), **fields}
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
        if binding.run in self.cancelled:
            return "run_cancelled"
        if binding.run in self.ambiguous:
            return "prior_attempt_ambiguous"
        if self.clock() >= binding.deadline:
            return "run_deadline_exhausted"
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
            count = accounting.upper_bound
            if not reason and (not accounting.trusted or not isinstance(count, int) or isinstance(count, bool) or count < 0):
                reason = "input_token_bound_unknown"
            if not reason and count > self.limits.input_per_request:
                reason = "single_request_input_exhausted"
            if not reason and (len(self.attempts) >= self.limits.requests or self.counts.get(binding.bucket, 0) >= binding.request_cap):
                reason = "physical_request_cap_exhausted"
            if not reason and self.input_occupied + count > self.limits.input_total:
                reason = "input_reservation_exhausted"
            if not reason and self.output_occupied + output > self.limits.output_total:
                reason = "output_reservation_exhausted"
            if reason:
                self._record("local_rejection", bucket=binding.bucket, reason=reason, usage=None)
                raise AdmissionDenied(reason)
            attempt_id = uuid.uuid4().hex
            row = {"attempt_id": attempt_id, "experiment": binding.experiment, "bucket": binding.bucket,
                   "unit": binding.unit, "run": binding.run, "role": binding.role,
                   "input_reserved": count, "output_reserved": output,
                   "input_method": accounting.method, "input_exact": accounting.exact,
                   "payload_sha256": hashlib.sha256(raw).hexdigest(), "status": "reserved", "usage": None}
            self._record("admitted", **row)
            self.seen.add(key)
            self.attempts[attempt_id] = row
            self.counts[binding.bucket] = self.counts.get(binding.bucket, 0) + 1
            self.input_occupied += count
            self.output_occupied += output
            return attempt_id

    def settle(self, attempt_id, *, status, usage=None, ambiguous=False):
        with self.lock:
            row = self.attempts[attempt_id]
            if row["status"] != "reserved":
                raise RuntimeError("attempt already settled")
            valid = isinstance(usage, dict)
            if valid:
                prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
                valid = all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (prompt, completion))
                total = usage.get("total_tokens")
                valid = valid and (total is None or (type(total) is int and total == prompt + completion))
                cached = usage.get("prompt_cache_hit_tokens", 0)
                details = usage.get("completion_tokens_details") or {}
                valid = valid and isinstance(details, dict)
                reasoning = details.get("reasoning_tokens", 0) if isinstance(details, dict) else None
                valid = valid and all(isinstance(x, int) and not isinstance(x, bool) and x >= 0 for x in (cached, reasoning))
                valid = valid and cached <= prompt and reasoning <= completion
                valid = valid and prompt <= row["input_reserved"] and completion <= row["output_reserved"]
            trusted_usage = usage if valid and status == "complete" else None
            self._record("settled", attempt_id=attempt_id, status=status, usage=trusted_usage,
                         reservation_retained=trusted_usage is None, ambiguous=ambiguous)
            row.update(status=status, usage=trusted_usage)
            if trusted_usage is not None:
                self.input_occupied -= row["input_reserved"] - usage["prompt_tokens"]
                self.output_occupied -= row["output_reserved"] - usage["completion_tokens"]
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
        self.done = not streaming

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
                    self._frame(json.loads(value))

    def _frame(self, data):
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return
        if self.last is not None:
            for name in ("prompt_tokens", "completion_tokens"):
                before, after = self.last.get(name), usage.get(name)
                if not isinstance(before, int) or not isinstance(after, int) or after < before:
                    self.invalid = True
        self.last = usage

    def finish(self):
        if not self.streaming:
            self._frame(json.loads(self.buffer))
        return self.last if self.done and not self.invalid else None


class RelayServer(ThreadingMixIn, UnixStreamServer):
    """每个socket绑定宿主的一个单元/角色；不相信客户端归属头。"""

    daemon_threads = True

    def __init__(self, socket_path, *, ledger: RelayLedger, binding: RelayBinding,
                 upstream: str, counter: Callable = unknown_input,
                 remote_authorized=False, api_key=None):
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
        status, usage, ambiguous = "transport_error", None, True
        self._headers_sent = False
        remaining = min(server.ledger.limits.request_seconds, server.binding.deadline - time.monotonic())
        try:
            status, usage, ambiguous = asyncio.run(asyncio.wait_for(self._forward(raw, payload), max(0.001, remaining)))
        except TimeoutError:
            status = "request_timeout"
        except (httpx.HTTPError, ConnectionError, OSError, ValueError):
            status = "transport_error"
        finally:
            server.ledger.settle(attempt, status=status, usage=usage, ambiguous=ambiguous)
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

    async def _forward(self, raw, payload):
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
                    async for chunk in response.aiter_bytes():
                        usage.feed(chunk)
                        self.wfile.write(chunk)
                        self.wfile.flush()

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
                return "cancelled_or_disconnected", None, True
            await copy_task
            if status_code != 200:
                return "upstream_error", None, False
            actual = usage.finish()
            return ("complete", actual, False) if usage.done else ("stream_incomplete", None, True)
        finally:
            for task in (copy_task, cancel_task):
                task.cancel()
            await asyncio.gather(copy_task, cancel_task, return_exceptions=True)
