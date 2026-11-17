"""Bounded nz.command.v2 RPC 与独立执行事实流，共用可唤醒 socket pump。"""
from __future__ import annotations

import base64
from collections import OrderedDict, deque
import hashlib
from contextlib import contextmanager
from contextvars import Context, ContextVar
from dataclasses import asdict, dataclass, field, fields
import json
import math
import selectors
import socket
import struct
import threading
import time
from typing import Iterator, Protocol

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError, public_error_from_wire
from nz_coder.runtime.core.execution_context import (
    CommandExecutionPolicy, CommandPolicyRestriction, capture_command_restriction,
)
from nz_coder.runtime.core.run_settings import RunSettings
from nz_coder.runtime.verification.domain.models import ExecutionReceipt


@dataclass(frozen=True)
class CommandChannelLimits:
    max_frame_bytes: int = 64 * 1024
    max_message_bytes: int = 16 * 1024 * 1024
    max_requests: int = 4

    @classmethod
    def from_settings(cls, settings: RunSettings) -> CommandChannelLimits:
        # bash正文有head/tail上限；JSON转义、metadata及列表另留有限预算。
        payload = (2 * settings.process_buffer_bytes + settings.process_read_max_bytes
                   + settings.process_write_max_bytes + 8192 * settings.process_max_per_workspace)
        return cls(max_message_bytes=max(16 * 1024 * 1024, 6 * payload),
                   max_requests=settings.max_parallel_tasks)


@dataclass(frozen=True)
class CommandFrame:
    kind: str
    request_id: int
    payload: dict


