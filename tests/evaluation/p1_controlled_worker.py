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

    # Observe the real verifier result, including fail-open trace, without
    # replacing its Gateway/Provider/SDK execution or its completion policy.
    from nz_coder.runtime.verification import sidecar_verifier
    original_verifier = sidecar_verifier.invoke_sidecar_verifier
    def observe_verifier(**kwargs):
        verdict = original_verifier(**kwargs)
        observed["verifier"] = dict(verdict=verdict.verdict, trace=verdict.trace)
        return verdict
    sidecar_verifier.invoke_sidecar_verifier = observe_verifier

    def response(request):
        body = json.loads(request.content)
        observed["wire_requests"].append({key: body.get(key) for key in ("model", "max_tokens", "thinking", "reasoning_effort")})
        observed["tool_names"] = [tool["function"]["name"] for tool in body.get("tools", [])]
        if descriptor.get("scenario") == "sidecar_unknown":
            return sidecar_scenario(body, len(observed["wire_requests"]))
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


def sidecar_scenario(body, number):
    """Only tmp fixture files are edited by actual tools. Never a model score."""
    payload = dict(id=f"offline-{number}", object="chat.completion", created=1, model=body["model"],
        usage=dict(prompt_tokens=100, prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60,
                   completion_tokens=20, total_tokens=120))
    if body.get("tool_choice") == {"type": "function", "function": {"name": "emit_sidecar_verdict"}}:
        payload["choices"] = []
        payload.pop("usage")
        return httpx.Response(200, json=payload)
    calls = [
        ("read_file", dict(path="textkit/parser.py")),
        ("edit_file", dict(path="textkit/parser.py",
            old_text='    return [item.strip() for item in text.split(",")]',
            new_text='    if not text.strip():\n        return []\n    return [item.strip() for item in text.split(",")]')),
        ("write_file", dict(path="tests/test_offline_probe.py", content=
            '"""Offline wiring fixture, not a model-generated score."""\n'
            'from textkit.parser import parse_items\n\n'
            'def test_blank():\n    assert parse_items(" ") == []\n')),
        ("bash", dict(command="python -m pytest -q tests")),
    ]
    message = dict(role="assistant", content="Offline wiring fixture completed; public tests passed.")
    finish = "stop"
    if number <= len(calls):
        name, arguments = calls[number - 1]
        message.update(content="", tool_calls=[dict(id=f"offline-tool-{number}", type="function",
            function=dict(name=name, arguments=json.dumps(arguments)))])
        finish = "tool_calls"
    payload["choices"] = [dict(index=0, finish_reason=finish, message=message)]
    return httpx.Response(200, json=payload)


if __name__ == "__main__":
    raise SystemExit(main())
