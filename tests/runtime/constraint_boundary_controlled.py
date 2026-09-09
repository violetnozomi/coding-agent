"""Offline process: actual CLI/Native/tools/sidecar, scripted external HTTP only."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import sys

import httpx


def main():
    root = Path(sys.argv[1])
    scenario = sys.argv[2]
    observed = dict(network_attempts=0, main=[], sidecar=[], native_builds=0)

    def deny_network(*args, **kwargs):
        observed["network_attempts"] += 1
        raise AssertionError("Unscripted network forbidden")

    socket.socket.connect = socket.socket.connect_ex = socket.create_connection = socket.getaddrinfo = deny_network
    from evaluation.linux_baseline.billing import Ledger, journal
    from evaluation.linux_baseline.live import authorized_policy
    from evaluation.linux_baseline.live_worker import bounded_product
    from evaluation.linux_baseline.runner import write_json
    from tests.evaluation.test_p1_live import authorization
    from tests.runtime.test_constraint_boundary_selftest import CANDIDATE, REQUIREMENT
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.interface.cli import main as product_main
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.execution.loop import AgentLoop

    def no_legacy(*args, **kwargs):
        raise AssertionError("Legacy loop must not run")

    AgentLoop.__init__ = no_legacy
    original = native_sdk.build_product_run_environment

    def native(*args, **kwargs):
        result = original(*args, **kwargs)
        observed["native_builds"] += 1
        return result

    native_sdk.build_product_run_environment = native
    policy = authorized_policy(authorization())
    ledger = Ledger(policy, journal(root / "billing.jsonl"))
    calls = [
        ("read_file", dict(path="distinct.py")),
        ("update_scratchpad", dict(category="plan", content=json.dumps({"boundary_check": CANDIDATE}))),
        ("write_file", dict(path="tests/test_distinct.py", content=
            'from distinct import distinct\n'
            'def test_single_consumption():\n    assert distinct(iter([2, 1, 2])) == [2, 1]\n')),
        ("bash", dict(command=CANDIDATE["command"])),
        None,
        ("edit_file", dict(path="distinct.py", old_text="    list(items)\n", new_text="")),
        ("bash", dict(command=CANDIDATE["command"])),
        None,
    ]
    if scenario == "denied":
        calls = [calls[1], calls[2], ("bash", dict(command="touch forbidden-execution")), None]

    def response(request):
        body = json.loads(request.content)
        sidecar = body.get("tool_choice") == {"type": "function", "function": {"name": "emit_sidecar_verdict"}}
        observed["sidecar" if sidecar else "main"].append(body)
        if sidecar:
            name, args = "emit_sidecar_verdict", dict(verdict="accept", reason="Controlled wiring verdict only")
            observed["sidecar_mode"] = {key: body.get(key) for key in ("thinking", "reasoning_effort", "max_tokens", "tool_choice")}
        else:
            number = len(observed["main"])
            if number > len(calls):
                raise AssertionError("Unbounded main continuation")
            call = calls[number - 1]
            name, args = call if call else (None, {})
        message = dict(role="assistant", content="Candidate checks completed.")
        if name:
            message.update(content="", tool_calls=[dict(id=f"tool-{len(observed['main'])}-{int(sidecar)}",
                type="function", function=dict(name=name, arguments=json.dumps(args)))])
        payload = dict(id=f"offline-{len(observed['main'])}-{int(sidecar)}", object="chat.completion", created=1,
            model=body["model"], choices=[dict(index=0, message=message, finish_reason="tool_calls" if name else "stop")],
            usage=dict(prompt_tokens=100, prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60,
                       completion_tokens=20, total_tokens=120))
        if scenario == "unknown" and len(observed["main"]) == 5:
            payload.pop("usage")
        return httpx.Response(200, json=payload)

    repo = root / "repo"
    snapshot = load_config_snapshot(repo)
    WorkspaceTrustStore().trust(repo, "workspace-control", snapshot.control_fingerprint)
    try:
        with bounded_product(policy, ledger, "offline-boundary-key", inner_factory=lambda: httpx.MockTransport(response)):
            return product_main(["run", "--cwd", str(repo), "--provider", "openai-compatible",
                "--model", policy.model, "--permission-mode", "plan" if scenario == "denied" else "auto",
                "--session", "boundary-offline", "--max-turns", "15", "--output", "jsonl", "--prompt",
                REQUIREMENT + (" Do not modify tests or execute commands." if scenario == "denied" else "")])
    finally:
        write_json(root / "observed.json", observed)
        write_json(root / "billing-summary.json", ledger.snapshot())


if __name__ == "__main__":
    raise SystemExit(main())
