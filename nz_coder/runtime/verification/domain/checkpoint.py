"""当前域检查点编解码；不从旧布尔值或显示 metadata 恢复通过。"""
from __future__ import annotations

from dataclasses import asdict, fields, replace
import json

from nz_coder.protocol.public_error import PublicError, PublicRuntimeError

from .identity import command_identity, observed_check_subject
from .models import (
    ArtifactFact, CHECK_SOURCES, CheckDecision, CheckSpec, CommandIdentity, ExecutionFact, Obligation, RunScope,
    TaskDefinition, TaskScope, WorkspaceVersion, normalize_check_authority,
)
from .requirements import Requirement, TaskContract
from .service import VerificationDomain


SCHEMA = "nz.verification.v3"


class CheckpointRestoreError(PublicRuntimeError):
    """已有验证检查点无法安全恢复。"""

    def __init__(self, *_args):
        super().__init__(PublicError("verification_restore_failed", "Verification checkpoint restore failed."))


class AuthoritySourceUnavailable(CheckpointRestoreError):
    """旧合同无法核对原始用户来源时，阻断激活并保留原文件。"""

    def __init__(self):
        PublicRuntimeError.__init__(self, PublicError("authority_source_unavailable",
            "Original user instructions are unavailable; the previous task cannot be resumed safely."))



def _shape(cls, value):
    if not isinstance(value, dict) or set(value) != {item.name for item in fields(cls)}:
        raise ValueError("invalid " + cls.__name__)
    return dict(value)


def _strings(value):
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("invalid string sequence")
    return tuple(value)


def _version(value):
    data = _shape(WorkspaceVersion, value)
    if any(type(data[key]) is not int or data[key] < 0
           for key in ("generation", "source_generation", "acceptance_generation")) or type(data["scope_complete"]) is not bool:
        raise ValueError("invalid workspace version")
    return WorkspaceVersion(**data)


def _task(value):
    data = _shape(TaskScope, value)
    if any(not isinstance(item, str) or not item for item in data.values()):
        raise ValueError("invalid task identity")
    return TaskScope(**data)


def _scope(value):
    data = _shape(RunScope, value)
    data["task"] = _task(data["task"])
    return RunScope(**data)


def _identity(value):
    data = _shape(CommandIdentity, value)
    data["argv"] = _strings(data["argv"])
    data["environment"] = tuple(_strings(pair) for pair in data["environment"])
    if (type(data["opaque"]) is not bool or not isinstance(data["cwd"], str)
            or not isinstance(data["original"], str) or any(len(pair) != 2 for pair in data["environment"])):
        raise ValueError("invalid command identity")
    return CommandIdentity(**data)


def _spec(value):
    data = _shape(CheckSpec, value)
    data["command"] = _identity(data["command"])
    data["targets"] = _strings(data["targets"])
    data["requirement_ids"] = _strings(data["requirement_ids"])
    data["covers"] = _strings(data["covers"])
    if type(data["required"]) is not bool or data["stage"] not in {"static", "targeted", "regression"}:
        raise ValueError("invalid check definition")
    # 当前格式的分类可重新派生，历史命令、失败 ID 和已有分类保持原样。
    if not data["native_runner_kind"] and observed_check_subject(data["command"]) is not None:
        data["native_runner_kind"] = "observed_output"
    if not isinstance(data["source"], str):
        raise ValueError("invalid check source")
    return normalize_check_authority(CheckSpec(**data))


def _fact(value):
    data = _shape(ExecutionFact, value)
    data["scope"] = _scope(data["scope"])
    data["command"] = _identity(data["command"])
    data["before"], data["after"] = _version(data["before"]), _version(data["after"])
    data["capabilities"], data["targets"] = _strings(data["capabilities"]), _strings(data["targets"])
    if type(data["in_process"]) is not bool:
        raise ValueError("invalid execution ownership")
    from .rules import validate_test_selection_attestation
    validate_test_selection_attestation(data["command"], outcome=data["outcome"], exit_code=data["exit_code"], test_count=data["test_count"], capabilities=data["capabilities"], output_complete=data["output_complete"], reliable=data["reliable"], test_selection_failure=data["test_selection_failure"])
    return ExecutionFact(**data)


