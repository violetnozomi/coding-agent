"""仅做协议响应和记录的本地假模型，不读写任务文件或运行测试。"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading


class ExecutionFake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, cancel=False):
        self.requests = []
        self.lock = threading.Lock()
        self.cancel = cancel
        super().__init__(("127.0.0.1", 0), _Handler)

    def message(self, request):
        schemas = [t.get("function", {}).get("name") for t in request.get("tools", [])]
        transcript = request["messages"]
        results = [m for m in transcript if m.get("role") == "tool"]
        calls = [c for m in transcript for c in (m.get("tool_calls") or [])]
        if schemas == ["emit_sidecar_verdict"]:
            assert "passed-current-generation" in str(transcript)
            name, args = "emit_sidecar_verdict", {"verdict": "accept", "reason": "Local execution-boundary probe: actual current-generation project test and recorded boundary checks passed."}
        elif transcript[-1].get("content") == "heartbeat":
            return {"role": "assistant", "content": "received"}
        elif self.cancel:
            name, args = "bash", {"command": "python heartbeat.py"}
        elif not results:
            name, args = "read_file", {"path": "calc.py"}
        elif not any(c["function"]["name"] == "edit_file" for c in calls):
            assert "value = 1" in str(results[-1])
            name, args = "edit_file", {"path": "calc.py", "old_text": "value = 1", "new_text": "value = 2"}
        elif not any("boundary_probe.py" in c["function"]["arguments"] for c in calls):
            assert "Error:" not in str(results[-1])
            name, args = "bash", {"command": "python boundary_probe.py"}
        elif not any("pytest" in c["function"]["arguments"] for c in calls):
            assert '"passed": true' in str(results[-1])
            name, args = "bash", {"command": "python -m pytest -q tests"}
        else:
            assert "1 passed" in str(results[-1])
            return {"role": "assistant", "content": "calc.py updated; project test and the workspace boundary probe passed."}
        assert name in schemas
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "local-call-" + str(len(self.requests)),
            "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class FormalFake(ExecutionFake):
    """正式入口的离线协议检查；A仅检查接入，不预填功能补丁。"""

    def message(self, request):
        names = [t.get("function", {}).get("name") for t in request.get("tools", [])]
        transcript = request["messages"]
        if names == ["emit_sidecar_verdict"]:
            text = str(transcript)
            if "payment.py" in text:
                assert "passed-current-generation" in text
                good = "+    if value < 0 and not allow_refund:" in text
                verdict = "accept" if good else "revise"
                reason = "Local protocol check of actual payment.py diff and current test evidence."
            else:
                verdict, reason = "blocked", "Offline protocol run did not implement the requested correlation ID feature."
            name, arguments = "emit_sidecar_verdict", {"verdict": verdict, "reason": reason}
        else:
            results = [m for m in transcript if m.get("role") == "tool"]
            if not results:
                name, arguments = "read_file", {"path": "app/api.py"}
            elif not any("pytest" in str(m.get("tool_calls")) for m in transcript):
                assert "handle" in str(results)
                name, arguments = "bash", {"command": "python -m pytest -q tests"}
            else:
                assert "passed" in str(results)
                return {"role": "assistant", "content": "Offline protocol only: read app/api.py and ran the existing tests. The requested feature is not implemented. External live model authorization is unavailable."}
        assert name in names
        return {"role": "assistant", "content": None, "tool_calls": [{"id": "formal-" + str(len(self.requests)),
                "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        request = json.loads(raw)
        with self.server.lock:
            self.server.requests.append(request)
        try:
            message = self.server.message(request)
            usage = {"prompt_tokens": len(raw), "completion_tokens": 7, "total_tokens": len(raw) + 7}
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            body = {"id": "local-response", "object": "chat.completion", "model": request["model"], "created": 1,
                    "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage}
            if request.get("stream"):
                delta = dict(message)
                if delta.get("tool_calls"):
                    delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(delta["tool_calls"])]
                first = {**body, "object": "chat.completion.chunk", "usage": None,
                         "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
                last = {**body, "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}
                data = ("data: " + json.dumps(first) + "\n\n" + "data: " + json.dumps(last) + "\n\ndata: [DONE]\n\n").encode()
                content_type = "text/event-stream"
            else:
                data, content_type = json.dumps(body).encode(), "application/json"
            self.send_response(200)
        except (AssertionError, KeyError, TypeError) as exc:
            data = json.dumps({"error": {"type": "local_probe_protocol", "message": str(exc)}}).encode()
            content_type = "application/json"
            self.send_response(400)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass
