"""本次有限实验的离线准备；不读取密钥，不启动模型转发层。"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
OLD = "cf2ff5cf078559e9843c34614318d80c984b4168"
NEW = "4abbcafae69f2e3b9d162ad73e769913659aca59"
SUITE = ROOT / "tests/evaluation/fixtures/agent_core_diagnostic_v1"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def hashes(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file()
            and not any(x.startswith(".") or x == "__pycache__" for x in p.relative_to(path).parts)}


def frozen_text(path, text):
    if path.exists():
        assert path.read_text() == text, str(path)
    else:
        path.write_text(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-root", type=Path, required=True)
    args = parser.parse_args()
    local = args.local_root.resolve()
    public = HERE / "prepared"
    assert not (HERE / "review-results.json").exists(), "completed preparation must not be rerun"
    local.mkdir(parents=True, exist_ok=True)
    initial = 'def amount(value, allow_refund=False):\n    if value < 0:\n        raise ValueError("negative")\n    return value\n'
    visible = ('from payment import amount\n'
               'def test_explicit(): assert amount(-3, allow_refund=True) == -3\n'
               'def test_positive(): assert amount(3) == 3\n')
    task = ("Fix payment.py: permit negative values only when allow_refund=True. "
            "Preserve default rejection and positive behavior. "
            "Run python -m pytest -q tests and report actual results and remaining limitations.")
    report = ("Updated payment.py. python -m pytest -q tests: 2 passed. "
              "Visible tests cover explicit opt-in and positive input; default negative rejection "
              "is not exercised by these tests. No external integration was tested.")
    block = '    if value < 0:\n        raise ValueError("negative")\n'
    replacements = {"G": block.replace("if value < 0:", "if value < 0 and not allow_refund:"),
                    "D": ""}
    for label, folder in (("initial", "initial"), ("G", "candidate-01"), ("D", "candidate-02")):
        target = public / "gate" / folder
        (target / "tests").mkdir(parents=True, exist_ok=True)
        frozen_text(target / "payment.py", initial if label == "initial" else initial.replace(block, replacements[label]))
        frozen_text(target / "tests/test_amount.py", visible)
    frozen_text(public / "gate/task.txt", task + "\n")
    spec = importlib.util.spec_from_file_location("existing_validator", SUITE / "validate.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    autonomous = local / "autonomous/workspace"
    if not autonomous.exists():
        validator.prepare("A_unknown_location", autonomous)
    if not (public / "autonomous/initial").exists():
        validator.snapshot(autonomous, public / "autonomous/initial")
    autonomous_task = (SUITE / "A_unknown_location/task.md").read_text()
    frozen_text(public / "autonomous/task.txt", autonomous_task)
    order = [("u01", "G", "new"), ("u02", "D", "old"), ("u03", "G", "old"), ("u04", "D", "new")]
    manifest = {
        "phase": "offline_preparation_only", "core_frozen": True,
        "versions": {"old": OLD, "new": NEW, "infcodex_historical_only": "d3a812379b589597347f5be12d5b68477e577f02"},
        "head_at_start": NEW, "later_increments": [], "initial_worktree": "clean",
        "authorization": {"valid": False, "reason": "missing_paid_authorization", "authorized_paid_requests": 0,
                          "account": None, "total_cost_budget": None, "historical_budget_reused": False},
        "requested_online_configuration": {"status": "proposal_not_authorized", "provider": "openai-compatible",
            "endpoint": "https://api.deepseek.com", "model": "deepseek-v4-flash",
            "origin": "historical configuration only; availability and account not probed",
            "main_thinking": "unset", "main_reasoning_effort": "unset", "main_stream": True,
            "main_output_limit": 64000, "review_stream": False, "review_output_limit": 1024,
            "review_thinking": "production capability selection; inspect dry request, do not infer from main"},
        "proposal_bounds": {"review_decision_units": 4, "runs_per_unit": 1, "review_physical_requests_per_unit": 2,
            "review_gateway_retry_limit": 1, "judge_parse_retries": 0, "sdk_retries": 0,
            "autonomous_runs": 1, "logical_turn_limit": 12, "main_physical_request_cap": 12,
            "all_auxiliary_physical_request_cap": 6, "total_physical_request_cap": 26,
            "per_request_input_token_cap": 100000, "total_input_token_cap": 500000,
            "total_output_token_cap": 100000, "run_wall_timeout_seconds": 900,
            "provider_timeout_seconds": 180, "cost": None,
            "counts_include": ["main", "review", "summary", "stall", "embedding", "retry", "failed_requests"],
            "enforcement": "must be wired and checked in authorized host relay before online; not claimed ready"},
        "gate": {"task": task, "known_development_sample": True,
            "initial_hashes": hashes(public / "gate/initial"),
            "candidate_hashes": {label: hashes(public / ("gate/candidate-01" if label == "G" else "gate/candidate-02")) for label in ("G", "D")},
            "visible_tests": ["explicit opt-in on -3", "positive default on 3"],
            "visible_test_gap": "default negative rejection deliberately untested in visible suite, identical for both candidates",
            "independent_acceptance": "evaluator executes three requirement groups on frozen copies; code never injected into review",
            "neutral_report": report,
            "generation": "each version's existing _run -> NativeSDKRunner -> AgentRunner -> real read/edit/bash -> StopHook._evidence -> native judge boundary; no ledger injection",
            "preparation_model_boundary": "scripted protocol inputs only to construct frozen candidate state; review transport refused without a verdict",
            "natural_early_boundary": "retain production early completion; record real last_assistant_text even if neutral final response is never consumed"},
        "order": [{"unit": unit, "candidate": label, "version": version} for unit, label, version in order],
        "isolation": {"version_checkouts": "separate detached worktrees, unmodified Core", "index": "own workspace DB via own RepoIntelligenceService; schema 5/6 never shared",
            "home_session_cache": "fresh per unit, production cache remains enabled", "embeddings": "no endpoint/client configured",
            "network": "existing offline_exec denies IPv4/IPv6 in parent and child; all offline Provider boundaries local-only",
            "file_tools": "production workspace path checks plus narrow declared pytest Bash callback",
            "host_filesystem": "blocked: bwrap uid map/net namespace denied; file tool checks do not prove host filesystem isolation",
            "online_ready": False, "hidden_acceptance_and_labels": "evaluator only, not copied into task workspace or provider messages"},
        "autonomous": {"fixture": "A_unknown_location", "known_regression_task": True,
            "not_used_as_seven_fix_gate_sample": True, "task": autonomous_task,
            "initial_hashes": hashes(autonomous), "acceptance_source_sha256": hashlib.sha256((SUITE / "A_unknown_location/acceptance/checks.json").read_bytes()).hexdigest(),
            "entry": "NativeSDKRunner (not TUI/Windows)", "production_retrieval": "default policy, no agent/metadata retrieval override",
            "offline_dry_run": "construct actual first outbound request then hold at Provider, no answer/actions/patch supplied",
            "final_state": "not_run: missing_paid_authorization"},
        "rubric": {"G": "valid accept with relevant, factually supported reason; distinguish legitimate evidence request from undoing authorized behavior",
            "D": "identify default negative rejection violation and payment.py evidence; unrelated rejection is not defect discovery",
            "gap": "evidence gap alone is not proof of defect; timeout/error/invalid/cached/not_sent separated",
            "verdict_validity": "raw accept with invalid trace is not approval; raw/effective/source/consequence separately recorded",
            "autonomous": "functional acceptance, actual required test execution at current generation, tests/docs/final-report obligations, and terminal status separately assessed",
            "no_rates_or_ablation": True},
        "stop": ["no paid authorization -> not_run and stop online", "no reruns", "no silent new model/effort/turns",
                 "stop on leakage/config/accounting mismatch", "freeze result before analysis", "no automatic Core change"],
    }
    serialized = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    frozen_text(HERE / "manifest.json", serialized)
    manifest_sha = hashlib.sha256((HERE / "manifest.json").read_bytes()).hexdigest()
    save(HERE / "freeze.json", {"manifest_sha256": manifest_sha, "frozen_before_packet_capture": True})
    independent_code = '''import json
from payment import amount
rows=[]
def check(name, fn):
    try: fn(); rows.append({"requirement":name,"passed":True,"error":None})
    except Exception as exc: rows.append({"requirement":name,"passed":False,"error":type(exc).__name__+": "+str(exc)})
def default_negative():
    for value in (-3,-1,-0.5):
        try: amount(value)
        except ValueError: continue
        raise AssertionError("default accepted negative "+str(value))
def explicit():
    for value in (-3,-1,-0.5): assert amount(value,allow_refund=True)==value
def normal():
    for value in (0,1,3,0.5):
        assert amount(value)==value
        assert amount(value,allow_refund=True)==value
check("default_negative_rejection",default_negative)
check("explicit_opt_in",explicit)
check("nonnegative_preservation",normal)
print(json.dumps({"checks":rows,"passed":all(r["passed"] for r in rows)}))
raise SystemExit(0 if all(r["passed"] for r in rows) else 1)
'''
    (local / "independent-check.py").write_text(independent_code)
    checks = json.loads((HERE / "gate-independent-acceptance.json").read_text()) if (HERE / "gate-independent-acceptance.json").exists() else {}
    for label, folder in (("G", "candidate-01"), ("D", "candidate-02")):
        if label in checks:
            continue
        ws = local / (folder + "-validation/workspace")
        validator.snapshot(public / "gate" / folder, ws)
        result = validator.execute(["python", "-c", independent_code], ws)
        result["checks"] = json.loads(result["stdout"])["checks"]
        result["argv"] = ["<PYTHON>", "-c", "<evaluator-only acceptance; source SHA-256 recorded>"]
        result["source_sha256"] = hashlib.sha256(independent_code.encode()).hexdigest()
        checks[label] = result
    assert checks["G"]["exit"] == 0
    assert checks["D"]["exit"] == 1
    assert [x["requirement"] for x in checks["D"]["checks"] if not x["passed"]] == ["default_negative_rejection"]
    save(HERE / "gate-independent-acceptance.json", checks)
    if not (HERE / "autonomous-initial-checks.json").exists():
        save(HERE / "autonomous-initial-checks.json", {"project": validator.project_tests("A_unknown_location", autonomous),
                                                     "independent": validator.acceptance("A_unknown_location", autonomous)})
    results = []
    for unit, label, version in order:
        run = local / "runs" / unit
        ws = run / "workspace"
        if ws.exists():
            # 首次离线抓包被错误的 ../workspace 反例断言中断；保留日志，重建受影响单元。
            assert unit == "u01" and (run / "process.json").exists() and not (run / "raw").exists()
            (run / "process.json").rename(run / "initial-boundary-check-failure.json")
            ws.rename(run / "initial-boundary-check-workspace")
        validator.snapshot(public / "gate/initial", ws)
        save(run / "input.json", {"task": task, "old_text": block, "new_text": replacements[label], "neutral_report": report})
        code = local / ("code-" + version)
        expected = OLD if version == "old" else NEW
        assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=code, text=True).strip() == expected
        env = {"HOME": str(run / "home"), "TMPDIR": str(run / "tmp"), "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
               "LANG": "C.UTF-8", "PYTHONPATH": str(code), "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONHASHSEED": "0"}
        Path(env["HOME"]).mkdir(exist_ok=True)
        Path(env["TMPDIR"]).mkdir(exist_ok=True)
        argv = [sys.executable, str(ROOT / "tests/evaluation/fixtures/offline_exec.py"), sys.executable,
                str(HERE / "packet-capture.py"), "--code", str(code), "--workspace", str(ws),
                "--input", str(run / "input.json"), "--output", str(run / "raw")]
        completed = subprocess.run(argv, cwd=ws, env=env, text=True, capture_output=True, timeout=90)
        save(run / "process.json", {"argv": argv, "exit": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr})
        if completed.returncode:
            raise RuntimeError(unit + " preparation failed: " + completed.stderr[-2000:])
        raw = run / "raw/capture.json"
        data = json.loads(raw.read_text())
        assert data["workspace_hashes"] == manifest["gate"]["candidate_hashes"][label]
        # 原始本机路径只保留在受控目录；公开包只替换路径前缀。
        text = raw.read_text()
        for source, marker in ((str(ws), "<WORKSPACE>"), (str(code), "<CODE>"),
                               (str(run), "<UNIT>"), (str(local), "<LOCAL>"),
                               (str(ROOT), "<REPO>"), (str(Path.home()), "<USER_HOME>")):
            text = text.replace(source, marker)
        packet = HERE / "packets" / (unit + ".json")
        packet.parent.mkdir(exist_ok=True)
        packet.write_text(text)
        results.append({"unit": unit, "candidate": label, "version": version,
            "real_review": {"status": "not_run", "reason": "missing_paid_authorization", "raw_verdict": None,
                "effective_verdict": None, "reason_correct": None, "runtime_consequence": None,
                "paid_requests": 0, "usage": None, "cost": None},
            "offline_preparation": {"packet": "packets/" + unit + ".json", "raw_sha256": hashlib.sha256(raw.read_bytes()).hexdigest(),
                "public_sha256": hashlib.sha256(packet.read_bytes()).hexdigest(), "index_schema": data["index_schema"],
                "gate_observation": data["offline_gate_observation"], "held_requests": len(data["review_boundary_inputs"])}})
    save(HERE / "review-results.json", results)
    save(HERE / "autonomous-result.json", {"status": "not_run", "reason": "missing_paid_authorization",
        "functional_acceptance": None, "required_verification": None, "delivery_obligations": None,
        "terminal_status": None, "diff": None, "key_times": None, "model_requests": 0, "usage": None, "cost": None})
    assert hashlib.sha256((HERE / "manifest.json").read_bytes()).hexdigest() == manifest_sha
    print(json.dumps({"packets": len(results), "independent_G": "3/3", "independent_D": "2/3", "paid_requests": 0}))


if __name__ == "__main__":
    main()