def _pairs(items: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate command JSON field")
        result[key] = value
    return result


def _finite(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite command number")
    return result


def _invalid_constant(value: str):
    raise ValueError("nonfinite command number")


def _decode_json(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_invalid_constant, parse_float=_finite)
    except (UnicodeError, RecursionError) as exc:
        raise ValueError("invalid command JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("command JSON must be an object")
    return value


def _encode_json(value: dict, limit: int) -> bytes:
    output = bytearray()
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":")).iterencode(value):
        encoded = part.encode("utf-8")
        if len(output) + len(encoded) > limit:
            raise ValueError("command message limit exceeded")
        output.extend(encoded)
    return bytes(output)


def _validate_frame(value: dict, limits: CommandChannelLimits) -> CommandFrame:
    if set(value) != {"schema", "kind", "request_id", "payload"} or value["schema"] != "nz.command.v2":
        raise ValueError("invalid command schema")
    kind, request_id, payload = value["kind"], value["request_id"], value["payload"]
    if type(request_id) is not int or request_id < 0 or not isinstance(payload, dict) or not isinstance(kind, str):
        raise ValueError("invalid command frame types")
    if kind in {"policy_ready", "policy_ack", "finish", "finish_ack", "error", "facts_barrier", "facts_barrier_ack", "facts_sealed"}:
        if request_id != 0:
            raise ValueError("invalid command control id")
    elif request_id == 0:
        raise ValueError("invalid command request id")
    if kind in {"request_begin", "response_begin", "fact_begin"}:
        if set(payload) != {"length"} or type(payload["length"]) is not int or not 0 < payload["length"] <= limits.max_message_bytes:
            raise ValueError("command message limit or length invalid")
    elif kind in {"request_chunk", "response_chunk", "fact_chunk"}:
        if set(payload) != {"seq", "data"} or type(payload["seq"]) is not int or payload["seq"] < 0 or not isinstance(payload["data"], str):
            raise ValueError("invalid command chunk")
    elif kind in {"request_end", "response_end", "fact_end", "fact_ack", "cancel", "policy_ack", "finish", "finish_ack"}:
        if payload:
            raise ValueError("invalid command control fields")
    elif kind == "policy_ready":
        if set(payload) != {"fingerprint"} or not isinstance(payload["fingerprint"], str):
            raise ValueError("invalid command policy fingerprint")
    elif kind in {"facts_barrier", "facts_barrier_ack", "facts_sealed"}:
        expected = ({"barrier_id"} if kind == "facts_barrier" else
                    {"barrier_id", "watermark"} if kind == "facts_barrier_ack" else {"watermark"})
        if set(payload) != expected or any(type(v) is not int or v < 0 for v in payload.values()):
            raise ValueError("invalid fact barrier")
    elif kind == "progress":
        if set(payload) != {"title", "metadata"} or not isinstance(payload["title"], str) or not isinstance(payload["metadata"], dict):
            raise ValueError("invalid command progress")
        if len(str(payload["metadata"].get("output", ""))) > 4000:
            raise ValueError("command progress limit exceeded")
    elif kind == "error":
        if set(payload) != {"code", "message"} or payload["code"] != "child_command_channel_error" or not isinstance(payload["message"], str):
            raise ValueError("invalid command error")
    else:
        raise ValueError("unknown command frame kind")
    return CommandFrame(kind, request_id, payload)


def encode_frame(frame: CommandFrame, *, limits: CommandChannelLimits) -> bytes:
    value = {"schema": "nz.command.v2", "kind": frame.kind,
             "request_id": frame.request_id, "payload": frame.payload}
    _validate_frame(value, limits)
    data = _encode_json(value, limits.max_frame_bytes - 4)
    return struct.pack("!I", len(data)) + data


class CommandFrameDecoder:
    def __init__(self, limits: CommandChannelLimits) -> None:
        self._limits = limits
        self._pending = bytearray()

    def feed(self, data: bytes) -> list[CommandFrame]:
        self._pending.extend(data)
        result = []
        while len(self._pending) >= 4:
            length = struct.unpack("!I", self._pending[:4])[0]
            if not 0 < length <= self._limits.max_frame_bytes - 4:
                raise ValueError("command frame limit exceeded")
            if len(self._pending) < length + 4:
                break
            body = bytes(self._pending[4:length + 4])
            del self._pending[:length + 4]
            result.append(_validate_frame(_decode_json(body), self._limits))
        return result

    def finish(self) -> None:
        if self._pending:
            raise ValueError("incomplete command frame at EOF")


@dataclass
class _IncomingMessage:
    length: int
    data: bytearray = field(default_factory=bytearray)
    seq: int = 0


class _Messages:
    def __init__(self) -> None:
        self.pending: dict[int, _IncomingMessage] = {}

    def receive(self, frame: CommandFrame) -> dict | None:
        request_id, payload = frame.request_id, frame.payload
        if frame.kind.endswith("_begin"):
            if request_id in self.pending:
                raise ValueError("duplicate command message")
            self.pending[request_id] = _IncomingMessage(payload["length"])
            return None
        message = self.pending.get(request_id)
        if message is None:
            raise ValueError("command message was not begun")
        if frame.kind.endswith("_chunk"):
            if payload["seq"] != message.seq:
                raise ValueError("out-of-order command chunk")
            try:
                raw = base64.b64decode(payload["data"], validate=True)
            except (ValueError, UnicodeError) as exc:
                raise ValueError("invalid command chunk encoding") from exc
            if not raw or len(message.data) + len(raw) > message.length:
                raise ValueError("command message length exceeded")
            message.data.extend(raw)
            message.seq += 1
            return None
        if len(message.data) != message.length:
            raise ValueError("incomplete command message")
        del self.pending[request_id]
        return _decode_json(bytes(message.data))


def _message_frames(prefix: str, request_id: int, data: bytes, limits: CommandChannelLimits) -> Iterator[CommandFrame]:
    yield CommandFrame(prefix + "_begin", request_id, {"length": len(data)})
    size = max(1, (limits.max_frame_bytes - 1024) * 3 // 4)
    for seq, offset in enumerate(range(0, len(data), size)):
        yield CommandFrame(prefix + "_chunk", request_id, {
            "seq": seq, "data": base64.b64encode(data[offset:offset + size]).decode("ascii"),
        })
    yield CommandFrame(prefix + "_end", request_id, {})


def _restriction(value: dict) -> CommandPolicyRestriction:
    if not isinstance(value, dict) or set(value) - {"strict_local_tools"}:
        raise ValueError("invalid command restriction")
    if value.get("strict_local_tools") is not None and type(value["strict_local_tools"]) is not bool:
        raise ValueError("invalid command restriction flag")
    return CommandPolicyRestriction(value.get("strict_local_tools"))


def _reply_payload(result: str | PublicError) -> dict:
    from nz_coder.tools import ToolOutput

    if isinstance(result, PublicError):
        return {"type": "public_error", "error": result.to_dict()}
    if isinstance(result, ToolOutput):
        return {"type": "tool_output", "content": str(result), "title": result.title,
                "metadata": result.metadata, "attachments": result.attachments,
                "execution_facts": [asdict(item) for item in result.execution_facts]}
    return {"type": "str", "content": str(result)}


def _decode_reply(value: dict) -> str | PublicError:
    from nz_coder.tools import ToolOutput

    if value.get("type") == "public_error" and set(value) == {"type", "error"}:
        error = public_error_from_wire(value["error"])
        if error is None:
            raise ValueError("invalid public command error")
        return error
    if value.get("type") == "str" and set(value) == {"type", "content"} and isinstance(value["content"], str):
        return value["content"]
    if (value.get("type") == "tool_output" and set(value) == {"type", "content", "title", "metadata", "attachments", "execution_facts"}
            and isinstance(value["content"], str) and isinstance(value["title"], str)
            and isinstance(value["metadata"], dict) and isinstance(value["attachments"], list)
            and all(isinstance(item, dict) for item in value["attachments"])
            and isinstance(value["execution_facts"], list)):
        return ToolOutput(value["content"], title=value["title"], metadata=value["metadata"], attachments=value["attachments"],
                          execution_facts=tuple(_decode_receipt(item) for item in value["execution_facts"]))
    raise ValueError("invalid command result")


def _decode_receipt(value: dict) -> ExecutionReceipt:
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(ExecutionReceipt)}:
        raise ValueError("invalid execution receipt fields")
    value = dict(value)
    for key in ("capabilities", "targets"):
        if not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key]):
            raise ValueError("invalid execution receipt coverage")
        value[key] = tuple(value[key])
    for key in ("producer", "command", "cwd", "outcome", "output_reference", "reason", "execution_id"):
        if not isinstance(value[key], str):
            raise ValueError("invalid execution receipt identity")
    for key in ("in_process", "output_complete", "reliable", "environment_error"):
        if type(value[key]) is not bool:
            raise ValueError("invalid execution receipt flag")
    for key in ("exit_code", "test_count"):
        if value[key] is not None and type(value[key]) is not int:
            raise ValueError("invalid execution receipt result")
    return ExecutionReceipt(**value)


@dataclass(frozen=True)
class CommandExecutionFact:
    settlement_id: str
    launch_request_id: int
    receipt: ExecutionReceipt


@dataclass
class ChildCommandEndpoint:
    socket: socket.socket
    settlement_id: str

    def close(self) -> None:
        self.socket.close()


class CommandChannelHandler(Protocol):
    def bind_pump(self, pump) -> None: ...
    def begin_request(self, request_id: int) -> None: ...
    def submit_request(self, request_id: int, tool: str, arguments: dict, restriction: CommandPolicyRestriction) -> None: ...
    def cancel_request(self, request_id: int) -> None: ...
    def peek_reply(self) -> tuple[int, str | PublicError] | None: ...
    def reply_sent(self, request_id: int) -> None: ...
    def take_progress(self) -> tuple[int, str, dict] | None: ...
    def policy_ready(self, fingerprint: str) -> None: ...
    def finish(self) -> None: ...
    def connection_lost(self, error: PublicError | None) -> None: ...
    def peek_fact(self) -> tuple[int, CommandExecutionFact] | None: ...
    def fact_sent(self, seq: int) -> None: ...
    def acknowledge_fact(self, seq: int) -> None: ...
    def fact_barrier_watermark(self) -> int | None: ...
    def fact_watermark_acknowledged(self, watermark: int) -> bool: ...
    def seal_facts(self, watermark: int) -> None: ...


