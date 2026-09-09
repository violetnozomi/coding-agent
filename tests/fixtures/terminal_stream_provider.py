"""Loopback-only SSE fixture for installed nz-coder PTY timeout acceptance.

Scripted responses prove wiring, never model capability. No credentials are read.
Run with --port and --log; GET /deny selects a single rejected-write scenario.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    step = 0
    mode = "repair"
    log_path: Path

    def log_message(self, *_args):
        pass

    def json_response(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/deny":
            Handler.mode, Handler.step = "deny", 0
            return self.json_response({"mode": "deny"})
        return self.json_response({"data": [{"id": "deepseek-v4-pro", "object": "model"}], "object": "list"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        choice = body.get("tool_choice", {})
        sidecar = isinstance(choice, dict) and choice.get("function", {}).get("name") == "emit_sidecar_verdict"
        name, args = None, {}
        text = "已新增并执行 test_value，1 test OK。Controlled transport verification complete."
        if sidecar:
            name, args = "emit_sidecar_verdict", {"verdict": "accept", "reason": "Controlled transport wiring only"}
        else:
            Handler.step += 1
            if Handler.mode == "deny":
                calls = [("write_file", {"path": "denied.txt", "content": "must not be written\n"})]
                text = "Permission was rejected. No write was executed."
            else:
                calls = [
                    ("read_file", {"path": "value.py"}),
                    ("edit_file", {"path": "value.py", "old_text": "return 1", "new_text": "return 2"}),
                    ("write_file", {"path": "test_value.py", "content": "import unittest\nfrom value import value\nclass ValueTest(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), 2)\n"}),
                    ("bash", {"command": "python -m unittest -v test_value"}),
                ]
            if Handler.step <= len(calls):
                name, args = calls[Handler.step - 1]
        with self.log_path.open("a") as log:
            log.write(json.dumps({"mode": Handler.mode, "step": Handler.step, "sidecar": sidecar,
                "tool": name, "stream": body.get("stream", False), "model": body.get("model")}) + "\n")
        message = {"role": "assistant", "content": "" if name else text}
        if name:
            message["tool_calls"] = [{"id": f"{Handler.mode}-{Handler.step}-{int(sidecar)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]
        base = {"id": f"controlled-{Handler.step}", "created": 1, "model": body["model"]}
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        finish = "tool_calls" if name else "stop"
        if not body.get("stream"):
            return self.json_response({**base, "object": "chat.completion", "choices": [{"index": 0,
                "message": message, "finish_reason": finish}], "usage": usage})
        delta = dict(message)
        for index, call in enumerate(delta.get("tool_calls", [])):
            call["index"] = index
        frames = [
            {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {**base, "object": "chat.completion.chunk", "choices": [], "usage": usage},
        ]
        data = b"".join(b"data: " + json.dumps(frame).encode() + b"\n\n" for frame in frames) + b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except BrokenPipeError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    Handler.log_path = args.log
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"CONTROLLED_PROVIDER_READY {args.port}", flush=True)
    server.serve_forever()
