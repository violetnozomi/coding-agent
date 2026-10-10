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
from dataclasses import asdict
import hashlib
import json
import multiprocessing
import os
import queue as queue_module
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

from nz_coder.swebench.adapter import SWEBenchAdapter, _safe_name
from nz_coder.swebench.guardrail import PatchGuardrail
from nz_coder.swebench.models import FailureFeedback, PatchRiskReport, RetryPlan
from nz_coder.runtime.core.execution_context import (
    agent_timeout_seconds,
    broad_tests_blocked,
    declared_test_scopes,
    max_agent_turns,
    max_parallel_tasks,
    nominal_agent_turns,
    repo_intelligence_mode,
    repo_retrieval_strategy,
    scoped_broad_test_guard,
    scoped_declared_test_scopes,
    scoped_runtime_overrides,
    strict_local_tools,
)
from nz_coder.runtime.process.workdir import current_workdir, scoped_workdir
from nz_coder.state.sessions import create_session_id
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


def default_swe_work_root(run_id: str, *, workspace: Path | None = None) -> Path:
    """Place ephemeral benchmark checkouts beside, never inside, the workspace."""
    start = Path(workspace or Path.cwd()).resolve()
    root = next(
        (candidate for candidate in (start, *start.parents)
         if (candidate / ".git").exists()),
        start,
    )
    project = _safe_name(root.name).strip(".") or "workspace"
    run = _safe_name(str(run_id)).strip(".") or "run"
    return root.parent / f".nz-coder-swebench-{project}" / "runs" / run


class AgentRunTimeout(TimeoutError):
    """Raised when a single agent instance exceeds the configured timeout."""


