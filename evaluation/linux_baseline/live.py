"""Explicit P1 paid gate and bounded execution plans, reusing the frozen P0 driver."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal, InvalidOperation
import importlib.metadata
import json
from pathlib import Path
import re
import sys

from . import runner
from .billing import BillingStopped, Ledger, Policy, _exception_category, claim_attempt, summarize
from .catalog import AGENT_REVISION, TASK_SPECS, digest, manifest
from .continuation import PREDECESSOR_ID

RATE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing/"
# Public-page lookup, NOT an asserted tariff effective date or account contract.
RATE_RETRIEVED_AT = "2026-09-08T06:47:24Z"
RATE_SOURCE_SHA256 = "899affbdbc33d0be620d8dea59e86f5036c11b5410b14d060b8d2874c74f38e5"
CONFIG_KEYS = frozenset(("authorized", "authorization_reference", "budget_currency", "total_budget",
    "per_task_budget", "allowed_tasks", "execution_order", "attempts_per_task", "auto_continue_remaining_tasks",
    "account_provider", "account_rates_confirmed", "endpoint", "model", "effort", "token_budget", "rate_date",
    "rate_source", "hit_rate", "miss_rate", "output_rate", "experiment_id", "harness_revision", "output_directory",
    "main_thinking", "main_output_limit", "auxiliary_thinking", "auxiliary_output_limit"))


def execution_plan(config: dict) -> tuple[str, ...]:
    """Only the original pair or the explicitly linked first T04 attempt."""
    tasks = config.get("allowed_tasks")
    if type(tasks) is not list or config.get("execution_order") != tasks:
        raise ValueError("Execution order must equal the authorized task list")
    if tasks == ["T01", "T04"] and "continuation" not in config:
        return ("T01", "T04")
    if tasks == ["T04"] and config.get("continuation") == {"predecessor_experiment_id": PREDECESSOR_ID}:
        return ("T04",)
    raise ValueError("Only the original pair or evidence-linked T04 continuation is supported")


def authorized_policy(config: dict) -> Policy:
    """Parse a user-confirmed grant, never turn the example into authorization."""
    if not isinstance(config, dict) or set(config) not in (CONFIG_KEYS, CONFIG_KEYS | {"continuation"}):
        raise ValueError("P1 config must contain exactly the documented fields; no secrets")
    plan = execution_plan(config)
    if not isinstance(config["output_directory"], str) or not Path(config["output_directory"]).is_absolute():
        raise ValueError("Bind the grant to one absolute output directory")
    required = dict(authorized=True, budget_currency="CNY", account_provider="DeepSeek",
                    account_rates_confirmed=True, attempts_per_task=1,
                    auto_continue_remaining_tasks=False, rate_source=RATE_SOURCE,
                    main_thinking="enabled", auxiliary_thinking="disabled",
                    main_output_limit=8000, auxiliary_output_limit=1024,
                    rate_date=RATE_RETRIEVED_AT[:10])  # Legacy config key means lookup date.
    if any(type(config.get(key)) is not type(value) or config.get(key) != value for key, value in required.items()):
        raise ValueError("Paid authorization or confirmed account/rates incomplete")
    if not isinstance(config.get("authorization_reference"), str) or not config["authorization_reference"].strip():
        raise ValueError("Explicit user authorization reference required")
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", str(config.get("experiment_id", ""))) or
            not re.fullmatch(r"[0-9a-f]{40}", str(config.get("harness_revision", "")))):
        raise ValueError("Frozen experiment identity and driver revision required")
    if any(type(config[key]) is not str or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", config[key])
           for key in ("total_budget", "per_task_budget")):
        raise ValueError("Budgets must be canonical nonnegative Decimal strings")
    try:
        policy = Policy(authorized=True, total_budget=Decimal(config["total_budget"]),
                        task_budget=Decimal(config["per_task_budget"]), endpoint=config["endpoint"],
                        model=config["model"], effort=config["effort"], token_budget=config["token_budget"],
                        main_thinking=config["main_thinking"], output_limit=config["main_output_limit"],
                        auxiliary_thinking=config["auxiliary_thinking"], auxiliary_output_limit=config["auxiliary_output_limit"],
                        hit_rate=Decimal(config["hit_rate"]), miss_rate=Decimal(config["miss_rate"]),
                        output_rate=Decimal(config["output_rate"]))
    except (KeyError, TypeError, InvalidOperation):
        raise ValueError("Explicit finite budget and frozen model configuration required") from None
    if (policy.total_budget <= 0 or policy.task_budget <= 0 or policy.hit_rate < Decimal("0.10")
            or policy.miss_rate < 3 or policy.output_rate < 9):
        raise ValueError("Insufficient budget or rates below published peak ceiling")
    if plan == ("T04",) and not policy.task_budget <= policy.total_budget <= Decimal("5"):
        raise ValueError("T04 continuation allocation cannot exceed 5 CNY")
    return policy


def freeze(config: dict, policy: Policy) -> dict:
    """Read-only version/environment checks, before creating any attempt."""
    from .live_worker import TOOLS
    head = runner.git(runner.ROOT, "rev-parse", "HEAD").decode().strip()
    if head != config["harness_revision"] or runner.git(runner.ROOT, "status", "--porcelain"):
        raise ValueError("Live requires the exact committed, clean frozen worktree")
    if runner.git(runner.ROOT, "diff", AGENT_REVISION, "--", "nz_coder"):
        raise ValueError("P0 Agent source has changed; do not mix experiments")
    versions = runner.environment_versions()
    versions["packages"]["httpx"] = importlib.metadata.version("httpx")
    pytest_version = versions["packages"].get("pytest") or "0"
    if not 7 <= int(pytest_version.split(".")[0]) < 9:
        raise ValueError("Live requires project-supported pytest >=7,<9")
    task_manifest = manifest()
    from .continuation import carryover
    return dict(experiment_id=config["experiment_id"], agent_revision=AGENT_REVISION,
                config_hash=digest(config),
                carryover=carryover(config) if execution_plan(config) == ("T04",) else None,
                selected_tasks=list(execution_plan(config)),
                harness_revision=head, task_manifest_hash=digest(task_manifest),
                acceptance_hashes={t["task_id"]: t["acceptance_revision"] for t in task_manifest["tasks"]},
                versions=versions, provider="openai-compatible", endpoint=policy.endpoint,
                account_provider="DeepSeek (user-confirmed, not independently queried)",
                model=policy.model, effort=policy.effort, thinking="enabled", stream=False,
                request_modes=dict(
                    main=dict(thinking=policy.main_thinking, effort=policy.effort, output_limit=policy.output_limit),
                    auxiliary=dict(thinking=policy.auxiliary_thinking, effort=None, output_limit=policy.auxiliary_output_limit),
                    transport_purpose="unknown; validates mode set, not message or tool labels",
                    budget_scope="shared task/experiment ledger; uncertainty stops all modes"),
                provider_model_version_at_lookup="DeepSeek-V4-Flash-0731",
                published_context="1M shared input/output", published_max_output="384K",
                profile="main; restricted P1 tool allowlist; child_agents=false", tools=list(TOOLS),
                system_source="nz_coder.runtime.conversation.prompt.build",
                system_source_blob=runner.git(runner.ROOT, "rev-parse", "HEAD:nz_coder/runtime/conversation/prompt.py").decode().strip(),
                system_actual_hashes="per-request billing.request_facts.system_sha256 (no plaintext)",
                agent_tree=runner.git(runner.ROOT, "rev-parse", "HEAD:nz_coder").decode().strip(),
                planning=False, reflection=False, mcp=False, paid_embeddings=False,
                auto_mode_classifier=False, model_sidecar="same client and ledger; no explicit alternate provider",
                sdk_retries=2, http_transport_retries=0, stop_after_billing_uncertainty=True,
                input_reservation_tokens=policy.input_ceiling, max_output_tokens=policy.output_limit,
                per_task_cumulative_token_budget=policy.token_budget, max_main_turns=30, worker_wall_seconds=600,
                wall_scope="whole worker including auxiliaries; export/acceptance after worker termination",
                rates=dict(source=RATE_SOURCE, retrieved_at=RATE_RETRIEVED_AT,
                           source_sha256=RATE_SOURCE_SHA256, effective_date=None,
                           calculation_basis="published peak-rate ceiling for all periods; not account invoice",
                           peak_periods="Mon-Fri 09:00-12:00 and 14:00-18:00 Asia/Shanghai",
                           off_peak_multiplier="0.5", currency="CNY", unit="per million tokens",
                           hit=str(policy.hit_rate), miss=str(policy.miss_rate), output=str(policy.output_rate)),
                total_budget=str(policy.total_budget), per_task_budget=str(policy.task_budget),
                authorization_reference=config["authorization_reference"])


def credential(policy: Policy) -> str:
    """Official product resolution; return secret only to the child environment."""
    from nz_coder.foundation.workspace_trust import load_config_snapshot
    from nz_coder.providers.models import active_model_selection
    from nz_coder.providers.configuration import provider_connection
    snapshot = load_config_snapshot(runner.ROOT)
    selected = active_model_selection(runner.ROOT, config_snapshot=snapshot)
    connection = provider_connection(selected.provider, config_snapshot=snapshot)
    if (selected.provider != "openai-compatible" or selected.model_id != policy.model
            or connection.base_url != policy.endpoint or selected.variant not in {None, policy.effort}):
        raise ValueError("Current Provider/model differs from the authorized snapshot")
    if not connection.api_key:
        raise ValueError("No credential resolved for the authorized Provider")
    return connection.api_key


def run(output: Path, config: dict) -> dict:
    policy = authorized_policy(config)  # NO preparation, credentials or worker before authorization.
    plan = execution_plan(config)
    if output.resolve() != Path(config["output_directory"]).resolve():
        raise ValueError("Output differs from the authorized single experiment directory")
    frozen = freeze(config, policy)
    from .continuation import carryover, claim
    carry = carryover(config) if plan == ("T04",) else None
    if carry is not None and frozen.get("carryover") != carry:
        raise BillingStopped("Frozen carryover differs from predecessor evidence")
    api_key = credential(policy)
    # Admission preview is memory-only, never a paid request or account probe.
    Ledger(policy, lambda _row: None).reserve("preflight", policy.output_limit)
    if carry is not None:
        claim(config, carry)
    output.mkdir(mode=0o700, parents=False)  # Existing experiment, even failed, is never resumed.
    runner.write_json(output / "frozen.json", frozen)
    results = {spec[0]: dict(task_id=spec[0], final_status="not_run", starts=0) for spec in TASK_SPECS}
    summary = dict(evidence_kind="live/P1", experiment_id=config["experiment_id"],
                   selected_tasks=list(plan), carryover=carry, tasks=results)
    runner.write_json(output / "summary.json", summary)
    remaining = policy.total_budget
    for task in plan:
        if freeze(config, policy) != frozen:
            raise BillingStopped("Frozen configuration changed; experiment stopped")
        task_policy = replace(policy, total_budget=remaining)
        try:
            Ledger(task_policy, lambda _row: None).reserve("preflight", policy.output_limit)
        except BillingStopped:
            summary["stopped_reason"] = "next request cannot be reserved"
            break
        directory = claim_attempt(output, task)
        spec = next(spec for spec in TASK_SPECS if spec[0] == task)
        repo = directory / "repo"
        runner.materialize(spec, repo, directory / "home")
        session = f"p1-{config['experiment_id']}-{task}"
        child_config = {**config, "total_budget": str(remaining)}
        descriptor = directory / "request.json"
        runner.write_json(descriptor, dict(config=child_config, session=session, prompt=spec[4],
                                          task_id=task, selected_tasks=list(plan), frozen_hash=digest(frozen)))
        env = runner.isolated_environment(directory / "home", runner.ROOT)
        env.update(API_KEY=api_key, API_BASE_URL=policy.endpoint, MODEL_PROVIDER="openai-compatible",
                   MODEL_ID=policy.model, MODEL_VARIANT="", NZ_AUTO_MODE_CLASSIFIER_ENABLED="0")
        result = dict(task_id=task, starts=1, final_status="in_progress", session=session)
        results[task] = result
        runner.write_json(output / "summary.json", summary)
        try:
            execution = runner.process([sys.executable, "-m", "evaluation.linux_baseline.live_worker", str(descriptor)],
                                       repo, env, directory / "runtime.jsonl", 600)
        except KeyboardInterrupt:
            # process() runs owned-process cleanup in finally, but its return
            # record was lost. Preserve artifacts without claiming cleanup OK.
            execution = dict(exit_code=None, timed_out=False, exception="KeyboardInterrupt",
                             cleanup_error="unverified_after_interrupt", elapsed_seconds=None)
        result.update(execution=execution, cleanup_ok=not execution["cleanup_error"])
        try:
            rows = [json.loads(line) for line in (directory / "billing.jsonl").read_text().splitlines()]
            billing = summarize(rows, task_policy)
            result["billing"] = billing
            memory_summary = directory / "billing-summary.json"
            if not memory_summary.exists():
                result["billing_journal"] = result.pop("billing")
                result["billing_consistency"] = "worker_summary_missing"
                raise BillingStopped("Worker final evidence missing; remaining budget unknown")
            if json.loads(memory_summary.read_text()) != billing:
                # A settlement line may have reached disk before fsync failed,
                # while the following uncertainty append also failed. Never
                # discard the worker's in-memory stop/hold based on that line.
                result["billing_journal"] = result.pop("billing")
                result["billing_consistency"] = "journal_worker_summary_mismatch"
                raise BillingStopped("Worker/journal evidence differs; remaining budget unknown")
            remaining = Decimal(billing["remaining_total_budget"])
            runtime_result, _events = runner.read_events(directory / "runtime.jsonl")
            status = (runtime_result or {}).get("status")
            result["runtime_status"] = status
            result["runtime_completed"] = status == "completed" and execution["exit_code"] == 0
            result["usage_complete"] = billing["usage_complete"]
            result["within_budget"] = all(Decimal(billing[key]) >= 0 for key in
                ("remaining_total_budget", "remaining_task_budget", "remaining_tokens"))
        except Exception as error:
            remaining = None  # A torn/missing journal is NOT an unused full budget.
            result.update(usage_complete=False, within_budget=None, billing_error=_exception_category(error))
        # Independent export/acceptance still runs after lost accounting or crashed runtime.
        acceptance_reliable = False
        try:
            patch = directory / "final.patch"
            result["patch"] = runner.export_patch(repo, patch, directory / "home")
            violations = runner.scope_violations(spec, result["patch"]["changed_files"])
            result["scope_violations"] = violations
            result["acceptance"] = runner.replay(spec, patch, directory / "replay")
            acceptance_reliable = set(result["acceptance"]) == {"target", "regression"} and all(
                not check.get("exception") and not check.get("cleanup_error")
                and not check.get("timed_out") and check.get("exit_code") in {0, 1, 2}
                and check.get("collected", 0) > 0
                for check in result["acceptance"].values())
            result["patch_verified"] = (acceptance_reliable and not violations
                                        and all(v["accepted"] for v in result["acceptance"].values()))
        except Exception as error:
            result.update(patch_verified=False, artifact_error=_exception_category(error))
        result["acceptance_reliable"] = acceptance_reliable
        reliable = (result.get("usage_complete") and not result.get("billing", {}).get("blocked")
                    and acceptance_reliable and not result.get("artifact_error")
                    and result.get("billing", {}).get("requests_sent", 0) > 0
                    and not execution["exception"] and not execution["cleanup_error"])
        result["final_status"] = runner.classify(execution=execution, runtime_status=result.get("runtime_status"),
            patch_verified=result["patch_verified"], within_budget=result.get("within_budget")) if reliable else "infrastructure_blocked"
        runner.write_json(directory / "result.json", result)
        runner.write_json(output / "summary.json", summary)
        if not reliable or result["final_status"] == "runtime_error":
            summary["stopped_reason"] = "billing or infrastructure uncertainty; no automatic retry"
            break
    summary["remaining_total_budget"] = str(remaining) if remaining is not None else None
    if carry is not None:
        summary["original_budget_remaining"] = (str(Decimal(carry["unallocated_balance"]) + remaining)
                                                 if remaining is not None else None)
        summary["historical_usage_complete"] = False  # Old request 14 stays unresolved.
    summary["stopped"] = True
    runner.write_json(output / "summary.json", summary)
    return summary
