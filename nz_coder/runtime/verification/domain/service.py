"""验证状态唯一写入者；共享运行锁内提交事实，不执行外部工作。"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json

from .identity import command_identity, commands_equivalent
from .models import (
    ActiveExecution, ArtifactFact, CHECK_SOURCES, CheckDecision, CheckSpec, ExecutionFact, Obligation, ReviewFact, RunScope,
    TaskDefinition, VerificationSnapshot, WorkspaceObservation, WorkspaceVersion, normalize_check_authority,
)
from .requirements import requirement_statuses
from .rules import evaluate_check, matches_check


class VerificationDomain:
    def __init__(self, *, lock, scope: RunScope, version: WorkspaceVersion, require_targeted: bool = False):
        self._lock = lock
        self.scope = scope
        self._version = version
        self._require_targeted = require_targeted
        self._definition = TaskDefinition(scope.task)
        self._specs: dict[str, CheckSpec] = {}
        self._decisions: dict[str, CheckDecision] = {}
        self._facts: dict[str, ExecutionFact] = {}
        self._attempt_counts: dict[str, int] = {}
        self._obligations: dict[str, Obligation] = {}
        self._failure_revisions: dict[str, int] = {}
        self._starts: dict[str, int] = {}
        self._active: dict[str, ActiveExecution] = {}
        self._artifacts: dict[str, ArtifactFact] = {}
        self._reviews: dict[str, ReviewFact] = {}
        self._requirement_revisions: dict[str, int] = {}
        self._integrity_errors: list[str] = []
        self._revision = 0
        self._positive_evidence_after = 0
        self._rebuild_required = False
        self._candidate_fingerprint = ""
        self._sealed: VerificationSnapshot | None = None

    def declare_task(self, definition: TaskDefinition) -> None:
        with self._lock:
            if self._sealed is not None:
                raise ValueError("verification domain is sealed")
            if definition.scope != self.scope.task:
                raise ValueError("different task scope requires a new domain")
            if definition.authority_revision < self._definition.authority_revision:
                raise ValueError("task authority revision cannot decrease")
            previous = self._definition.contract
            incoming = definition.contract
            previous_items = {item.id: item for item in previous.requirements} if previous is not None else {}
            changed = {item.id for item in (incoming.requirements if incoming else ())
                       if previous_items.get(item.id) != item}
            # 宿主提交的是合并真实用户指令后的完整定义；不能把已撤销要求重新合回。
            incoming_ids = {item.id for item in incoming.requirements} if incoming else set()
            changed |= previous_items.keys() - incoming_ids
            if incoming is None:
                self._requirement_revisions.clear()
            if incoming is not None:
                from .requirements import apply_write_constraints
                incoming = apply_write_constraints(incoming)
                incoming_items = {item.id: item for item in incoming.requirements}
                changed |= {id for id in previous_items.keys() | incoming_items.keys()
                            if previous_items.get(id) != incoming_items.get(id)}
                self._requirement_revisions = {id: revision for id, revision in self._requirement_revisions.items()
                                              if id in incoming_items}
                self._reviews = {key: value for key, value in self._reviews.items()
                                 if not changed.intersection(value.requirement_ids)}
            value = replace(definition, contract=incoming)
            if value != self._definition:
                for requirement_id in changed:
                    if incoming is not None and any(item.id == requirement_id for item in incoming.requirements):
                        self._requirement_revisions[requirement_id] = self._revision + 1
                self._definition = value
                self._revision += 1

    def register_check(self, spec: CheckSpec) -> str:
        with self._lock:
            if self._sealed is not None:
                raise ValueError("verification domain is sealed")
            if spec.source not in CHECK_SOURCES:
                raise ValueError("unknown check source")
            spec = normalize_check_authority(spec)
            if spec.source == "planner" and not self._require_targeted:
                spec = replace(spec, required=False)
            previous = self._specs.get(spec.id)
            if previous is not None:
                if (previous.command, previous.capability, previous.targets) != (spec.command, spec.capability, spec.targets):
                    raise ValueError("check identity cannot change under an existing id")
                spec = replace(spec, required=spec.required or previous.required,
                    native_runner_kind=spec.native_runner_kind or previous.native_runner_kind,
                               requirement_ids=tuple(dict.fromkeys((*previous.requirement_ids, *spec.requirement_ids))))
                rank = {"model_execution": 0, "failure_evidence": 1, "planner": 2, "user_declared": 3}
                if rank.get(previous.source, 0) > rank.get(spec.source, 0):
                    spec = replace(spec, source=previous.source, covers=previous.covers)
            if spec != previous:
                self._specs[spec.id] = spec
                self._decisions.setdefault(spec.id, CheckDecision(spec.id, "pending"))
                self._revision += 1
            return spec.id

    def replace_user_checks(self, specs: tuple[CheckSpec, ...]) -> None:
        """同步本轮声明；撤销门槛不能删除真实失败与正在执行的资源。"""
        with self._lock:
            if self._sealed is not None:
                raise ValueError("verification domain is sealed")
            if any(spec.source != "user_declared" for spec in specs):
                raise ValueError("user check set must contain only user declarations")
            wanted = {spec.id for spec in specs}
            for id, previous in tuple(self._specs.items()):
                if previous.source == "user_declared" and id not in wanted:
                    retired = replace(previous, required=False, requirement_ids=(), covers=())
                    if retired != previous:
                        self._specs[id] = retired
                        self._obligations.pop(id, None)
                        self._failure_revisions.pop(id, None)
                        self._revision += 1
            for spec in specs:
                self.register_check(spec)

    def replace_plan(self, specs: tuple[CheckSpec, ...]) -> None:
        with self._lock:
            wanted = {spec.id for spec in specs}
            for id, previous in tuple(self._specs.items()):
                # 在途验证有独立 active 约束；撤销计划不必延迟到终态后再维护一份计划。
                if previous.source == "planner" and id not in wanted and id not in self._obligations:
                    self._specs[id] = replace(previous, required=False)
                    self._revision += 1
            for spec in specs:
                self.register_check(spec)

    def begin_execution(self, execution_id: str, check_ids: tuple[str, ...], *, mutates: bool = False,
                        verifies: bool = False) -> int:
        with self._lock:
            if self._sealed is not None:
                raise ValueError("verification domain is sealed")
            if execution_id in self._starts:
                raise ValueError("execution already started")
            if any(id not in self._specs for id in check_ids):
                raise ValueError("unregistered check")
            self._revision += 1
            self._starts[execution_id] = self._revision
            self._active[execution_id] = ActiveExecution(check_ids, mutates, verifies or bool(check_ids))
            return self._revision

    def end_execution(self, execution_id: str) -> None:
        """宿主调用组已结清；分项裁决仍由真实执行事实决定。"""
        with self._lock:
            if self._sealed is None and execution_id in self._active:
                del self._active[execution_id]
                self._revision += 1

    def execution_settled(self, execution_id: str) -> bool:
        with self._lock:
            return execution_id in self._starts and execution_id not in self._active

    @property
    def requires_targeted(self) -> bool:
        return self._require_targeted

    @property
    def task_definition(self) -> TaskDefinition:
        with self._lock:
            return self._definition

    def observe_workspace(self, observation: WorkspaceObservation) -> None:
        with self._lock:
            if self._sealed is not None:
                return
            version = observation.version
            if version.generation < self._version.generation:
                raise ValueError("workspace generation cannot decrease")
            relevant_change = version.acceptance_generation != self._version.acceptance_generation
            if not version.scope_complete:
                # 内容版本不表示观察完整性；旧区间启动的正证据必须单独失效。
                self._positive_evidence_after = self._revision + 1
            if relevant_change or not version.scope_complete:
                for id, decision in self._decisions.items():
                    if decision.status in {"passed", "covered"}:
                        self._decisions[id] = CheckDecision(id, "pending", reason="workspace changed")
                for id, obligation in self._obligations.items():
                    self._obligations[id] = replace(obligation, resolved=False, resolved_by="")
            if not observation.paths and relevant_change:
                self._artifacts = {path: replace(fact, complete=False) for path, fact in self._artifacts.items()}
            else:
                for path in observation.paths:
                    self._artifacts.pop(path, None)
            if observation.execution_id in self._active:
                execution = self._active[observation.execution_id]
                if execution.verifies or execution.check_ids or observation.keep_execution_open:
                    self._active[observation.execution_id] = replace(execution, mutates=observation.keep_execution_open)
                else:
                    del self._active[observation.execution_id]
            self._version = version
            self._revision += 1

    def observe_execution(self, fact: ExecutionFact) -> tuple[CheckDecision, ...]:
        with self._lock:
            if fact.scope != self.scope:
                raise ValueError("execution scope does not belong to this run")
            if self._sealed is not None:
                return ()
            if fact.started_revision is None and fact.execution_id in self._starts:
                fact = replace(fact, started_revision=self._starts[fact.execution_id])
            previous = self._facts.get(fact.execution_id)
            if previous is not None:
                if previous != fact:
                    self._integrity_errors.append("conflicting terminal fact: " + fact.execution_id)
                    self._revision += 1
                return ()
            self._facts[fact.execution_id] = fact
            self._active.pop(fact.execution_id, None)
            decisions = []
            for id, spec in self._specs.items():
                if not matches_check(spec, fact) or spec.capability not in fact.capabilities:
                    continue
                self._attempt_counts[id] = self._attempt_counts.get(id, 0) + 1
                decision = evaluate_check(spec, fact, self._version)
                if decision.status == "passed" and not self._positive_evidence_current(fact):
                    decision = CheckDecision(id, "pending", reason="execution started before workspace scope loss")
                obligation = self._obligations.get(id)
                if spec.required and decision.status in {"failed", "blocked_environment"}:
                    failures = (*obligation.failure_execution_ids, fact.execution_id) if obligation else (fact.execution_id,)
                    self._obligations[id] = Obligation(id, failures)
                    self._failure_revisions[id] = self._revision + 1
                elif decision.status == "passed" and obligation:
                    started = self._starts.get(fact.execution_id,
                        fact.started_revision if fact.started_revision is not None else -1)
                    if started < self._failure_revisions[id]:
                        decision = replace(decision, status="pending", reason="execution started before unresolved failure")
                    else:
                        self._obligations[id] = replace(obligation, resolved=True, resolved_by=fact.execution_id)
                if decision.status != "pending" or self._decisions[id].status not in {"failed", "blocked_environment"}:
                    self._decisions[id] = decision
                if decision.status == "passed" and spec.source == "user_declared":
                    for covered_id in spec.covers:
                        candidate = self._specs.get(covered_id)
                        if (candidate and candidate.source == "planner" and covered_id not in self._obligations
                                and not any(matches_check(candidate, old) and candidate.capability in old.capabilities
                                            for old in self._facts.values())
                                and not (self._require_targeted and candidate.stage == "static")):
                            self._decisions[covered_id] = CheckDecision(covered_id, "covered", fact.execution_id,
                                "covered by explicit user acceptance", fact.after.generation, spec.id)
                decisions.append(decision)
            self._revision += 1
            return tuple(decisions)

    def _positive_evidence_current(self, fact: ExecutionFact) -> bool:
        started = self._starts.get(fact.execution_id, fact.started_revision)
        return not self._positive_evidence_after or (started is not None and started > self._positive_evidence_after)

    def observe_artifact(self, fact: ArtifactFact) -> None:
        with self._lock:
            if fact.scope != self.scope.task:
                raise ValueError("artifact scope does not belong to this task")
            if self._sealed is None:
                previous = self._artifacts.get(fact.path)
                # 重新扫描只恢复完整性；不能把未改过的旧文件重贴为新要求的写入。
                def identity(item):
                    return (item.scope, item.path, item.operation, item.generation, item.fingerprint, item.present)
                revision = (previous.established_revision if previous is not None and identity(previous) == identity(fact)
                            else self._revision + 1)
                fact = replace(fact, established_revision=revision)
                if previous == fact:
                    return
                self._artifacts[fact.path] = fact
                self._revision += 1

    def review_basis(self) -> str:
        """只摘要审查所依赖的材料状态，不把评审自身和控制计数放入身份。"""
        with self._lock:
            value = {"definition": asdict(self._definition), "version": asdict(self._version),
                     "positive_evidence_after": self._positive_evidence_after,
                     "specs": [asdict(item) for item in self._specs.values()],
                     "checks": [asdict(item) for item in self._decisions.values()],
                     "artifacts": [asdict(item) for item in self._artifacts.values()],
                     "executions": [asdict(item) for item in self._facts.values()]}
            return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def observe_review(self, fact: ReviewFact) -> None:
        with self._lock:
            if fact.scope != self.scope.task:
                raise ValueError("review scope does not belong to this task")
            if self._sealed is None:
                self._reviews[fact.request_id] = fact
                self._revision += 1

    def set_completion_candidate(self, fingerprint: str) -> None:
        """候选属于运行内身份；改变正文也必须使正审查和冻结版本失效。"""
        with self._lock:
            if self._sealed is not None:
                raise ValueError("verification domain is sealed")
            if fingerprint != self._candidate_fingerprint:
                self._candidate_fingerprint = fingerprint
                self._revision += 1

    def snapshot(self) -> VerificationSnapshot:
        return self._snapshot()

    def _snapshot(self, *, rebuilding: bool = False) -> VerificationSnapshot:
        with self._lock:
            if self._sealed is not None:
                return self._sealed
            specs, decisions = tuple(self._specs.values()), tuple(self._decisions.values())
            basis = self.review_basis()
            reviews = tuple(item for item in self._reviews.values()
                            if item.basis_fingerprint == basis
                            and item.candidate_fingerprint == self._candidate_fingerprint)
            requirements = requirement_statuses(self._definition.contract, specs, decisions,
                tuple(self._artifacts.values()), reviews, self._version,
                tuple(self._facts.values()), self._requirement_revisions)
            active_required = any(item.mutates or item.verifies for item in self._active.values())
            any_pass = any(item.status == "passed" for item in decisions)
            required_pending = any(self._decisions[spec.id].status not in {"passed", "covered"} for spec in specs if spec.required)
            unresolved = any(not item.resolved for item in self._obligations.values())
            test_decisions = [self._decisions[item.id] for item in specs if item.capability == "test"
                              and self._decisions[item.id].status != "covered"]
            targeted_pending = self._require_targeted and self._version.source_generation > 0 and not any(
                item.status == "passed" for item in test_decisions)
            execution_ready = not (required_pending or unresolved or active_required or targeted_pending
                         or not self._version.scope_complete or self._integrity_errors)
            ready = execution_ready and (not self._rebuild_required or rebuilding) and all(item.status == "satisfied" for item in requirements)
            unresolved_checks = {item.check_id for item in self._obligations.values() if not item.resolved}
            negative = [self._facts.get(item.failure_execution_ids[-1]) for item in self._obligations.values()
                        if not item.resolved and item.failure_execution_ids]
            failed = any(item is None or not item.environment_error for item in negative)
            blocked = any(item is not None and item.environment_error for item in negative)
            state = ("failed_repairable" if failed else "blocked_environment" if blocked
                     else "passed" if execution_ready and any_pass else "unverified" if execution_ready else "verifying")
            test_ids = {spec.id for spec in specs if spec.capability == "test"}
            test_pending = targeted_pending or any(
                spec.required and self._decisions[spec.id].status not in {"passed", "covered"}
                for spec in specs if spec.id in test_ids)
            test_pending = test_pending or bool(unresolved_checks.intersection(test_ids)) or any(
                test_ids.intersection(item.check_ids) for item in self._active.values())
            required_tests = [self._decisions[item.id] for item in specs if item.capability == "test" and item.required]
            tests_passed = False if test_pending else True if (
                required_tests and all(item.status in {"passed", "covered"} for item in required_tests)
                or self._require_targeted and any(item.status == "passed" for item in test_decisions)) else None
            snapshot = VerificationSnapshot(self._revision, self.scope, self._version,
                self._definition.authority_revision, specs, decisions, tuple(self._obligations.values()),
                ready, tests_passed, state, requirements, tuple(self._integrity_errors), self._rebuild_required and not rebuilding,
                artifacts=tuple(self._artifacts.values()),
                last_execution_id=next(reversed(self._facts), ""), execution_ready=execution_ready, executions=tuple(self._facts.values()), targeted_pending=targeted_pending,
                reviews=reviews, candidate_fingerprint=self._candidate_fingerprint, strict=self._require_targeted,
                workspace_mutating=any(item.mutates for item in self._active.values()))
            return replace(snapshot, ready_for_completion=snapshot.assess_completion("completed").status == "completed")

    def rebuild_from_current_acceptance(self) -> bool:
        """未知旧计划只有当前授权合同的完整新证据可以覆盖。"""
        with self._lock:
            if self._sealed is not None or not self._rebuild_required:
                return False
            snapshot = self._snapshot(rebuilding=True)
            current_acceptance = any(
                spec.source == "user_declared" and self._decisions[spec.id].status == "passed"
                and self._facts[self._decisions[spec.id].execution_id].scope == self.scope
                for spec in self._specs.values() if self._decisions[spec.id].execution_id in self._facts)
            if not current_acceptance or not snapshot.ready_for_completion:
                return False
            self._rebuild_required = False
            self._revision += 1
            return True

    def seal(self, *, expected_revision: int, terminal_reason: str,
             changed_python_sources: tuple[str, ...] | None = ()) -> VerificationSnapshot:
        with self._lock:
            if self._sealed is not None:
                if terminal_reason != self._sealed.terminal_reason:
                    raise ValueError("Cannot change frozen verification status")
                return self._sealed
            snapshot = self.snapshot()
            if expected_revision != snapshot.revision:
                raise ValueError("verification revision changed before freeze")
            if terminal_reason == "completed" and (not snapshot.ready_for_completion
                    or snapshot.assess_completion("completed").status != "completed"):
                raise ValueError("verification remains unconfirmed")
            self._sealed = replace(snapshot, terminal_reason=terminal_reason,
                                   changed_python_syntax=snapshot.python_syntax_gap(changed_python_sources))
            return self._sealed

    def checkpoint(self) -> dict:
        from .checkpoint import encode_domain

        return encode_domain(self)

    def contract_projection(self, command: str, targets: tuple[str, ...]) -> dict:
        """合同进度只投影域事实；旧 activation 的尝试不能跳过本次复测。"""
        with self._lock:
            identity = command_identity(command, self.scope.task.workspace)
            specs = [spec for spec in self._specs.values() if spec.source == "user_declared"
                     and commands_equivalent(spec.command, identity)]
            current = [fact for fact in self._facts.values() if fact.scope == self.scope
                       and (fact.outcome != "succeeded" or self._positive_evidence_current(fact))
                       and any(matches_check(spec, fact) for spec in specs)]
            latest = current[-1] if current else None
            decisions = [self._decisions[spec.id] for spec in specs]
            passed = (True if latest and decisions and all(item.status == "passed" for item in decisions)
                      else False if any(item.status == "failed" for item in decisions) else None)
            return {"command": command, "targets": list(targets),
                    "attempted_generation": latest.before.acceptance_generation if latest else -1,
                    "attempts": max((self._attempt_counts.get(spec.id, 0) for spec in specs), default=0),
                    "passed": passed, "output": latest.reason if latest else "",
                    "source": latest.origin if latest else "", "zone": latest.zone if latest else ""}
