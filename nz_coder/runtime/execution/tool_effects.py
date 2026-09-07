"""Focused tool facts, result observations, transaction finish and committed effects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nz_coder.runtime.core.tool_contracts import ToolTrace
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.tool_platform.execution import ToolExecutionResult
from nz_coder.runtime.agent.child_result import ChildAgentResult
from nz_coder.runtime.agent.lineage import SessionLineage
from nz_coder.runtime.agent.admission import AdmissionInvariantSession
from nz_coder.runtime.verification.recovery import RecoveryState
from nz_coder.intelligence.verification import VerificationManager
from nz_coder.intelligence.code_index import IndexStats
from nz_coder.runtime.observability.run_evidence import RunEvidence
from nz_coder.state.changes import ChangeTracker
from nz_coder.state.skills import SkillLoader
from nz_coder.state.transaction import TransactionManager
from nz_coder.runtime.core.execution_context import (
    broad_tests_blocked,
    set_broad_tests_blocked,
)
from nz_coder.runtime.core.execution_context import strict_local_tools
from nz_coder.state.workdir import current_workdir
from nz_coder.tool_platform.policies.command_policy import classify_bash
from nz_coder.tool_platform.permissioning.interaction import format_tool_summary
from nz_coder.runtime.execution.tool_executor import tool_category
from nz_coder.protocol.shell_diagnostics import shell_output_facts
from nz_coder.protocol.message_schema import stamp_user_message
from nz_coder.tools import (
    collect_filesystem_mutation_paths,
    is_filesystem_mutation_tool,
)


@dataclass
class ToolRunFacts:
    """Single mutable owner; legacy Product fields are projections of these values."""

    tool_calls_this_run: int = 0
    used_save_memory: bool = False
    sidecar_risky_shell_ops: int = 0
    sidecar_unattributed_write_ops: int = 0
    last_terminal_summary: str = ""

    def set_terminal_summary(self, summary: str) -> None:
        self.last_terminal_summary = summary


class ToolTransaction:
    """Settle the injected manager; its partial recovery state stays authoritative."""

    def __init__(self, txn: TransactionManager, trace: ToolTrace) -> None:
        if txn is None:
            raise TypeError("ToolTransaction requires a transaction manager")
        self.txn = txn
        self.trace = trace

    @property
    def active(self) -> bool:
        return self.txn.active

    def begin(self) -> None:
        self.txn.begin()

    def finish(self, has_write: bool, all_succeeded: bool, messages: list) -> None:
        """根据工具分发结果提交或回滚事务。"""
        if not has_write:
            return
        if all_succeeded:
            self.txn.commit()
            return
        rollback_report = self.txn.rollback()
        if not rollback_report:
            return
        self.trace("transaction_rollback", report=rollback_report)
        messages.append(
            stamp_user_message(
                {
                    "role": "user",
                    "content": f"<transaction-rollback>\n{rollback_report}\n</transaction-rollback>",
                    "_nz_synthetic": True,
                }
            )
        )


class ToolResultRecorder:
    """Coordinate execution observations, never reinterpret them as committed effects."""

    def __init__(
        self,
        *,
        facts: ToolRunFacts,
        vm: VerificationManager,
        runtime_state: RuntimeState,
        scratchpad,
        skills: SkillLoader,
        run_evidence: RunEvidence,
        admission: AdmissionInvariantSession | None,
        lineage: SessionLineage,
        run_id: str,
        trace: ToolTrace,
        publish: Callable[[str, dict], None],
    ) -> None:
        self.facts = facts
        self.vm = vm
        self.runtime_state = runtime_state
        self.scratchpad = scratchpad
        self.skills = skills
        self.run_evidence = run_evidence
        self.admission = admission
        self.lineage = lineage
        self.run_id = run_id
        self.trace = trace
        self.publish = publish

    def strict_completed(self, dispatched: list) -> bool:
        verification = self.vm.status()
        if (
            not strict_local_tools()
            or not self.runtime_state.strict_generation_terminal_ready()
            or verification.get("verification_needed")
            or verification.get("verification_state") not in {"passed", "degraded"}
        ):
            return False
        self.facts.last_terminal_summary = (
            "Changed-file verification passed for a non-empty source diff; "
            "the strict SWE-bench patch is finalized."
        )
        self.trace(
            "strict_verification_terminal",
            changed_files=list(self.runtime_state.changed_files),
            diff_chars=self.runtime_state.diff_chars,
            mutation_generation=self.runtime_state.mutation_generation,
        )
        return True

    def record_result(self, result_r: ToolExecutionResult) -> bool:
        """观察工具结果并更新 verification/scratchpad/runtime 状态。"""
        if result_r.executed and not result_r.dispatch_failed:
            self.facts.tool_calls_this_run += 1
            if result_r.name == "save_memory":
                self.facts.used_save_memory = True
            if result_r.name == "bash":
                command = str((result_r.tool_input or {}).get("command") or "")
                classification = classify_bash(command)
                if classification.get("dangerous") or classification.get("mutating"):
                    self.facts.sidecar_risky_shell_ops += 1
            if (
                result_r.is_write
                and not str((result_r.tool_input or {}).get("path") or "").strip()
            ):
                self.facts.sidecar_unattributed_write_ops += 1
        self._observe_write_tool(result_r)
        self._observe_verification_tool(result_r)
        self._observe_runtime_tool(result_r)
        self._observe_run_evidence(result_r)
        if self.admission is not None:
            self.admission.record_tool_result(result_r)
        return result_r.dispatch_failed

    def _observe_write_tool(self, result_r: ToolExecutionResult) -> None:
        """写工具成功后更新验证状态并激活路径相关 skill。"""
        if not (
            result_r.is_write and result_r.executed and not result_r.dispatch_failed
        ):
            return
        self.vm.mark_write(
            result_r.name,
            result_r.tool_input,
            output=result_r.output,
        )
        edited_path = result_r.tool_input.get("path", "")
        if not edited_path:
            return
        activated = self.skills.activate_for_paths(
            [str(current_workdir() / edited_path)]
        )
        if activated:
            self.trace("skills_activated", names=activated)

    def _observe_verification_tool(self, result_r: ToolExecutionResult) -> None:
        """根据 bash / symbol check / verify 工具结果更新验证状态。"""
        if result_r.executed and result_r.name == "bash":
            self.vm.observe_bash(
                result_r.tool_input,
                result_r.output,
                result_r.dispatch_failed,
                result_r.command_failed,
                exit_code=(result_r.metadata or {}).get("exit"),
            )
            self._record_bash_failure(result_r)
        if (
            result_r.executed
            and not result_r.dispatch_failed
            and result_r.name == "python_symbol_check"
        ):
            self.vm.observe_symbol_check(result_r.output, result_r.tool_input)
        if (
            result_r.executed
            and not result_r.dispatch_failed
            and result_r.name == "verify_changed_files"
        ):
            self.vm.observe_verify_changed_files(result_r.output)

    def _record_bash_failure(self, result_r: ToolExecutionResult) -> None:
        """把失败测试摘要写入 scratchpad，减少同一 session 内重复踩坑。"""
        if not result_r.command_failed:
            return
        from nz_coder.runtime.verification.recovery import (
            _extract_failed_tests,
            _extract_traceback,
        )

        failed = _extract_failed_tests(result_r.output)
        tb = _extract_traceback(result_r.output, max_chars=300)
        if not failed and not tb:
            return
        note = ""
        if failed:
            note += "Failed: " + ", ".join(failed[:3])
        if tb:
            first_line = tb.splitlines()[-1][:120] if tb.splitlines() else ""
            note += (" | " if note else "") + first_line
        if note:
            self.scratchpad.update("failure", note[:500])

    def _observe_runtime_tool(self, result_r: ToolExecutionResult) -> None:
        """把成功工具调用写入 RuntimeState。"""
        if not (result_r.executed and not result_r.dispatch_failed):
            return
        acceptance = self.runtime_state.observe_tool(
            result_r.name,
            result_r.tool_input,
            result_r.output,
            succeeded=not result_r.command_failed,
        )
        if acceptance is not None:
            self.vm.observe_acceptance_contract(
                acceptance["command"],
                acceptance["output"],
                passed=acceptance["passed"],
            )
        if self.runtime_state.has_diff and not broad_tests_blocked():
            set_broad_tests_blocked(True)

    def _observe_run_evidence(self, result_r: ToolExecutionResult) -> None:
        """Best-effort record structured evidence without affecting loop control."""
        try:
            self.run_evidence.task_mode = self.runtime_state.task_mode
            self.run_evidence.record_tool_result(
                result_r.name,
                result_r.tool_input,
                result_r.output,
                success=(
                    result_r.executed
                    and not result_r.dispatch_failed
                    and not result_r.command_failed
                ),
                dispatch_failed=result_r.dispatch_failed,
                command_failed=result_r.command_failed,
                metadata=result_r.metadata,
            )
            metadata = result_r.metadata if isinstance(result_r.metadata, dict) else {}
            child_outcome = ChildAgentResult.from_metadata(
                metadata,
                final_text=str(result_r.output or ""),
                name=str(result_r.name or "child"),
            )
            if (
                result_r.name in {"task", "apply_agent_changes"}
                and child_outcome is not None
            ):
                self.lineage.append(
                    "child_outcome",
                    {
                        "task_id": child_outcome.task_id,
                        "name": child_outcome.name,
                        "session_id": child_outcome.session_id,
                        "agent_id": child_outcome.agent_id,
                        "trace_id": child_outcome.trace_id,
                        "status": child_outcome.status,
                        "changed_files": list(child_outcome.changed_files),
                        "structured": child_outcome.structured_present,
                        "limit_reached": child_outcome.limit_reached,
                        "interrupted": child_outcome.interrupted,
                    },
                )
            self._record_lineage_artifact(result_r)
        except Exception as exc:
            self.trace("run_evidence_failed", tool=result_r.name, error=str(exc))

    def _record_lineage_artifact(self, result_r: ToolExecutionResult) -> None:
        """Record bounded file, command, and attachment provenance for recovery."""
        if not result_r.executed or result_r.dispatch_failed:
            return
        payload: dict = {
            "tool": str(result_r.name)[:120],
            "action": "write" if result_r.is_write else "observe",
        }
        tool_input = (
            result_r.tool_input if isinstance(result_r.tool_input, dict) else {}
        )
        paths: list[str] = []
        path = tool_input.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
        for key in ("files", "changes"):
            values = tool_input.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                candidate = item.get("path") if isinstance(item, dict) else item
                if (
                    isinstance(candidate, str)
                    and candidate.strip()
                    and candidate not in paths
                ):
                    paths.append(candidate.strip())
        if paths:
            payload["paths"] = paths[:50]
        if result_r.name == "bash" and isinstance(tool_input.get("command"), str):
            payload["command"] = str(tool_input["command"])[:1000]
            payload["status"] = "failed" if result_r.command_failed else "passed"
        attachments = (
            result_r.attachments if isinstance(result_r.attachments, list) else []
        )
        if attachments:
            payload["attachments"] = [
                {
                    "mime": str(item.get("mime") or "")[:200],
                    "filename": str(item.get("filename") or "")[:300],
                }
                for item in attachments[:20]
                if isinstance(item, dict)
            ]
        if not any(key in payload for key in ("paths", "command", "attachments")):
            return
        artifact_key = (
            f"{self.run_id}:{self.facts.tool_calls_this_run}:"
            f"{result_r.name}:{hashlib.sha256(json.dumps(tool_input, sort_keys=True, default=str).encode()).hexdigest()[:16]}"
        )
        self.lineage.append_unique("artifact_ledger", artifact_key, payload)

    def trace_result(
        self,
        result_r: ToolExecutionResult,
        output: str,
        tool_call_id: str = "",
        index: int | None = None,
    ) -> None:
        """记录工具调用 trace。"""
        self.trace(
            "tool_call",
            tool_call_id=tool_call_id or None,
            index=index,
            name=result_r.name,
            status=(
                "error"
                if output.startswith("Error:") or output.startswith("Denied")
                else (
                    "nonzero" if output.startswith("Command exited with code") else "ok"
                )
            ),
            executed=bool(result_r.executed),
            dispatch_failed=bool(result_r.dispatch_failed),
            command_failed=bool(result_r.command_failed),
            is_write=bool(result_r.is_write),
            input=result_r.tool_input,
            duration_ms=round(float(getattr(result_r, "duration_ms", 0.0) or 0.0), 3),
            queue_wait_ms=round(
                float(getattr(result_r, "queue_wait_ms", 0.0) or 0.0), 3
            ),
            output_len=len(output),
            output=output,
        )
        self.publish(
            "session.tool.completed",
            {
                "tool_call_id": tool_call_id or None,
                "index": index,
                "name": result_r.name,
                "status": (
                    "error"
                    if result_r.dispatch_failed
                    else ("nonzero" if result_r.command_failed else "ok")
                ),
                "executed": bool(result_r.executed),
                "is_write": bool(result_r.is_write),
                "command_failed": bool(result_r.command_failed),
                "category": (
                    str(getattr(result_r, "category", "") or "")
                    or tool_category(result_r.name)
                ),
                "summary": format_tool_summary(result_r.name, result_r.tool_input),
                "duration_ms": round(
                    float(getattr(result_r, "duration_ms", 0.0) or 0.0),
                    3,
                ),
                "output_len": len(output),
                "output": output,
                **(
                    {"metadata": shell_output_facts(result_r.metadata)}
                    if result_r.name == "bash"
                    and (result_r.command_failed or result_r.dispatch_failed)
                    else {}
                ),
            },
        )


class CodingWriteEffects:
    """Existing committed-patch analysis and service refresh, without a host."""

    def __init__(
        self,
        *,
        workspace: Path,
        change_tracker: ChangeTracker,
        runtime_state: RuntimeState,
        run_evidence: RunEvidence,
        recovery: RecoveryState,
        admission: AdmissionInvariantSession | None,
        project_profile: Callable[[], dict],
        refresh_index: Callable[[list[str], Path], IndexStats],
        diagnostics: Callable[[list[str], Path], str],
        trace: ToolTrace,
    ) -> None:
        self.workspace = workspace
        self.change_tracker = change_tracker
        self.runtime_state = runtime_state
        self.run_evidence = run_evidence
        self.recovery = recovery
        self.admission = admission
        self.project_profile = project_profile
        self.refresh_index = refresh_index
        self.diagnostics = diagnostics
        self.trace = trace

    def post_write(self, dispatched: list, messages: list[dict]) -> None:
        if self.admission is not None:
            for _index, _call, result in dispatched:
                self.admission.record_committed_mutation(result)
        self.recovery.reset_tool_call_history(reason="workspace_changed")
        self.refresh_patch_risk(messages)
        self.refresh_code_index(dispatched)
        self.attach_lsp_write_diagnostics(dispatched, messages)

    def _committed_write_paths(self, dispatched: list) -> tuple[list[str], str]:
        """Collect file paths from successful, committed write tool calls."""
        paths: list[str] = []
        last_tool_call_id = ""
        for _index, tool_call, result in dispatched:
            if not (
                is_filesystem_mutation_tool(result.name)
                and result.executed
                and not result.dispatch_failed
            ):
                continue
            if result.tool_input.get("dry_run"):
                continue

            result_paths = list(collect_filesystem_mutation_paths(result.tool_input))

            if result_paths:
                paths.extend(result_paths)
                last_tool_call_id = str(tool_call.get("id") or "")

        return list(dict.fromkeys(paths)), last_tool_call_id

    def refresh_code_index(self, dispatched: list) -> None:
        """Incrementally refresh indexed files after a successful transaction."""
        paths, _ = self._committed_write_paths(dispatched)
        paths.extend(self.change_tracker.current_changed_paths())
        paths.extend(self.change_tracker.current_deleted_paths())
        paths = list(dict.fromkeys(paths))
        if not paths:
            return
        try:
            stats = self.refresh_index(paths, self.workspace)
        except Exception as exc:
            self.trace("code_index_refresh_failed", error=str(exc))
            return
        self.trace(
            "code_index_refreshed",
            files=len(paths),
            indexed=stats.indexed,
            removed=stats.removed,
        )

    def attach_lsp_write_diagnostics(self, dispatched: list, messages: list) -> None:
        """Append committed-file diagnostics to the last write tool message."""
        paths, last_tool_call_id = self._committed_write_paths(dispatched)

        if not paths or not last_tool_call_id:
            return
        try:
            block = self.diagnostics(paths, self.workspace)
        except Exception as exc:
            self.trace("lsp_write_diagnostics_failed", error=str(exc))
            return
        if not block:
            return
        tool_message = next(
            (
                message
                for message in reversed(messages)
                if message.get("role") == "tool"
                and message.get("tool_call_id") == last_tool_call_id
            ),
            None,
        )
        if tool_message is None:
            return
        tool_message["content"] = f"{tool_message.get('content', '')}\n\n{block}"
        self.trace(
            "lsp_write_diagnostics",
            files=len(dict.fromkeys(paths)),
            output_len=len(block),
        )

    def refresh_patch_risk(self, messages: list) -> None:
        """Analyze committed agent changes and inject one conservative review per patch."""
        try:
            from nz_coder.intelligence.impact_analyzer import (
                analyze_patch_impact,
                format_impact_report,
            )

            changed = self.change_tracker.current_changed_paths()
            deleted = self.change_tracker.current_deleted_paths()
            diff = self.change_tracker.render_current_diff() if changed else ""
            report = analyze_patch_impact(
                changed_files=changed,
                diff_text=diff,
                project_profile=self.project_profile(),
                deleted_files=deleted,
                requested_paths=self.runtime_state.requested_paths,
                task_mode=self.runtime_state.task_mode,
                diff_chars=len(diff),
            )
            self.runtime_state.patch_risk = report
            self.runtime_state.has_diff = bool(changed)
            self.runtime_state.changed_files = list(changed)
            self.runtime_state.diff_chars = len(diff)
            from nz_coder.runtime.agent.task_policy import is_test_file

            self.runtime_state.tests_modified = any(
                is_test_file(path) for path in changed
            )
            self.run_evidence.impact_review = dict(report)
            self.trace(
                "patch_risk_refreshed",
                risk=report.get("risk"),
                requires_replan=bool(report.get("requires_replan")),
                fingerprint=report.get("fingerprint"),
                signals=len(report.get("risk_signals", [])),
            )
            fingerprint = str(report.get("fingerprint") or "")
            if not report.get("requires_replan") or not fingerprint:
                return
            if fingerprint == self.runtime_state.risk_feedback_fingerprint:
                return
            self.runtime_state.risk_feedback_fingerprint = fingerprint
            messages.append(
                stamp_user_message(
                    {
                        "role": "user",
                        "content": (
                            "<patch-risk-review>\n"
                            + format_impact_report(report)
                            + "\nReview whether these public API or scope changes are required by the user task. "
                            "Revise the approach before finalizing; do not mechanically preserve risky hunks.\n"
                            "</patch-risk-review>"
                        ),
                        "_nz_synthetic": True,
                    }
                )
            )
        except Exception as exc:
            self.trace("patch_risk_failed", error=str(exc))
