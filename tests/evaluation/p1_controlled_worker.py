"""Offline subprocess fixture: real CLI/Native/Provider, only HTTP responses scripted."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import sys

import httpx

from evaluation.linux_baseline.live_worker import run
from evaluation.linux_baseline.runner import write_json


def main():
    path = Path(sys.argv[1])
    descriptor = json.loads(path.read_text())
    observed = dict(network_attempts=0, wire_requests=[], native_builds=0)
    def no_network(*args, **kwargs):
        observed["network_attempts"] += 1
        raise AssertionError("Offline test attempted a network connection")
    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = no_network
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.execution.loop import AgentLoop
    def no_legacy(*args, **kwargs):
        raise AssertionError("Legacy loop must not run")
    AgentLoop.__init__ = no_legacy
    original = native_sdk.build_product_run_environment
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        observed["native_builds"] += 1
        return result
    native_sdk.build_product_run_environment = observe

    def response(request):
        body = json.loads(request.content)
        observed["wire_requests"].append({key: body.get(key) for key in ("model", "max_tokens", "thinking", "reasoning_effort")})
        observed["tool_names"] = [tool["function"]["name"] for tool in body.get("tools", [])]
        if len(observed["wire_requests"]) == 1:
            return httpx.Response(200, json={"id": "offline-one", "object": "chat.completion", "created": 1,
                "model": body["model"], "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                    "role": "assistant", "content": "", "tool_calls": [{"id": "offline-write", "type": "function",
                    "function": {"name": "write_file", "arguments": json.dumps(dict(path="tests/p1_probe.txt", content="OFFLINE ONLY\n"))}}]}}],
                "usage": {"prompt_tokens": 100, "prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 100,
                          "completion_tokens": 20, "total_tokens": 120}})
        # Lose billing on the second response: all later gateway/SDK retries must stop.
        return httpx.Response(200, json={"id": "offline-unknown", "choices": []})
    try:
        return run(descriptor["config"], path.parent / "repo", "p1-offline-only", descriptor["prompt"],
                   path.parent, inner_factory=lambda: httpx.MockTransport(response))
    finally:
        write_json(path.parent / "controlled.json", observed)


if __name__ == "__main__":
    raise SystemExit(main())