def _definition(value):
    data = _shape(TaskDefinition, value)
    data["scope"] = _task(data["scope"])
    if type(data["authority_revision"]) is not int or data["authority_revision"] < 0:
        raise ValueError("invalid authority revision")
    if data["contract"] is not None:
        contract = _shape(TaskContract, data["contract"])
        requirements = []
        for item in contract["requirements"]:
            requirement = _shape(Requirement, item)
            for key in ("expected_artifacts", "depends_on", "required_evidence"):
                requirement[key] = _strings(requirement[key])
            requirement["artifact_operations"] = tuple(_strings(pair) for pair in requirement["artifact_operations"])
            requirements.append(Requirement(**requirement))
        contract["requirements"] = tuple(requirements)
        contract["constraints"] = _strings(contract["constraints"])
        contract["acceptance_commands"] = _strings(contract["acceptance_commands"])
        data["contract"] = TaskContract(**contract)
    return TaskDefinition(**data)


def encode_domain(domain: VerificationDomain) -> dict:
    """只存活跃义务及当前裁决所需事实，长轨迹仍由既有工件保存。"""
    with domain._lock:
        wanted = {item.execution_id for item in domain._decisions.values() if item.execution_id}
        wanted.update(id for item in domain._obligations.values() for id in item.failure_execution_ids)
        wanted.update(item.resolved_by for item in domain._obligations.values() if item.resolved_by)
        wanted.update(fact.execution_id for fact in domain._facts.values()
                      if fact.outcome == "failed" or fact.exit_code not in {None, 0})
        failed_commands = {fact.command for fact in domain._facts.values()
                           if fact.outcome == "failed" or fact.exit_code not in {None, 0}}
        for command in failed_commands:
            latest = next(fact for fact in reversed(domain._facts.values()) if fact.command == command)
            wanted.add(latest.execution_id)
        from .rules import matches_check, supports_retry
        for spec in domain._specs.values():
            if not spec.required and spec.id not in domain._obligations and not any(
                    matches_check(spec, fact) and fact.execution_id in wanted for fact in domain._facts.values()):
                continue
            latest = next((fact for fact in reversed(domain._facts.values())
                           if spec.capability in fact.capabilities and matches_check(spec, fact)), None)
            if latest is not None:
                wanted.add(latest.execution_id)
            support = next((fact for fact in reversed(domain._facts.values())
                            if supports_retry(spec, fact, domain.scope.task)), None)
            if support is not None:
                wanted.add(support.execution_id)
        data = {
            "schema": SCHEMA, "scope": asdict(domain.scope), "version": asdict(domain._version),
            "definition": asdict(domain._definition), "revision": domain._revision,
            "requirement_revisions": dict(domain._requirement_revisions),
            "require_targeted": domain._require_targeted, "specs": [asdict(item) for item in domain._specs.values()],
            "facts": [asdict(fact) for id, fact in domain._facts.items() if id in wanted],
            "decisions": [asdict(item) for item in domain._decisions.values()],
            "artifacts": [asdict(item) for item in domain._artifacts.values()],
            "attempt_counts": dict(domain._attempt_counts),
            "obligations": [asdict(item) for item in domain._obligations.values()],
            "failure_revisions": dict(domain._failure_revisions),
            "integrity_errors": list(domain._integrity_errors), "rebuild_required": domain._rebuild_required or bool(domain._active),
        }
        return json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))


