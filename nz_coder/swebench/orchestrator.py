"""Retry orchestrator: coordinates adapter, guardrail, and agent loop.

RetryOrchestrator is the only component that:
  - decides whether to apply a previous patch (using PatchRiskReport)
  - builds the initial agent message list (combining instance prompt,
    previous-attempt context, and FailureFeedback)
  - manages git clone / apply / diff collection
  - runs the agent loop with timeout
  - handles empty-patch retries
"""
from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import queue as queue_module
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from nz_coder.swebench.adapter import SWEBenchAdapter, _safe_name
from nz_coder.swebench.guardrail import PatchGuardrail
from nz_coder.swebench.models import FailureFeedback, PatchRiskReport, RetryPlan
from nz_coder.runtime.execution_context import scoped_runtime_overrides
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.swebench.artifacts import AttemptJournal, export_public_trajectory
from nz_coder.swebench.policy import STRICT_ALLOWED_TOOLS
from nz_coder.swebench.trace_budget import (
    TraceBudget,
    archive_instance_diagnostics,
    evaluate_trace_budget,
    write_trace_budget_report,
)

if TYPE_CHECKING:
    pass


DEFAULT_BENCH_DIR = Path.cwd() / ".nz-coder" / "swebench-lite"
DEFAULT_REPO_CACHE_DIR = DEFAULT_BENCH_DIR / "repo-cache"


class AgentRunTimeout(TimeoutError):
    """Raised when a single agent instance exceeds the configured timeout."""


def _strict_agent_protocol() -> str:
    """Return the model-visible local-only execution contract for strict runs."""
    return (
        "\n\nStrict local tool protocol:\n"
        "- bash never changes directory with cd. Set bash.workdir to a workspace-relative "
        "subdirectory instead.\n"
        "- Allowed direct local commands: cat, cmp, cut, diff, file, grep, head, ls, pwd, "
        "rg, sort, stat, tail, tr, tree, uniq, wc.\n"
        "- Allowed Git subcommands: git diff | grep | ls-files | rev-parse | status. "
        "Git history, remotes, and network access are forbidden.\n"
        "- Allowed Python verification: python3 -m py_compile | compileall | pytest. "
        "Do not use python3 -c, scripts, package installation, redirects, command "
        "substitution, or URLs.\n"
        "Structured navigation decisions:\n"
        "- Before reading 3 or more files in one module, call repo_map on the smallest "
        "relevant directory.\n"
        "- When the known function, class, or method name is available, call read_symbol "
        "instead of reading the whole file.\n"
        "- Before changing a shared symbol, use find_symbol_callers or code_references; "
        "use analyze_impact to inspect affected callers and tests.\n"
    )


def _classify_tool_log_status(output: str) -> str:
    """Separate strict process-policy feedback from execution failures."""
    value = str(output or "")
    if value.startswith(("Error:", "Denied")):
        if "SWE-bench strict mode" in value:
            return "policy_rejected"
        return "error"
    if value.startswith("Command exited with code"):
        return "nonzero"
    return "ok"


