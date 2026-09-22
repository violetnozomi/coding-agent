"""One-shot frozen verifier experiment; builder is offline, send requires explicit CLI."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = "ac47114affa6556ab8b56e1bb9e1a02171514fc0"
HISTORY = ROOT / "docs/evidence/diagnostic-v1-C-real-2026-09-22/C/nzcoder"
SUITE = ROOT / "tests/evaluation/fixtures/agent_core_diagnostic_v1"
sys.path.insert(0, str(ROOT))


def read(path):
    return json.loads(path.read_text())


def sha(value):
    data = value.encode() if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def save(name, value):
    target = OUT / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def lines(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def old_sections(text):
    """Exact pre-ac47114 section semantics, scoped only to offline OLD build."""
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", text)]
    if not starts:
        return [text] if text else []
    return [text[start:(starts[i + 1] if i + 1 < len(starts) else len(text))]
            for i, start in enumerate(starts)]


def source_hashes():
    names = subprocess.check_output(["git", "ls-files", "nz_coder"], cwd=ROOT, text=True).splitlines()
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in names}


def baseline():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert head == BASE
    assert not subprocess.check_output(["git", "diff", BASE, "--", "nz_coder"], cwd=ROOT)
    return head


def build():
    from nz_coder.runtime.verification import sidecar_verifier as verifier
    from nz_coder.runtime.verification.hooks import StopHookContext
    from nz_coder.runtime.verification.reference_evidence import reference_digest
    from nz_coder.state.changes import render_change_diff

    baseline()
    assert not (OUT / "provider/ONE_REQUEST_STARTED.json").exists(), "Experiment already consumed"
    save("preflight/authorization.json", {
        "authorized": True, "authorization_source": "current user instruction",
        "scope": "exactly one real semantic verifier Provider request",
        "main_model_requests_allowed": 0, "semantic_verifier_requests_allowed": 1,
        "planner_requests_allowed": 0, "infcodex_requests_allowed": 0,
        "embedding_requests_allowed": 0, "automatic_retry_allowed": False,
    })
    save("preflight/baseline.json", {"head": BASE, "origin_main": subprocess.check_output(
        ["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip(),
        "initial_worktree_clean": True, "production_diff": False})
    save("preflight/source-hashes.json", source_hashes())
    request = lines(HISTORY / "provider-requests.jsonl")[-1]
    assert request["request_id"] == 26 and request["purpose"] == "verifier"
    historical = read(HISTORY / "semantic-review.json")[1]
    payload = request["payload"]
    assert payload["messages"] == historical["visible_input"]
    snapshot = next(x for x in lines(HISTORY / "state-snapshots.jsonl")
                    if x["seq"] == int(request["runtime_snapshot"]))
    state = snapshot["state"]
    result = read(HISTORY / "result.json")
    transcript, final_text = result["messages"], result["final_text"]
    initial = SUITE / "C_long_horizon/workspace"
    final = HISTORY / "final-files"
    changes = []
    for name in snapshot["run_evidence"]["actual_output_paths"]:
        before, after = initial / name, final / name
        changes.append({"path": name, "before_exists": before.exists(),
                        "before": before.read_text() if before.exists() else "",
                        "after_exists": after.exists(), "after": after.read_text() if after.exists() else ""})
    diff = render_change_diff({"run_id": snapshot["run_id"], "workspace": snapshot["workspace"], "changes": changes})
    # This is the native review producer, not the evaluator's sorted workspace.diff.
    save("packets/native-diff-source.json", {"text": diff, "sha256": sha(diff),
        "provenance": "render_change_diff over frozen initial/final bytes in historical first-write order"})
    tracker = SimpleNamespace(current_changed_paths=lambda: state["changed_files"],
                              current_deleted_paths=lambda: [], render_current_diff=lambda: diff)
    hook = verifier.SidecarVerifierHook(SimpleNamespace(change_tracker=tracker, workdir=final), None, env={})
    stop = StopHookContext(transcript=tuple(transcript), last_assistant_text=final_text, runtime_state=state)
    with patch.object(verifier, "_diff_sections", old_sections):
        old, _, _ = hook._evidence(stop)
    assert verifier.build_verifier_user_message(old) == payload["messages"][1]["content"], "OLD not byte-equivalent"
    current, _, _ = hook._evidence(stop)
    # Isolate the user's sole allowed variable. _diff_sections ALSO affects
    # compatibility-delta criteria, which this protocol explicitly freezes.
    fixed = dataclasses.replace(old, file_edit_summary=current.file_edit_summary)
    fixed_payload = copy.deepcopy(payload)
    fixed_payload["messages"][1]["content"] = verifier.build_verifier_user_message(fixed)
    assert payload["messages"][0] == {"role": "system", "content": verifier.VERIFIER_SYSTEM_PROMPT}
    assert payload["tools"] == [{"type": "function", "function": verifier.VERIFIER_REPORT_TOOL}]
    assert payload["model"] == "deepseek-v4-flash" and payload["max_tokens"] == 1024
    assert payload["thinking"] == {"type": "disabled"} and payload["stream"] is False
    for name, packet, context in [("old", payload, old), ("fixed", fixed_payload, fixed)]:
        save(f"packets/packet-{name}.json", {"request": packet, "context": dataclasses.asdict(context)})
    save("packets/historical-verifier2-summary.json", {
        "request_id": 26, "purpose": "verifier", "response": historical["response"],
        "runtime_state": state, "transcript": transcript, "final_assistant_text": final_text,
    })
    non_diff_fields = [field.name for field in dataclasses.fields(old) if field.name != "file_edit_summary"]
    assert all(getattr(old, name) == getattr(fixed, name) for name in non_diff_fields)
    left = payload["messages"][1]["content"]
    right = fixed_payload["messages"][1]["content"]
    marker = "=== FILE EDITS PERFORMED THIS TURN ==="
    end = "=== MAIN AGENT FINAL TEXT (the answer the agent is delivering) ==="
    assert left.split(marker)[0] == right.split(marker)[0]
    assert left.split(end)[1] == right.split(end)[1]
    identities = {
        "system_prompt": payload["messages"][0]["content"],
        "user_task": (SUITE / "C_long_horizon/task.md").read_text(),
        "authority": state["task_reference_evidence"],
        "reference_digest": reference_digest(old.authoritative_references, old.omitted_reference_count),
        "transcript": transcript, "final_assistant_text": final_text,
        "additional_criteria": old.additional_criteria, "runtime_contract": state["task_contract"],
        "runtime_ledger": state["requirement_ledger"], "verification": state["verification_contract"],
        "tool_schema": payload["tools"], "provider_model_config": {k: v for k, v in payload.items() if k != "messages"},
        "diff_source": diff,
    }
    save("packets/packet-delta.json", {
        "old_matches_historical_verifier2_bytes": True,
        "unchanged": {name: {"old_sha256": sha(value), "fixed_sha256": sha(value), "equal": True}
                      for name, value in identities.items()},
        "allowed_change": "file_edit_summary and rendered FILE EDITS section only",
        "old_user_sha256": sha(left), "fixed_user_sha256": sha(right),
        "all_other_rendered_sections_equal": True,
        "current_hook_additional_criteria_would_change": old.additional_criteria != current.additional_criteria,
        "control": "Historical additional criteria deliberately retained to isolate file-edit projection; not a full current-hook replay",
    })
    writer = "configkit/config/writer.py"
    hint = dict(fixed.file_edit_summary)[writer]
    assert "def to_dict(config):" in hint and "## configkit/config/writer.py" in hint
    assert "def validate(data):" not in hint and "diff evidence omitted" not in hint
    writer_rendered = right.split(f"- {writer}: ", 1)[1].split("\n- ", 1)[0]
    assert "def to_dict(config):" in writer_rendered
    save("analysis/writer-evidence.json", {
        "path": writer, "hint": hint, "hint_length": len(hint), "hint_sha256": sha(hint),
        "position_in_user_message": right.index(f"- {writer}:"),
        "truncated": "truncated]" in writer_rendered, "omitted": False, "independently_attributed": True,
    })
    comparison = {}
    for name, text in [("old", left), ("fixed", right)]:
        section = text.split(marker, 1)[1].split(end, 1)[0]
        # Exact duplicate-line byte metric, not an estimate of redundant semantic tokens.
        rows = section.splitlines(keepends=True)
        duplicate_chars = sum(len(row) for row in rows) - sum(len(row) for row in set(rows))
        comparison[name] = {"sections": len(old.file_edit_summary), "total_chars": len(section),
            "duplicated_chars": duplicate_chars, "duplication_metric": "repeated identical lines after first occurrence, within rendered FILE EDITS section",
            "writer_own_section": "def to_dict(config):" in section.split(f"- {writer}: ", 1)[1].split("\n- ", 1)[0],
            "writer_omitted_marker": "diff evidence omitted" in section.split(f"- {writer}: ", 1)[1].split("\n- ", 1)[0],
            "omitted_markers": section.count("diff evidence omitted"),
            "budget": {"max_total_hint_chars": verifier.VERIFIER_DIFF_MAX_TOTAL, "max_each_hint_chars": verifier.VERIFIER_DIFF_MAX_EACH}}
    save("analysis/diff-evidence-comparison.json", comparison)
    spec = (initial / "CONFIG_SPEC.md").read_text()
    assert json.dumps(spec, ensure_ascii=False) in right
    save("analysis/authority-visibility.json", {"full_original_spec": True,
        "hash": sha(spec), "port_excludes_bool": "be an integer 1..65535, excluding bool" in spec,
        "no_overwrite": "No validation error may overwrite the input or an existing destination." in spec,
        "extra_highlighting_or_checklist": False})
    for name, text in [("system", payload["messages"][0]["content"]), ("user", right)]:
        (OUT / f"packets/request-visible-{name}.txt").write_text(text)
    save("preflight/provider-config.json", {"provider": "openai-compatible", "base_url": "https://api.deepseek.com",
        **{k: v for k, v in payload.items() if k not in ("messages", "tools")},
        "physical_cap": 1, "transport_retries": 0, "sdk_used": False, "redirects": False,
        "transport": "same historical httpx direct POST; no retrying SDK/Gateway", "timeout_seconds": 180, "connect_timeout_seconds": 30})

    spec_loader = importlib.util.spec_from_file_location("validator", SUITE / "validate.py")
    validator = importlib.util.module_from_spec(spec_loader)
    spec_loader.loader.exec_module(validator)
    with tempfile.TemporaryDirectory(prefix="c-frozen-review-") as td:
        workspace = Path(td) / "workspace"
        hashes = validator.snapshot(final, workspace)
        assert hashes == read(HISTORY / "final-hashes.json")
        public = validator.project_tests("C_long_horizon", workspace)
        accepted = validator.acceptance("C_long_horizon", workspace)
        assert public["exit"] == 0 and "46 passed" in public["stdout"]
        assert accepted["passed"] == 11 and accepted["total"] == 12
        assert [x["name"] for x in accepted["checks"] if not x["passed"]] == ["invalid_write_preserves_destination"]
        assert validator.hashes(workspace) == hashes
        save("preflight/frozen-workspace-hashes.json", hashes)
        save("preflight/project-tests.json", public)
        save("preflight/acceptance.json", accepted)
    save("preflight/READY.json", {"request_sha256": sha(fixed_payload), "source_hashes_sha256": sha(source_hashes()),
         "builder_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "all_checks_passed": True})
    print("Preflight PASS: OLD byte-identical; only file-edit section changed; writer independent; 46 tests / 11 of 12. No Provider request.")


def public_response(value):
    if isinstance(value, dict):
        return {key: public_response(item) for key, item in value.items()
                if key not in {"reasoning_content", "reasoning", "private_reasoning", "provider_extra"}}
    if isinstance(value, list):
        return [public_response(item) for item in value]
    return value


def send_once():
    import httpx
    from nz_coder.runtime.verification.sidecar_verifier import parse_verifier_report

    baseline()
    ready = read(OUT / "preflight/READY.json")
    payload = read(OUT / "packets/packet-fixed.json")["request"]
    assert sha(payload) == ready["request_sha256"]
    assert sha(source_hashes()) == ready["source_hashes_sha256"]
    assert hashlib.sha256(Path(__file__).read_bytes()).hexdigest() == ready["builder_sha256"]
    assert read(OUT / "preflight/authorization.json")["authorized"]
    values = {}
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    base = values.get("API_BASE_URL", "https://api.deepseek.com").rstrip("/")
    assert base == "https://api.deepseek.com"
    key = values["API_KEY"]
    assert key
    (OUT / "provider").mkdir(exist_ok=True)
    # Exclusive durable marker burns authorization BEFORE any network I/O.
    with (OUT / "provider/ONE_REQUEST_STARTED.json").open("x") as stream:
        json.dump({"time": time.time(), "cap": 1, "purpose": "semantic-verifier", "retry_allowed": False}, stream)
    save("provider/request.json", payload)
    attempts = []

    def count(request):
        if attempts:
            raise RuntimeError("Physical request cap exhausted")
        assert request.method == "POST" and str(request.url) == base + "/v1/chat/completions"
        attempts.append({"ordinal": 1, "method": "POST", "purpose": "semantic-verifier", "time": time.time()})
        save("provider/attempts.json", attempts)

    started = time.monotonic()
    outcome = {"http_status": None, "parsed_verdict": None, "error_type": None}
    usage = None
    try:
        with httpx.Client(transport=httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=httpx.Timeout(180, connect=30),
                          event_hooks={"request": [count]}) as client:
            response = client.post(base + "/v1/chat/completions", json=payload,
                                   headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        outcome["http_status"] = response.status_code
        outcome["provider_request_id"] = response.headers.get("x-request-id")
        raw = response.json()
        safe = public_response(raw)
        assert key not in json.dumps(safe), "provider echoed credential"
        outcome["response"] = safe
        response.raise_for_status()
        choice = raw["choices"][0]
        outcome["finish_reason"] = choice.get("finish_reason")
        calls = choice["message"].get("tool_calls") or []
        assert len(calls) == 1 and calls[0]["function"]["name"] == "emit_sidecar_verdict"
        args = json.loads(calls[0]["function"]["arguments"])
        assert isinstance(args, dict) and set(args) <= {"verdict", "reason", "suggestedFix"}
        assert args.get("verdict") in {"accept", "revise", "blocked"} and isinstance(args.get("reason"), str)
        assert all(isinstance(v, str) for v in args.values())
        verdict = parse_verifier_report({"input": args}, exact=True)
        assert verdict.trace == "verifier_ok"
        outcome["parsed_verdict"] = dataclasses.asdict(verdict)
        usage = safe.get("usage")
    except Exception as exc:
        outcome["error_type"] = type(exc).__name__
        # No retry, no SDK fallback and no error body/credential logging.
    outcome["duration_seconds"] = time.monotonic() - started
    outcome["physical_requests"] = len(attempts)
    outcome["retries"] = 0
    save("provider/response.json", outcome)
    save("provider/usage.json", {"provider": "openai-compatible", "model": payload["model"],
        "raw_usage": usage, "duration_seconds": outcome["duration_seconds"],
        "attempts": len(attempts), "retries": 0, "cost": None, "cost_source": "not exposed by Provider"})
    print(json.dumps({"physical_requests": len(attempts), "http_status": outcome["http_status"],
                     "verdict": outcome["parsed_verdict"], "error_type": outcome["error_type"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["build", "send-once"])
    if parser.parse_args().action == "build":
        build()
    else:
        send_once()