class _SocketPump:
    """Only this registered thread touches the nonblocking socket and closes it."""

    def __init__(self, stream: socket.socket, limits: CommandChannelLimits, name: str) -> None:
        self._socket = stream
        self._limits = limits
        self._name = name
        self._stop = threading.Event()
        self._launch_done = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._launch_lock = threading.Lock()
        self._launch_requested = False
        self._wakeup_read, self._wakeup_write = socket.socketpair()
        self._wakeup_read.setblocking(False)
        self._wakeup_write.setblocking(False)

    def start(self) -> None:
        with self._launch_lock:
            if self._launch_requested or self._closed.is_set():
                raise ValueError("command pump is already started or closed")
            self._launch_requested = True
        try:
            self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
            self._thread.start()
        except BaseException:
            if self._thread is None or self._thread.ident is None:
                for resource in (self._socket, self._wakeup_read, self._wakeup_write):
                    while True:
                        try:
                            resource.close()
                            break
                        except OSError:
                            time.sleep(0.05)
                self._closed.set()
            raise
        finally:
            self._launch_done.set()

    def request_stop(self) -> None:
        self._stop.set()
        self.wake()

    def wake(self) -> None:
        try:
            self._wakeup_write.send(b"\0")
        except (BlockingIOError, OSError):
            pass  # 已有唤醒字节或关闭竞态；待发事实仍保留到确认。

    def close(self, *, deadline: float) -> bool:
        self.request_stop()
        with self._launch_lock:
            if not self._launch_requested:
                try:
                    for resource in (self._socket, self._wakeup_read, self._wakeup_write):
                        resource.close()
                except OSError:
                    return False
                self._closed.set()
                self._launch_done.set()
        if not self._launch_done.wait(max(0.0, deadline - time.monotonic())):
            return False
        if self._thread is not None and self._thread.ident is not None:
            self._thread.join(max(0.0, deadline - time.monotonic()))
        return self._closed.is_set() and not (self._thread is not None and self._thread.is_alive())

    def _next_frame(self) -> CommandFrame | None:
        raise NotImplementedError

    def _received(self, frame: CommandFrame) -> None:
        raise NotImplementedError

    def _sent(self, frame: CommandFrame) -> None:
        pass

    def _disconnected(self, error: PublicError | None) -> None:
        raise NotImplementedError

    def _run(self) -> None:
        error = None
        selector = None
        try:
            self._socket.setblocking(False)
            selector = selectors.DefaultSelector()
            selector.register(self._socket, selectors.EVENT_READ)
            selector.register(self._wakeup_read, selectors.EVENT_READ)
            decoder = CommandFrameDecoder(self._limits)
            pending = b""
            offset = 0
            frame = None
            while not self._stop.is_set():
                if not pending:
                    frame = self._next_frame()
                    if frame is not None:
                        pending = encode_frame(frame, limits=self._limits)
                        offset = 0
                selector.modify(self._socket, selectors.EVENT_READ | (selectors.EVENT_WRITE if pending else 0))
                eof = False
                for key, flags in selector.select():
                    if key.fileobj is self._wakeup_read:
                        try:
                            self._wakeup_read.recv(4096)
                        except BlockingIOError:
                            pass
                        continue
                    if flags & selectors.EVENT_READ:
                        try:
                            data = self._socket.recv(self._limits.max_frame_bytes)
                        except BlockingIOError:
                            data = None
                        if data == b"":
                            decoder.finish()
                            eof = True
                            break
                        if data:
                            for incoming in decoder.feed(data):
                                if self._stop.is_set():
                                    break
                                self._received(incoming)
                    if flags & selectors.EVENT_WRITE and pending:
                        try:
                            count = self._socket.send(memoryview(pending)[offset:])
                        except BlockingIOError:
                            count = None
                        if count == 0:
                            raise ValueError("command socket stopped sending")
                        if count is not None:
                            offset += count
                            if offset == len(pending):
                                assert frame is not None
                                self._sent(frame)
                                pending = b""
                if eof:
                    break
        except BaseException:
            error = PublicError("child_command_channel_error", "Child command channel failed.")
        finally:
            try:
                self._disconnected(error)
            finally:
                # 关闭失败由同一owner重试；其他线程不能跨过它关闭同一个fd。
                for resource in (selector, self._socket, self._wakeup_read, self._wakeup_write):
                    if resource is None:
                        continue
                    while True:
                        try:
                            resource.close()
                            break
                        except OSError:
                            time.sleep(0.05)
                self._closed.set()