class UnstableAgentWorkspace(RuntimeError):
    """Owned execution did not settle; do not label its patch frozen."""


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
        "- Allowed Python verification: python3 -m py_compile | compileall | pytest; "
        "python3 tests/runtests.py <dotted.test.label>; "
        "python3 bin/test <test_name|workspace/test_file.py> --no-colors. "
        f"The prepared command PATH resolves python3 to {shutil.which('python3') or 'unknown'}. "
        "Use the repository-native runner when pytest is unavailable. "
        "Do not use python3 -c, arbitrary scripts, package installation, redirects, command "
        "substitution, or URLs.\n"
        "- web_search and every network tool are unavailable. Git history/remotes, "
        "package installation, absolute or outside-workspace paths, and full test "
        "suites are forbidden; calling them only wastes a turn.\n"
        "- Run relevant workspace-relative targeted tests as needed within the run budget. "
        "Broad pytest and tox remain unavailable on this restricted tool surface.\n"
        "Structured navigation decisions:\n"
        "- If the exact file is unknown, call repo_map once on the smallest relevant "
        "directory, then narrow with grep_search.\n"
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
        if value.startswith("Denied") or "SWE-bench strict mode" in value:
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
        instance = {key: instance[key] for key in ("instance_id", "repo", "base_commit", "problem_statement")}
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
        benchmark_session_id = create_session_id("swe")
        tracer = trace_cls(
            trace_dir=trace_dir,
            enabled=True,
            session_id=benchmark_session_id,
        )
        tool_log: list[dict] = []

        def log_tool(name: str, output: str) -> None:
            status = "unknown"
            tool_log.append({
                "tool": name,
                "name": name,
                "status": status,
                "output_len": len(output),
                "output": output[:512],
                "evidence_kind": "display_callback_not_execution_fact",
            })
            preview = output.replace("\n", " ")[:160]
            print(f"  {name}: {status} {preview}")

        with scoped_workdir(repo_dir):
            system_prompt = build_prompt() + (
                f"\n\nYou are solving a SWE-bench {self.adapter.profile.name.title()} task "
                "in a checked-out repository. "
                "Resolve the stated requirements using repository evidence and focused local verification. "
                "Read and reread relevant files as needed; new source files, reproducers, and appropriate "
                "test changes are allowed. Do not weaken tests to conceal a failure. "
                "After changes, inspect the diff and use verify_changed_files plus relevant tests. "
                "Repeat focused verification when new evidence or a change justifies it. "
                "Static checks alone do not prove the issue is solved. Report missing requirements and "
                "environment blockers honestly; do not install packages or fetch answers. "
                "The inference environment is prepared separately; verify available dependencies rather "
                "than assuming the project is installed or uninstalled. "
                "Use read_tool_result with an opaque artifact ID to retrieve truncated evidence, and "
                "repo_context for bounded local repository queries. Completion review is runtime-owned; "
                "review_run_evidence is an optional read-only advisory tool, not a mandatory step."
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
            "session_id": benchmark_session_id,
        }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        tracer.log(
            "benchmark_instance",
            instance_id=instance_id,
            benchmark_profile=self.adapter.profile.name,
            prompt=messages[0]["content"],
            strict=bool(strict),
            attempts=1,
            session_id=benchmark_session_id,
        )

        # ── Agent run ─────────────────────────────────────────────────────────
        feedback_str = plan.failure_feedback.to_agent_prompt(plan.previous_patch) if (plan and plan.failure_feedback) else None
        effective_retries = plan.empty_patch_retries if plan else empty_patch_retries

        def run_attempt() -> dict:
            agent_kwargs = {"session_id": benchmark_session_id}
            if strict:
                agent_kwargs["tool_allowlist"] = STRICT_ALLOWED_TOOLS
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
                    agent_kwargs=agent_kwargs,
                )

        agent_status = {}
        model_patch = None
        capture_attempted = False
        adapter_error = None
        try:
            agent_status = run_attempt()
            tool_log = _settled_tool_facts(repo_dir) or tool_log
            capture_attempted = True
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

            status = _benchmark_result_status(
                agent_status,
                model_patch=model_patch,
                risk_reasons=risk_reasons,
                blocking_risk=risk_report.has_blocking,
            )
            summary = f"patch_chars={len(model_patch)}, tools={len(tool_log)}"
            if empty_retry_count:
                summary += f", empty_patch_retries={empty_retry_count}"
            if risk_reasons:
                summary += ", risk=" + ",".join(risk_reasons)

        except AgentRunTimeout as exc:
            agent_status = {"status": "timeout", "error": str(exc)}
            try:
                model_patch = _collect_diff(repo_dir)
            except (OSError, UnicodeError, subprocess.SubprocessError) as capture_exc:
                model_patch = None
                agent_status["patch_capture_error"] = str(capture_exc)
            status = "agent_failed"
            summary = str(exc)
            risk_reasons = ["agent_status:timeout"]
        except UnstableAgentWorkspace as exc:
            agent_status = {"status": "error", "error": str(exc)}
            model_patch = None
            status = "agent_failed"
            summary = str(exc)
            risk_reasons = ["execution_scope_not_stopped"]
        except Exception as exc:
            if agent_status:
                adapter_error = str(exc)
            else:
                agent_status = {"status": "exception", "error": str(exc)}
            # 普通运行错误不代表补丁不存在；在进程停止后尽力保留实际工作树。
            if not capture_attempted:
                try:
                    model_patch = _collect_diff(repo_dir)
                except (OSError, UnicodeError, subprocess.SubprocessError) as capture_exc:
                    model_patch = None
                    agent_status["patch_capture_error"] = str(capture_exc)
            status = "adapter_failed" if adapter_error else "agent_failed"
            summary = str(exc)
            risk_reasons = ["adapter_exception" if adapter_error else "agent_status:exception"]
        tool_log = _settled_tool_facts(repo_dir) or tool_log
        return {
            "instance_id": instance_id,
            "repo": instance.get("repo"),
            "base_commit": instance.get("base_commit"),
            "initial_state": clone.get("initial_state"),
            "status": status,
            "summary": summary,
            "agent_status": agent_status,
            "adapter_error": adapter_error,
            "session_id": locals().get("benchmark_session_id", ""),
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
            "model_patch": model_patch or "",
            "patch_status": ("unstable" if "execution_scope_not_stopped" in risk_reasons else "not_captured") if model_patch is None else ("present" if model_patch else "empty"),
            "patch_sha256": hashlib.sha256(model_patch.encode("utf-8")).hexdigest() if model_patch is not None else None,
            "eval_status": "pending" if model_patch else "not_run",
            "official_resolved": None,
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
        completed_ids = attempt_journal.completed_ids() if attempt_journal else set()
        open_claims = attempt_journal.attempted_ids() - completed_ids if attempt_journal else set()
        for index, instance in enumerate(instances, start=1):
            if instance["instance_id"] in completed_ids:
                print(f"[RESUME] {instance['instance_id']}: durable result already recorded; skipping.")
                continue
            if instance["instance_id"] in open_claims:
                # 无法证明 claim 后没有请求：不重建工作树，不启动第二次解题。
                results.append({"instance_id": instance["instance_id"], "status": "interrupted",
                    "agent_status": {"status": "interrupted"}, "patch_status": "not_captured",
                    "eval_status": "not_run", "official_resolved": None,
                    "workdir": str(work_root / _safe_name(instance["instance_id"])),
                    "summary": "open claim: prior inference state is unknown; restart refused"})
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
                # 原生结束状态和补丁验收分开；耗尽轮次不能抹掉已经形成的补丁。
                attempt_journal.record({
                    "instance_id": instance["instance_id"],
                    "attempt": 1,
                    "status": result.get("status"),
                    "agent_status": result.get("agent_status"),
                    "patch_status": result.get("patch_status"),
                    "patch_sha256": result.get("patch_sha256"),
                    "eval_status": result.get("eval_status", "not_run"),
                    "official_resolved": None,
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
                            "session_id": result.get("session_id"),
                            "model_patch": result.get("model_patch", ""),
                            "patch_sha256": result.get("patch_sha256"),
                        },
                        budget=trace_budget,
                    )
                    result["trace_archive"] = str(archived.bundle_path)
                    result["trace_archive_bytes"] = archived.used_bytes
                    result["diagnostic_cleanup_safe"] = archived.cleanup_safe
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
                if result.get("workdir") and result.get("diagnostic_cleanup_safe") and result.get("patch_status") != "unstable":
                    _cleanup_completed_worktree(
                        Path(str(result["workdir"])),
                        work_root,
                    )
                    result["workdir_cleaned"] = True
                elif result.get("workdir"):
                    result["cleanup_skipped"] = "owned Session diagnostic archive unavailable or execution unstable"
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
    if timeout > 0:
        return _run_agent_attempt_in_subprocess(
            agent_cls, system_prompt, tracer, messages, log_tool, timeout,
            agent_kwargs=agent_kwargs,
        )
    agent = agent_cls(
        system_prompt, permission_mode="auto", tracer=tracer, **(agent_kwargs or {})
    )
    _bind_attempt_evidence(agent)
    try:
        return _run_agent_with_timeout(agent, messages, log_tool, timeout=timeout)
    finally:
        _close_attempt_environment(agent)


def _run_agent_attempt_in_subprocess(
    agent_cls,
    system_prompt: str,
    tracer,
    messages: list[dict],
    log_tool,
    timeout: int,
    agent_kwargs: dict | None = None,
) -> dict:
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue()
    stop_event = ctx.Event()
    execution_snapshot = {
        "workdir": str(current_workdir()),
        "runtime_overrides": {
            "max_agent_turns": max_agent_turns(),
            "nominal_agent_turns": nominal_agent_turns(),
            "agent_timeout_seconds": agent_timeout_seconds(),
            "max_parallel_tasks": max_parallel_tasks(),
            "strict_local_tools": strict_local_tools(),
            "repo_intelligence_mode": repo_intelligence_mode(),
            "repo_retrieval_strategy": repo_retrieval_strategy(),
        },
        "broad_tests_blocked": broad_tests_blocked(),
        "declared_test_scopes": declared_test_scopes(),
    }
    process = ctx.Process(
        target=_agent_attempt_worker,
        args=(
            agent_cls,
            system_prompt,
            tracer,
            messages,
            result_queue,
            execution_snapshot,
            agent_kwargs,
            stop_event,
        ),
    )
    process.start()
    descendants = {}
    execution_stopped = False
    try:
        # Drain the result before joining.  multiprocessing.Queue writes from a
        # feeder thread, so joining first deadlocks once the payload exceeds the
        # OS pipe buffer: the child waits for the feeder while the parent waits
        # for the child.  Full tool output already lives in the trace artifact;
        # this channel carries only the bounded result projection below.
        try:
            deadline = time.monotonic() + timeout
            while True:
                descendants.update(_linux_owned_descendants(process.pid))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise queue_module.Empty
                try:
                    payload = result_queue.get(timeout=min(.05, remaining))
                    break
                except queue_module.Empty:
                    if not process.is_alive():
                        raise
        except queue_module.Empty:
            if process.is_alive():
                if not _stop_agent_process(process, stop_event, descendants):
                    raise UnstableAgentWorkspace("timeout: attempt descendants did not stop")
                execution_stopped = True
                raise AgentRunTimeout(f"agent timed out after {timeout}s")
            process.join()
            if not _stop_agent_process(process, stop_event, descendants):
                raise UnstableAgentWorkspace("worker exited but descendants did not stop")
            execution_stopped = True
            raise RuntimeError(
                "agent subprocess exited without a result "
                f"(exitcode={process.exitcode})"
            )

        process.join(5)
        if not _stop_agent_process(process, stop_event, descendants):
            raise UnstableAgentWorkspace("attempt cleanup did not stop owned execution")
        execution_stopped = True
        for event in payload.get("tool_events", []):
            log_tool(event["name"], event.get("output", ""))
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error", "agent subprocess failed"))
        return payload["agent_status"]
    finally:
        try:
            if not execution_stopped and not _stop_agent_process(process, stop_event, descendants):
                raise UnstableAgentWorkspace("attempt interrupted with active descendants")
        finally:
            result_queue.close()
            result_queue.join_thread()


def _stop_agent_process(process, stop_event=None, descendants=None) -> bool:
    """先取消 Runtime；Linux 兜底捕获本 worker 后代，包含另起进程组的 Bash。"""
    from nz_coder.runtime.process.platform_runtime import terminate_process_tree

    descendants = {**(descendants or {}), **_linux_owned_descendants(process.pid)}
    if stop_event is not None:
        stop_event.set()
        process.join(2)
    descendants.update(_linux_owned_descendants(process.pid))
    if os.name == "nt" and process.is_alive():
        terminate_process_tree(process, force=True)
    for pid, identity in descendants.items():
        if _linux_process_identity(pid) == identity:
            owned = SimpleNamespace(pid=pid, kill=lambda pid=pid: os.kill(pid, signal.SIGKILL))
            terminate_process_tree(owned, force=True)
    if not process.is_alive():
        process.join()
    else:
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        if all(_linux_process_identity(pid) != identity for pid, identity in descendants.items()):
            return not process.is_alive()
        time.sleep(.01)
    return False


def _linux_process_identity(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else fields[19]
    except (OSError, IndexError):
        return None


def _linux_owned_descendants(root_pid: int) -> dict[int, str]:
    if not Path("/proc").is_dir():
        return {}
    children = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                children[int(path.parent.name)] = (int(fields[1]), fields[19])
        except (OSError, ValueError, IndexError):
            continue
    owned = {root_pid}
    while next_ids := {pid for pid, (parent, _) in children.items() if parent in owned} - owned:
        owned.update(next_ids)
    return {pid: children[pid][1] for pid in owned - {root_pid}}


def _agent_attempt_worker(
    agent_cls, system_prompt: str, tracer, messages: list[dict], queue,
    execution_snapshot: dict,
    agent_kwargs: dict | None = None,
    stop_event=None,
) -> None:
    tool_events: list[dict] = []

    def child_log_tool(name: str, output: str) -> None:
        tool_events.append({"name": name, "output": output[:512]})

    try:
        with (
            scoped_workdir(execution_snapshot["workdir"]),
            scoped_runtime_overrides(**execution_snapshot["runtime_overrides"]),
            scoped_broad_test_guard(execution_snapshot["broad_tests_blocked"]),
            scoped_declared_test_scopes(execution_snapshot["declared_test_scopes"]),
        ):
            agent = agent_cls(
                system_prompt, permission_mode="auto", tracer=tracer,
                **(agent_kwargs or {}),
            )
            _bind_attempt_evidence(agent)
            try:
                agent_status = asyncio.run(_run_cancellable_attempt(agent, messages, child_log_tool, stop_event))
            finally:
                _close_attempt_environment(agent)
            queue.put({
                "ok": True,
                "agent_status": agent_status,
                "tool_events": tool_events,
            })
    except BaseException as exc:
        queue.put({"ok": False, "error": repr(exc), "tool_events": tool_events})


async def _run_cancellable_attempt(agent, messages, log_tool, stop_event):
    task = asyncio.create_task(agent.run(messages, on_tool=log_tool, stream=False))
    try:
        while not task.done():
            if stop_event is not None and stop_event.is_set():
                context = getattr(agent, "active_run_context", None)
                if context is not None:
                    context.cancellation = stop_event
                task.cancel()
                break
            await asyncio.wait({task}, timeout=.02)
        return await task
    finally:
        if not task.done():
            task.cancel()


def _close_attempt_environment(agent) -> None:
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    from nz_coder.runtime.process.process_service import close_workspace_process_service

    try:
        if isinstance(agent, ProductRunEnvironment):
            agent.close()
    finally:
        close_workspace_process_service(current_workdir())


def _bind_attempt_evidence(agent) -> None:
    from nz_coder.runtime.execution.loop import ProductRunEnvironment

    if not isinstance(agent, ProductRunEnvironment):
        return
    directory = current_workdir() / ".nz-coder-runs"
    directory.mkdir(exist_ok=True)
    original = agent._model_gateway_observer
    calls = {}
    call_lock = threading.Lock()

    def append(name, payload):
        encoded = (json.dumps({"event": name, "session_id": agent.session_id, **payload},
                              ensure_ascii=False, default=str) + "\n").encode("utf-8")
        descriptor = os.open(directory / "execution-facts.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def observer(name, payload):
        if name in {"model_call_start", "model_call_finish"}:
            # 实际 Gateway 边界的保守启动证据；不声称 start 一定已经到达 Provider。
            key = (threading.get_ident(), str(payload.get("purpose")))
            with call_lock:
                if name == "model_call_start":
                    calls[key] = uuid.uuid4().hex
                identity = calls.get(key)
            append(name, {**payload, "call_id": identity})
        original(name, payload)

    def tool_result(context):
        append("tool_execution_result", {"result": asdict(context.result),
            "mutation_generation": context.loop.runtime_state.mutation_generation,
            "verification_generation": context.loop.runtime_state.verification_generation})

    agent._model_gateway_observer = observer
    agent.hooks.register_after_tool_result(tool_result)


def _settled_tool_facts(workspace: Path) -> list[dict]:
    path = workspace / ".nz-coder-runs" / "execution-facts.jsonl"
    if not path.is_file():
        return []
    facts = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            # 中断可能留下半条记录，不能把缺失事实补写成成功。
            continue
        if row.get("event") != "tool_execution_result":
            continue
        result = row["result"]
        status = "policy_rejected" if result["permission_denied"] else (
            "error" if result["dispatch_failed"] else "nonzero" if result["command_failed"] else
            "ok" if result["executed"] else "unknown")
        facts.append({"tool": result["name"], "name": result["name"], "status": status,
                      "generation": row["mutation_generation"], "executed": result["executed"],
                      "dispatch_failed": result["dispatch_failed"], "command_failed": result["command_failed"]})
    return facts


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

def _benchmark_result_status(
    agent_status: dict,
    *,
    model_patch: str,
    risk_reasons: list[str],
    blocking_risk: bool = False,
) -> str:
    """Preserve terminal runtime failures instead of reporting empty work."""
    terminal = str((agent_status or {}).get("status") or "")
    if terminal in {
        "aborted", "error", "cancelled", "timeout", "exception", "max_turns",
    }:
        return "agent_failed"
    if not str(model_patch or "").strip():
        return "empty_patch"
    if blocking_risk:
        return "agent_failed"
    if (agent_status or {}).get("verification_needed"):
        if (
            (agent_status or {}).get("verification_state")
            == "blocked_environment"
            and terminal == "completed_unverified"
        ):
            return "risky"
        return "agent_failed"
    return "risky" if risk_reasons else "completed"


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
    cache_dir = _ensure_repo_cache(repo, url, timeout)
    clone_url = str(cache_dir) if cache_dir is not None else url
    clone = _run(["git", "clone", "--quiet", clone_url, str(repo_dir)], cwd=repo_dir.parent, timeout=timeout)
    if clone.returncode != 0 and clone_url != url:
        shutil.rmtree(repo_dir, ignore_errors=True)
        clone = _run(["git", "clone", "--quiet", url, str(repo_dir)], cwd=repo_dir.parent, timeout=timeout)
    if clone.returncode != 0:
        return _process_result(clone, f"git clone failed for {repo}")
    checkout = _run(["git", "checkout", "--quiet", instance["base_commit"]], cwd=repo_dir, timeout=timeout)
    if checkout.returncode != 0:
        return _process_result(checkout, f"git checkout failed for {instance['base_commit']}")
    original_tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo_dir, timeout=30).stdout.strip()
    if sanitize_history:
        sanitized = _reinitialize_repo_at_base(repo_dir, timeout)
        if sanitized.returncode != 0:
            return _process_result(sanitized, "failed to sanitize post-base Git history")
    _run(["git", "status", "--short"], cwd=repo_dir, timeout=30)
    local_head = _run(["git", "rev-parse", "HEAD"], cwd=repo_dir, timeout=30).stdout.strip()
    local_tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo_dir, timeout=30).stdout.strip()
    if not original_tree or local_tree != original_tree:
        return {"returncode": 2, "summary": "sanitized checkout changed the original source tree"}
    return {"returncode": 0, "summary": "repo ready", "initial_state": {
        "original_base_commit": instance["base_commit"], "local_head": local_head,
        "original_tree": original_tree, "local_tree": local_tree}}


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


def _ensure_repo_cache(repo: str, url: str, timeout: int) -> Path | None:
    """Return a local mirror, creating it atomically on the first checkout.

    Concurrent batch workers clone into independent temporary directories.  A
    single rename publishes the complete mirror; a worker that loses that race
    discards its staging directory and reuses the winner.  Cache setup is an
    optimization, so filesystem or clone failures fall back to the existing
    direct remote-clone path.
    """
    cache_dir = _repo_cache_dir(repo)
    if cache_dir.exists():
        return cache_dir
    try:
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f".{cache_dir.name}-",
            dir=cache_dir.parent,
        ) as staging:
            mirror_dir = Path(staging) / "repo.git"
            cloned = _run(
                ["git", "clone", "--mirror", "--quiet", url, str(mirror_dir)],
                cwd=cache_dir.parent,
                timeout=timeout,
            )
            if cloned.returncode != 0:
                return None
            try:
                mirror_dir.rename(cache_dir)
            except OSError:
                if not cache_dir.exists():
                    return None
    except OSError:
        return None
    return cache_dir if cache_dir.exists() else None


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
    # 临时索引捕获最终字节：包含暂存/未暂存和新增文件，不改真实索引或按文件名删产物。
    with tempfile.TemporaryDirectory(prefix="nz-swe-diff-") as directory:
        index = Path(directory) / "index"
        actual_index = repo_dir / ".git" / "index"
        if actual_index.is_file():
            shutil.copyfile(actual_index, index)
        environment = {**os.environ, "GIT_INDEX_FILE": str(index)}
        for command in (
            ["git", "add", "-A", "--", ".", ":!.nz-coder", ":!.nz-coder-runs"],
            ["git", "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD",
             "--", ".", ":!.nz-coder", ":!.nz-coder-runs"],
        ):
            result = subprocess.run(command, cwd=repo_dir, env=environment, capture_output=True,
                                    text=True, encoding="utf-8", timeout=30, check=True)
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


# ── Prediction file helpers ───────────────────────────────────────────────────

def _write_prediction(pred_file, instance_id: str, model_name: str, result: dict) -> None:
    model_patch = result.get("model_patch", "")
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