class RetryOrchestrator:
    """Coordinates adapter + guardrail + agent loop for SWE-bench retry runs.

    Typical flow
    ------------
    1. build_retry_plan()    — load feedback, analyse risk, decide strategy
    2. build_initial_messages() — compose the agent's opening conversation
    3. run_instance()        — clone repo, run agent, collect diff
    """

    def __init__(self, adapter: SWEBenchAdapter, guardrail: PatchGuardrail):
        self.adapter = adapter
        self.guardrail = guardrail

    # ── Plan building ─────────────────────────────────────────────────────────

    def build_retry_plan(
        self,
        instance_id: str,
        previous_patch: str,
        eval_log_dir: Path,
        *,
        max_feedback_chars: int = 4000,
        empty_patch_retries: int = 1,
    ) -> RetryPlan:
        """Load official feedback + analyse patch risk → RetryPlan.

        No agent is started here; this is pure decision logic.
        """
        feedback = self.adapter.load_feedback(
            instance_id, eval_log_dir, max_output_chars=max_feedback_chars
        )
        risk = self.guardrail.analyze(
            previous_patch, regression_context=feedback.has_regressions
        )
        apply_patch = self._should_apply_previous_patch(previous_patch, feedback, risk)
        return RetryPlan(
            instance_id=instance_id,
            apply_previous_patch=apply_patch,
            previous_patch=previous_patch,
            failure_feedback=feedback,
            risk_report=risk,
            start_from_clean=not apply_patch,
            empty_patch_retries=empty_patch_retries,
        )

    def _should_apply_previous_patch(
        self,
        patch: str,
        feedback: FailureFeedback,
        risk: PatchRiskReport,
    ) -> bool:
        """Decide whether to git-apply the previous patch into the retry worktree."""
        if not patch.strip():
            return False
        # Broad enum coercion is always too risky to carry forward
        if any(i.category in {"broad_enum_value_coercion", "broad_enum_value_coercion_under_regression_guard"}
               for i in risk.items):
            return False
        if not feedback.has_regressions:
            return True
        # Under regression guard, any blocking item means start clean
        blocking_categories = {
            "deleted_methods_under_regression_guard",
            "deleted_classes_under_regression_guard",
            "added_classes_under_regression_guard",
            "added_methods_under_regression_guard",
            "magic_separator_index_under_header_rows",
            "broad_except_under_regression_guard",
        }
        if any(i.category in blocking_categories for i in risk.items):
            return False
        return True

    # ── Message composition ───────────────────────────────────────────────────

    def build_initial_messages(self, instance: dict, plan: RetryPlan) -> list[dict]:
        """Compose the agent's opening message list from instance + plan.

        Message order:
          1. instance prompt (problem statement)
          2. previous attempt block (if there was a previous patch)
          3. official failure feedback block (if feedback is available)
        """
        messages: list[dict] = [
            {"role": "user", "content": self.adapter.format_instance_prompt(instance)}
        ]
        if plan.previous_patch.strip():
            messages.append({
                "role": "user",
                "content": self._format_previous_attempt_prompt(plan),
            })
        if plan.failure_feedback:
            messages.append({
                "role": "user",
                "content": plan.failure_feedback.to_agent_prompt(plan.previous_patch),
            })
        return messages

    def _format_previous_attempt_prompt(self, plan: RetryPlan) -> str:
        """Build the <previous-attempt> block.

        Replaces the old _format_previous_attempt_prompt() function.
        When apply_previous_patch is True the patch excerpt is shown.
        When False (start_from_clean) the risk summary is shown instead.
        """
        if plan.apply_previous_patch:
            guidance = (
                "The repository already contains the previous prediction patch. "
                "That patch did not resolve the official SWE-bench test. "
                "Do not simply re-submit it; inspect why it failed and make the smallest "
                "additional correction needed."
            )
            patch_section = f"Previous patch excerpt:\n{_truncate_middle(plan.previous_patch, 12000)}"
        else:
            risk_block = plan.risk_report.to_prompt_block() if plan.risk_report else "- no risk data available"
            guidance = (
                "The previous prediction patch is NOT applied to the repository because "
                "it caused official regressions and contains structural patch-quality "
                "risks. Treat it as an anti-example, not a starting point. Work from the "
                "clean base checkout and produce a non-empty minimal patch that fixes the "
                "FAIL_TO_PASS behavior while preserving PASS_TO_PASS behavior. "
                "Do not stop after inspection: you must edit the implicated source file "
                "unless the issue statement is impossible to reproduce."
            )
            patch_section = (
                "Previous patch risk summary:\n"
                f"{risk_block}\n\n"
                "The risky patch body is intentionally omitted to avoid copying its "
                "implementation. Use the clean checkout plus official failure feedback "
                "to make the smallest correct source edit."
            )
        return (
            "<previous-attempt>\n"
            f"{guidance}\n\n"
            f"{patch_section}\n"
            "</previous-attempt>"
        )

    # ── Instance execution ────────────────────────────────────────────────────

    def run_instance(
        self,
        instance: dict,
        plan: RetryPlan | None,
        *,
        work_root: Path,
        run_id: str,
        config,
        build_prompt,
        agent_cls,
        trace_cls,
        clone_timeout: int,
        agent_timeout: int,
        empty_patch_retries: int = 0,
        strict: bool = False,
    ) -> dict:
        """Clone repo, run agent, collect diff. Returns result dict.

        *plan* is None for first-pass runs (no previous patch / feedback).
        When *plan* is provided its messages are prepended to the conversation.
        """
        instance_id = instance["instance_id"]
        repo_dir = work_root / _safe_name(instance_id)
        started = time.time()
        print(f"\n[{instance_id}]")

        # ── Repo setup ────────────────────────────────────────────────────────
        clone = _prepare_repo(
            instance,
            repo_dir,
            clone_timeout,
            sanitize_history=strict,
        )
        if clone["returncode"] != 0:
            return {
                "instance_id": instance_id,
                "status": "setup_failed",
                "summary": clone["summary"],
                "stdout": clone.get("stdout", "")[-4000:],
                "stderr": clone.get("stderr", "")[-4000:],
                "duration": round(time.time() - started, 1),
                "model_patch": "",
            }

        if plan and plan.previous_patch.strip() and plan.apply_previous_patch:
            applied = _apply_patch_text(repo_dir, plan.previous_patch, timeout=120)
            if applied.returncode != 0:
                return {
                    "instance_id": instance_id,
                    "status": "setup_failed",
                    "summary": "failed to apply previous prediction patch",
                    "stdout": applied.stdout[-4000:],
                    "stderr": applied.stderr[-4000:],
                    "duration": round(time.time() - started, 1),
                    "model_patch": "",
                }

        # ── Agent setup ───────────────────────────────────────────────────────
        trace_dir = repo_dir / ".nz-coder-runs"
        tracer = trace_cls(trace_dir=trace_dir, enabled=True)
        tool_log: list[dict] = []
        tool_generation = 0

        def log_tool(name: str, output: str) -> None:
            nonlocal tool_generation
            status = _classify_tool_log_status(output)
            if status == "ok" and name in {
                "write_file", "edit_file", "apply_patch", "replace_lines",
                "python_structural_edit", "scaffold_project", "write_files_batch",
            }:
                tool_generation += 1
            tool_log.append({
                "tool": name,
                "name": name,
                "status": status,
                "generation": tool_generation,
                "output_len": len(output),
                "output": output[:512],
            })
            preview = output.replace("\n", " ")[:160]
            print(f"  {name}: {status} {preview}")

        with scoped_workdir(repo_dir):
            system_prompt = build_prompt() + (
                f"\n\nYou are solving a SWE-bench {self.adapter.profile.name.title()} task "
                "in a checked-out repository. "
                "Make the minimal source-code change needed to satisfy the issue. "
                "Do not edit tests unless the issue explicitly requires it. "
                "IMPORTANT: Always use 'python3' (not 'python') to run Python code. "
                "IMPORTANT: Do NOT create any new files in the repository. "
                "Clean up any scratch files before finishing. "
                "IMPORTANT: This is a raw source checkout - the package is NOT installed. "
                "Do NOT try `from <package> import ...` to verify your fix; "
                "it will often fail with ModuleNotFoundError. "
                "\n"
                "Search and verification protocol:\n"
                "1. Start with grep_search using key issue tokens, failing test names, "
                "and traceback clues if available.\n"
                "2. Inspect at most 3 candidate files before making the first edit.\n"
                "3. Prefer read_symbol over read_file when a candidate "
                "function/class/method is known.\n"
                "4. After any source edit, call diff_status.\n"
                "5. If diff_status shows a non-empty source-only diff, call "
                "verify_changed_files.\n"
                "6. Do NOT run pytest, tox, or full test suites after a source diff "
                "exists — they often fail due to missing dependencies, import errors, "
                "display backend issues, database setup, or package installation "
                "problems that are NOT caused by your patch.\n"
                "7. If verify_changed_files passes, finalize the patch.\n"
                "8. If local tests fail due to environment issues (missing modules, "
                "import errors, database config, display backends), stop verifying "
                "and leave the source patch for official SWE-bench evaluation.\n"
                "9. A plausible non-empty source patch is better than no patch."
            )
            if strict:
                system_prompt += _strict_agent_protocol()

        # Build message list
        if plan is not None:
            messages = self.build_initial_messages(instance, plan)
        else:
            messages = [{"role": "user", "content": self.adapter.format_instance_prompt(instance)}]
        public_input_path = trace_dir / "public-inference-input.json"
        public_input_path.write_text(json.dumps({
            "event": "benchmark_instance",
            "instance_id": instance_id,
            "benchmark_profile": self.adapter.profile.name,
            "prompt": messages[0]["content"],
            "strict": bool(strict),
            "attempts": 1,
        }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tracer.log(
            "benchmark_instance",
            instance_id=instance_id,
            benchmark_profile=self.adapter.profile.name,
            prompt=messages[0]["content"],
            strict=bool(strict),
            attempts=1,
        )

        # ── Agent run ─────────────────────────────────────────────────────────
        feedback_str = plan.failure_feedback.to_agent_prompt(plan.previous_patch) if (plan and plan.failure_feedback) else None
        effective_retries = plan.empty_patch_retries if plan else empty_patch_retries

        def run_attempt() -> dict:
            with (
                scoped_workdir(repo_dir),
                scoped_runtime_overrides(
                    agent_timeout_seconds=agent_timeout,
                    strict_local_tools=strict,
                ),
            ):
                return _run_agent_attempt(
                    agent_cls,
                    system_prompt,
                    tracer,
                    messages,
                    log_tool,
                    agent_timeout,
                    agent_kwargs=(
                        {"tool_allowlist": STRICT_ALLOWED_TOOLS}
                        if strict
                        else None
                    ),
                )

        try:
            agent_status = run_attempt()
            model_patch = _collect_diff(repo_dir)
            empty_retry_count = 0
            while _should_retry_empty_patch(
                model_patch,
                has_feedback=feedback_str is not None,
                attempts=empty_retry_count,
                max_retries=effective_retries,
            ):
                empty_retry_count += 1
                messages.append({
                    "role": "user",
                    "content": _format_empty_patch_retry_feedback(empty_retry_count, effective_retries),
                })
                agent_status = run_attempt()
                model_patch = _collect_diff(repo_dir)

            # Risk analysis on the final patch
            has_regressions = bool(plan and plan.failure_feedback and plan.failure_feedback.has_regressions)
            risk_report = self.guardrail.analyze(model_patch, regression_context=has_regressions)
            risk_reasons = _agent_status_risk_labels(agent_status, tool_log) + risk_report.risk_labels()

            status = "completed" if not risk_reasons else "risky"
            if not model_patch.strip():
                status = "empty_patch"
            summary = f"patch_chars={len(model_patch)}, tools={len(tool_log)}"
            if empty_retry_count:
                summary += f", empty_patch_retries={empty_retry_count}"
            if risk_reasons:
                summary += ", risk=" + ",".join(risk_reasons)

        except AgentRunTimeout as exc:
            agent_status = {"status": "timeout", "error": str(exc)}
            model_patch = _collect_diff(repo_dir)
            status = "agent_failed"
            summary = str(exc)
            risk_reasons = ["agent_status:timeout"]
        except Exception as exc:
            agent_status = {"status": "exception", "error": str(exc)}
            model_patch = ""
            status = "agent_failed"
            summary = str(exc)
            risk_reasons = ["agent_status:exception"]
        return {
            "instance_id": instance_id,
            "repo": instance.get("repo"),
            "base_commit": instance.get("base_commit"),
            "status": status,
            "summary": summary,
            "agent_status": agent_status,
            "trace": str(tracer.path),
            "workdir": str(repo_dir),
            "duration": round(time.time() - started, 1),
            "tool_calls": len(tool_log),
            "tool_errors": sum(1 for row in tool_log if row["status"] == "error"),
            "policy_rejections": sum(
                1 for row in tool_log if row["status"] == "policy_rejected"
            ),
            "process_warnings": _agent_status_process_warnings(agent_status, tool_log),
            "risk_reasons": risk_reasons,
            "empty_patch_retries": locals().get("empty_retry_count", 0),
            "model_patch": model_patch,
            "public_input": str(locals().get("public_input_path", "")),
        }

    # ── Batch helpers (used by cli.py) ────────────────────────────────────────

    def run_batch(
        self,
        instances: list[dict],
        *,
        work_root: Path,
        run_id: str,
        config,
        build_prompt,
        agent_cls,
        trace_cls,
        clone_timeout: int,
        agent_timeout: int,
        empty_patch_retries: int,
        pred_file,
        model_name: str,
        strict: bool = False,
        attempt_journal: AttemptJournal | None = None,
        predictions_path: Path | None = None,
        public_trajectories_dir: Path | None = None,
        cleanup_worktrees: bool = False,
        trace_budget: TraceBudget | None = None,
        max_new_instances: int | None = None,
    ) -> list[dict]:
        """First-pass: run agent on each instance without previous predictions."""
        results = []
        attempted_ids = attempt_journal.attempted_ids() if attempt_journal else set()
        for index, instance in enumerate(instances, start=1):
            if instance["instance_id"] in attempted_ids:
                print(f"[RESUME] {instance['instance_id']}: pass@1 attempt already claimed; never rerunning.")
                continue
            if trace_budget is not None:
                pressure = evaluate_trace_budget(trace_budget)
                if pressure.hard_limit_reached:
                    report_path = write_trace_budget_report(trace_budget, pressure)
                    print(
                        "[TRACE BUDGET] Hard limit reached before next pass@1 "
                        f"claim: {pressure.used_bytes} bytes; report={report_path}"
                    )
                    break
                if pressure.warning:
                    print(
                        "[TRACE BUDGET] Warning threshold reached: "
                        f"{pressure.used_bytes}/{trace_budget.hard_limit_bytes} bytes"
                    )
            print(f"\n[{index}/{len(instances)}] {instance['instance_id']}")
            if attempt_journal is not None:
                attempt_journal.claim(instance["instance_id"])
            result = self.run_instance(
                instance,
                plan=None,
                work_root=work_root,
                run_id=run_id,
                config=config,
                build_prompt=build_prompt,
                agent_cls=agent_cls,
                trace_cls=trace_cls,
                clone_timeout=clone_timeout,
                agent_timeout=agent_timeout,
                empty_patch_retries=empty_patch_retries,
                strict=strict,
            )
            results.append(result)
            if attempt_journal is not None:
                trajectory = ""
                if public_trajectories_dir is not None:
                    trajectory_path = Path(public_trajectories_dir) / f"{instance['instance_id']}.jsonl"
                    trace_path = Path(str(result.get("trace") or ""))
                    if trace_path.is_file():
                        export_public_trajectory(
                            trace_path,
                            trajectory_path,
                            workspace=Path(result.get("workdir") or work_root),
                            preamble_path=Path(str(result.get("public_input") or "")),
                        )
                    else:
                        trajectory_path.parent.mkdir(parents=True, exist_ok=True)
                        trajectory_path.write_text(json.dumps({
                            "event": "inference_not_started",
                            "instance_id": instance["instance_id"],
                            "status": result.get("status"),
                            "summary": result.get("summary", ""),
                        }, ensure_ascii=False) + "\n", encoding="utf-8")
                    trajectory = str(trajectory_path)
                model_patch = result.get("model_patch", "")
                if result.get("status") == "agent_failed":
                    model_patch = ""
                attempt_journal.record({
                    "instance_id": instance["instance_id"],
                    "attempt": 1,
                    "status": result.get("status"),
                    "trajectory": trajectory,
                    "prediction": {
                        "instance_id": instance["instance_id"],
                        "model_name_or_path": model_name,
                        "model_patch": model_patch,
                    },
                })
                if predictions_path is not None:
                    attempt_journal.write_predictions(predictions_path)
            elif pred_file is not None:
                _write_prediction(pred_file, instance["instance_id"], model_name, result)
            if trace_budget is not None:
                trace_path = Path(str(result.get("trace") or ""))
                workdir = Path(str(result.get("workdir") or ""))
                if trace_path.is_file() and str(result.get("workdir") or ""):
                    archived = archive_instance_diagnostics(
                        instance_id=str(instance["instance_id"]),
                        workdir=workdir,
                        run_root=work_root,
                        trace_path=trace_path,
                        public_input_path=(
                            Path(str(result["public_input"]))
                            if result.get("public_input")
                            else None
                        ),
                        metadata={
                            "status": result.get("status"),
                            "summary": result.get("summary", ""),
                            "patch_chars": len(str(result.get("model_patch") or "")),
                            "trace": str(trace_path),
                        },
                        budget=trace_budget,
                    )
                    result["trace_archive"] = str(archived.bundle_path)
                    result["trace_archive_bytes"] = archived.used_bytes
                    if archived.warning:
                        print(
                            "[TRACE BUDGET] Warning threshold reached after "
                            f"{instance['instance_id']}: "
                            f"{archived.used_bytes}/{trace_budget.hard_limit_bytes} bytes"
                        )
                    if archived.hard_limit_reached:
                        write_trace_budget_report(
                            trace_budget,
                            evaluate_trace_budget(trace_budget),
                        )
                else:
                    result["trace_archive_skipped"] = "raw trace unavailable"
            if cleanup_worktrees:
                if result.get("workdir"):
                    _cleanup_completed_worktree(
                        Path(str(result["workdir"])),
                        work_root,
                    )
                    result["workdir_cleaned"] = True
            print(f"[{result['status'].upper()}] {instance['instance_id']}: {result.get('summary', '')}")
            if max_new_instances is not None and len(results) >= max_new_instances:
                print(
                    "[PAUSE] Reached this invocation's durable-result limit: "
                    f"{len(results)}/{max_new_instances}."
                )
                break
        return results

    def retry_batch(
        self,
        instances: list[dict],
        previous_predictions: dict[str, str],
        *,
        eval_log_dir: Path,
        work_root: Path,
        run_id: str,
        config,
        build_prompt,
        agent_cls,
        trace_cls,
        clone_timeout: int,
        agent_timeout: int,
        empty_patch_retries: int,
        max_feedback_chars: int,
        pred_file,
        model_name: str,
    ) -> list[dict]:
        """Second-pass: retry using official harness feedback."""
        results = []
        for index, instance in enumerate(instances, start=1):
            instance_id = instance["instance_id"]
            previous_patch = previous_predictions.get(instance_id, "")
            if not previous_patch.strip():
                print(f"[SKIP] {instance_id}: previous prediction has an empty patch.")
                continue

            print(f"\n[{index}/{len(instances)}] {instance_id}")
            plan = self.build_retry_plan(
                instance_id,
                previous_patch,
                eval_log_dir,
                max_feedback_chars=max_feedback_chars,
                empty_patch_retries=empty_patch_retries,
            )
            result = self.run_instance(
                instance,
                plan=plan,
                work_root=work_root,
                run_id=run_id,
                config=config,
                build_prompt=build_prompt,
                agent_cls=agent_cls,
                trace_cls=trace_cls,
                clone_timeout=clone_timeout,
                agent_timeout=agent_timeout,
            )
            if plan.failure_feedback:
                result["feedback_summary"] = {
                    "resolved": plan.failure_feedback.resolved,
                    "patch_applied": plan.failure_feedback.patch_applied,
                    "failing_tests": plan.failure_feedback.fail_to_pass,
                    "regression_tests": plan.failure_feedback.pass_to_pass,
                    "passing_tests_count": len(plan.failure_feedback.passing_tests),
                    "has_regressions": plan.failure_feedback.has_regressions,
                }
            result["previous_patch_applied_to_repo"] = plan.apply_previous_patch
            result["previous_patch_chars"] = len(previous_patch)
            results.append(result)
            _write_prediction(pred_file, instance_id, model_name, result)
            print(f"[{result['status'].upper()}] {instance_id}: {result.get('summary', '')}")
        return results


# ── Agent execution helpers ───────────────────────────────────────────────────

def _run_agent_attempt(
    agent_cls, system_prompt: str, tracer, messages: list[dict], log_tool,
    timeout: int, agent_kwargs: dict | None = None,
) -> dict:
    if timeout > 0 and hasattr(os, "fork"):
        return _run_agent_attempt_in_subprocess(
            agent_cls, system_prompt, tracer, messages, log_tool, timeout,
            agent_kwargs=agent_kwargs,
        )
    agent = agent_cls(
        system_prompt, permission_mode="auto", tracer=tracer, **(agent_kwargs or {})
    )
    return _run_agent_with_timeout(agent, messages, log_tool, timeout=timeout)


def _run_agent_attempt_in_subprocess(
    agent_cls,
    system_prompt: str,
    tracer,
    messages: list[dict],
    log_tool,
    timeout: int,
    agent_kwargs: dict | None = None,
) -> dict:
    ctx = multiprocessing.get_context("fork")
    result_queue = ctx.Queue()
    process = ctx.Process(
        target=_agent_attempt_worker,
        args=(agent_cls, system_prompt, tracer, messages, result_queue, agent_kwargs),
    )
    process.start()
    try:
        # Drain the result before joining.  multiprocessing.Queue writes from a
        # feeder thread, so joining first deadlocks once the payload exceeds the
        # OS pipe buffer: the child waits for the feeder while the parent waits
        # for the child.  Full tool output already lives in the trace artifact;
        # this channel carries only the bounded result projection below.
        try:
            payload = result_queue.get(timeout=timeout)
        except queue_module.Empty:
            if process.is_alive():
                _stop_agent_process(process)
                raise AgentRunTimeout(f"agent timed out after {timeout}s")
            process.join()
            raise RuntimeError(
                "agent subprocess exited without a result "
                f"(exitcode={process.exitcode})"
            )

        process.join(5)
        if process.is_alive():
            _stop_agent_process(process)
            raise RuntimeError("agent subprocess returned a result but did not exit")
        for event in payload.get("tool_events", []):
            log_tool(event["name"], event.get("output", ""))
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error", "agent subprocess failed"))
        return payload["agent_status"]
    finally:
        result_queue.close()
        result_queue.join_thread()


def _stop_agent_process(process) -> None:
    """Terminate one benchmark Agent child without leaving a live process."""
    if not process.is_alive():
        process.join()
        return
    process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)


def _agent_attempt_worker(
    agent_cls, system_prompt: str, tracer, messages: list[dict], queue,
    agent_kwargs: dict | None = None,
) -> None:
    tool_events: list[dict] = []

    def child_log_tool(name: str, output: str) -> None:
        tool_events.append({"name": name, "output": output[:512]})

    try:
        agent = agent_cls(
            system_prompt, permission_mode="auto", tracer=tracer,
            **(agent_kwargs or {}),
        )
        agent_status = asyncio.run(agent.run(messages, on_tool=child_log_tool, stream=False))
        queue.put({"ok": True, "agent_status": agent_status, "tool_events": tool_events})
    except BaseException as exc:
        queue.put({"ok": False, "error": repr(exc), "tool_events": tool_events})


def _run_agent_with_timeout(agent, messages: list[dict], log_tool, *, timeout: int) -> dict:
    if timeout <= 0:
        return asyncio.run(agent.run(messages, on_tool=log_tool, stream=False))
    if not hasattr(signal, "SIGALRM"):
        return asyncio.run(agent.run(messages, on_tool=log_tool, stream=False))

    def _handle_timeout(signum, frame):
        raise AgentRunTimeout(f"agent timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
    old_alarm = signal.alarm(timeout)
    try:
        return asyncio.run(agent.run(messages, on_tool=log_tool, stream=False))
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
        if old_alarm:
            signal.alarm(old_alarm)


# ── Empty-patch retry helpers ─────────────────────────────────────────────────

def _should_retry_empty_patch(
    model_patch: str,
    *,
    has_feedback: bool,
    attempts: int,
    max_retries: int,
) -> bool:
    return has_feedback and not model_patch.strip() and attempts < max(0, max_retries)


def _format_empty_patch_retry_feedback(attempt: int, max_retries: int) -> str:
    return (
        "<empty-patch-retry>\n"
        f"Retry attempt {attempt}/{max_retries}: your previous response left the repository "
        "with an empty patch. In a SWE-bench retry this is a failed attempt, because the "
        "official harness has already shown unresolved FAIL_TO_PASS behavior.\n\n"
        "Required action now:\n"
        "1. Re-open the implicated source file and the failing test.\n"
        "2. Make one minimal source-code edit; do not edit tests.\n"
        "3. Preserve PASS_TO_PASS behavior and avoid broad rewrites.\n"
        "4. Run `python3 -m py_compile <changed_file>` or the narrowest available test.\n\n"
        "Do not answer with analysis only. Finish with a non-empty git diff.\n"
        "</empty-patch-retry>"
    )


# ── Agent-status risk labels (non-patch risks) ────────────────────────────────

def _agent_status_risk_labels(agent_status: dict, tool_log: list[dict]) -> list[str]:
    """Return risk labels derived from agent execution status (not patch content).

    These complement PatchRiskReport.risk_labels() in the result dict.
    """
    reasons: list[str] = []
    status = (agent_status or {}).get("status")
    if status != "completed":
        reasons.append(f"agent_status:{status or 'unknown'}")
    if (agent_status or {}).get("verification_needed"):
        reasons.append("verification_needed")

    significant_error_tools = frozenset({
        "bash", "apply_patch", "write_file", "edit_file",
        "replace_lines", "python_structural_edit",
    })
    ignorable_error_patterns = (
        "Path escapes workspace",
        "before_symbol not found",
        "after_symbol not found",
        "symbol not found",
    )

    def _is_ignorable(row: dict) -> bool:
        out = row.get("output", "") or ""
        return any(pattern in out for pattern in ignorable_error_patterns)

    runtime = (agent_status or {}).get("runtime") or {}
    final_generation = runtime.get("mutation_generation")

    def _belongs_to_final_generation(row: dict) -> bool:
        generation = row.get("generation")
        if not isinstance(final_generation, int) or not isinstance(generation, int):
            return True
        return generation == final_generation

    if any(
        row["status"] == "error"
        and (row.get("name") or row.get("tool")) in significant_error_tools
        and not _is_ignorable(row)
        and _belongs_to_final_generation(row)
        for row in tool_log
    ):
        reasons.append("tool_errors")

    verification_passed = not (agent_status or {}).get("verification_needed", True)
    if any(row["status"] == "nonzero" for row in tool_log) and not verification_passed:
        reasons.append("nonzero_commands")

    return reasons


def _agent_status_process_warnings(
    agent_status: dict,
    tool_log: list[dict],
) -> list[str]:
    """Keep recovered execution defects visible without poisoning final risk."""
    warnings: list[str] = []
    policy_rejections = sum(
        1 for row in tool_log if row.get("status") == "policy_rejected"
    )
    if policy_rejections:
        warnings.append(f"strict_policy_rejections:{policy_rejections}")

    final_generation = ((agent_status or {}).get("runtime") or {}).get(
        "mutation_generation"
    )
    significant_error_tools = frozenset({
        "bash", "apply_patch", "write_file", "edit_file",
        "replace_lines", "python_structural_edit",
    })
    recovered_errors = sum(
        1
        for row in tool_log
        if row.get("status") == "error"
        and (row.get("name") or row.get("tool")) in significant_error_tools
        and isinstance(final_generation, int)
        and isinstance(row.get("generation"), int)
        and row["generation"] < final_generation
    )
    if recovered_errors:
        warnings.append(f"recovered_tool_errors:{recovered_errors}")
    return warnings


# ── Repo management helpers ───────────────────────────────────────────────────

def _prepare_repo(
    instance: dict,
    repo_dir: Path,
    timeout: int,
    *,
    sanitize_history: bool = False,
) -> dict:
    repo_dir = Path(repo_dir).resolve()
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    repo = instance["repo"]
    url = f"https://github.com/{repo}.git"
    cache_dir = _repo_cache_dir(repo)
    clone_url = str(cache_dir) if cache_dir.exists() else url
    clone = _run(["git", "clone", "--quiet", clone_url, str(repo_dir)], cwd=repo_dir.parent, timeout=timeout)
    if clone.returncode != 0 and clone_url != url:
        shutil.rmtree(repo_dir, ignore_errors=True)
        clone = _run(["git", "clone", "--quiet", url, str(repo_dir)], cwd=repo_dir.parent, timeout=timeout)
    if clone.returncode != 0:
        return _process_result(clone, f"git clone failed for {repo}")
    checkout = _run(["git", "checkout", "--quiet", instance["base_commit"]], cwd=repo_dir, timeout=timeout)
    if checkout.returncode != 0:
        return _process_result(checkout, f"git checkout failed for {instance['base_commit']}")
    if sanitize_history:
        sanitized = _reinitialize_repo_at_base(repo_dir, timeout)
        if sanitized.returncode != 0:
            return _process_result(sanitized, "failed to sanitize post-base Git history")
    _run(["git", "status", "--short"], cwd=repo_dir, timeout=30)
    return {"returncode": 0, "summary": "repo ready"}


def _reinitialize_repo_at_base(repo_dir: Path, timeout: int) -> subprocess.CompletedProcess:
    """Replace cloned history with one local base snapshot so gold fixes are absent."""
    git_dir = repo_dir / ".git"
    if not git_dir.is_dir() or git_dir.parent != repo_dir:
        return subprocess.CompletedProcess([], 2, "", "invalid benchmark Git directory")
    shutil.rmtree(git_dir)
    commands = (
        ["git", "init", "--quiet"],
        ["git", "config", "user.name", "NZ-Coder Benchmark"],
        ["git", "config", "user.email", "benchmark@localhost"],
        ["git", "add", "-A"],
        ["git", "commit", "--quiet", "-m", "SWE-bench base snapshot"],
    )
    last = subprocess.CompletedProcess([], 0, "", "")
    for command in commands:
        last = _run(list(command), cwd=repo_dir, timeout=timeout)
        if last.returncode != 0:
            return last
    return last


def _repo_cache_dir(repo: str) -> Path:
    return DEFAULT_REPO_CACHE_DIR / f"{_safe_name(repo)}.git"


def _apply_patch_text(repo_dir: Path, patch_text: str, timeout: int) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".patch", delete=False) as tmp:
        tmp.write(patch_text)
        patch_path = Path(tmp.name)
    try:
        return _run(
            ["git", "apply", "--whitespace=nowarn", str(patch_path)],
            cwd=repo_dir,
            timeout=timeout,
        )
    finally:
        patch_path.unlink(missing_ok=True)


def _collect_diff(repo_dir: Path) -> str:
    _cleanup_scratch_files(repo_dir)
    _run(["git", "add", "-N", ".", ":!.nz-coder", ":!.nz-coder-runs"], cwd=repo_dir, timeout=30)
    result = _run(["git", "diff", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"], cwd=repo_dir, timeout=30)
    return result.stdout


def _cleanup_completed_worktree(workdir: Path, work_root: Path) -> None:
    """Remove one completed checkout without allowing a broad delete target."""
    root = Path(work_root).resolve()
    candidate = Path(workdir).resolve()
    if candidate.parent != root:
        raise ValueError(
            f"refusing cleanup outside a direct child of work root: {candidate}"
        )
    if not candidate.exists():
        return
    if not candidate.is_dir():
        raise ValueError(f"refusing cleanup of non-directory worktree: {candidate}")
    shutil.rmtree(candidate)


def _cleanup_scratch_files(repo_dir: Path) -> None:
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=repo_dir, capture_output=True, text=True, timeout=30,
    )
    for fname in result.stdout.splitlines():
        if _is_scratch_file(fname):
            path = repo_dir / fname
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass

    result2 = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=A"],
        cwd=repo_dir, capture_output=True, text=True, timeout=30,
    )
    for fname in result2.stdout.splitlines():
        if not _is_scratch_file(fname):
            continue
        subprocess.run(
            ["git", "rm", "--cached", "--force", fname],
            cwd=repo_dir, capture_output=True, text=True, timeout=30,
        )
        path = repo_dir / fname
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def _is_scratch_file(fname: str) -> bool:
    if "/" in fname:
        return False
    lower = fname.lower()
    return lower.startswith("test_") or lower.endswith("_test.py") or lower.endswith("_test.txt")


# ── Prediction file helpers ───────────────────────────────────────────────────

def _write_prediction(pred_file, instance_id: str, model_name: str, result: dict) -> None:
    model_patch = result.get("model_patch", "")
    if result.get("status") == "agent_failed":
        model_patch = ""
    pred_file.write(json.dumps({
        "instance_id": instance_id,
        "model_name_or_path": model_name,
        "model_patch": model_patch,
    }, ensure_ascii=False) + "\n")
    pred_file.flush()


# ── Low-level subprocess helpers ──────────────────────────────────────────────

def _run(cmd: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _decode_timeout_output(exc.stdout)
        stderr = _decode_timeout_output(exc.stderr)
        detail = f"Command timed out after {timeout}s: {' '.join(cmd)}"
        stderr = f"{stderr}\n{detail}".strip()
        return subprocess.CompletedProcess(cmd, 124, stdout, stderr)


def _decode_timeout_output(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _process_result(result: subprocess.CompletedProcess, summary: str) -> dict:
    return {
        "returncode": result.returncode,
        "summary": summary,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def _truncate_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n...<truncated>...\n" + text[-half:]