class ParentCommandPump(_SocketPump):
    def __init__(self, socket: socket.socket, *, handler: CommandChannelHandler, limits: CommandChannelLimits) -> None:
        super().__init__(socket, limits, "nz-command-parent-pump")
        self._handler = handler
        self._messages = _Messages()
        self._controls: OrderedDict[str, CommandFrame] = OrderedDict()
        self._response: Iterator[CommandFrame] | None = None
        self._progress_turn = True
        self._fact_stream: Iterator[CommandFrame] | None = None
        self._barriers: dict[int, int | None] = {}
        handler.bind_pump(self)

    def _received(self, frame: CommandFrame) -> None:
        if frame.kind in {"request_begin", "request_chunk", "request_end"}:
            if frame.kind == "request_begin":
                self._handler.begin_request(frame.request_id)
            value = self._messages.receive(frame)
            if value is not None:
                if set(value) != {"tool", "arguments", "restriction"} or not isinstance(value["tool"], str) or not isinstance(value["arguments"], dict):
                    raise ValueError("invalid command request")
                self._handler.submit_request(frame.request_id, value["tool"], value["arguments"], _restriction(value["restriction"]))
        elif frame.kind == "cancel":
            self._messages.pending.pop(frame.request_id, None)
            self._handler.cancel_request(frame.request_id)
        elif frame.kind == "policy_ready":
            self._handler.policy_ready(frame.payload["fingerprint"])
            self._controls.setdefault("policy_ack", CommandFrame("policy_ack", 0, {}))
        elif frame.kind == "finish":
            self._handler.finish()
            self._controls.setdefault("finish_ack", CommandFrame("finish_ack", 0, {}))
        elif frame.kind == "fact_ack":
            self._handler.acknowledge_fact(frame.request_id)
        elif frame.kind == "facts_barrier":
            self._barriers[frame.payload["barrier_id"]] = None
        elif frame.kind == "facts_sealed":
            self._handler.seal_facts(frame.payload["watermark"])
        else:
            raise ValueError("invalid child command message")

    def _next_frame(self) -> CommandFrame | None:
        if self._controls:
            return self._controls.popitem(last=False)[1]
        for token, watermark in list(self._barriers.items()):
            if watermark is None:
                watermark = self._handler.fact_barrier_watermark()
                self._barriers[token] = watermark
            if watermark is not None and self._handler.fact_watermark_acknowledged(watermark):
                del self._barriers[token]
                return CommandFrame("facts_barrier_ack", 0, {"barrier_id": token, "watermark": watermark})
        if self._fact_stream is None:
            pending_fact = self._handler.peek_fact()
            if pending_fact is not None:
                seq, fact = pending_fact
                self._fact_stream = _message_frames("fact", seq,
                    _encode_json(asdict(fact), self._limits.max_message_bytes), self._limits)
        if self._fact_stream is not None:
            return next(self._fact_stream)
        if self._response is None:
            reply = self._handler.peek_reply()
            if reply is not None:
                request_id, result = reply
                try:
                    data = _encode_json(_reply_payload(result), self._limits.max_message_bytes)
                except (ValueError, TypeError, RecursionError):
                    data = _encode_json(_reply_payload("Error: Command message limit exceeded"), self._limits.max_message_bytes)
                self._response = _message_frames("response", request_id, data, self._limits)
        if self._progress_turn or self._response is None:
            self._progress_turn = False
            progress = self._handler.take_progress()
            if progress is not None:
                request_id, title, metadata = progress
                frame = CommandFrame("progress", request_id, {"title": title, "metadata": metadata})
                try:
                    encode_frame(frame, limits=self._limits)
                except (ValueError, TypeError, RecursionError):
                    pass  # 进度允许合并/丢弃；最终响应始终通过有界分片发送。
                else:
                    return frame
        if self._response is not None:
            self._progress_turn = True
            return next(self._response)
        return None

    def _sent(self, frame: CommandFrame) -> None:
        if frame.kind == "response_end":
            self._handler.reply_sent(frame.request_id)
            self._response = None
        elif frame.kind == "fact_end":
            self._handler.fact_sent(frame.request_id)
            self._fact_stream = None
        elif frame.kind == "finish_ack":
            self._stop.set()

    def _disconnected(self, error: PublicError | None) -> None:
        self._messages.pending.clear()
        self._handler.connection_lost(error)


