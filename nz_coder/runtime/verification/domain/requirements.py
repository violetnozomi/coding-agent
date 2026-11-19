"""任务需求的纯值与证据投影，不解析模型输出、不读取文件。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import Mapping

from nz_coder.foundation.file_kinds import is_documentation_file, is_python_cache_path, is_test_file
from .models import ArtifactFact, CheckDecision, CheckSpec, ReviewFact, WorkspaceVersion


@dataclass(frozen=True)
class EvidenceRef:
    type: str
    path: str | None = None
    command: str | None = None
    generation: int = 0
    fingerprint: str | None = None


@dataclass(frozen=True)
class Requirement:
    id: str
    description: str
    kind: str
    expected_artifacts: tuple[str, ...] = ()
    satisfaction_mode: str = "mixed"
    depends_on: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    artifact_operations: tuple[tuple[str, str], ...] = ()


def is_generic_artifact_requirement(requirement: Requirement) -> bool:
    return requirement.kind in {"test", "docs"} and not requirement.expected_artifacts


def requires_semantic_review(requirement: Requirement) -> bool:
    """语义模式及显式审查证据共享入口，类别本身不产生验收权。"""
    return requirement.satisfaction_mode in {"semantic", "mixed"} or "semantic_review" in requirement.required_evidence


def matches_artifact_kind(requirement: Requirement, path: str) -> bool:
    # 编译缓存不是作者编写的测试/文档，权限所用的全局路径分类仍保持不变。
    if is_python_cache_path(path):
        return False
    return is_test_file(path) if requirement.kind == "test" else is_documentation_file(path)


@dataclass(frozen=True)
class TaskContract:
    objective: str = ""
    requirements: tuple[Requirement, ...] = ()
    constraints: tuple[str, ...] = ()
    acceptance_commands: tuple[str, ...] = ()
    contract_version: int = 2

    def to_dict(self) -> dict:
        return {
            "objective": self.objective,
            "requirements": [{**asdict(item), "expected_artifacts": list(item.expected_artifacts),
                              "depends_on": list(item.depends_on), "required_evidence": list(item.required_evidence),
                              "artifact_operations": [list(pair) for pair in item.artifact_operations]}
                             for item in self.requirements],
            "constraints": list(self.constraints), "acceptance_commands": list(self.acceptance_commands),
            "contract_version": self.contract_version,
        }


def apply_write_constraints(contract: TaskContract) -> TaskContract:
    """合同合并与域合并共用禁改约束，防止已撤销的写义务被旧定义重新加回。"""
    prefix = "Do not modify "
    immutable = "Do not modify test files." in contract.constraints
    readonly = {clause[len(prefix):-1] for clause in contract.constraints
                if clause.startswith(prefix) and clause.endswith(".") and clause != "Do not modify test files."}
    requirements = []
    removed = set()

    def allowed(path: str) -> bool:
        return path not in readonly and not (immutable and is_test_file(path))

    for item in contract.requirements:
        artifacts = tuple(path for path in item.expected_artifacts if allowed(path))
        if item.expected_artifacts and not artifacts and item.kind in {"artifact", "test", "docs"}:
            removed.add(item.id)
            continue
        requirements.append(replace(item, expected_artifacts=artifacts,
            artifact_operations=tuple((path, operation) for path, operation in item.artifact_operations if allowed(path))))
    return replace(contract, requirements=tuple(
        replace(item, depends_on=tuple(id for id in item.depends_on if id not in removed)) for item in requirements))


@dataclass(frozen=True)
class RequirementStatus:
    requirement: Requirement
    status: str
    evidence: tuple[EvidenceRef, ...] = ()
    deterministic_ready: bool = False

    def to_dict(self) -> dict:
        return {"requirement": TaskContract(requirements=(self.requirement,)).to_dict()["requirements"][0],
                "status": self.status, "evidence": [asdict(item) for item in self.evidence],
                "mutation_generation": self.mutation_generation, "deterministic_ready": self.deterministic_ready}

    @property
    def mutation_generation(self) -> int:
        return max((item.generation for item in self.evidence), default=0)


@dataclass(frozen=True)
class RequirementLedgerView:
    """只读需求投影；没有观察、写入或恢复验证状态的能力。"""

    items: Mapping[str, RequirementStatus]
    latest_generation: int = 0
    latest_verification_generation: int = -1

    def __post_init__(self):
        object.__setattr__(self, "items", MappingProxyType(dict(self.items)))

    @classmethod
    def from_snapshot(cls, snapshot):
        verified = any(item.status == "passed" for item in snapshot.checks)
        return cls({item.requirement.id: item for item in snapshot.requirements},
                   snapshot.version.acceptance_generation,
                   snapshot.version.acceptance_generation if verified else -1)

    @classmethod
    def from_contract(cls, contract: TaskContract):
        return cls({item.id: RequirementStatus(item, "pending") for item in contract.requirements})

    @classmethod
    def from_dict(cls, value: dict):
        """读取当前公开投影供报告使用，所得值不能回写验证域。"""
        if not isinstance(value, dict):
            raise ValueError("requirement projection must be an object")
        items = {}
        for raw in value.get("items", ()):
            definition = dict(raw["requirement"])
            for key in ("expected_artifacts", "depends_on", "required_evidence"):
                definition[key] = tuple(definition.get(key, ()))
            definition["artifact_operations"] = tuple(tuple(pair) for pair in definition.get("artifact_operations", ()))
            requirement = Requirement(**definition)
            items[requirement.id] = RequirementStatus(requirement, raw["status"],
                tuple(EvidenceRef(**item) for item in raw.get("evidence", ())), bool(raw.get("deterministic_ready")))
        return cls(items, int(value.get("latest_generation", 0)), int(value.get("latest_verification_generation", -1)))

    def to_dict(self) -> dict:
        return {"items": [item.to_dict() for item in self.items.values()],
                "latest_generation": self.latest_generation,
                "latest_verification_generation": self.latest_verification_generation}

    def status(self, requirement_id: str) -> str:
        item = self.items.get(requirement_id)
        return item.status if item is not None else "pending"

    def unresolved(self) -> tuple[RequirementStatus, ...]:
        return tuple(item for item in self.items.values() if item.status != "satisfied")

    @staticmethod
    def semantic_review_ready(progress: RequirementStatus, generation: int, *, acceptance_required: bool = True) -> bool:
        if not requires_semantic_review(progress.requirement):
            return False
        return progress.deterministic_ready

    def semantic_review_pending_only(self, *, acceptance_required: bool = True, generation: int | None = None) -> bool:
        generation = self.latest_generation if generation is None else generation
        unresolved = self.unresolved()
        return bool(unresolved) and all(self.semantic_review_ready(item, generation,
            acceptance_required=acceptance_required) for item in unresolved)


def requirement_statuses(
    contract: TaskContract | None, specs: tuple[CheckSpec, ...], decisions: tuple[CheckDecision, ...],
    artifacts: tuple[ArtifactFact, ...], reviews: tuple[ReviewFact, ...], version: WorkspaceVersion,
    historical_facts: tuple = (), requirement_revisions: dict | None = None,
) -> tuple[RequirementStatus, ...]:
    if contract is None:
        return ()
    passed = {item.check_id: item for item in decisions if item.status == "passed"}
    execution_facts = {item.execution_id: item for item in historical_facts}
    revisions = requirement_revisions or {}
    facts = {item.path: item for item in artifacts if item.complete and item.fingerprint}
    results: dict[str, RequirementStatus] = {}
    for _ in contract.requirements:
        for requirement in contract.requirements:
            evidence = []
            generic = is_generic_artifact_requirement(requirement)
            artifact_only = requirement.kind in {"artifact", "docs"} and requirement.satisfaction_mode == "deterministic"
            operations = dict(requirement.artifact_operations)
            paths = requirement.expected_artifacts if not generic else tuple(
                path for path, fact in facts.items() if matches_artifact_kind(requirement, path)
                and fact.established_revision >= revisions.get(requirement.id, 0))
            for path in paths:
                fact = facts.get(path)
                if not fact:
                    continue
                if (artifact_only or path in operations) and fact.established_revision < revisions.get(requirement.id, 0):
                    continue
                operation = operations.get(path)
                if operation is not None and fact.operation != operation and not (operation == "change" and fact.operation == "create"):
                    continue
                if operation is None and fact.operation not in {"create", "change"}:
                    continue
                if fact.present == (operation == "delete"):
                    continue
                artifact_kind = {"create": "file_created", "change": "file_changed", "delete": "file_deleted"}[fact.operation]
                evidence.append(EvidenceRef(artifact_kind, path=path,
                                            generation=fact.generation, fingerprint=fact.fingerprint))
            # 宿主显式声明的工件类型仍需实际修改；不由测试命令名称认领业务覆盖。
            artifacts_ready = bool(evidence) if generic else (
                len(evidence) == len(requirement.expected_artifacts)
                and (bool(requirement.expected_artifacts) or requirement.kind != "artifact"))
            matched = [spec for spec in specs if spec.id in passed and requirement.id in spec.requirement_ids
                       and passed[spec.id].execution_id in execution_facts
                       and (execution_facts[passed[spec.id].execution_id].started_revision or 0) >= revisions.get(requirement.id, 0)]
            evidence.extend(EvidenceRef("verification_passed" if spec.source == "user_declared" else "command_passed",
                                        command=spec.command.original,
                                        generation=version.acceptance_generation) for spec in matched)
            relevant_reviews = [item for item in reviews if requirement.id in item.requirement_ids]
            review = relevant_reviews[-1] if relevant_reviews else None
            reviewed = bool(review and review.executed and review.accepted and review.materials_fingerprint
                            and review.version.acceptance_generation == version.acceptance_generation)
            needs_review = requires_semantic_review(requirement)
            if reviewed and needs_review:
                evidence.append(EvidenceRef("semantic_review_passed", generation=version.acceptance_generation,
                                            fingerprint=review.materials_fingerprint))
            execution_ready = True
            if requirement.kind == "verification":
                required = [spec for spec in specs if spec.required and requirement.id in spec.requirement_ids]
                execution_ready = bool(required) and all(spec.id in passed for spec in required)
            dependencies_ready = all(results.get(id) and results[id].status == "satisfied" for id in requirement.depends_on)
            prerequisites_ready = all(results.get(id) and results[id].deterministic_ready for id in requirement.depends_on)
            deterministic_ready = artifacts_ready and execution_ready and prerequisites_ready
            satisfied = deterministic_ready and dependencies_ready and (reviewed or not needs_review)
            # 历史通过可以解释之前做过什么，但不参与当前满足条件。
            from .rules import evaluate_check
            for spec in specs:
                if requirement.id not in spec.requirement_ids:
                    continue
                for fact in historical_facts:
                    if fact.after.acceptance_generation == version.acceptance_generation:
                        continue
                    if evaluate_check(spec, fact, fact.after).status == "passed":
                        old = EvidenceRef("verification_passed" if spec.source == "user_declared" else "command_passed",
                                          command=fact.command.original, generation=fact.after.acceptance_generation)
                        if old not in evidence:
                            evidence.append(old)
            results[requirement.id] = RequirementStatus(requirement,
                "satisfied" if satisfied else ("candidate" if evidence or any(f.path in requirement.expected_artifacts and f.operation != "read" for f in artifacts) else "pending"), tuple(evidence), deterministic_ready)
    return tuple(results[item.id] for item in contract.requirements)
