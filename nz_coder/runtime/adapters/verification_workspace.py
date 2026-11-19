"""宿主采集范围和产物事实；磁盘核对在运行协调锁外完成。"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path

from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.process.workspace_snapshot import SnapshotError
from nz_coder.runtime.process.workspace_scope_execution import (
    python_verification_artifact_paths, removed_python_bytecode_paths, settled_changes,
)
from nz_coder.runtime.verification.domain.models import ArtifactFact, ExecutionReceipt


@dataclass(frozen=True)
class WorkspaceSettlement:
    changes: dict | None
    observed_changes: dict
    complete: bool
    version_matches: bool
    reference: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    execution_unchanged: bool = False
    unconfirmed_caches: dict[str, str] = field(default_factory=dict)


def mutation_path_identities(workspace: Path, paths) -> dict:
    """仅对已确认写入保存目录项身份，供恢复时去重；不能证明内容或检查通过。"""
    identities = {}
    for value in paths:
        path = Path(workspace) / value
        try:
            relative = path.relative_to(workspace)
            if ".." in relative.parts:
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                identities[relative.as_posix()] = "missing"
            else:
                identities[relative.as_posix()] = [info.st_dev, info.st_ino, info.st_mode,
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns]
        except (OSError, ValueError):
            continue
    return identities


def unaccounted_workspace_changes(store, changes: dict, cursor: dict) -> dict:
    recorded = cursor.get("recorded_changes", {})
    if not recorded:
        return dict(changes)
    current = mutation_path_identities(store.workspace, recorded)
    return {path: operation for path, operation in changes.items()
            if path not in recorded or path not in current or current[path] != recorded[path]}


def collect_workspace_settlement(result, store, generation: int, cursor: dict, *,
                                 python_verification_origin: ExecutionReceipt | None = None) -> WorkspaceSettlement:
    """恢复完整范围时补记真实变化；范围错误独立于内容变化。"""
    proof = result.workspace_scope
    if proof is None:
        return WorkspaceSettlement(None, {}, bool(cursor.get("complete", True)), True)
    anchor = cursor.get("reference")
    errors = dict(proof.get("errors") or {})
    changes, matches = settled_changes(result, store, generation,
        anchor=anchor if cursor.get("complete", True) else None)
    complete = matches and changes is not None
    if not complete and not errors:
        errors["comparison"] = ("workspace scope generation or workspace mismatch" if not matches
            else "workspace scope before/after proof is unavailable or incomplete")
    observed = dict(changes or {})
    reference = ""
    if complete:
        try:
            reference = proof["after"]
            if anchor and not cursor.get("complete", True):
                observed = {**store.verification_changes(anchor, reference), **observed}
        except SnapshotError as exc:
            complete = False
            errors["recovery"] = str(exc)
    unconfirmed = {}
    if changes and complete:
        continuous = not anchor or (bool(cursor.get("complete", True)) and anchor == proof["before"])
        recovered = not cursor.get("complete", True) and bool(anchor)
        if continuous or recovered:
            # 只解释本次连续执行区间里的产物，不能把此前的外部漂移一起豁免。
            # 缓存删除沿用连续旧区间要求，不能靠补全扫描解释未知期间的删除。
            artifacts = removed_python_bytecode_paths(result, changes, store) if continuous else set()
            artifacts.update(python_verification_artifact_paths(result, changes, store,
                python_verification_origin=python_verification_origin, unconfirmed=unconfirmed))
            if recovered:
                # 补全旧未知区间时，仅解释本次新增的缓存；之前的漂移仍保留为事实。
                artifacts.difference_update(store.verification_changes(anchor, proof["before"]))
            changes = {path: operation for path, operation in changes.items() if path not in artifacts}
            observed = {path: operation for path, operation in observed.items() if path not in artifacts}
    execution_unchanged = complete and changes == {}
    if complete and not cursor.get("complete", True):
        changes = unaccounted_workspace_changes(store, observed, cursor)
    return WorkspaceSettlement(changes, observed, complete, matches, reference, errors, execution_unchanged, unconfirmed)


def collect_artifact_facts(snapshot, workspace: Path, operations: dict, generation: int) -> tuple[ArtifactFact, ...]:
    """产物必须有真实变更来源且仍在磁盘成立，删除核对目录项本身。"""
    root = Path(workspace).resolve()
    previous = {fact.path: fact for fact in snapshot.artifacts}
    paths = {path for item in snapshot.requirements for path in item.requirement.expected_artifacts}
    # 重核既有事实并纳入真实新修改；不能从文件存在推导修改，也不能改写用户明确的路径约束。
    paths.update(previous)
    from nz_coder.foundation.file_kinds import is_test_file, is_documentation_file, is_python_cache_path
    paths.update(path for path in operations if not is_python_cache_path(path)
                 and (is_test_file(path) or is_documentation_file(path)))
    facts = []
    for path in sorted(paths):
        prior = previous.get(path)
        operation = operations.get(path) or (prior.operation if prior else "read")
        if operation == "replace":
            operation = "change"
        if operation == "change" and prior is not None and prior.operation == "create":
            operation = "create"
        target = root / path
        complete, present, fingerprint = False, True, ""
        try:
            if target.resolve().is_relative_to(root):
                if operation == "delete":
                    try:
                        target.lstat()
                    except (FileNotFoundError, NotADirectoryError):
                        complete, present = True, False
                        fingerprint = hashlib.sha256((path + ":deleted").encode()).hexdigest()
                elif target.is_file():
                    _body, identity = WorkspaceFileAccess(root).read_bytes_with_identity(str(target.resolve().relative_to(root)), maximum=4 * 1024 * 1024)
                    complete, fingerprint = True, identity.content_hash
        except (OSError, ValueError, RuntimeError):
            complete = False
        evidence_generation = prior.generation if prior and prior.fingerprint == fingerprint and prior.operation == operation else generation
        facts.append(ArtifactFact(snapshot.scope.task, path, operation, evidence_generation,
                                  fingerprint, complete=complete, present=present))
    return tuple(facts)
