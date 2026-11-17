"""验证域的不可变边界值；不依赖运行编排或工具注册表。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from .requirements import RequirementStatus, TaskContract


@dataclass(frozen=True)
class TaskScope:
    session_id: str
    task_lineage_id: str
    workspace: str


@dataclass(frozen=True)
class RunScope:
    task: TaskScope
    interaction_run_id: str
    agent_id: str


@dataclass(frozen=True)
class WorkspaceVersion:
    generation: int = 0
    source_generation: int = 0
    acceptance_generation: int = 0
    scope_complete: bool = True


@dataclass(frozen=True)
class CommandIdentity:
    argv: tuple[str, ...]
    cwd: str
    environment: tuple[tuple[str, str], ...] = ()
    opaque: bool = False
    original: str = field(default="", compare=False)


@dataclass(frozen=True)
class CheckSpec:
    id: str
    command: CommandIdentity
    capability: str
    targets: tuple[str, ...] = ()
    required: bool = False
    stage: str = "targeted"
    source: str = "model_execution"
    requirement_ids: tuple[str, ...] = ()
    native_runner_kind: str = ""
    covers: tuple[str, ...] = ()
    coverage_mode: str = "identity"


CHECK_SOURCES = frozenset({"user_declared", "planner", "failure_evidence", "model_execution"})


def normalize_check_authority(spec: CheckSpec) -> CheckSpec:
    """观察不产生验收权；未知来源交给恢复完整性规则处理。"""
    return replace(spec, required=False) if spec.source in {"model_execution", "failure_evidence"} else spec


@dataclass(frozen=True)
class ActiveExecution:
    check_ids: tuple[str, ...]
    mutates: bool = False
    verifies: bool = False


@dataclass(frozen=True)
class ExecutionFact:
    execution_id: str
    scope: RunScope
    command: CommandIdentity
    outcome: str
    before: WorkspaceVersion
    after: WorkspaceVersion
    capabilities: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    exit_code: int | None = None
    in_process: bool = False
    test_count: int | None = None
    output_complete: bool = True
    scope_complete: bool = True
    reliable: bool = True
    environment_error: bool = False
    output_reference: str = ""
    reason: str = ""
    started_revision: int | None = None
    origin: str = "model"
    zone: str = ""
    test_selection_failure: str = ""


def is_behavior_failure(fact: ExecutionFact) -> bool:
    """可信执行的业务负证据；空选择和环境缺项没有实际行为结论。"""
    return bool(fact.capabilities and fact.reliable and fact.output_complete and fact.scope_complete
                and not fact.environment_error and not fact.test_selection_failure and fact.test_count != 0
                and (fact.outcome == "failed" or fact.exit_code not in {None, 0}))


def is_check_execution(fact: ExecutionFact, specs: tuple[CheckSpec, ...]) -> bool:
    """展示缺证只针对真实检查意图；普通程序的原始事实仍保留在域中。"""
    return bool(any(capability != "custom_assert" for capability in fact.capabilities)
                or any(spec.command == fact.command for spec in specs))


@dataclass(frozen=True)
class CheckDecision:
    check_id: str
    status: str
    execution_id: str = ""
    reason: str = ""
    evidence_generation: int | None = None
    covered_by: str = ""


@dataclass(frozen=True)
class ExecutionReceipt:
    """宿主生产者的原始回执；运行归属和版本由执行适配器绑定。"""

    producer: str
    command: str
    cwd: str
    outcome: str
    exit_code: int | None = None
    capabilities: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    in_process: bool = False
    output_complete: bool = True
    reliable: bool = True
    environment_error: bool = False
    test_count: int | None = None
    output_reference: str = ""
    reason: str = ""
    execution_id: str = field(default_factory=lambda: uuid4().hex)
    test_selection_failure: str = ""


@dataclass(frozen=True)
class TaskDefinition:
    scope: TaskScope
    authority_revision: int = 0
    authority_message_id: str = ""
    contract: TaskContract | None = None


@dataclass(frozen=True)
class WorkspaceObservation:
    version: WorkspaceVersion
    paths: tuple[str, ...] = ()
    operations: tuple[tuple[str, str], ...] = ()
    execution_id: str = ""
    keep_execution_open: bool = False


@dataclass(frozen=True)
class ArtifactFact:
    scope: TaskScope
    path: str
    operation: str
    generation: int
    fingerprint: str
    complete: bool = True
    present: bool = True
    established_revision: int = 0


@dataclass(frozen=True)
class ReviewFact:
    request_id: str
    scope: TaskScope
    requirement_ids: tuple[str, ...]
    version: WorkspaceVersion
    materials_fingerprint: str
    accepted: bool
    executed: bool = True
    basis_fingerprint: str = ""
    candidate_fingerprint: str = ""


def candidate_fingerprint(text: str, structured=None) -> str:
    """正文和结构化载荷共同标识交付候选，不能只认证其中一份。"""
    payload = json.dumps({"text": text, "structured": structured}, sort_keys=True,
                         ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class CompletionAssessment:
    status: str
    blocking_ids: tuple[str, ...] = ()
    review_required: bool = False
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class Obligation:
    check_id: str
    failure_execution_ids: tuple[str, ...]
    resolved: bool = False
    resolved_by: str = ""


@dataclass(frozen=True)
class VerificationSnapshot:
    revision: int
    scope: RunScope
    version: WorkspaceVersion
    authority_revision: int
    specs: tuple[CheckSpec, ...]
    checks: tuple[CheckDecision, ...]
    obligations: tuple[Obligation, ...]
    ready_for_completion: bool
    tests_passed: bool | None
    state: str
    requirements: tuple[RequirementStatus, ...] = ()
    integrity_errors: tuple[str, ...] = ()
    rebuild_required: bool = False
    terminal_reason: str = ""
    artifacts: tuple[ArtifactFact, ...] = ()
    last_execution_id: str = ""
    execution_ready: bool = False
    executions: tuple[ExecutionFact, ...] = ()
    targeted_pending: bool = False
    reviews: tuple[ReviewFact, ...] = ()
    candidate_fingerprint: str = ""
    strict: bool = False

    @property
    def latest_review(self) -> ReviewFact | None:
        return next((item for item in reversed(self.reviews)
                     if item.executed and item.materials_fingerprint), None)

    @property
    def behavior_failures(self) -> tuple[ExecutionFact, ...]:
        return tuple(fact for fact in self.executions if is_behavior_failure(fact))

    @property
    def review_pending(self) -> bool:
        from .requirements import requires_semantic_review

        behavior_verified = any(spec.capability in {"test", "custom_assert"}
                                and self.decision(spec.id).status == "passed" for spec in self.specs)
        # 静态检查只证明语法/类型等性质，不能代替开发目标的行为证据。
        needs_review = (bool(self.candidate_fingerprint) and not self.strict
                        or bool(self.behavior_failures)
                        or self.version.source_generation > 0 and not behavior_verified)
        semantic_pending = any(requires_semantic_review(item.requirement) and item.status != "satisfied"
                               for item in self.requirements)
        return semantic_pending or bool(needs_review and not (self.latest_review and self.latest_review.accepted))

    def assess_completion(self, proposed: str) -> CompletionAssessment:
        """收尾和冻结共用同一事实裁决；缺证不能替代明确条件未满足。"""
        if proposed not in {"completed", "completed_unverified"}:
            return CompletionAssessment(proposed)
        if self.integrity_errors:
            return CompletionAssessment("error", reasons=self.integrity_errors)
        blocking = tuple(spec.id for spec in self.specs
                         if spec.required and self.decision(spec.id).status not in {"passed", "covered"})
        missing = tuple(item.requirement.id for item in self.requirements if not item.deterministic_ready)
        if blocking or self.targeted_pending or missing:
            return CompletionAssessment("blocked", (*blocking, *missing), reasons=("Explicit requirements remain unconfirmed.",))
        if not self.execution_ready or self.rebuild_required:
            explicit = tuple(item.requirement.id for item in self.requirements
                             if "semantic_review" in item.requirement.required_evidence and item.status != "satisfied")
            return CompletionAssessment("blocked" if explicit else "completed_unverified", explicit,
                reasons=("Execution or workspace evidence remains unsettled.",))
        if self.latest_review is not None and not self.latest_review.accepted:
            return CompletionAssessment("blocked", review_required=True, reasons=("Delivery review requires revision.",))
        if self.review_pending:
            explicit = any("semantic_review" in item.requirement.required_evidence and item.status != "satisfied"
                           for item in self.requirements)
            return CompletionAssessment("blocked" if explicit else "completed_unverified",
                review_required=True, reasons=("Current delivery has not been reviewed.",))
        return CompletionAssessment(proposed)

    def decision(self, check_id: str) -> CheckDecision:
        return next(item for item in self.checks if item.check_id == check_id)

    @property
    def acceptance_checks(self) -> tuple[dict, ...]:
        """同一命令的所有能力合成一个只读展示项，不把首项当成完整验收。"""
        groups = {}
        facts = {item.execution_id: item for item in self.executions}
        for spec in self.specs:
            if spec.source == "user_declared" and spec.required:
                groups.setdefault(spec.command, []).append(spec)
        items = []
        for command, specs in groups.items():
            decisions = [self.decision(spec.id) for spec in specs]
            passed = all(item.status == "passed" for item in decisions)
            status = ("passed" if passed else "failed" if any(item.status == "failed" for item in decisions)
                      else "blocked_environment" if any(item.status == "blocked_environment" for item in decisions) else "pending")
            execution = next((facts[item.execution_id] for item in reversed(decisions)
                              if item.execution_id in facts), None)
            items.append({"command": command.original, "cwd": command.cwd,
                "status": status, "generation": self.version.acceptance_generation if passed else None,
                "check_ids": [spec.id for spec in specs], "output": execution.reason if execution else "",
                "origin": execution.origin if execution else "", "zone": execution.zone if execution else ""})
        return tuple(items)

    def retry_eligible(self, spec: CheckSpec) -> bool:
        """重试资格只表示有真实执行依据，不提供当前版本的通过结论。"""
        from .identity import command_identity
        from .rules import supports_retry

        retry = spec.command
        identity = command_identity(retry.original, retry.cwd)
        if spec.source not in CHECK_SOURCES or not identity.argv or identity.argv[0] in {
            "verify_changed_files", "python_symbol_check", "verify_project_build",
        }:
            return False
        executed = any(supports_retry(spec, fact, self.scope.task) for fact in self.executions)
        if spec.source == "user_declared" and spec.required:
            return True
        if identity.opaque:
            # 可信回执已判断过命令可靠性；普通 2>&1 不应因 Shell 身份 opaque 而失去复测资格。
            return executed
        if spec.source == "user_declared" or (spec.source == "planner" and spec.stage == "static"):
            return True
        return executed

    def to_dict(self) -> dict:
        """只读投影；字典可被展示层修改，但不能回写域。"""
        stages = []
        facts = {item.execution_id: item for item in self.executions}
        unresolved = {item.check_id for item in self.obligations if not item.resolved}
        negatives = {item.check_id: facts.get(item.failure_execution_ids[-1]) for item in self.obligations
                     if not item.resolved and item.failure_execution_ids}
        def retry(spec):
            # 主体匹配不改写执行输入；调度方另行判断自动复测的可靠性。
            return spec.command.original
        for name in ("static", "targeted", "regression"):
            commands = []
            for spec in self.specs:
                if spec.stage != name:
                    continue
                decision = self.decision(spec.id)
                commands.append({
                    "id": spec.id, "command": spec.command.original, "retry_command": retry(spec), "cwd": spec.command.cwd,
                    "scope": spec.command.cwd, "required": spec.required, "status": decision.status,
                    "capability": spec.capability, "targets": list(spec.targets),
                    "automation_provenance": spec.source, "reason": decision.reason,
                    "retry_eligible": self.retry_eligible(spec),
                    "evidence_generation": decision.evidence_generation,
                    "execution_id": decision.execution_id, "satisfied_by": decision.covered_by,
                    "attempted_current": bool(decision.execution_id in facts
                        and facts[decision.execution_id].scope == self.scope
                        and facts[decision.execution_id].before.acceptance_generation == self.version.acceptance_generation),
                    "last_command": facts[decision.execution_id].command.original
                        if decision.status == "passed" and decision.execution_id in facts else "",
                })
            if commands:
                required = [item for item in commands if item["required"] or item["id"] in unresolved]
                failed = any(id in negatives and (negatives[id] is None or not negatives[id].environment_error)
                             for id in (item["id"] for item in required))
                blocked_stage = any(id in negatives and negatives[id] is not None and negatives[id].environment_error
                                    for id in (item["id"] for item in required))
                status = ("failed" if failed else
                          "blocked_environment" if blocked_stage else
                          "passed" if required and all(item["status"] in {"passed", "covered"} and item["id"] not in unresolved for item in required) else
                          "pending" if required else "optional")
                stages.append({"name": name, "commands": commands, "observed": [], "status": status})
        latest = next((item for item in self.checks if item.execution_id == self.last_execution_id), None)
        spec = next((item for item in self.specs if latest and item.id == latest.check_id), None)
        blocked = None
        for fact in reversed(self.executions):
            failed = [self._spec(item.check_id) for item in self.obligations
                      if not item.resolved and item.failure_execution_ids[-1] == fact.execution_id and fact.environment_error]
            if failed:
                spec_blocked = next((item for item in failed if item.stage == "static"), failed[0])
                blocked = {"command": fact.command.original, "cwd": fact.command.cwd,
                           "stage": spec_blocked.stage, "exit_code": fact.exit_code, "output": fact.reason}
                break
        pending_stages = [item["name"] for item in stages if any(
            (check["required"] and check["status"] not in {"passed", "covered"}) or check["id"] in unresolved
            for check in item["commands"])]
        return {
            "domain_revision": self.revision, "task_lineage_id": self.scope.task.task_lineage_id,
            "strict": self.strict,
            "acceptance_checks": list(self.acceptance_checks),
            "workspace": self.scope.task.workspace, "interaction_run_id": self.scope.interaction_run_id,
            "task_authority_epoch": self.authority_revision,
            "mutation_generation": self.version.generation,
            "source_mutation_generation": self.version.source_generation,
            "acceptance_mutation_generation": self.version.acceptance_generation,
            "verification_needed": not self.execution_ready, "completion_ready": self.ready_for_completion, "verification_state": self.state,
            "tests_passed": self.tests_passed, "rebuild_required": self.rebuild_required,
            "candidate_fingerprint": self.candidate_fingerprint,
            "delivery_review": ({"request_id": self.latest_review.request_id,
                "accepted": self.latest_review.accepted,
                "candidate_fingerprint": self.latest_review.candidate_fingerprint}
                if self.latest_review is not None else None),
            "mutation_scope_unknown": not self.version.scope_complete,
            "targeted_pending": self.targeted_pending,
            "review_pending": self.review_pending,
            "ordinary_failures": [{"execution_id": fact.execution_id, "command": fact.command.original,
                "cwd": fact.command.cwd, "generation": fact.after.acceptance_generation, "exit_code": fact.exit_code,
                "output_reference": fact.output_reference} for fact in self.behavior_failures],
            "last_verification": ({"command": facts[latest.execution_id].command.original if latest.execution_id in facts else spec.command.original, "stage": spec.stage,
                                   "status": latest.status, "output": latest.reason,
                                   "cwd": spec.command.cwd, "scope": spec.command.cwd,
                                   "evidence_generation": latest.evidence_generation} if spec and latest else None),
            "verification_pipeline": {"stages": stages},
            "environment_blocker": blocked,
            "next_required_stage": pending_stages[0] if pending_stages else ("targeted" if self.targeted_pending else None),
            "ignored_verifications": [{"command": fact.command.original, "cwd": fact.command.cwd,
                "reason": "no tests executed" if fact.test_count == 0 else "result cannot prove success"}
                for fact in self.executions if is_check_execution(fact, self.specs)
                and (not fact.reliable or not fact.output_complete or fact.test_count == 0)],
            "test_obligations": [
                {"check_id": item.check_id, "command": self._spec(item.check_id).command.original,
                 "retry_command": retry(self._spec(item.check_id)),
                 "retry_eligible": self.retry_eligible(self._spec(item.check_id)),
                 "stage": self._spec(item.check_id).stage,
                 "scope": self._spec(item.check_id).command.cwd, "cwd": self._spec(item.check_id).command.cwd,
                 "evidence_generation": self.decision(item.check_id).evidence_generation if item.resolved else None,
                 "status": "passed" if item.resolved else "pending",
                 "failure_execution_ids": list(item.failure_execution_ids), "resolved_by": item.resolved_by}
                for item in self.obligations
            ],
            "requirements": [item.to_dict() for item in self.requirements],
            "integrity_errors": list(self.integrity_errors), "terminal_reason": self.terminal_reason,
        }

    def _spec(self, check_id: str) -> CheckSpec:
        return next(item for item in self.specs if item.id == check_id)