@dataclass
class _ClientCall:
    request_id: int
    context: Context | None
    done: threading.Event = field(default_factory=threading.Event)
    result: str | PublicError = "Error: Child command channel unavailable"
    request_sent: bool = False
    want_cancel: bool = False
    cancel_sent: bool = False
    abandoned: bool = False


class _ClientPump(_SocketPump):
    def __init__(self, client: ChildCommandClient) -> None:
        super().__init__(client._endpoint.socket, client._limits, "nz-command-child-pump")
        self._client = client

    def _received(self, frame: CommandFrame) -> None:
        self._client._received(frame)

    def _next_frame(self) -> CommandFrame | None:
        return self._client._next_frame()

    def _sent(self, frame: CommandFrame) -> None:
        self._client._sent(frame)

    def _disconnected(self, error: PublicError | None) -> None:
        self._client._disconnected(error)


class ChildCommandClient:
    def __init__(self, endpoint: ChildCommandEndpoint, *, limits: CommandChannelLimits, deadline: float) -> None:
        self._endpoint = endpoint
        self._limits = limits
        self._deadline = deadline
        self._condition = threading.Condition(threading.RLock())
        self._calls: dict[int, _ClientCall] = {}
        self._outgoing: OrderedDict[int, Iterator[CommandFrame]] = OrderedDict()
        self._messages = _Messages()
        self._high_water = 0
        self._ready = threading.Event()
        self._ready_ok = False
        self._policy_fingerprint: str | None = None
        self._policy_to_send: str | None = None
        self._finish = threading.Event()
        self._finish_requested = False
        self._finish_to_send = False
        self._finish_ok = False
        self._lost = False
        self._fact_receiver = None
        self._launch_receivers: dict[int, tuple[Context, object]] = {}
        self._fact_messages = _Messages()
        self._fact_acks = deque()
        self._fact_digests: dict[int, str] = {}
        self._barrier_high_water = 0
        self._barriers_to_send = deque()
        self._barrier_results: dict[int, int] = {}
        self._facts_sealed_to_send: int | None = None
        self._pump = _ClientPump(self)
        try:
            self._pump.start()
        except BaseException:
            # 部分启动失败也必须由原pump退役；父监督器仍持有隔离本体期限。
            self._pump.request_stop()
            while not self._pump.close(deadline=time.monotonic() + 0.05):
                pass
            raise

    @property
    def deadline(self) -> float:
        return self._deadline

    def set_fact_receiver(self, callback) -> None:
        """只影响后续启动；旧进程保留其启动时的接收者和上下文。"""
        with self._condition:
            self._fact_receiver = callback

    def facts_barrier(self, *, deadline: float) -> int:
        with self._condition:
            if self._lost:
                raise RuntimeError("command fact channel disconnected")
            self._barrier_high_water += 1
            token = self._barrier_high_water
            self._barriers_to_send.append(token)
        self._pump.wake()
        with self._condition:
            ready = self._condition.wait_for(lambda: token in self._barrier_results or self._lost,
                max(0.0, min(deadline, self._deadline) - time.monotonic()))
            if self._lost:
                raise RuntimeError("command fact channel disconnected")
            if not ready:
                raise TimeoutError("command fact barrier deadline exceeded")
            return self._barrier_results.pop(token)

    def facts_sealed(self, watermark: int) -> None:
        with self._condition:
            self._facts_sealed_to_send = watermark
        self._pump.wake()

    def policy_ready(self, policy: CommandExecutionPolicy, *, deadline: float) -> bool:
        with self._condition:
            if self._lost:
                return False
            if self._policy_fingerprint is not None:
                if self._policy_fingerprint != policy.fingerprint:
                    return False
            else:
                # 同一次子任务的验证修复会再次initialize；只复用同一策略握手。
                self._policy_fingerprint = policy.fingerprint
                self._policy_to_send = policy.fingerprint
        self._pump.wake()
        self._ready.wait(max(0.0, min(deadline, self._deadline) - time.monotonic()))
        return self._ready_ok

    def call(self, tool: str, arguments: dict, *, cancel_event: threading.Event | None) -> str:
        from contextvars import copy_context

        if cancel_event is not None and cancel_event.is_set():
            return "Error: Command cancelled"
        restriction = capture_command_restriction()
        try:
            data = _encode_json({"tool": tool, "arguments": arguments, "restriction": {
                "strict_local_tools": restriction.strict_local_tools,
            }}, self._limits.max_message_bytes)
        except (ValueError, TypeError, RecursionError):
            return "Error: Command message limit or encoding invalid"
        with self._condition:
            if not self._ready_ok or self._lost or self._finish_requested:
                return "Error: Child command channel unavailable"
            if len(self._calls) >= self._limits.max_requests:
                return "Error: Command request limit reached"
            starts_process = tool == "process" and arguments.get("operation") == "start"
            if starts_process and self._fact_receiver is None:
                return "Error: Process fact receiver is not initialized"
            self._high_water += 1
            request_id = self._high_water
            record = _ClientCall(request_id, copy_context())
            self._calls[request_id] = record
            if starts_process:
                self._launch_receivers[request_id] = (copy_context(), self._fact_receiver)
            self._outgoing[request_id] = _message_frames("request", request_id, data, self._limits)
        self._pump.wake()
        try:
            while not record.done.wait(0.05):
                cancelled = cancel_event is not None and cancel_event.is_set()
                if cancelled or time.monotonic() >= self._deadline:
                    with self._condition:
                        record.want_cancel = True
                        record.abandoned = True
                        record.context = None
                    self._pump.wake()
                    return "Error: Command cancelled" if cancelled else "Error: Command deadline exceeded"
            # pump只保存回复；调用方收到完整终态后抛错，仍走同一清理路径。
            if isinstance(record.result, PublicError):
                raise PublicRuntimeError(record.result)
            return record.result
        finally:
            with self._condition:
                if record.done.is_set() or self._lost:
                    self._calls.pop(request_id, None)
                record.context = None
                self._condition.notify_all()

    def _next_frame(self) -> CommandFrame | None:
        with self._condition:
            if self._fact_acks:
                return CommandFrame("fact_ack", self._fact_acks.popleft(), {})
            if self._barriers_to_send:
                return CommandFrame("facts_barrier", 0, {"barrier_id": self._barriers_to_send.popleft()})
            if self._facts_sealed_to_send is not None:
                watermark, self._facts_sealed_to_send = self._facts_sealed_to_send, None
                return CommandFrame("facts_sealed", 0, {"watermark": watermark})
            if self._policy_to_send is not None:
                fingerprint, self._policy_to_send = self._policy_to_send, None
                return CommandFrame("policy_ready", 0, {"fingerprint": fingerprint})
            for record in self._calls.values():
                if record.want_cancel and record.request_sent and not record.cancel_sent:
                    record.cancel_sent = True
                    return CommandFrame("cancel", record.request_id, {})
            if self._finish_to_send:
                self._finish_to_send = False
                return CommandFrame("finish", 0, {})
            if self._outgoing:
                request_id = next(iter(self._outgoing))
                frame = next(self._outgoing[request_id])
                self._outgoing.move_to_end(request_id)
                return frame
        return None

    def _sent(self, frame: CommandFrame) -> None:
        if frame.kind == "request_end":
            with self._condition:
                self._outgoing.pop(frame.request_id)
                record = self._calls.get(frame.request_id)
                if record is not None:
                    record.request_sent = True

    def _received(self, frame: CommandFrame) -> None:
        if frame.kind in {"fact_begin", "fact_chunk", "fact_end"}:
            value = self._fact_messages.receive(frame)
            if value is None:
                return
            if (set(value) != {"settlement_id", "launch_request_id", "receipt"}
                    or value["settlement_id"] != self._endpoint.settlement_id
                    or type(value["launch_request_id"]) is not int):
                raise ValueError("execution fact owner mismatch")
            fact = CommandExecutionFact(value["settlement_id"], value["launch_request_id"], _decode_receipt(value["receipt"]))
            digest = hashlib.sha256(_encode_json(value, self._limits.max_message_bytes)).hexdigest()
            with self._condition:
                previous = self._fact_digests.get(frame.request_id)
                receiver = self._launch_receivers.get(fact.launch_request_id)
            if previous is not None and previous != digest:
                raise ValueError("conflicting execution fact")
            if previous is None:
                if receiver is None:
                    raise ValueError("unknown execution fact launch")
                context, callback = receiver
                if context.run(callback, fact) is False:
                    raise ValueError("execution fact was not accepted")
            with self._condition:
                self._fact_digests[frame.request_id] = digest
                self._launch_receivers.pop(fact.launch_request_id, None)
                self._fact_acks.append(frame.request_id)
            self._pump.wake()
        elif frame.kind == "facts_barrier_ack":
            with self._condition:
                token = frame.payload["barrier_id"]
                if not 0 < token <= self._barrier_high_water:
                    raise ValueError("unknown fact barrier")
                self._barrier_results[token] = frame.payload["watermark"]
                self._condition.notify_all()
        elif frame.kind == "policy_ack":
            with self._condition:
                if self._policy_fingerprint is None or self._ready.is_set():
                    raise ValueError("unexpected policy ACK")
                self._ready_ok = True
                self._ready.set()
        elif frame.kind == "finish_ack":
            with self._condition:
                if not self._finish_requested or self._calls:
                    raise ValueError("unexpected finish ACK")
                self._finish_ok = True
                self._finish.set()
        elif frame.kind == "progress":
            with self._condition:
                record = self._calls.get(frame.request_id)
                if record is None:
                    raise ValueError("unknown command progress id")
                context = record.context if not record.abandoned else None
            if context is not None:
                from nz_coder.tools import report_tool_metadata

                try:
                    context.run(report_tool_metadata, title=frame.payload["title"], metadata=frame.payload["metadata"])
                except BaseException:
                    pass
        elif frame.kind in {"response_begin", "response_chunk", "response_end"}:
            with self._condition:
                record = self._calls.get(frame.request_id)
                if record is None:
                    raise ValueError("unknown command response id")
                value = self._messages.receive(frame)
                if value is not None:
                    record.result = _decode_reply(value)
                    record.done.set()
                    if record.abandoned:
                        del self._calls[frame.request_id]
                    self._condition.notify_all()
        else:
            raise ValueError("invalid parent command message")

    def _disconnected(self, error: PublicError | None) -> None:
        with self._condition:
            self._lost = True
            self._ready.set()
            self._finish.set()
            self._messages.pending.clear()
            self._fact_messages.pending.clear()
            self._outgoing.clear()
            for request_id, record in list(self._calls.items()):
                record.done.set()
                if record.abandoned:
                    del self._calls[request_id]
            self._condition.notify_all()

    def finish(self, *, deadline: float) -> bool:
        try:
            watermark = self.facts_barrier(deadline=deadline)
        except (RuntimeError, TimeoutError):
            return False
        self.facts_sealed(watermark)
        with self._condition:
            if self._calls or not self._ready_ok or self._lost:
                return False
            if not self._finish_requested:
                self._finish_requested = True
                self._finish_to_send = True
        self._pump.wake()
        self._finish.wait(max(0.0, min(deadline, self._deadline) - time.monotonic()))
        return self._finish_ok

    def close(self, *, deadline: float) -> bool:
        if not self._pump.close(deadline=deadline):
            return False
        with self._condition:
            while self._calls:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            self._launch_receivers.clear()
            self._fact_receiver = None
            return True


_CURRENT_COMMAND_CLIENT: ContextVar[ChildCommandClient | None] = ContextVar(
    "nz_child_command_client", default=None,
)


def current_command_client() -> ChildCommandClient | None:
    return _CURRENT_COMMAND_CLIENT.get()


@contextmanager
def scoped_command_client(client: ChildCommandClient) -> Iterator[None]:
    token = _CURRENT_COMMAND_CLIENT.set(client)
    try:
        yield
    finally:
        _CURRENT_COMMAND_CLIENT.reset(token)
