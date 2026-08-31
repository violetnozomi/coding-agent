"""Controlled fault-injection evidence for production recovery components."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys
import time
from unittest.mock import patch

from nz_coder.intelligence.verification import VerificationManager
from nz_coder.lsp.write_diagnostics import collect_write_diagnostics
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.runtime.agent.agent_resilience import ProviderAttemptController
from nz_coder.runtime.execution.tool_executor import ToolExecutor
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
            tool_exception.dispatch_failed and "fault fixture" in tool_exception.output
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
        partial.write_text("partial\n", encoding="utf-8")
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
    with patch(
        "nz_coder.lsp.write_diagnostics.config.LSP_ENABLED", True,
    ), patch(
        "nz_coder.lsp.write_diagnostics.config.LSP_WRITE_DIAGNOSTICS_ENABLED", True,
    ), patch(
        "nz_coder.lsp.write_diagnostics.get_client_for_file",
        side_effect=RuntimeError("LSP unavailable"),
    ):
        diagnostics = collect_write_diagnostics(["app.py"], workspace)
    runs.append({
        "case_id": "R6", "failure": "lsp_unavailable", "expected_action": "optional_fallback",
        "observed_action": "optional_fallback" if diagnostics == "" else "incorrect",
        "evidence": "write remains committed; diagnostics omitted",
    })

    plan = {
        "stages": [{
            "name": "targeted", "required": True,
            "commands": [{"command": "pytest -q", "required": True}],
        }],
    }
    manager = VerificationManager(RecoveryState(), _Tracer(), plan_builder=lambda _paths: plan)
    manager.mark_write("write_file", {"path": "app.py"})
    manager.observe_bash(
        {"command": "pytest -q"},
        "Command exited with code 127\npytest: command not found",
        dispatch_failed=False, command_failed=True,
        exit_code=127,
    )
    verification = manager.status()
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
