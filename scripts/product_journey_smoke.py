#!/usr/bin/env python3
"""Drive real PTY product journeys against a protocol-faithful local provider."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "product-smoke-model"
FILE_NAME = "product-smoke.txt"
FILE_CONTENT = "NZ-Coder interactive product journey passed.\n"
_ANSI = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class _ProviderHandler(BaseHTTPRequestHandler):
    """Serve model discovery and a deterministic write-file coding turn."""

    server_version = "NZCoderProductProvider/1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/models":
            self.send_error(404)
            return
        self._json({
            "object": "list",
            "data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}],
        })

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(payload)  # type: ignore[attr-defined]
        messages = list(payload.get("messages") or [])
        has_tool_result = any(item.get("role") == "tool" for item in messages)
        if payload.get("stream"):
            self._stream_completion(has_tool_result)
        else:
            self._json(self._buffered_completion(has_tool_result))

    def _stream_completion(self, has_tool_result: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if has_tool_result:
            chunks = [
                _chunk({"role": "assistant", "content": "Product smoke complete."}),
                _chunk({}, finish_reason="stop"),
            ]
        else:
            chunks = [
                _chunk({
                    "role": "assistant",
                    "tool_calls": [{
                        "index": 0,
                        "id": "call_product_smoke",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": FILE_NAME,
                                "content": FILE_CONTENT,
                            }),
                        },
                    }],
                }),
                _chunk({}, finish_reason="tool_calls"),
            ]
        for chunk in chunks:
            self.wfile.write(
                f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
            )
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    @staticmethod
    def _buffered_completion(has_tool_result: bool) -> dict:
        if has_tool_result:
            message = {"role": "assistant", "content": "Product smoke complete."}
            finish_reason = "stop"
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_product_smoke",
                    "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": FILE_NAME,
                            "content": FILE_CONTENT,
                        }),
                    },
                }],
            }
            finish_reason = "tool_calls"
        return {
            "id": "chatcmpl-product-smoke",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL_ID,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }

    def _json(self, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _chunk(delta: dict, *, finish_reason: str | None = None) -> dict:
    return {
        "id": "chatcmpl-product-smoke",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": MODEL_ID,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }


@contextmanager
def _provider_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProviderHandler)
    server.requests = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class _PTYProduct:
    """Bounded PTY driver for the actual no-argument terminal product."""

    def __init__(self, workspace: Path, environment: dict[str, str]) -> None:
        master, slave = pty.openpty()
        self.master = master
        self.output = bytearray()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "nz_coder"],
            cwd=workspace,
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)

    def send(self, value: bytes) -> None:
        os.write(self.master, value)

    def wait_for(self, marker: str, *, timeout: float = 10.0, after: int = 0) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = _plain(bytes(self.output[after:]))
            if marker in text:
                return len(self.output)
            if self.process.poll() is not None:
                break
            readable, _, _ = select.select([self.master], [], [], 0.05)
            if not readable:
                continue
            try:
                self.output.extend(os.read(self.master, 65_536))
            except OSError:
                break
        tail = " ".join(_plain(bytes(self.output[-4_000:])).split())
        raise RuntimeError(f"PTY did not show {marker!r}; tail={tail!r}")

    def stop(self) -> str:
        if self.process.poll() is None:
            self.send(b"\x03")
            time.sleep(0.15)
            self.send(b"\x03")
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=3)
        while True:
            readable, _, _ = select.select([self.master], [], [], 0)
            if not readable:
                break
            try:
                self.output.extend(os.read(self.master, 65_536))
            except OSError:
                break
        os.close(self.master)
        return _plain(bytes(self.output))


def _plain(value: bytes) -> str:
    return _ANSI.sub("", value.decode("utf-8", errors="replace")).replace("\r", "")


def _environment(workspace: Path, endpoint: str, *, connected: bool) -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if (
            "API_KEY" in name
            or name.upper() in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
            or name in {"PYTHONHOME", "VIRTUAL_ENV"}
        ):
            environment.pop(name, None)
    environment.update({
        "PYTHONPATH": str(ROOT),
        "HOME": str(workspace / "home"),
        "TERM": "xterm-256color",
        "COLUMNS": "100",
        "LINES": "30",
        "MODEL_PROVIDER": "openai-compatible",
        "MODEL_ID": MODEL_ID,
        "API_KEY": "product-smoke-key" if connected else "",
        "API_BASE_URL": endpoint if connected else "https://api.openai.com/v1",
        "PERMISSION_MODE": "acceptEdits",
        "NZ_PROVIDER_MAX_RETRIES": "0",
        "NZ_PROVIDER_HARD_TIMEOUT_SECONDS": "10",
        "NZ_PROVIDER_STREAM_IDLE_TIMEOUT_SECONDS": "5",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "MEMORY_AUTO_EXTRACT": "0",
        "MEMORY_AUTO_DREAM": "0",
    })
    (workspace / "home").mkdir()
    return environment


def _first_provider() -> dict:
    with tempfile.TemporaryDirectory(prefix="nz-product-provider-") as directory:
        workspace = Path(directory)
        with _provider_server() as server:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            terminal = _PTYProduct(
                workspace,
                _environment(workspace, endpoint, connected=False),
            )
            try:
                cursor = terminal.wait_for("Credential missing for provider", timeout=8)
                terminal.send(b"/connect\r")
                cursor = terminal.wait_for("Connect provider", after=cursor)
                terminal.send(b"\r")
                cursor = terminal.wait_for("API_KEY:", after=cursor)
                terminal.send(b"product-smoke-key\r")
                cursor = terminal.wait_for("BASE_URL:", after=cursor)
                terminal.send(b"\x05\x15" + endpoint.encode("utf-8") + b"\r")
                cursor = terminal.wait_for("Models", after=cursor)
                terminal.send(b"\r")
                terminal.wait_for(
                    f"Switched model to openai-compatible/{MODEL_ID}",
                    after=cursor,
                )
            finally:
                output = terminal.stop()

        env_path = workspace / ".env"
        selection_path = workspace / ".nz-coder" / "models" / "selection.json"
        if not env_path.is_file() or env_path.stat().st_mode & 0o777 != 0o600:
            raise RuntimeError("provider setup did not create a private workspace .env")
        env_text = env_path.read_text(encoding="utf-8")
        if "API_KEY=product-smoke-key" not in env_text or endpoint not in env_text:
            raise RuntimeError("provider setup did not persist the selected connection")
        selection = json.loads(selection_path.read_text(encoding="utf-8"))
        if selection.get("model_id") != MODEL_ID:
            raise RuntimeError("provider setup did not activate the discovered model")
        if "Traceback" in output:
            raise RuntimeError("provider setup exposed a traceback")
        return {"provider_setup": "passed", "models_discovered": 1}


def _interactive_coding() -> dict:
    with tempfile.TemporaryDirectory(prefix="nz-product-coding-") as directory:
        workspace = Path(directory)
        with _provider_server() as server:
            endpoint = f"http://127.0.0.1:{server.server_port}/v1"
            terminal = _PTYProduct(
                workspace,
                _environment(workspace, endpoint, connected=True),
            )
            try:
                cursor = terminal.wait_for("NZ-Coder · IDLE", timeout=8)
                terminal.send(
                    b"Create product-smoke.txt with the requested product smoke sentence.\r"
                )
                terminal.wait_for("Product smoke complete.", timeout=15, after=cursor)
            finally:
                output = terminal.stop()
            requests = list(server.requests)  # type: ignore[attr-defined]

        target = workspace / FILE_NAME
        if target.read_text(encoding="utf-8") != FILE_CONTENT:
            raise RuntimeError("interactive Agent journey did not create the expected file")
        if len(requests) < 2:
            raise RuntimeError("interactive Agent journey did not complete model-tool-model")
        if not any(
            any(message.get("role") == "tool" for message in request.get("messages", []))
            for request in requests
        ):
            raise RuntimeError("interactive Agent journey never returned tool evidence")
        if "Traceback" in output:
            raise RuntimeError("interactive Agent journey exposed a traceback")
        return {
            "interactive_coding": "passed",
            "provider_requests": len(requests),
            "file_written": FILE_NAME,
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("journey", choices=("first-provider", "interactive-coding"))
    args = parser.parse_args(argv)
    if os.name != "posix":
        raise RuntimeError("real PTY product journeys currently require POSIX")
    result = _first_provider() if args.journey == "first-provider" else _interactive_coding()
    print("NZ_PRODUCT_METRICS " + json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
