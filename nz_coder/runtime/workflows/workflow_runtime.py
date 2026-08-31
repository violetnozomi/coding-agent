"""Bounded declarative workflows over the Session-owned child-agent manager."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from nz_coder.runtime.agent.child_result import ChildAgentResult
from nz_coder.runtime.workflows.workflow_contracts import workflow_contract
from nz_coder.runtime.workflows.workflow_host import (
    build_workflow_approval_summary,
    clamp_workflow_limits,
    evaluate_workflow_approval,
    resolve_workflow_identity,
    validate_workflow_display_name,
)
from nz_coder.runtime.workflows.workflow_manifest import validate_workflow_manifest
from nz_coder.runtime.workflows.workflow_run_store import (
    WorkflowRunStore,
    build_workflow_cost_report,
)
from nz_coder.tools import ToolOutput, register


_SUCCESS = frozenset({"completed", "completed_unverified"})
_RUN_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,200}")
_MAX_PHASES = 32
_MAX_ITEMS = 256
_MAX_PROMPT_CHARS = 60_000


class WorkflowControlError(RuntimeError):
    """Base class for structural failures that must stop the whole workflow."""


class WorkflowLimitError(WorkflowControlError):
    """A declared or runtime workflow bound was exceeded."""


class WorkflowQualityError(WorkflowControlError):
    """The plan failed preflight before any child was published."""


class WorkflowBudgetError(WorkflowControlError):
    """The current run exhausted its live output-token budget."""


class WorkflowAbortError(WorkflowControlError):
    """The caller cancelled the workflow and its active children."""


@dataclass(frozen=True)
class WorkflowFinding:
    """One machine-readable workflow preflight finding."""

    code: str
    message: str
    severity: str = "error"

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


class WorkflowResultCache:
    """Private content-addressed successful-result cache with copy-forward."""

    def __init__(self, run_root: Path, run_id: str, *, read_from: str = ""):
        if not _RUN_ID_RE.fullmatch(run_id):
            raise ValueError("workflow run_id contains unsafe characters")
        if read_from and not _RUN_ID_RE.fullmatch(read_from):
            raise ValueError("resume_from contains unsafe characters")
        self.run_id = run_id
        self.results_dir = run_root / run_id / "results"
        self.read_dir = run_root / read_from / "results" if read_from else None
        self.results_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.results_dir.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.Lock()

    @staticmethod
    def _filename(key: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", key)
        return f"{safe[:240]}.json"

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or payload.get("status") not in _SUCCESS:
            return None
        try:
            return ChildAgentResult.from_dict(payload).to_dict()
        except (TypeError, ValueError):
            return None

    def get(self, key: str) -> dict | None:
        filename = self._filename(key)
        with self._lock:
            own = self._read(self.results_dir / filename)
            if own is not None:
                return own
            if self.read_dir is None:
                return None
            prior = self._read(self.read_dir / filename)
            if prior is None:
                return None
            self._write(self.results_dir / filename, prior)
            return copy.deepcopy(prior)

    def set(self, key: str, result: dict) -> None:
        if result.get("status") not in _SUCCESS:
            return
        with self._lock:
            self._write(self.results_dir / self._filename(key), result)

    @staticmethod
    def _write(path: Path, result: dict) -> None:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)


def _task_count(phase: dict) -> int:
    mode = phase.get("mode")
    if mode == "parallel":
        return len(phase.get("tasks") or [])
    if mode == "pipeline":
        return len(phase.get("items") or []) * len(phase.get("stages") or [])
    if mode == "map_reduce":
        return len(phase.get("items") or []) + 1
    if mode == "synthesize":
        return 1
    if mode == "workflow":
        nested = phase.get("_nested_plan")
        if isinstance(nested, dict):
            return sum(
                _task_count(item)
                for item in nested.get("phases", [])
                if isinstance(item, dict)
            )
    if mode == "quality_gate":
        return 0
    return 0


def planned_workflow_agents(plan: dict) -> int:
    """Return the static worst-case Agent count used by host admission."""
    phases = plan.get("phases") if isinstance(plan, dict) else None
    if not isinstance(phases, list):
        return 0
    planned = sum(_task_count(item) for item in phases if isinstance(item, dict))
    verifier = plan.get("verification")
    if isinstance(verifier, dict) and verifier.get("enabled", True) is True:
        revisions = verifier.get("max_revisions", 1)
        if isinstance(revisions, int) and not isinstance(revisions, bool):
            synthesis_phases = sum(
                1 for phase in phases
                if isinstance(phase, dict)
                and phase.get("mode") in {"map_reduce", "synthesize"}
            )
            planned += synthesis_phases * (1 + 2 * max(0, revisions))
    return planned


def lint_workflow_plan(
    plan: Any,
    *,
    remaining_agents: int,
    workspace: Path | None = None,
    concurrency_cap: int | None = None,
) -> list[WorkflowFinding]:
    """Statically reject unsafe or internally inconsistent workflow plans."""
    findings: list[WorkflowFinding] = []
    if not isinstance(plan, dict):
        return [WorkflowFinding("plan-not-object", "workflow plan must be an object")]
    manifest = None
    if plan.get("manifest") is not None:
        try:
            manifest = validate_workflow_manifest(plan["manifest"])
        except ValueError as exc:
            findings.append(WorkflowFinding("invalid-manifest", str(exc)))
    phases = plan.get("phases")
    token_budget = plan.get("token_budget")
    if token_budget is not None and (
        not isinstance(token_budget, int)
        or isinstance(token_budget, bool)
        or token_budget <= 0
    ):
        findings.append(WorkflowFinding(
            "invalid-token-budget",
            "workflow token_budget must be a positive integer",
        ))
    verifier = plan.get("verification")
    if verifier is not None:
        if not isinstance(verifier, dict):
            findings.append(WorkflowFinding(
                "invalid-verifier-config",
                "workflow verification must be an object",
            ))
        else:
            enabled = verifier.get("enabled", True)
            revisions = verifier.get("max_revisions", 1)
            if not isinstance(enabled, bool):
                findings.append(WorkflowFinding(
                    "invalid-verifier-config",
                    "workflow verification.enabled must be boolean",
                ))
            if (
                not isinstance(revisions, int)
                or isinstance(revisions, bool)
                or not 0 <= revisions <= 2
            ):
                findings.append(WorkflowFinding(
                    "invalid-verifier-config",
                    "workflow verification.max_revisions must be an integer from 0 to 2",
                ))
    if not isinstance(phases, list) or not phases:
        return [WorkflowFinding("missing-phases", "workflow plan requires non-empty phases")]
    if len(phases) > _MAX_PHASES:
        findings.append(WorkflowFinding("too-many-phases", f"workflow exceeds {_MAX_PHASES} phases"))
    names: set[str] = set()
    prior: set[str] = set()
    planned = 0
    producing = 0
    final_mode = ""
    artifact_names: set[str] = set()
    for index, phase in enumerate(phases):
        if not isinstance(phase, dict):
            findings.append(WorkflowFinding("phase-not-object", f"phase {index} must be an object"))
            continue
        name = str(phase.get("name") or "").strip()
        if not name:
            findings.append(WorkflowFinding("missing-phase-name", f"phase {index} requires name"))
        elif name in names:
            findings.append(WorkflowFinding("duplicate-phase", f"phase name {name!r} is duplicated"))
        names.add(name)
        mode = str(phase.get("mode") or "parallel")
        final_mode = mode
        if mode not in {"parallel", "pipeline", "map_reduce", "synthesize", "workflow", "quality_gate"}:
            findings.append(WorkflowFinding("unknown-phase-mode", f"phase {name or index} has unknown mode {mode!r}"))
            continue
        artifact_name = phase.get("artifact")
        if artifact_name is not None:
            if not isinstance(artifact_name, str) or not artifact_name.strip():
                findings.append(WorkflowFinding(
                    "invalid-artifact-name",
                    f"phase {name or index} artifact must be a non-empty string",
                ))
            elif artifact_name in artifact_names:
                findings.append(WorkflowFinding(
                    "duplicate-artifact-name",
                    f"phase {name or index} reuses artifact name {artifact_name!r}",
                ))
            else:
                artifact_names.add(artifact_name)
        log_message = phase.get("log")
        if log_message is not None and (
            not isinstance(log_message, str) or not log_message.strip()
            or len(log_message) > 4000
        ):
            findings.append(WorkflowFinding(
                "invalid-workflow-log",
                f"phase {name or index} log must contain 1 to 4000 characters",
            ))
        items = phase.get("items") or []
        if mode in {"pipeline", "map_reduce"} and (not isinstance(items, list) or not items):
            findings.append(WorkflowFinding("missing-items", f"phase {name or index} requires non-empty items"))
        if isinstance(items, list) and len(items) > _MAX_ITEMS:
            findings.append(WorkflowFinding("fanout-too-large", f"phase {name or index} exceeds {_MAX_ITEMS} items"))
        tasks: list[Any]
        if mode == "parallel":
            tasks = phase.get("tasks") or []
            if not isinstance(tasks, list) or not tasks:
                findings.append(WorkflowFinding("missing-tasks", f"phase {name or index} requires tasks"))
                tasks = []
            producing += len(tasks)
        elif mode == "pipeline":
            tasks = phase.get("stages") or []
            if not isinstance(tasks, list) or not tasks:
                findings.append(WorkflowFinding("missing-stages", f"phase {name or index} requires stages"))
                tasks = []
            producing += len(items) if isinstance(items, list) else 0
        elif mode == "map_reduce":
            tasks = [phase.get("map")]
        elif mode in {"workflow", "quality_gate"}:
            tasks = []
            producing += 1
        else:
            tasks = []
        concurrency = phase.get("concurrency")
        if concurrency is not None and (
            not isinstance(concurrency, int)
            or isinstance(concurrency, bool)
            or concurrency < 1
        ):
            findings.append(WorkflowFinding(
                "invalid-concurrency",
                f"phase {name or index} concurrency must be a positive integer",
            ))
        phase_scopes: list[tuple[int, list[str]]] = []
        for task_index, task in enumerate(tasks):
            if not isinstance(task, dict):
                findings.append(WorkflowFinding("task-not-object", f"phase {name or index} task {task_index} must be an object"))
                continue
            if not str(task.get("prompt") or "").strip():
                findings.append(WorkflowFinding("missing-task-prompt", f"phase {name or index} task {task_index} requires prompt"))
            read_only = task.get("read_only", False)
            if not isinstance(read_only, bool):
                findings.append(WorkflowFinding("invalid-read-only", f"phase {name or index} task {task_index} read_only must be boolean"))
            isolation = str(task.get("isolation") or "thread")
            if isolation not in {"thread", "process"}:
                findings.append(WorkflowFinding(
                    "invalid-isolation",
                    f"phase {name or index} task {task_index} isolation must be thread or process",
                ))
            if read_only is not True and not task.get("target_paths"):
                findings.append(WorkflowFinding("write-task-without-scope", f"phase {name or index} task {task_index} requires target_paths"))
            if workspace is not None and task.get("target_paths"):
                try:
                    from nz_coder.runtime.agent.subagent import (
                        _normalize_scope_paths,
                        _overlapping_paths,
                    )

                    scopes = _normalize_scope_paths(
                        task.get("target_paths"),
                        workspace,
                    )
                except ValueError as exc:
                    findings.append(WorkflowFinding(
                        "unsafe-target-path",
                        f"phase {name or index} task {task_index}: {exc}",
                    ))
                else:
                    if mode == "parallel" and read_only is not True:
                        for prior_index, prior_scopes in phase_scopes:
                            overlap = _overlapping_paths(scopes, prior_scopes)
                            if overlap:
                                findings.append(WorkflowFinding(
                                    "overlapping-parallel-write-scopes",
                                    f"phase {name or index} tasks {prior_index} and {task_index} overlap: {', '.join(overlap)}",
                                ))
                        phase_scopes.append((task_index, scopes))
        if mode == "workflow":
            nested = phase.get("_nested_plan")
            if not isinstance(nested, dict):
                findings.append(WorkflowFinding(
                    "unresolved-nested-workflow",
                    f"phase {name or index} nested workflow was not resolved before preflight",
                ))
            elif any(
                isinstance(item, dict) and item.get("mode") == "workflow"
                for item in nested.get("phases", [])
            ):
                findings.append(WorkflowFinding(
                    "nested-workflow-depth-exceeded",
                    f"phase {name or index} exceeds one nested workflow level",
                ))
            else:
                for nested_finding in lint_workflow_plan(
                    nested,
                    remaining_agents=remaining_agents,
                    workspace=workspace,
                    concurrency_cap=concurrency_cap,
                ):
                    findings.append(WorkflowFinding(
                        f"nested-{nested_finding.code}",
                        f"phase {name or index}: {nested_finding.message}",
                        nested_finding.severity,
                    ))
        if mode in {"map_reduce", "synthesize", "quality_gate"}:
            if mode in {"map_reduce", "synthesize"} and not str(phase.get("rubric") or "").strip():
                findings.append(WorkflowFinding("missing-rubric", f"phase {name or index} requires a synthesis rubric"))
            sources = phase.get("from_phases") or []
            if not isinstance(sources, list):
                findings.append(WorkflowFinding("invalid-phase-sources", f"phase {name or index} from_phases must be an array"))
            else:
                unknown = [str(source) for source in sources if str(source) not in prior]
                if unknown:
                    findings.append(WorkflowFinding("forward-phase-reference", f"phase {name or index} references unavailable phases: {', '.join(unknown)}"))
        planned += _task_count(phase)
        prior.add(name)
    if isinstance(verifier, dict) and verifier.get("enabled", True) is True:
        revisions = verifier.get("max_revisions", 1)
        if isinstance(revisions, int) and not isinstance(revisions, bool):
            synthesis_phases = sum(
                1 for phase in phases
                if isinstance(phase, dict)
                and phase.get("mode") in {"map_reduce", "synthesize"}
            )
            # One verifier for the initial synthesis, then one synthesis and
            # one verifier for each allowed revision.
            planned += synthesis_phases * (1 + 2 * max(0, revisions))
    if planned > max(0, int(remaining_agents)):
        findings.append(WorkflowFinding(
            "literal-fanout-exceeds-max-agents",
            f"workflow plans {planned} agent(s), exceeding remaining maxAgents capacity {max(0, int(remaining_agents))}",
        ))
    if manifest is not None:
        declared_phases = manifest["phases"]
        actual_phases = [
            str(phase.get("name") or "").strip()
            for phase in phases if isinstance(phase, dict)
        ]
        if declared_phases != actual_phases:
            findings.append(WorkflowFinding(
                "manifest-phase-mismatch",
                "workflow manifest phases must exactly match plan phase order",
            ))
        if planned > manifest["max_agents"]:
            findings.append(WorkflowFinding(
                "manifest-agent-cap-exceeded",
                f"workflow plans {planned} agent(s), exceeding manifest max_agents {manifest['max_agents']}",
            ))
        if manifest["max_agents"] > max(0, int(remaining_agents)):
            findings.append(WorkflowFinding(
                "manifest-agent-cap-unavailable",
                "workflow manifest max_agents exceeds remaining Session capacity",
            ))
        if concurrency_cap is not None and manifest["max_concurrency"] > concurrency_cap:
            findings.append(WorkflowFinding(
                "manifest-concurrency-unavailable",
                "workflow manifest max_concurrency exceeds Session concurrency capacity",
            ))
        if manifest["read_only"]:
            write_tasks = [
                task
                for phase in phases if isinstance(phase, dict)
                for task in (
                    phase.get("tasks") or phase.get("stages")
                    or ([phase.get("map")] if phase.get("map") else [])
                )
                if isinstance(task, dict) and task.get("read_only") is not True
            ]
            if write_tasks:
                findings.append(WorkflowFinding(
                    "manifest-read-only-violation",
                    "workflow manifest declares read_only but contains write-capable tasks",
                ))
            write_nested = [
                phase for phase in phases
                if isinstance(phase, dict)
                and phase.get("mode") == "workflow"
                and isinstance(phase.get("_nested_plan"), dict)
                and isinstance(phase["_nested_plan"].get("manifest"), dict)
                and phase["_nested_plan"]["manifest"].get("read_only") is not True
            ]
            if write_nested:
                findings.append(WorkflowFinding(
                    "manifest-read-only-nested-violation",
                    "read_only workflow cannot invoke a write-capable nested workflow",
                ))
        declared_budget = manifest.get("token_budget")
        if token_budget is not None and declared_budget not in {None, token_budget}:
            findings.append(WorkflowFinding(
                "manifest-token-budget-mismatch",
                "workflow manifest token_budget must match plan token_budget",
            ))
    if (
        producing > 1
        and plan.get("require_synthesis", True) is not False
        and final_mode not in {"map_reduce", "synthesize"}
    ):
        findings.append(WorkflowFinding(
            "missing-final-synthesis",
            "multi-result workflow requires a gated synthesize or map_reduce phase",
        ))
    unique: list[WorkflowFinding] = []
    seen: set[tuple[str, str]] = set()
    for finding in findings:
        key = (finding.code, finding.message)
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    return unique


def _replace(value: Any, *, item: Any, previous: Any, index: int) -> Any:
    if not isinstance(value, str):
        return copy.deepcopy(value)
    item_text = json.dumps(item, ensure_ascii=False, default=str)
    previous_text = json.dumps(previous, ensure_ascii=False, default=str)
    return (
        value.replace("{item}", item_text[:20_000])
        .replace("{previous}", previous_text[:20_000])
        .replace("{index}", str(index))
    )


def _materialize_task(template: dict, *, item: Any = "", previous: Any = "", index: int = 0) -> dict:
    task = {
        key: (
            [_replace(entry, item=item, previous=previous, index=index) for entry in value]
            if isinstance(value, list)
            else _replace(value, item=item, previous=previous, index=index)
        )
        for key, value in template.items()
    }
    prompt = str(task.get("prompt") or "")
    if len(prompt) > _MAX_PROMPT_CHARS:
        raise WorkflowLimitError(f"materialized task prompt exceeds {_MAX_PROMPT_CHARS} characters")
    return task


class WorkflowRuntime:
    """Execute one preflighted plan through the real BackgroundAgentManager."""

    def __init__(
        self,
        manager,
        *,
        run_id: str,
        resume_from: str = "",
        token_budget: int | None = None,
        cancel_event: threading.Event | None = None,
        max_concurrency: int | None = None,
        display_name: str = "",
        host_policy: dict | None = None,
        approval_summary: dict | None = None,
        approval_receipt: dict | None = None,
        on_started=None,
    ):
        self.manager = manager
        self.run_id = run_id
        self.cache = WorkflowResultCache(
            manager._workflow.root / "runs",
            run_id,
            read_from=resume_from,
        )
        self._occurrences: dict[str, int] = {}
        self._occurrence_lock = threading.Lock()
        self.replayed = 0
        self.token_budget = int(token_budget) if token_budget is not None else None
        self.spent_tokens = 0
        self.cancel_event = cancel_event or threading.Event()
        self.max_concurrency = (
            max(1, int(max_concurrency)) if max_concurrency is not None else None
        )
        self.display_name = (
            validate_workflow_display_name(display_name) if display_name else ""
        )
        self.host_policy = copy.deepcopy(host_policy or {})
        self.approval_summary = copy.deepcopy(approval_summary or {})
        self.approval_receipt = copy.deepcopy(approval_receipt or {})
        self.on_started = on_started
        self.workflow_name = "workflow"
        self._active_task_ids: set[str] = set()
        self._active_lock = threading.Lock()
        self._verification_config: dict = {}
        self._run_store = WorkflowRunStore(self.cache.results_dir.parent)
        self._artifacts: list[dict] = []
        self._started_at = 0.0
        self._started_wall = 0.0
        self._managed_started = False
        self._managed_lock = threading.Lock()
        self._capsule_ref: dict | None = None
        self._capsule_preflight: dict | None = None
        self._worktree_sweep: dict = {"removed": [], "warnings": []}

    def _check_control(self) -> None:
        if self.cancel_event.is_set():
            raise WorkflowAbortError("workflow aborted by caller")
        if self.manager.workflow_run_stopped(self.run_id):
            raise WorkflowAbortError("workflow stopped by manager")
        if self.token_budget is not None and self.spent_tokens >= self.token_budget:
            raise WorkflowBudgetError(
                f"tokenBudget cap ({self.token_budget}) exhausted; spent={self.spent_tokens}"
            )

    def _before_spawn(self) -> None:
        self._ensure_managed_run("workflow")
        self._check_control()
        if not self.manager.wait_workflow_spawn_gate(self.run_id, self.cancel_event):
            raise WorkflowAbortError("workflow stopped before next Agent spawn")

    def _ensure_managed_run(self, name: str) -> None:
        with self._managed_lock:
            if self._managed_started:
                return
            self.manager.begin_workflow_run(
                self.run_id,
                self.display_name or name,
            )
            self._managed_started = True
            if self._started_at <= 0:
                self._started_at = time.monotonic()
                self._started_wall = time.time()

    def artifact(self, name: str, value: Any) -> dict:
        """Persist one bounded JSON artifact and emit its durable reference."""
        reference = self._run_store.write_artifact(name, value)
        self._artifacts.append(reference)
        self.manager.record_workflow_event(
            "artifact_written",
            data={"run_id": self.run_id, **reference},
        )
        return reference

    def log(self, message: str, data: dict | None = None) -> None:
        """Emit one bounded structured progress observation."""
        text = str(message or "").strip()
        if not text or len(text) > 4000:
            raise WorkflowQualityError("workflow log must contain 1 to 4000 characters")
        self.manager.record_workflow_event(
            "workflow_log",
            data={"run_id": self.run_id, "message": text, "data": dict(data or {})},
        )

    def _states_for_run(self) -> list[dict]:
        return [
            state for state in self.manager._states()
            if state.get("workflow_run_id") == self.run_id
        ]

    def _write_terminal_record(
        self,
        status: str,
        *,
        outputs: dict | None = None,
        error: str = "",
    ) -> dict:
        report = build_workflow_cost_report(
            self._states_for_run(),
            wall_clock_seconds=max(0.0, time.monotonic() - self._started_at),
        )
        def summary_text(value: Any) -> str:
            if isinstance(value, str):
                return value.strip()
            if isinstance(value, dict):
                for key in ("final_text", "summary", "report", "text", "result"):
                    text = summary_text(value.get(key))
                    if text:
                        return text
                return ""
            if isinstance(value, list):
                parts = [summary_text(item) for item in value]
                return "\n\n".join(item for item in parts if item)
            return ""

        result_summary = (
            summary_text(outputs[next(reversed(outputs))]) if outputs else ""
        )
        record = {
            "run_id": self.run_id,
            "status": status,
            "started_at": self._started_wall,
            "ended_at": time.time(),
            "artifacts": copy.deepcopy(self._artifacts),
            "efficiency_report": report,
            "phase_names": list((outputs or {}).keys()),
            "worktree_sweep": copy.deepcopy(self._worktree_sweep),
            "workflow_name": self.workflow_name,
            **({"display_name": self.display_name} if self.display_name else {}),
            "host_policy": copy.deepcopy(self.host_policy),
            "approval_summary": copy.deepcopy(self.approval_summary),
            "approval_receipt": copy.deepcopy(self.approval_receipt),
            **({"result_summary": result_summary[:20_000]} if result_summary else {}),
            **(
                {"capsule_ref": copy.deepcopy(self._capsule_ref)}
                if self._capsule_ref is not None else {}
            ),
            **({"error": str(error)[:4000]} if error else {}),
        }
        self._run_store.write_terminal(record)
        return report

    def _sweep_terminal_worktrees(self) -> dict:
        from nz_coder.runtime.workflows.workflow_sweep import sweep_workflow_worktrees

        self._worktree_sweep = sweep_workflow_worktrees(
            self._states_for_run(),
            self.manager.workspace,
            run_id=self.run_id,
        )
        self.manager.record_workflow_event(
            "worktree_sweep_completed",
            data={"run_id": self.run_id, **self._worktree_sweep},
        )
        return self._worktree_sweep

    def _accrue(self, result: dict) -> None:
        usage = result.get("usage") if isinstance(result, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        amount = usage.get("output", usage.get("output_tokens", usage.get("total", 0)))
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            self.spent_tokens += max(0, int(amount))
        self.manager.record_workflow_event(
            "budget_updated",
            data={
                "run_id": self.run_id,
                "spent": self.spent_tokens,
                "total": self.token_budget,
                "remaining": (
                    None
                    if self.token_budget is None
                    else max(0, self.token_budget - self.spent_tokens)
                ),
            },
        )
    def _track(self, task_id: str, active: bool) -> None:
        with self._active_lock:
            if active:
                self._active_task_ids.add(task_id)
            else:
                self._active_task_ids.discard(task_id)

    def stop_active(self, reason: str) -> list[str]:
        with self._active_lock:
            ids = sorted(self._active_task_ids)
        if ids:
            self.manager.stop(ids, reason=reason, timeout_ms=2000)
        return ids

    def phase(self, name: str, fn: Callable[[], Any]) -> Any:
        self._check_control()
        self.manager.record_workflow_event("phase_started", data={"name": name, "run_id": self.run_id})
        try:
            return fn()
        finally:
            self.manager.record_workflow_event("phase_finished", data={"name": name, "run_id": self.run_id})

    def parallel(self, thunks: list[Callable[[], Any]], *, concurrency: int | None = None) -> list[Any | None]:
        self._check_control()
        if len(thunks) > _MAX_ITEMS:
            raise WorkflowLimitError(f"parallel received more than {_MAX_ITEMS} items")
        if not thunks:
            return []
        cap = min(
            concurrency or self.max_concurrency or self.manager.concurrency_cap,
            self.max_concurrency or self.manager.concurrency_cap,
            self.manager.concurrency_cap,
            len(thunks),
        )
        if cap < 1:
            raise WorkflowLimitError("parallel concurrency must be positive")
        results: list[Any | None] = [None] * len(thunks)
        with ThreadPoolExecutor(max_workers=cap, thread_name_prefix="nz-workflow") as pool:
            pending = {pool.submit(thunk): index for index, thunk in enumerate(thunks)}
            for future in as_completed(pending):
                index = pending[future]
                try:
                    results[index] = future.result()
                except WorkflowControlError:
                    raise
                except Exception:
                    results[index] = None
        return results

    def pipeline(self, items: list[Any], stages: list[dict], *, phase: str) -> list[Any | None]:
        def chain(item: Any, index: int) -> Any | None:
            previous: Any = item
            for template in stages:
                task = _materialize_task(template, item=item, previous=previous, index=index)
                task["phase"] = phase
                previous = self.run_agent(task)
                if previous is None:
                    return None
            return previous

        return self.parallel([
            lambda item=item, index=index: chain(item, index)
            for index, item in enumerate(items)
        ])

    def _cache_key(self, task: dict) -> str:
        canonical = json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
        with self._occurrence_lock:
            occurrence = self._occurrences.get(digest, 0)
            self._occurrences[digest] = occurrence + 1
        return f"{digest}#{occurrence}"

    def run_agent(self, task: dict) -> dict | None:
        self._check_control()
        key = self._cache_key(task)
        cached = self.cache.get(key)
        if cached is not None:
            self.replayed += 1
            self.manager.record_workflow_event(
                "task_replayed",
                task_id=str(cached.get("task_id") or ""),
                data={"name": str(task.get("name") or "task"), "run_id": self.run_id},
            )
            return cached
        self._before_spawn()
        launch_task = copy.deepcopy(task)
        launch_task["workflow_run_id"] = self.run_id
        started = self.manager.start([launch_task])
        if str(started).startswith("Error:"):
            if "maxAgents" in str(started):
                raise WorkflowLimitError(str(started))
            raise RuntimeError(str(started))
        task_id = started.metadata["task_ids"][0]
        self._track(task_id, True)
        try:
            settled = self.manager.wait_until_settled(task_id, self.cancel_event)
            if not settled:
                self.manager.stop(
                    [task_id],
                    reason="workflow aborted by caller",
                    timeout_ms=2000,
                )
                raise WorkflowAbortError("workflow aborted by caller")
            waited = self.manager.wait([task_id], timeout_ms=0)
        finally:
            self._track(task_id, False)
        self._check_control()
        results = waited.metadata.get("child_results") or []
        if not results:
            return None
        result = ChildAgentResult.from_dict(results[0]).to_dict()
        self._accrue(result)
        if result["status"] not in _SUCCESS:
            return None
        self.cache.set(key, result)
        return result

    def synthesize(self, inputs: list[Any], rubric: str, *, phase: str) -> dict:
        self._check_control()
        normalized = []
        for item in inputs:
            if isinstance(item, dict) and "final_text" in item:
                normalized.append(item["final_text"])
            else:
                normalized.append(item)
        body = json.dumps(normalized, ensure_ascii=False, indent=2, default=str)
        prompt = (
            "You are the final synthesis owner for a bounded coding workflow.\n"
            "Use only the supplied child results. Preserve concrete evidence, distinguish "
            "confirmed findings from uncertainty, and do not claim unobserved work.\n\n"
            f"Rubric:\n{rubric.strip()}\n\nInputs:\n{body[:_MAX_PROMPT_CHARS]}"
        )
        # Synthesis is intentionally fresh and bypasses resume cache: it is the
        # final fold over possibly replayed inputs and counts as a real Agent.
        self._before_spawn()
        started = self.manager.start([{
            "name": "synthesize",
            "prompt": prompt,
            "read_only": True,
            "phase": phase,
            "model_hint": "deep",
            "workflow_run_id": self.run_id,
        }])
        if str(started).startswith("Error:"):
            raise WorkflowLimitError(str(started))
        task_id = started.metadata["task_ids"][0]
        self._track(task_id, True)
        try:
            settled = self.manager.wait_until_settled(task_id, self.cancel_event)
            if not settled:
                self.manager.stop(
                    [task_id],
                    reason="workflow aborted during synthesis",
                    timeout_ms=2000,
                )
                raise WorkflowAbortError("workflow aborted during synthesis")
            waited = self.manager.wait([task_id], timeout_ms=0)
        finally:
            self._track(task_id, False)
        self._check_control()
        results = waited.metadata.get("child_results") or []
        if not results:
            raise RuntimeError("workflow synthesis did not settle")
        result = ChildAgentResult.from_dict(results[0]).to_dict()
        self._accrue(result)
        if result["status"] not in _SUCCESS:
            raise RuntimeError(f"workflow synthesis failed: {result['status']}")
        self.manager.record_workflow_event(
            "synthesis_completed",
            task_id=task_id,
            data={"phase": phase, "run_id": self.run_id},
        )
        return result

    def verify_synthesis(
        self,
        synthesis: dict,
        inputs: list[Any],
        *,
        phase: str,
        criteria: str,
    ) -> dict:
        """Run a fresh-context, deep-tier verifier and return a typed verdict."""
        self._check_control()
        prompt = (
            "You are an independent sidecar verifier. Do not continue the workflow and "
            "do not rewrite the answer. Judge whether the proposed synthesis is supported "
            "by the supplied child evidence and criteria. Attempt refutation. End with a "
            "fenced JSON object matching the required schema.\n\n"
            f"Criteria:\n{criteria or 'Evidence-grounded, complete, and non-fabricated.'}\n\n"
            f"Proposed synthesis:\n{str(synthesis.get('final_text') or '')[:20_000]}\n\n"
            f"Child evidence:\n{json.dumps(inputs, ensure_ascii=False, default=str)[:30_000]}"
        )
        schema = {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["accept", "revise", "blocked"]},
                "reason": {"type": "string", "minLength": 1},
                "suggested_fix": {"type": "string"},
            },
            "required": ["verdict", "reason"],
            "additionalProperties": False,
        }
        self.manager.record_workflow_event(
            "verifier_started",
            data={"run_id": self.run_id, "phase": phase},
        )
        self._before_spawn()
        started = self.manager.start([{
            "name": "sidecar-verifier",
            "prompt": prompt,
            "read_only": True,
            "phase": f"{phase}:verifier",
            "model_hint": "deep",
            "output_schema": schema,
            "workflow_run_id": self.run_id,
        }])
        if str(started).startswith("Error:"):
            raise WorkflowLimitError(str(started))
        task_id = started.metadata["task_ids"][0]
        self._track(task_id, True)
        try:
            settled = self.manager.wait_until_settled(task_id, self.cancel_event)
            if not settled:
                self.manager.stop([task_id], reason="workflow verifier aborted", timeout_ms=2000)
                raise WorkflowAbortError("workflow verifier aborted")
            waited = self.manager.wait([task_id], timeout_ms=0)
        finally:
            self._track(task_id, False)
        self._check_control()
        results = waited.metadata.get("child_results") or []
        # Sidecar is fail-open on provider/schema failures, matching InfCodeX.
        verdict = {
            "verdict": "accept",
            "reason": "sidecar unavailable; fail-open",
            "trace": "sidecar_failure",
        }
        if results:
            result = ChildAgentResult.from_dict(results[0]).to_dict()
            self._accrue(result)
            structured = result.get("structured")
            if (
                result.get("status") in _SUCCESS
                and isinstance(structured, dict)
                and structured.get("verdict") in {"accept", "revise", "blocked"}
                and str(structured.get("reason") or "").strip()
            ):
                verdict = {
                    "verdict": structured["verdict"],
                    "reason": str(structured["reason"])[:4000],
                    "suggested_fix": str(structured.get("suggested_fix") or "")[:4000],
                    "trace": "verifier_ok",
                    "task_id": task_id,
                }
        self.manager.record_workflow_event(
            "verifier_verdict",
            task_id=task_id,
            data={"run_id": self.run_id, "phase": phase, **verdict},
        )
        return verdict

    def synthesize_verified(self, inputs: list[Any], rubric: str, *, phase: str) -> dict:
        synthesis = self.synthesize(inputs, rubric, phase=phase)
        config = self._verification_config
        if not config.get("enabled", False):
            return synthesis
        revisions = int(config.get("max_revisions", 1))
        criteria = str(config.get("criteria") or rubric)
        for attempt in range(revisions + 1):
            verdict = self.verify_synthesis(
                synthesis,
                inputs,
                phase=phase,
                criteria=criteria,
            )
            synthesis["sidecar_verification"] = verdict
            if verdict["verdict"] == "accept":
                return synthesis
            if verdict["verdict"] == "blocked":
                raise WorkflowQualityError(f"sidecar verifier blocked delivery: {verdict['reason']}")
            if attempt >= revisions:
                raise WorkflowQualityError(
                    f"sidecar verifier revision budget exhausted: {verdict['reason']}"
                )
            revision_rubric = (
                f"{rubric}\n\nA prior synthesis failed independent verification. "
                f"Correct this specific issue: {verdict['reason']}"
            )
            synthesis = self.synthesize(inputs, revision_rubric, phase=phase)
        return synthesis

    def _execute_phases(
        self,
        plan: dict,
        *,
        depth: int = 0,
        prefix: str = "",
    ) -> tuple[dict[str, Any], Any]:
        """Execute resolved phases on this runtime so nested work shares limits."""
        if depth > 1:
            raise WorkflowLimitError("nested workflows are limited to one level")
        outputs: dict[str, Any] = {}
        for phase in plan["phases"]:
            self._check_control()
            name = str(phase["name"])
            qualified = f"{prefix}{name}"
            mode = str(phase.get("mode") or "parallel")

            def run_phase() -> Any:
                if mode == "parallel":
                    tasks = []
                    for template in phase["tasks"]:
                        task = copy.deepcopy(template)
                        task["phase"] = qualified
                        tasks.append(task)
                    return self.parallel(
                        [lambda task=task: self.run_agent(task) for task in tasks],
                        concurrency=phase.get("concurrency"),
                    )
                if mode == "pipeline":
                    return self.pipeline(
                        phase["items"], phase["stages"], phase=qualified
                    )
                if mode == "map_reduce":
                    template = phase["map"]
                    mapped = self.parallel([
                        lambda item=item, index=index: self.run_agent(dict(
                            _materialize_task(template, item=item, index=index),
                            phase=qualified,
                        ))
                        for index, item in enumerate(phase["items"])
                    ], concurrency=phase.get("concurrency"))
                    return self.synthesize_verified(
                        [item for item in mapped if item is not None],
                        str(phase["rubric"]),
                        phase=qualified,
                    )
                if mode == "workflow":
                    if depth >= 1:
                        raise WorkflowLimitError(
                            "nested workflows are limited to one level"
                        )
                    nested = phase.get("_nested_plan")
                    if not isinstance(nested, dict):
                        raise WorkflowQualityError(
                            f"nested workflow {name} was not resolved before execution"
                        )
                    self.log(
                        f"nested workflow started: {phase.get('workflow') or name}",
                        {"phase": qualified, "ref": phase.get("_nested_ref") or {}},
                    )
                    _nested_outputs, nested_final = self._execute_phases(
                        nested,
                        depth=depth + 1,
                        prefix=f"{qualified}/",
                    )
                    self.log(
                        f"nested workflow completed: {phase.get('workflow') or name}",
                        {"phase": qualified},
                    )
                    return nested_final
                sources = phase.get("from_phases") or list(outputs)
                values: list[Any] = []
                for source in sources:
                    value = outputs.get(str(source))
                    values.extend(value if isinstance(value, list) else [value])
                if mode == "quality_gate":
                    from nz_coder.runtime.workflows.workflow_review import review_quality_gate

                    result = review_quality_gate(
                        [item for item in values if item is not None]
                    )
                    self.log(
                        f"quality gate completed for {qualified}",
                        {
                            "kind": "review_quality_gate",
                            "actionable_findings": len(result["actionable_findings"]),
                            "unresolved_findings": len(result["unresolved_findings"]),
                            "unqualified_approval_allowed": result["unqualified_approval_allowed"],
                        },
                    )
                    return result
                return self.synthesize_verified(
                    [item for item in values if item is not None],
                    str(phase["rubric"]),
                    phase=qualified,
                )

            outputs[name] = self.phase(qualified, run_phase)
            if phase.get("artifact") is not None:
                self.artifact(str(phase["artifact"]), outputs[name])
            if phase.get("log") is not None:
                self.log(str(phase["log"]), {"phase": qualified})
        self._check_control()
        final = outputs[str(plan["phases"][-1]["name"])]
        return outputs, final

    def execute(self, plan: dict) -> dict:
        self._verification_config = copy.deepcopy(plan.get("verification") or {})
        self._capsule_ref = copy.deepcopy(plan.get("_capsule_ref"))
        self._capsule_preflight = copy.deepcopy(plan.get("_capsule_preflight"))
        self._started_at = time.monotonic()
        manifest = plan.get("manifest")
        run_name = (
            str(manifest.get("name") or "workflow")
            if isinstance(manifest, dict)
            else str(plan.get("name") or "workflow")
        )
        self.workflow_name = run_name
        self._ensure_managed_run(run_name)
        self.manager.record_workflow_event(
            "workflow_run_started",
            data={
                "run_id": self.run_id,
                "name": run_name,
                **({"display_name": self.display_name} if self.display_name else {}),
                "approval_summary": copy.deepcopy(self.approval_summary),
                "approval_receipt": copy.deepcopy(self.approval_receipt),
            },
        )
        if callable(self.on_started):
            self.on_started({
                "run_id": self.run_id,
                "name": run_name,
                "display_name": self.display_name,
                "status": "running",
                "approval_summary": copy.deepcopy(self.approval_summary),
            })
        outputs: dict[str, Any] = {}
        try:
            outputs, final = self._execute_phases(plan)
            self._sweep_terminal_worktrees()
            efficiency_report = self._write_terminal_record(
                "completed",
                outputs=outputs,
            )
            outcome = {
                "run_id": self.run_id,
                "status": "completed",
                "outputs": outputs,
                "result": final,
                "replayed_agents": self.replayed,
                "budget": {
                    "total": self.token_budget,
                    "spent": self.spent_tokens,
                    "remaining": (
                        None
                        if self.token_budget is None
                        else max(0, self.token_budget - self.spent_tokens)
                    ),
                },
                "workflow_snapshot": self.manager._workflow.snapshot(),
                "contract": workflow_contract(),
                "artifacts": copy.deepcopy(self._artifacts),
                "efficiency_report": efficiency_report,
                "worktree_sweep": copy.deepcopy(self._worktree_sweep),
                "workflow_name": self.workflow_name,
                **({"display_name": self.display_name} if self.display_name else {}),
                "host_policy": copy.deepcopy(self.host_policy),
                "approval_summary": copy.deepcopy(self.approval_summary),
                "approval_receipt": copy.deepcopy(self.approval_receipt),
                **(
                    {"capsule_ref": copy.deepcopy(self._capsule_ref)}
                    if self._capsule_ref is not None else {}
                ),
                **(
                    {"capsule_preflight": copy.deepcopy(self._capsule_preflight)}
                    if self._capsule_preflight is not None else {}
                ),
            }
            self.manager.record_workflow_event(
                "workflow_run_completed",
                data={"run_id": self.run_id, "spent_tokens": self.spent_tokens},
            )
            self.manager.record_workflow_outcome(outcome)
            self.manager.finish_workflow_run(self.run_id, "completed")
            return outcome
        except Exception as exc:
            stopped = self.stop_active(
                "workflow stopped" if isinstance(exc, WorkflowAbortError) else "workflow failed"
            )
            event_type = (
                "workflow_run_stopped"
                if isinstance(exc, WorkflowAbortError)
                else "workflow_run_failed"
            )
            terminal_status = (
                "stopped" if isinstance(exc, WorkflowAbortError) else "failed"
            )
            self._sweep_terminal_worktrees()
            self._write_terminal_record(
                terminal_status,
                outputs=outputs,
                error=str(exc),
            )
            self.manager.record_workflow_event(
                event_type,
                data={
                    "run_id": self.run_id,
                    "error": str(exc)[:4000],
                    "stopped_task_ids": stopped,
                },
            )
            self.manager.finish_workflow_run(
                self.run_id,
                terminal_status,
                str(exc),
            )
            raise


def workflow_run(
    plan: dict | None = None,
    resume_from: str = "",
    resume_target: str = "",
    capsule_name: str = "",
    capsule_source: str = "",
    capsule_args: dict | None = None,
    host_policy: dict | None = None,
    display_name: str = "",
    approval_decision: str = "",
    approval_digest: str = "",
    _run_id: str = "",
    _on_started=None,
) -> str:
    """Preflight and execute one bounded declarative workflow plan."""
    try:
        from nz_coder.runtime.agent.agent_manager import _current_manager

        manager = _current_manager()
        if resume_from and resume_target:
            return "Error: provide either resume_from or resume_target, not both"
        if display_name:
            display_name = validate_workflow_display_name(display_name)
        if resume_target:
            identity = resolve_workflow_identity(
                resume_target,
                workspace=manager.workspace,
                runs_root=manager._workflow.root / "runs",
            )
            if identity.get("kind") != "run":
                return (
                    "Error: workflow resume target must resolve uniquely to a run; "
                    f"got {identity.get('kind')}"
                )
            resume_from = str(identity["run_id"])
        capsule_ref = None
        capsule_preflight = None
        if capsule_name:
            if plan is not None:
                return "Error: provide either plan or capsule_name, not both"
            from nz_coder.runtime.workflows.workflow_resolver import resolve_workflow_capsule

            resolved = resolve_workflow_capsule(
                capsule_name,
                capsule_args or {},
                workspace=manager.workspace,
                source=capsule_source,
            )
            capsule_ref = resolved["ref"]
            capsule_preflight = resolved["preflight"]
            plan = copy.deepcopy(resolved["capsule"]["plan"])
            plan["_capsule_ref"] = copy.deepcopy(capsule_ref)
            plan["_capsule_preflight"] = copy.deepcopy(capsule_preflight)
        if not isinstance(plan, dict):
            return "Error: workflow_run requires plan or capsule_name"
        from nz_coder.runtime.workflows.workflow_resolver import resolve_nested_workflows

        plan = resolve_nested_workflows(plan, workspace=manager.workspace)
        manifest = plan.get("manifest") if isinstance(plan.get("manifest"), dict) else {}
        effective_limits = clamp_workflow_limits(
            manifest,
            host_policy,
            system_max_agents=manager.agent_cap,
            system_max_concurrency=manager.concurrency_cap,
        )
        approval_summary = build_workflow_approval_summary(
            manifest,
            host_policy,
            system_max_agents=manager.agent_cap,
            system_max_concurrency=manager.concurrency_cap,
        )
        findings = lint_workflow_plan(
            plan,
            remaining_agents=manager.agent_cap - manager.spawned_count(),
            workspace=manager.workspace,
            concurrency_cap=manager.concurrency_cap,
        )
        effective_agent_cap = effective_limits.get("max_agents")
        planned_agents = planned_workflow_agents(plan)
        if (
            isinstance(effective_agent_cap, int)
            and planned_agents > effective_agent_cap
        ):
            findings.append(WorkflowFinding(
                "host-agent-cap-exceeded",
                f"workflow plans {planned_agents} agent(s), exceeding effective host maxAgents {effective_agent_cap}",
            ))
        errors = [finding for finding in findings if finding.severity == "error"]
        if errors:
            return ToolOutput(
                "Error: workflow preflight failed:\n"
                + "\n".join(f"- [{item.code}] {item.message}" for item in errors),
                title="Workflow preflight",
                metadata={"workflow_findings": [item.to_dict() for item in findings]},
            )
        if not approval_decision:
            from nz_coder.runtime.workflows.workflow_host import (
                current_workflow_approval_asker,
                workflow_approval_digest,
            )

            approval_asker = current_workflow_approval_asker()
            if approval_asker is not None:
                approval_decision = str(
                    approval_asker(copy.deepcopy(approval_summary)) or "cancel"
                )
                approval_digest = workflow_approval_digest(approval_summary)
        approval_receipt = evaluate_workflow_approval(
            approval_summary,
            decision=approval_decision,
            expected_digest=approval_digest,
            headless=not approval_decision,
        )
        if approval_receipt["outcome"] != "started":
            return ToolOutput(
                f"Workflow {approval_receipt['outcome']}: {approval_receipt['reason']}.",
                title="Workflow approval",
                metadata={
                    "workflow_approval": approval_receipt,
                    "workflow_approval_summary": approval_summary,
                },
            )
        run_id = str(_run_id or f"workflow-{uuid.uuid4().hex}")
        if not re.fullmatch(r"workflow-[A-Za-z0-9_-]{8,128}", run_id):
            return "Error: invalid internal workflow run id"
        from nz_coder.tools import current_tool_cancel_event

        runtime = WorkflowRuntime(
            manager,
            run_id=run_id,
            resume_from=resume_from,
            token_budget=(
                effective_limits.get("token_budget")
                if effective_limits.get("token_budget") is not None
                else plan.get("token_budget")
            ),
            cancel_event=current_tool_cancel_event(),
            max_concurrency=effective_limits.get("max_concurrency"),
            display_name=display_name,
            host_policy=host_policy,
            approval_summary=approval_summary,
            approval_receipt=approval_receipt,
            on_started=_on_started,
        )
        outcome = runtime.execute(copy.deepcopy(plan))
        final = outcome.get("result")
        final_text = (
            str(final.get("final_text") or "")
            if isinstance(final, dict)
            else json.dumps(final, ensure_ascii=False, default=str)
        )
        return ToolOutput(
            final_text or f"Workflow {run_id} completed.",
            title="Workflow completed",
            metadata={"workflow_result": outcome},
        )
    except WorkflowControlError as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: workflow failed: {exc}"


register(
    name="workflow_run",
    description=(
        "Run a bounded preflighted child-Agent workflow with parallel, pipeline, "
        "map-reduce, gated synthesis, successful-result resume, and durable events."
    ),
    parameters={
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "description": "Declarative phases using parallel, pipeline, map_reduce, or synthesize mode.",
            },
            "capsule_name": {
                "type": "string",
                "description": "Saved inert workflow capsule name; mutually exclusive with plan.",
            },
            "capsule_source": {
                "type": "string",
                "enum": ["project", "personal"],
            },
            "capsule_args": {
                "type": "object",
                "description": "Bounded arguments for a built-in or saved capsule.",
            },
            "resume_from": {
                "type": "string",
                "description": "Optional prior workflow run ID whose successful child results seed replay.",
            },
            "resume_target": {
                "type": "string",
                "description": "Run ID or unique display name whose successful child results seed replay.",
            },
            "host_policy": {
                "type": "object",
                "description": "Optional max_agents, max_concurrency, and token_budget ceilings; min-wins.",
            },
            "display_name": {
                "type": "string",
                "description": "Optional printable run alias persisted for history and identity resolution.",
            },
        },
    },
    handler=workflow_run,
    execution="serial",
)
