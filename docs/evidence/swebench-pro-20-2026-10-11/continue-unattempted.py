"""只补做环境准备未完成的固定题目；旧账目先对齐，预算只用剩余额度。"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import time

DOC = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pro_run", DOC / "run-pro.py")
pro = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pro)
OUT = pro.OUT


def remaining_plan(*, preparation_retry=False):
    # 原协调器完全结束才可补做，避免两个独立账本同时占用同一授权。
    if Path("/proc/1368343").exists():
        raise RuntimeError("original_coordinator_still_running")
    original = OUT / "live"
    summary = json.loads((original / "summary.json").read_text())
    raw = (original / "ledger.jsonl").read_bytes()
    events = [json.loads(line) for line in raw.splitlines()]
    audit = json.loads((OUT / "accounting-stop-audit.json").read_text())
    if hashlib.sha256(raw).hexdigest() != audit["raw_ledger_sha256"]:
        raise RuntimeError("previous_ledger_changed_after_stop_audit")
    failures = [e for e in events if e["event"] == "accounting_contract_failed"]
    # 用户在看到 8000/8001 的具体偏差和余量方案后明确要求继续；只续接此已核实停止。
    if len(failures) != 1 or failures[0]["attempt_id"] != audit["attempt_id"]:
        raise RuntimeError("unrecognized_previous_accounting_stop")
    admissions = {e["attempt_id"]: e for e in events if e["event"] == "admitted"}
    settlements = [e for e in events if e["event"] == "settled"]
    if len(settlements) != len(admissions) or {e["attempt_id"] for e in settlements} != set(admissions):
        raise RuntimeError("previous_requests_not_uniquely_settled")
    if any(e.get("reservation_retained") or not e.get("usage_valid")
           or e.get("usage") is None or e.get("estimated_cost_reported_cny") is None for e in settlements):
        raise RuntimeError("previous_usage_not_valid_for_continuation")
    for event in settlements:
        if not event.get("contract_failed"):
            continue
        if (event["attempt_id"] != audit["attempt_id"] or event.get("input_over_reservation") != 0
                or event.get("output_over_reservation") != 1 or event.get("ambiguous")
                or event.get("usage_conflict") or event["usage"]["completion_tokens"] != 8001
                or admissions[event["attempt_id"]]["output_reserved"] != 8000):
            raise RuntimeError("previous_failure_not_the_authorized_output_margin_case")
    used = {"requests": len(admissions), "input_total": sum(e["usage"]["prompt_tokens"] for e in settlements),
            "output_total": sum(e["usage"]["completion_tokens"] for e in settlements),
            "cost_cny": sum(e["estimated_cost_reported_cny"] for e in settlements)}
    if used["requests"] != summary["physical_requests"] or abs(used["cost_cny"] - summary["estimated_cost_occupied_cny"]) > 1e-8:
        raise RuntimeError("previous_summary_ledger_mismatch")
    limits = events[0]["limits"]
    manifest = json.loads((OUT / "task-manifest.json").read_text())
    prior = {e["instance_id"]: e for e in summary["tasks"]}
    candidates = []
    for i, task in enumerate(manifest["tasks"], 1):
        old = prior[task["instance_id"]]
        if old["attempt"] != 0:
            continue
        if any(a["unit"] == task["instance_id"] for a in admissions.values()):
            raise RuntimeError("zero_attempt_report_has_actual_provider_request")
        candidates.append({"number": f"{i:02d}", "task": task, "original_report": old})
    remaining = {k: limits[k] - used[k] for k in ("requests", "input_total", "output_total")}
    remaining["cost_cny"] = events[0]["fee_authorization"]["cost_limit"] - used["cost_cny"]
    if any(v <= 0 for v in remaining.values()):
        raise RuntimeError("previous_global_budget_exhausted")
    plan = {"predecessor_ledger_sha256": hashlib.sha256(raw).hexdigest(), "used": used,
            "remaining": remaining, "original_limits": limits, "tasks": candidates,
            "phase_rule": "one paid attempt per fixed task; no retry of attempted tasks",
            "authorized_superseded_stop": {"attempt_id": audit["attempt_id"], "output_over_reservation": 1,
                "authorization_text": "用户先回复继续进行，继而明确要求：你别设限制行吗？取消本轮人为单次输出限制，保留人民币50元总预算。",
                "old_ledger_unchanged": True, "provider_output_reservation_tokens": 393216, "wire_max_tokens": None}}
    if not preparation_retry:
        return plan
    if Path("/proc/1476085").exists():
        raise RuntimeError("continuation_coordinator_still_running")
    current = OUT / "next"
    completed = json.loads((current / "summary.json").read_text())
    current_raw = (current / "ledger.jsonl").read_bytes()
    current_events = [json.loads(line) for line in current_raw.splitlines()]
    authorization = current_events[0]["fee_authorization"]
    if (authorization["predecessor_ledger_sha256"] != plan["predecessor_ledger_sha256"]
            or abs(authorization["cost_limit"] - remaining["cost_cny"]) > 1e-8):
        raise RuntimeError("continuation_fee_chain_mismatch")
    calls = [e for e in current_events if e["event"] == "admitted"]
    settled = [e for e in current_events if e["event"] == "settled"]
    if (any(e["event"] == "accounting_contract_failed" for e in current_events)
            or len(calls) != len({e["attempt_id"] for e in calls})
            or len(settled) != len(calls)
            or {e["attempt_id"] for e in settled} != {e["attempt_id"] for e in calls}
            or any(e.get("reservation_retained") or not e.get("usage_valid") or e.get("usage") is None
                   or e.get("estimated_cost_reported_cny") is None for e in settled)):
        raise RuntimeError("continuation_accounting_not_closed")
    current_cost = sum(e["estimated_cost_reported_cny"] for e in settled)
    if (completed["physical_requests"] != len(calls)
            or abs(current_cost - completed["estimated_cost_occupied_cny"]) > 1e-8):
        raise RuntimeError("continuation_summary_ledger_mismatch")
    outcomes = {e["instance_id"]: e for e in completed["tasks"]}
    if len(outcomes) != len(candidates) or set(outcomes) != {c["task"]["instance_id"] for c in candidates}:
        raise RuntimeError("continuation_task_reports_incomplete")
    unstarted = []
    for candidate in candidates:
        identifier = candidate["task"]["instance_id"]
        if outcomes[identifier]["attempt"] != 0:
            continue
        if any(e["unit"] == identifier for e in calls):
            raise RuntimeError("preparation_retry_has_actual_model_request")
        unstarted.append({**candidate, "previous_preparation_record": str(current / candidate["number"])})
    # 准备失败不是付费重试；关闭前阶段账目后，仅首次运行尚未尝试的题。
    used["requests"] += len(calls)
    used["input_total"] += sum(e["usage"]["prompt_tokens"] for e in settled)
    used["output_total"] += sum(e["usage"]["completion_tokens"] for e in settled)
    used["cost_cny"] += current_cost
    remaining["requests"] = limits["requests"] - used["requests"]
    remaining["cost_cny"] = 50 - used["cost_cny"]
    plan["superseded_token_quota_remaining"] = {k: limits[k] - used[k] for k in ("input_total", "output_total")}
    for key in ("input_total", "output_total"):
        remaining.pop(key)
    if remaining["requests"] <= 0 or remaining["cost_cny"] <= 0:
        raise RuntimeError("remaining_aggregate_budget_exhausted")
    plan.update(tasks=unstarted, source_fee_ledger_chain_sha256=[plan["predecessor_ledger_sha256"],
                hashlib.sha256(current_raw).hexdigest()], predecessor_ledger_sha256=hashlib.sha256(current_raw).hexdigest())
    return plan


def isolated_image(row, slot):
    image = row["configuration"]["environment"]["docker_image"]
    if pro.command(["docker", "image", "inspect", image], check=False).returncode:
        pro.registry_preflight(image, slot)
        # 只延长大镜像的准备阶段；Agent 的请求数和 3000 秒上限不变。
        pro.command(["docker", "pull", image], log=slot / "image-pull.txt", timeout=1800)
    info = json.loads(pro.command(["docker", "image", "inspect", image]).stdout)[0]
    base = info["RepoDigests"][0]
    version = pro.command(["docker", "run", "--rm", "--network=none", "--entrypoint", "python3", base, "--version"]).stdout.strip().split()[1]
    if tuple(map(int, version.split(".")[:2])) >= (3, 10):
        base, tag = pro.runtime_image(row, slot)
        return base, tag, "python3"
    build = OUT / "runtime/isolated-agent-313"
    config = json.loads((OUT / "isolated-python-build-config.json").read_text())
    recipe = (build / "Dockerfile").read_text().splitlines()
    recipe[0] = f"FROM {base}"
    (build / "Dockerfile").write_text("\n".join(recipe) + "\n")
    tag = "nz-pro20-" + slot.name + ":isolated-agent"
    pro.command(["docker", "build", "--network=none", "--pull=false", "-t", tag, str(build)], log=slot / "image-build.txt", timeout=240)
    pro.save(slot / "environment.json", {"source_image": base, "source_image_id": info["Id"],
        "source_size_bytes": info["Size"], "project_python": version, "agent_python": "3.13",
        "agent_executable": config["agent_executable"], "agent_image": tag,
        "agent_image_id": pro.command(["docker", "image", "inspect", tag, "--format", "{{.Id}}"]).stdout.strip(),
        "user_authorized_isolation": "补隔离 Agent Python（建议）", "original_project_toolchain_unchanged": True})
    return base, tag, config["agent_executable"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--preparation-retry", action="store_true")
    args = parser.parse_args()
    plan = remaining_plan(preparation_retry=args.preparation_retry)
    pro.save(OUT / ("preparation-retry-plan.json" if args.preparation_retry else "continuation-plan.json"), plan)
    print(json.dumps({"remaining": plan["remaining"], "unattempted": [t["number"] for t in plan["tasks"]]}, ensure_ascii=False), flush=True)
    if not args.execute or not plan["tasks"]:
        return
    smoke = json.loads((OUT / "df/verification.json").read_text())
    if not (smoke["actual_native_sdk"] and smoke["wire_max_tokens_absent"]
            and smoke["paid_model_requests"] == 0):
        raise RuntimeError("provider_default_output_smoke_not_verified")
    from nz_coder.evaluation.model_relay import RelayLedger, RelayLimits
    from nz_coder.swebench.trace_budget import TraceBudget, archive_instance_diagnostics
    sys.path.insert(0, str(pro.ARTIFACTS / "nz-provider-contract-2026-10-09/official/recipe-site"))
    from nz_coder.evaluation.deepseek_counting import DeepSeekV41Counter
    from dotenv import dotenv_values
    counter = DeepSeekV41Counter(pro.ARTIFACTS / "nz-provider-contract-2026-10-09/official/audited-recipe/static/tokenizers/v41/tokenizer.json")
    key = dotenv_values(pro.ROOT / ".env").get("API_KEY")
    if not key:
        raise RuntimeError("authorized_host_key_unavailable")
    live = OUT / ("pending" if args.preparation_retry else "next")
    live.mkdir()
    grant = json.loads((OUT / "live/authorization-used.json").read_text())
    grant.update(cost_limit=plan["remaining"]["cost_cny"], scope=[t["task"]["instance_id"] for t in plan["tasks"]],
        valid_from=time.time() - 1, valid_until=time.time() + 86400,
        predecessor_ledger_sha256=plan["predecessor_ledger_sha256"], original_total_authorization_cny=50,
        previous_used=plan["used"], provider_output_reservation_tokens=393216,
        authorized_superseded_stop=plan["authorized_superseded_stop"],
        authorization_text=grant["authorization_text"] + f" 用户看到8001停止原因后回复继续进行，并进一步要求：你别设限制行吗？主请求不传max_tokens，按官方容量预留费用；沿用50元总预算，仅补做{len(plan['tasks'])}个尚未尝试实例。")
    pro.save(live / "authorization-used.json", grant)
    if args.preparation_retry:
        grant["source_fee_ledger_chain_sha256"] = plan["source_fee_ledger_chain_sha256"]
        pro.save(live / "authorization-used.json", grant)
    limits = RelayLimits(requests=plan["remaining"]["requests"], input_per_request=1000000,
        input_total=__import__("math").ceil(grant["cost_limit"] / grant["prices_cny_per_million"]["cached_input"] * 1000000),
        output_total=__import__("math").ceil(grant["cost_limit"] / grant["prices_cny_per_million"]["output"] * 1000000),
        request_seconds=600)
    ledger = RelayLedger(live / "ledger.jsonl", limits, empirical_authorization=grant)
    pro.save(live / "execution-config.json", {"limits": asdict(limits), "plan": plan,
        "model": pro.MODEL, "source_commit": pro.command(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "native_entry_sha256": hashlib.sha256((DOC / "native-entry.py").read_bytes()).hexdigest(),
        "main_requests_per_task": 40, "auxiliary_requests_per_task": 6, "task_seconds": 3000,
        "wire_max_tokens": None, "output_fee_reservation_per_request": 393216, "internal_default_output_reserve": 65536, "context_tokens": 1000000, "stream": False, "thinking": None, "reasoning_effort": None})
    manifest = json.loads((OUT / "task-manifest.json").read_text())
    checksums = {}
    for line in (OUT / "official-SHA256SUMS").read_text().splitlines():
        h, p = line.split(maxsplit=1)
        checksums[p.lstrip("*").removeprefix("./")] = h
    budget = TraceBudget(OUT / "archives", warning_bytes=858993459, hard_limit_bytes=1073741824, cleanup_target_bytes=644245094)
    rows = []
    try:
        for candidate in plan["tasks"]:
            task = candidate["task"]
            slot = live / candidate["number"]
            slot.mkdir()
            report = {"instance_id": task["instance_id"], "attempt": 0, "agent_status": None, "resolved": None,
                "original_preparation_record": str(OUT / "live" / slot.name), "preparation_retry_not_agent_retry": True}
            if args.preparation_retry:
                report["previous_preparation_record"] = candidate["previous_preparation_record"]
            pro.save(slot / "identity.json", report)
            print("START " + slot.name + " " + task["instance_id"], flush=True)
            base = tag = case = None
            try:
                if ledger.failed_contracts:
                    raise RuntimeError("accounting_stop")
                if ledger.cost_occupied_cny + 3.145728 > grant["cost_limit"] or len(ledger.attempts) >= limits.requests:
                    raise RuntimeError("remaining_global_budget_stop")
                if shutil.disk_usage(OUT).free < 1024 ** 3:
                    raise RuntimeError("storage_stop")
                base, tag, executable = isolated_image(task, slot)
                case = pro.prepare_case(slot, tag, task, False)
                config = json.loads((case / "input/run.json").read_text())
                config["provider_default_output"] = True
                pro.save(case / "input/run.json", config)
                report["attempt"] = 1
                pro.run_case(slot, case, tag, ledger, counter, key=key, agent_python=executable)
                patch = pro.freeze(slot, case)
                result_path = case / "result/result.json"
                result = json.loads(result_path.read_text()) if result_path.exists() else None
                report.update(status="attempted", agent_status=result["status"] if result else "missing_result",
                    patch_sha256=hashlib.sha256(patch).hexdigest())
                verdict = pro.grade(slot, base, task, manifest, checksums)
                report.update(resolved=verdict["resolved"], eval_status=verdict["status"])
                pro.archive_case(slot, case, task, live.name, budget, archive_instance_diagnostics)
            except Exception as exc:
                report.update(status="blocked" if case is None else "attempt_or_delivery_error", error=repr(exc))
            finally:
                report["physical_requests"] = sum(a["run"] == slot.name for a in ledger.attempts.values())
                if tag:
                    pro.command(["docker", "image", "rm", tag], log=slot / "image-cleanup.txt", check=False)
                if base:
                    pro.command(["docker", "image", "rm", base], log=slot / "base-cleanup.txt", check=False)
                    pro.command(["docker", "image", "rm", task["configuration"]["environment"]["docker_image"]], log=slot / "base-tag-cleanup.txt", check=False)
                rows.append(report)
                pro.save(slot / "report.json", report)
                pro.save(live / "progress.json", rows)
                print("END " + json.dumps(report, ensure_ascii=False), flush=True)
        pro.save(live / "summary.json", {"tasks": rows, "physical_requests": len(ledger.attempts),
            "estimated_cost_occupied_cny": ledger.cost_occupied_cny, "accounting_failures": ledger.failed_contracts,
            "aggregate_estimated_cost_cny": plan["used"]["cost_cny"] + ledger.cost_occupied_cny})
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