def restore_domain(checkpoint, *, lock, scope: RunScope, version: WorkspaceVersion,
                   require_targeted: bool | None = None, authority: TaskDefinition | None = None) -> VerificationDomain:
    domain = VerificationDomain(lock=lock, scope=scope, version=version, require_targeted=bool(require_targeted))
    if checkpoint is None:
        domain._rebuild_required = True
        return domain
    try:
        expected = {"schema", "scope", "version", "definition", "revision", "require_targeted", "specs", "facts",
                    "decisions", "artifacts", "requirement_revisions", "attempt_counts", "obligations", "failure_revisions", "integrity_errors", "rebuild_required"}
        if not isinstance(checkpoint, dict) or set(checkpoint) != expected or checkpoint["schema"] not in {"nz.verification.v1", "nz.verification.v2", SCHEMA}:
            raise ValueError("invalid verification checkpoint schema")
        needs_authority = checkpoint["schema"] != SCHEMA
        legacy = checkpoint["schema"] == "nz.verification.v1"
        if legacy:
            old_fields = {item.name for item in fields(ExecutionFact)} - {"test_selection_failure"}
            if not isinstance(checkpoint["facts"], list) or any(not isinstance(item, dict) or set(item) != old_fields for item in checkpoint["facts"]):
                raise ValueError("invalid v1 execution fact")
            checkpoint = dict(checkpoint, schema=SCHEMA, facts=[dict(item, test_selection_failure="") for item in checkpoint["facts"]])
        if _scope(checkpoint["scope"]).task != scope.task or _definition(checkpoint["definition"]).scope != scope.task:
            raise ValueError("verification checkpoint scope mismatch")
        _version(checkpoint["version"])
        if type(checkpoint["revision"]) is not int or checkpoint["revision"] < 0:
            raise ValueError("invalid verification revision")
        if type(checkpoint["require_targeted"]) is not bool or type(checkpoint["rebuild_required"]) is not bool:
            raise ValueError("invalid verification checkpoint flag")
        if checkpoint["rebuild_required"]:
            # v3 没有旧进程身份；扫描磁盘或新测试通过不能证明旧写入者已经退出。
            raise CheckpointRestoreError("Historical execution resources remain unconfirmed")
        specs = tuple(_spec(item) for item in checkpoint["specs"])
        if len({spec.id for spec in specs}) != len(specs):
            raise ValueError("duplicate check identity")
        protected = {item["id"] for item in checkpoint["specs"] if item["required"]}
        facts = tuple(_fact(item) for item in checkpoint["facts"])
        if any(fact.scope.task != scope.task for fact in facts):
            raise ValueError("historical fact scope mismatch")
        fact_index = {fact.execution_id: fact for fact in facts}
        if len(fact_index) != len(facts):
            raise ValueError("duplicate execution fact")
        obligations = []
        for item in checkpoint["obligations"]:
            data = _shape(Obligation, item)
            data["failure_execution_ids"] = _strings(data["failure_execution_ids"])
            if (data["check_id"] not in {spec.id for spec in specs} or type(data["resolved"]) is not bool
                    or type(data["resolved_by"]) is not str
                    or (data["resolved"] and data["resolved_by"] not in fact_index)
                    or (not data["resolved"] and data["resolved_by"])):
                raise ValueError("invalid verification obligation")
            # 新 activation 没有环境等价证明，旧正证据和结清状态都需重新确认。
            obligations.append(Obligation(data["check_id"], data["failure_execution_ids"]))
        decisions = tuple(CheckDecision(**_shape(CheckDecision, item)) for item in checkpoint["decisions"])
        if len(decisions) != len(specs) or {item.check_id for item in decisions} != {spec.id for spec in specs}:
            raise ValueError("checkpoint checks are incomplete")
        if any(item.execution_id and item.execution_id not in fact_index for item in decisions):
            raise ValueError("checkpoint decision has no execution fact")
        counts = checkpoint["attempt_counts"]
        if not isinstance(counts, dict) or any(id not in {spec.id for spec in specs}
                or type(count) is not int or count < 0 for id, count in counts.items()):
            raise ValueError("invalid check attempt counts")
        failures = checkpoint["failure_revisions"]
        if (not isinstance(failures, dict) or any(key not in {spec.id for spec in specs}
                or type(value) is not int or not 0 <= value <= checkpoint["revision"] for key, value in failures.items())
                or any(item.check_id not in failures for item in obligations)):
            raise ValueError("invalid failure revision")
        from .rules import is_test_selection_failure, matches_check, observed_check_stage
        spec_index = {spec.id: spec for spec in specs}
        if any(not item.failure_execution_ids or any(
                id not in fact_index or not matches_check(spec_index[item.check_id], fact_index[id])
                or spec_index[item.check_id].capability not in fact_index[id].capabilities
                or not (fact_index[id].outcome == "failed"
                    or (type(fact_index[id].exit_code) is int and fact_index[id].exit_code != 0))
                for id in item.failure_execution_ids) for item in obligations):
            raise ValueError("checkpoint obligation has no matching failure fact")
        selection_only = {item.check_id for item in obligations if item.failure_execution_ids and all(
            id in fact_index and matches_check(spec_index[item.check_id], fact_index[id])
            and is_test_selection_failure(spec_index[item.check_id], fact_index[id])
            for id in item.failure_execution_ids)}
        # 原始失败事实仍保留；只纠正完整证据证明没有业务负结果的选择义务。
        obligations = [item for item in obligations if item.check_id not in selection_only
                       and spec_index[item.check_id].required]
        artifacts = []
        for raw in checkpoint["artifacts"]:
            artifact = _shape(ArtifactFact, raw)
            artifact["scope"] = _task(artifact["scope"])
            if (artifact["scope"] != scope.task or not isinstance(artifact["path"], str)
                    or artifact["operation"] not in {"create", "change", "delete", "read"}
                    or type(artifact["generation"]) is not int or artifact["generation"] < 0
                    or type(artifact["established_revision"]) is not int
                    or not 0 <= artifact["established_revision"] <= checkpoint["revision"]
                    or not isinstance(artifact["fingerprint"], str)
                    or type(artifact["complete"]) is not bool or type(artifact["present"]) is not bool):
                raise ValueError("invalid artifact provenance")
            artifacts.append(replace(ArtifactFact(**artifact), complete=False))
        definition = _definition(checkpoint["definition"])
        requirement_revisions = checkpoint["requirement_revisions"]
        ids = {item.id for item in definition.contract.requirements} if definition.contract else set()
        if not isinstance(requirement_revisions, dict) or any(key not in ids or type(value) is not int
                or not 0 <= value <= checkpoint["revision"] for key, value in requirement_revisions.items()):
            raise ValueError("invalid requirement revision")
        integrity_errors = list(_strings(checkpoint["integrity_errors"]))
        inconsistent = {spec.id for spec in specs
                        if command_identity(spec.command.original, spec.command.cwd) != spec.command}
        for id in sorted(inconsistent):
            error = "inconsistent command identity: " + id
            if error not in integrity_errors:
                integrity_errors.append(error)
        # 先校验全部引用，再纠正自动误登记；不能借清理掩盖损坏或越权状态。
        from nz_coder.intelligence.verification_planner import classify_verification_command
        retired = {spec.id for spec in specs
                   if spec.source in {"model_execution", "failure_evidence"}
                   and spec.id not in protected and spec.id not in inconsistent
                   and observed_check_stage(spec.command, spec.capability,
                       classify_verification_command(spec.command.original)) is None
                   and not any(fact.in_process and spec.capability in fact.capabilities and matches_check(spec, fact)
                               for fact in facts)}
        specs = tuple(spec for spec in specs if spec.id not in retired)
        decisions = tuple(item for item in decisions if item.check_id not in retired)
        obligations = [item for item in obligations if item.check_id not in retired]
        domain._artifacts = {fact.path: fact for fact in artifacts}
        domain._definition = definition
        domain._requirement_revisions = dict(requirement_revisions)
        domain._revision = checkpoint["revision"]
        # 恢复后的正结论必须来自本次新启动；沿用已有时序，无需另存格式。
        domain._positive_evidence_after = domain._revision
        domain._require_targeted = checkpoint["require_targeted"] if require_targeted is None else require_targeted
        domain._specs = {spec.id: spec for spec in specs}
        domain._decisions = {item.check_id: item if item.status in {"failed", "blocked_environment"} and item.check_id not in selection_only
                             else CheckDecision(item.check_id, "pending", reason="activation environment requires confirmation")
                             for item in decisions}
        domain._facts = {fact.execution_id: fact for fact in facts}
        domain._attempt_counts = {id: count for id, count in counts.items() if id not in retired}
        domain._obligations = {item.check_id: item for item in obligations}
        domain._failure_revisions = {id: revision for id, revision in failures.items()
                                     if id in domain._obligations}
        domain._integrity_errors = integrity_errors
        domain._rebuild_required = checkpoint["rebuild_required"] or bool(inconsistent)
        for spec in specs:
            if spec.source not in CHECK_SOURCES:
                error = "unknown check source: " + spec.id
                if error not in domain._integrity_errors:
                    domain._integrity_errors.append(error)
                domain._rebuild_required = True
        if needs_authority:
            if authority is None:
                raise AuthoritySourceUnavailable()
            domain.declare_task(authority)
            # 旧标签可能混入模型建议；授权集合只能由后续宿主同步重新建立。
            domain._specs = {id: replace(spec, required=False, requirement_ids=(), covers=())
                             for id, spec in domain._specs.items()}
            domain._obligations.clear()
            domain._failure_revisions.clear()
        return domain
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        raise CheckpointRestoreError("verification checkpoint restore failed") from error
