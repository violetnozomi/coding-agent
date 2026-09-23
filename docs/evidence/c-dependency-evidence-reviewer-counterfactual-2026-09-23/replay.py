"""Build/audit/send the one authorized CF-C2 request."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "ce93e9c4f97dd6492e5f6fcdf9d54ecd1b8bc5ec"


def load(path):
    return json.loads(path.read_text())


def digest(value):
    if isinstance(value, str):
        raw = value.encode()
    else:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    target = OUT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build():
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip() == BASE
    assert subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip() == BASE
    assert not subprocess.check_output(["git", "status", "--short"], cwd=ROOT)
    baseline = load(OUT / "baseline/historical-request.json")
    base_request = baseline["request"]
    dependency = load(OUT / "counterfactual/dependency-evidence.json")
    section = dependency["context_field"]
    assert section.startswith("=== RELATED UNCHANGED IMPLEMENTATION EVIDENCE ===")
    assert "Path: configkit/store.py\nSymbol: save\nRelation: direct_caller" in section
    assert "def save(path, config):" in section
    assert "encoded = dumps(config)" in section and "write_text(encoded)" in section
    assert "invalid_write_preserves_destination" not in section and "11/12" not in section
    user = base_request["messages"][1]["content"]
    marker = "=== MAIN AGENT FINAL TEXT (the answer the agent is delivering) ==="
    assert marker in user and "RELATED UNCHANGED IMPLEMENTATION EVIDENCE" not in user
    cf_request = copy.deepcopy(base_request)
    cf_request["messages"][1]["content"] = user.replace("\n" + marker, "\n" + section + "\n" + marker, 1)
    # Everything outside the explicitly inserted section must be byte-equivalent.
    before = user.split(marker, 1)
    after = cf_request["messages"][1]["content"].split(marker, 1)
    assert before[0] == after[0].split(section + "\n", 1)[0]
    assert before[1] == after[1]
    for key in base_request:
        if key != "messages":
            assert cf_request[key] == base_request[key], key
    audit = {
        "baseline_request_sha256": digest(base_request),
        "counterfactual_request_sha256": digest(cf_request),
        "system_message_equal": cf_request["messages"][0] == base_request["messages"][0],
        "model_equal": cf_request["model"] == base_request["model"],
        "tool_schema_equal": cf_request["tools"] == base_request["tools"],
        "thinking_equal": cf_request["thinking"] == base_request["thinking"],
        "max_tokens_equal": cf_request["max_tokens"] == base_request["max_tokens"],
        "transcript_authority_verification_final_answer_criteria_equal": True,
        "file_edit_evidence_equal": True,
        "expected_delta": "supporting_repository_evidence absent -> current production-generated bounded section",
        "dependency_digest": dependency["digest"],
        "dependency_chars": len(section),
        "clean": True,
    }
    assert all(v is True for k, v in audit.items() if k.endswith("equal") or k == "clean")
    save("counterfactual/request.json", cf_request)
    save("counterfactual/request-diff.json", audit)
    (OUT / "counterfactual/packet.txt").write_text(cf_request["messages"][1]["content"])
    save("analysis/single-variable-audit.json", audit)
    save("analysis/packet-audit.json", {
        "authority_clause_present": "No validation error may overwrite the input or an existing destination." in cf_request["messages"][1]["content"],
        "bool_clause_present": "be an integer in 1..65535 (booleans are rejected)" in cf_request["messages"][1]["content"] or "be an integer 1..65535, excluding bool" in cf_request["messages"][1]["content"],
        "writer_diff_present": "def to_dict(config):" in cf_request["messages"][1]["content"],
        "store_save_present": "def save(path, config):" in cf_request["messages"][1]["content"],
        "verification_46_passed_present": "46 passed" in cf_request["messages"][1]["content"],
        "hidden_evaluator_name_absent": "invalid_write_preserves_destination" not in cf_request["messages"][1]["content"],
        "acceptance_ratio_absent": "11/12" not in cf_request["messages"][1]["content"],
        "oracle_absent": "oracle/" not in cf_request["messages"][1]["content"],
    })
    save("baseline/authorization.json", {"authorized": True, "source": "current user instruction", "physical_requests": 1, "main": 0, "planner": 0, "embedding": 0, "infcodex": 0, "retry": False})
    print("BUILD PASS: exact baseline plus one production-generated dependency section; no Provider request")


def public_response(value):
    if isinstance(value, dict):
        return {k: public_response(v) for k, v in value.items() if str(k).lower() not in {"reasoning_content", "private_reasoning", "reasoning"}}
    if isinstance(value, list):
        return [public_response(v) for v in value]
    return value


def send_once():
    import httpx
    from nz_coder.runtime.verification.sidecar_verifier import parse_verifier_report

    build()
    marker = OUT / "result/ONE_REQUEST_STARTED.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    with marker.open("x") as stream:
        stream.write(json.dumps({"cap": 1, "purpose": "semantic-verifier", "retry": False}))
    payload = load(OUT / "counterfactual/request.json")
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    base = values.get("API_BASE_URL", "https://api.deepseek.com").rstrip("/")
    assert base == "https://api.deepseek.com" and values.get("API_KEY")
    attempts = []
    def count(request):
        if attempts:
            raise RuntimeError("physical request cap exhausted")
        assert str(request.url) == base + "/v1/chat/completions"
        attempts.append({"ordinal": 1, "method": "POST", "purpose": "semantic-verifier", "time": time.time()})
        save("result/attempts.json", attempts)
    started = time.monotonic()
    outcome = {"http_status": None, "parsed_verdict": None, "error_type": None}
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False, follow_redirects=False,
                          timeout=httpx.Timeout(180, connect=30), event_hooks={"request": [count]}) as client:
            response = client.post(base + "/v1/chat/completions", json=payload,
                                   headers={"Authorization": "Bearer " + values["API_KEY"], "Content-Type": "application/json"})
        outcome["http_status"] = response.status_code
        outcome["provider_request_id"] = response.headers.get("x-request-id")
        raw = response.json()
        safe = public_response(raw)
        assert values["API_KEY"] not in json.dumps(safe)
        outcome["response"] = safe
        response.raise_for_status()
        choice = raw["choices"][0]
        outcome["finish_reason"] = choice.get("finish_reason")
        calls = choice["message"].get("tool_calls") or []
        assert len(calls) == 1 and calls[0]["function"]["name"] == "emit_sidecar_verdict"
        args = json.loads(calls[0]["function"]["arguments"])
        assert isinstance(args, dict) and set(args) <= {"verdict", "reason", "suggestedFix"}
        assert args.get("verdict") in {"accept", "revise", "blocked"} and isinstance(args.get("reason"), str)
        verdict = parse_verifier_report({"input": args}, exact=True)
        assert verdict.trace == "verifier_ok"
        outcome["raw_tool_call"] = {"name": calls[0]["function"]["name"], "arguments": args}
        outcome["parsed_verdict"] = {"verdict": verdict.verdict, "reason": verdict.reason, "suggested_fix": verdict.suggested_fix, "trace": verdict.trace}
        outcome["usage"] = safe.get("usage")
    except Exception as exc:
        outcome["error_type"] = type(exc).__name__
    outcome["duration_seconds"] = time.monotonic() - started
    outcome["physical_requests"] = len(attempts)
    outcome["retries"] = 0
    save("result/raw-response.json", outcome)
    save("result/provider-accounting.json", {"provider": "openai-compatible", "model": payload["model"], "attempts": len(attempts), "retries": 0, "usage": outcome.get("usage"), "duration_seconds": outcome["duration_seconds"], "cost": None, "cost_source": "not exposed"})
    print(json.dumps({"physical_requests": len(attempts), "http_status": outcome["http_status"], "verdict": outcome["parsed_verdict"], "error_type": outcome["error_type"]}, ensure_ascii=False))


if __name__ == "__main__":
    argparse.ArgumentParser().add_argument("action", choices=["build", "send-once"])
    import sys
    action = sys.argv[1]
    (build if action == "build" else send_once)()
