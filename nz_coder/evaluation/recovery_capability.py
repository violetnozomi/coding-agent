"""Controlled fault-injection evidence for production recovery components."""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
import shlex
import sys
import time
from unittest.mock import patch

from nz_coder.runtime.verification.domain.service import VerificationDomain
from nz_coder.runtime.verification.domain.models import RunScope, TaskScope, WorkspaceVersion, ExecutionFact
from nz_coder.runtime.adapters.verification_plans import register_verification_plan
from nz_coder.runtime.verification.domain.identity import command_identity
import threading
from nz_coder.lsp.write_diagnostics import collect_write_diagnostics
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.agent.agent_resilience import ProviderAttemptController
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.runtime.core.run_settings import current_run_settings, scoped_run_settings
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.transaction import TransactionManager
from nz_coder.tools.bash import run_bash


class _Tracer:
    def log(self, *_args, **_kwargs) -> None:
        return None


class _Unrenderable:
    def __str__(self) -> str:
        raise ValueError("malformed result fixture")


def _tool_call(name: str, arguments: str = "{}") -> dict:
    return {"id": f"fault-{name}", "function": {"name": name, "arguments": arguments}}


def run_recovery_fault_injection_suite(output_dir: Path) -> dict:
    """Exercise deterministic failure paths without claiming model intelligence."""
    target = Path(output_dir).resolve()
    workspace = target / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []

    timeout_marker = workspace / "timeout-marker.txt"
    command = (
        f"{shlex.quote(sys.executable)} -c \"import time; from pathlib import Path; "
        "time.sleep(2); Path('timeout-marker.txt').write_text('leaked')\""
    )
    with scoped_workdir(workspace):
        timeout_result = run_bash(command, timeout=1)
    time.sleep(1.2)
    runs.append({
        "case_id": "R1", "failure": "tool_timeout", "expected_action": "kill_and_report",
        "observed_action": "kill_and_report" if (
            "timed out" in str(timeout_result).casefold() and not timeout_marker.exists()
        ) else "incorrect",
        "evidence": str(timeout_result),
    })

    executor = ToolExecutor(PermissionManager("auto"))
    with patch("nz_coder.runtime.execution.tool_executor.dispatch", side_effect=RuntimeError("fault fixture")):
        tool_exception = executor.execute_one(_tool_call("read_file"), 0)
    runs.append({
        "case_id": "R2", "failure": "tool_exception", "expected_action": "return_repair_evidence",
        "observed_action": "return_repair_evidence" if (
            tool_exception.dispatch_failed
            and tool_exception.output == (
                "Error: read_file failed: An internal error occurred."
            )
            and "fault fixture" not in tool_exception.output
        ) else "incorrect",
        "evidence": tool_exception.output,
    })

    decision = ProviderAttemptController(max_retries=2).decide(
        TimeoutError("stream stalled"), attempt=1, streaming=True,
        stable_boundary=False, retryable=True,
    )
    runs.append({
        "case_id": "R3", "failure": "provider_interruption",
        "expected_action": "non_streaming_fallback", "observed_action": decision.action,
        "evidence": decision.reason,
    })

    partial = workspace / "partial.txt"
    partial.write_text("before\n", encoding="utf-8")
    with scoped_workdir(workspace):
        transaction = TransactionManager()
        transaction.begin()
        transaction.track("partial.txt")
        from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
        WorkspaceFileAccess(workspace).write_text("partial.txt", "partial\n", transaction=transaction)
        rollback = transaction.rollback()
    runs.append({
        "case_id": "R4", "failure": "partial_write", "expected_action": "rollback",
        "observed_action": "rollback" if partial.read_text(encoding="utf-8") == "before\n" else "incorrect",
        "evidence": rollback,
    })

    with patch("nz_coder.runtime.execution.tool_executor.dispatch", return_value=_Unrenderable()):
        malformed = executor.execute_one(_tool_call("read_file"), 0)
    runs.append({
        "case_id": "R5", "failure": "malformed_tool_result",
        "expected_action": "return_repair_evidence",
        "observed_action": "return_repair_evidence" if malformed.metadata.get("malformed_result") else "incorrect",
        "evidence": malformed.output,
    })

    source = workspace / "app.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    diagnostic_settings = replace(
        current_run_settings(),
        lsp_enabled=True,
        lsp_write_diagnostics_enabled=True,
    )
    with scoped_run_settings(diagnostic_settings), patch(
        "nz_coder.lsp.write_diagnostics.get_client_for_file",
        side_effect=RuntimeError("LSP unavailable"),
    ):
        diagnostics = collect_write_diagnostics(["app.py"], workspace)
    runs.append({
        "case_id": "R6", "failure": "lsp_unavailable", "expected_action": "optional_fallback",
        "observed_action": "optional_fallback" if (diagnostics["reports"] and
            diagnostics["reports"][0]["status"] == "unavailable" and source.read_text() == "VALUE = 1\n") else "incorrect",
        "evidence": "write remains committed; diagnostics explicitly unavailable",
    })

    plan = {
        "stages": [{
            "name": "targeted", "required": True,
            "commands": [{"command": "pytest -q", "required": True}],
        }],
    }
    scope = RunScope(TaskScope("recovery-eval", "unavailable-command", str(workspace)), "run", "main")
    version = WorkspaceVersion(1, 1, 1)
    domain = VerificationDomain(lock=threading.RLock(), scope=scope, version=version, require_targeted=True)
    register_verification_plan(domain, plan)
    id = domain.snapshot().specs[0].id
    domain.begin_execution("command-not-found", (id,))
    domain.observe_execution(ExecutionFact("command-not-found", scope, command_identity("pytest -q", str(workspace)),
        "failed", version, version, capabilities=("test",), exit_code=127,
        environment_error=True, reason="pytest: command not found"))
    verification = domain.snapshot().to_dict()
    runs.append({
        "case_id": "R7", "failure": "verification_command_unavailable",
        "expected_action": "blocked_environment",
        "observed_action": verification["verification_state"],
        "evidence": verification.get("environment_blocker"),
    })

    passed = sum(run["observed_action"] == run["expected_action"] for run in runs)
    result = {
        "benchmark_version": 1,
        "suite_type": "controlled-recovery-fault-injection",
        "evidence_kind": "mechanism-not-model-intelligence",
        "runs": runs,
        "passed": passed,
        "total": len(runs),
        "success_rate": passed / len(runs),
    }
    target.mkdir(parents=True, exist_ok=True)
    (target / "recovery-fault-injection.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8",
    )
    return result


__all__ = ["run_recovery_fault_injection_suite"]
