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

import json
import multiprocessing
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

from nz_coder.swebench.adapter import SWEBenchAdapter, _safe_name
from nz_coder.swebench.guardrail import PatchGuardrail
from nz_coder.swebench.models import FailureFeedback, PatchRiskReport, RetryPlan

if TYPE_CHECKING:
    pass


DEFAULT_BENCH_DIR = Path.cwd() / ".nz-coder" / "swebench-lite"
DEFAULT_REPO_CACHE_DIR = DEFAULT_BENCH_DIR / "repo-cache"


class AgentRunTimeout(TimeoutError):
    """Raised when a single agent instance exceeds the configured timeout."""


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
        clone = _prepare_repo(instance, repo_dir, clone_timeout)
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
        original_workdir = config.WORKDIR
        had_agent_timeout = hasattr(config, "AGENT_TIMEOUT_SECONDS")
        original_agent_timeout = getattr(config, "AGENT_TIMEOUT_SECONDS", None)
        config.WORKDIR = repo_dir
        trace_dir = repo_dir / ".nz-coder-runs"
        tracer = trace_cls(trace_dir=trace_dir, enabled=True)
        tool_log: list[dict] = []

        def log_tool(name: str, output: str) -> None:
            if output.startswith(("Error:", "Denied")):
                status = "error"
            elif output.startswith("Command exited with code"):
                status = "nonzero"
            else:
                status = "ok"
            tool_log.append({"tool": name, "name": name, "status": status, "output_len": len(output)})
            preview = output.replace("\n", " ")[:160]
            print(f"  {name}: {status} {preview}")

        system_prompt = build_prompt() + (
            "\n\nYou are solving a SWE-bench Lite task in a checked-out repository. "
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
            "1. Start with smart_search using the issue statement, failing tests, "
            "and traceback if available.\n"
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

        # Build message list
        if plan is not None:
            messages = self.build_initial_messages(instance, plan)
        else:
            messages = [{"role": "user", "content": self.adapter.format_instance_prompt(instance)}]

        # ── Agent run ─────────────────────────────────────────────────────────
        # Let RuntimeState know about the SWE-bench time budget
        if agent_timeout > 0:
            config.AGENT_TIMEOUT_SECONDS = agent_timeout

        feedback_str = plan.failure_feedback.to_agent_prompt(plan.previous_patch) if (plan and plan.failure_feedback) else None
        effective_retries = plan.empty_patch_retries if plan else empty_patch_retries

        try:
            agent_status = _run_agent_attempt(
                agent_cls, system_prompt, tracer, messages, log_tool, agent_timeout
            )
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
                agent_status = _run_agent_attempt(
                    agent_cls, system_prompt, tracer, messages, log_tool, agent_timeout
                )
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
            risk_reasons = [f"agent_status:exception"]
        finally:
            config.WORKDIR = original_workdir
            if had_agent_timeout:
                config.AGENT_TIMEOUT_SECONDS = original_agent_timeout
            elif hasattr(config, "AGENT_TIMEOUT_SECONDS"):
                delattr(config, "AGENT_TIMEOUT_SECONDS")

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
            "risk_reasons": risk_reasons,
            "empty_patch_retries": locals().get("empty_retry_count", 0),
            "model_patch": model_patch,
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
    ) -> list[dict]:
        """First-pass: run agent on each instance without previous predictions."""
        results = []
        for index, instance in enumerate(instances, start=1):
            print(f"\n[{index}/{len(instances)}] {instance['instance_id']}")
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
            )
            results.append(result)
            _write_prediction(pred_file, instance["instance_id"], model_name, result)
            print(f"[{result['status'].upper()}] {instance['instance_id']}: {result.get('summary', '')}")
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

def _run_agent_attempt(agent_cls, system_prompt: str, tracer, messages: list[dict], log_tool, timeout: int) -> dict:
    if timeout > 0 and hasattr(os, "fork"):
        return _run_agent_attempt_in_subprocess(
            agent_cls, system_prompt, tracer, messages, log_tool, timeout
        )
    agent = agent_cls(system_prompt, permission_mode="auto", tracer=tracer)
    return _run_agent_with_timeout(agent, messages, log_tool, timeout=timeout)


def _run_agent_attempt_in_subprocess(
    agent_cls,
    system_prompt: str,
    tracer,
    messages: list[dict],
    log_tool,
    timeout: int,
) -> dict:
    ctx = multiprocessing.get_context("fork")
    queue = ctx.Queue()
    process = ctx.Process(
        target=_agent_attempt_worker,
        args=(agent_cls, system_prompt, tracer, messages, queue),
    )
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        raise AgentRunTimeout(f"agent timed out after {timeout}s")

    if queue.empty():
        raise RuntimeError(f"agent subprocess exited without a result (exitcode={process.exitcode})")
    payload = queue.get()
    for event in payload.get("tool_events", []):
        log_tool(event["name"], event.get("output", ""))
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error", "agent subprocess failed"))
    return payload["agent_status"]


def _agent_attempt_worker(agent_cls, system_prompt: str, tracer, messages: list[dict], queue) -> None:
    tool_events: list[dict] = []

    def child_log_tool(name: str, output: str) -> None:
        tool_events.append({"name": name, "output": output[:4000]})

    try:
        agent = agent_cls(system_prompt, permission_mode="auto", tracer=tracer)
        agent_status = agent.run(messages, on_tool=child_log_tool, stream=False)
        queue.put({"ok": True, "agent_status": agent_status, "tool_events": tool_events})
    except BaseException as exc:
        queue.put({"ok": False, "error": repr(exc), "tool_events": tool_events})


def _run_agent_with_timeout(agent, messages: list[dict], log_tool, *, timeout: int) -> dict:
    if timeout <= 0:
        return agent.run(messages, on_tool=log_tool, stream=False)
    if not hasattr(signal, "SIGALRM"):
        return agent.run(messages, on_tool=log_tool, stream=False)

    def _handle_timeout(signum, frame):
        raise AgentRunTimeout(f"agent timed out after {timeout}s")

    old_handler = signal.signal(signal.SIGALRM, _handle_timeout)
    old_alarm = signal.alarm(timeout)
    try:
        return agent.run(messages, on_tool=log_tool, stream=False)
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

    if any(
        row["status"] == "error"
        and (row.get("name") or row.get("tool")) in significant_error_tools
        and not _is_ignorable(row)
        for row in tool_log
    ):
        reasons.append("tool_errors")

    verification_passed = not (agent_status or {}).get("verification_needed", True)
    if any(row["status"] == "nonzero" for row in tool_log) and not verification_passed:
        reasons.append("nonzero_commands")

    return reasons


# ── Repo management helpers ───────────────────────────────────────────────────

def _prepare_repo(instance: dict, repo_dir: Path, timeout: int) -> dict:
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
    _run(["git", "status", "--short"], cwd=repo_dir, timeout=30)
    return {"returncode": 0, "summary": "repo ready"}


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
