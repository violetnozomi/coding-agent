"""宿主执行边界生成并消费范围证明，不从工具文本推导完整性。"""
from __future__ import annotations

import importlib.util
import re
import shlex
import sys
from pathlib import Path, PurePosixPath

from .workspace_snapshot import SnapshotError, WorkspaceSnapshotStore


def execute_observed(store: WorkspaceSnapshotStore, generation: int, execute, *, on_before=None):
    from nz_coder.tools import current_tool_cancel_event

    errors = {}

    def capture(phase):
        try:
            return store.verification_snapshot(cancel_event=current_tool_cancel_event())
        except SnapshotError as exc:
            errors[phase] = str(exc)
            return None

    # 与同一宿主的 shell/文件写入串行，防止把别的工具副作用算给本次验证。
    with store._lock:
        before = capture("before")
        if on_before is not None:
            on_before(before)
        result = execute()
        metadata = result.metadata or {}
        unsettled = result.name == "bash" and (
            any(metadata.get(key) for key in (
                "cancelled", "timed_out", "output_incomplete", "output_limit_exceeded",
            )) or ("exit" in metadata and metadata["exit"] is None)
        )
        if unsettled:
            errors["after"] = "execution unsettled"
        result.workspace_scope = {
            "workspace": str(store.workspace), "generation": generation,
            "before": before, "after": capture("after") if not unsettled else None,
        }
        if errors:
            result.workspace_scope["errors"] = errors
        return result


def settled_changes(result, store, generation: int, *, anchor: str | None = None):
    """None=缺少完整证明，{}=完整证明无变化；第二项指出执行版本是否匹配。"""
    proof = result.workspace_scope
    if proof is None:
        return None, True
    if (not isinstance(store, WorkspaceSnapshotStore) or not isinstance(proof, dict)
            or proof.get("workspace") != str(store.workspace)
            or proof.get("generation") != generation):
        return None, False
    try:
        changes = store.verification_changes(proof.get("before"), proof.get("after"))
        if anchor is not None:
            changes = {**store.verification_changes(anchor, proof.get("before")), **changes}
    except SnapshotError:
        return None, True
    return changes, True


def _bytecode_for_unchanged_source(path: str, before: dict, after: dict) -> bool:
    parts = PurePosixPath(path).parts
    if len(parts) < 2 or parts[-2] != "__pycache__":
        return False
    entry = after.get(path) or before.get(path) or {}
    name = re.fullmatch(r"(.+)\.cpython-\d+(?:\.opt-[12]|-pytest-\d+(?:\.\d+)*)?\.pyc", parts[-1])
    if entry.get("kind") != "file" or not name:
        return False
    source = str(PurePosixPath(*parts[:-2]) / (name[1] + ".py"))
    original = before.get(source)
    return bool(original and original.get("kind") == "file" and original == after.get(source))


def _unconfirmed_bytecode_reason(path: str, after: dict, store, interpreter) -> str:
    """核对稳定快照中的缓存头部与源码身份，不反序列化或执行字节码。"""
    from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess

    target = PurePosixPath(path)
    source = str(target.parent.parent / (target.name.rsplit(".cpython-", 1)[0] + ".py"))
    try:
        access = WorkspaceFileAccess(store.workspace)
        cache, cache_identity = access.read_bytes_with_identity(path, maximum=store.max_file_bytes)
        body, source_identity = access.read_bytes_with_identity(source, maximum=store.max_file_bytes)
    except (OSError, ValueError, RuntimeError):
        return "cache or source could not be read"
    if (cache_identity.content_hash != after[path].get("digest")
            or source_identity.content_hash != after[source].get("digest")
            or len(cache) <= 16):
        return "cache or source identity changed, or header was incomplete"
    tag, magic = interpreter
    if not target.name.startswith(f"{PurePosixPath(source).stem}.{tag}.") and not target.name.startswith(
            f"{PurePosixPath(source).stem}.{tag}-pytest-"):
        return "cache tag does not match a confirmed interpreter"
    if cache[:4].hex() != magic:
        return "cache magic does not match the interpreter"
    flags = int.from_bytes(cache[4:8], "little")
    if flags in {1, 3}:
        if cache[:4] != importlib.util.MAGIC_NUMBER:
            return "source hash cannot be verified for this interpreter"
        return "" if cache[8:16] == importlib.util.source_hash(body) else "source hash differs"
    if flags != 0:
        return "unsupported cache header flags"
    if (int.from_bytes(cache[8:12], "little") != (source_identity.mtime_ns // 1_000_000_000 & 0xFFFFFFFF)
            or int.from_bytes(cache[12:16], "little") != (source_identity.size & 0xFFFFFFFF)):
        return "source timestamp or size differs"
    return ""


def _cache_directories(paths: set[str], directories: set[str], entries: dict) -> set[str]:
    """目录只能随已核实的全部后代一起结清，不能遮住缓存中的业务文件。"""
    for directory in sorted(directories, key=lambda path: path.count("/"), reverse=True):
        descendants = {path for path in entries if path.startswith(directory + "/")}
        if descendants and descendants <= paths:
            paths.add(directory)
    return paths


def removed_python_bytecode_paths(result, changes: dict, store) -> set[str]:
    """逐项核对缓存删除，混合删除中的普通文件和目录仍是变化。"""
    if result.name != "bash" or not changes:
        return set()
    from .workspace_scope import load
    try:
        before = load(store, result.workspace_scope["before"])
        after = load(store, result.workspace_scope["after"])
    except SnapshotError:
        return set()
    files, directories = set(), set()
    for path, operation in changes.items():
        if operation != "delete":
            continue
        entry = before.get(path) or {}
        if entry.get("kind") == "directory" and PurePosixPath(path).name == "__pycache__":
            directories.add(path)
        elif _bytecode_for_unchanged_source(path, before, after):
            files.add(path)
    return _cache_directories(files, directories, before)


def python_verification_artifact_paths(result, changes: dict, store, *, python_verification_origin=None,
                                      unconfirmed=None) -> set[str]:
    """逐项核实可靠 Python 检查的缓存，不豁免其他输入变化。"""
    if result.name not in {"bash", "process", "verify_changed_files"} or not changes:
        return set()
    from nz_coder.intelligence.verification_planner import (
        classify_verification_segments, verification_command_segments,
        verification_success_is_reliable,
    )
    command = str(result.tool_input.get("command") or "")
    if result.name == "process":
        operation = str(result.tool_input.get("operation") or "").strip().lower()
        if operation == "read":
            # read 的 command 参数未被执行，只有同运行的可信 launch 回执可解释缓存。
            if python_verification_origin is None:
                return set()
            command = python_verification_origin.command
        elif operation != "start":
            return set()
    if result.name in {"bash", "process"}:
        from nz_coder.runtime.verification.domain.identity import command_identity, observed_check_subject
        subject = observed_check_subject(command_identity(command, str(store.workspace)))
        if subject is not None:
            # 展示包装影响执行结果可信度，不会把已核实的解释器缓存变成源码写入。
            command = subject.original
    if result.name == "verify_changed_files":
        from nz_coder.runtime.verification.domain.identity import command_identity
        receipts = result.execution_facts
        identities = tuple(command_identity(item.command, item.cwd) for item in receipts)
        if not receipts or any(item.producer != result.name or item.capabilities != ("syntax",)
                               for item in receipts) or any(
            identity.opaque or len(identity.argv) < 4 or identity.argv[1:3] != ("-m", "py_compile")
            for identity in identities
        ):
            return set()
        command = receipts[0].command
    # 来源可以是复合命令中的真实 Python 检查；缓存性质由下面的文件身份核对决定。
    segments = verification_command_segments(command)
    python_checks = [words for words in segments
                     if words and re.fullmatch(r"(?:(?:python|pypy)(?:\d+(?:\.\d+)*)?|pytest|py\.test)", Path(words[0]).name)
                     and classify_verification_segments(shlex.join(words))]
    if not python_checks:
        return set()
    from .workspace_scope import load
    try:
        before = load(store, result.workspace_scope["before"])
        after = load(store, result.workspace_scope["after"])
    except SnapshotError:
        return set()
    from nz_coder.runtime.agent.task_policy import _python_entry

    # pytest 运行记录没有源码/缓存头证明，仍要求单个可归因入口。
    pytest = len(segments) == 1 and verification_success_is_reliable(command) and any(
        Path(words[0]).name in {"pytest", "py.test"} or bool(
        (entry := _python_entry(words)) is not None and entry[2] and entry[0] in {"pytest", "py.test"})
        for words in python_checks)
    artifacts, cache_directories = set(), set()
    interpreter = (sys.implementation.cache_tag, importlib.util.MAGIC_NUMBER.hex())
    if result.name == "bash" and any(receipt.producer == "bash" for receipt in result.execution_facts):
        interpreter = (result.metadata or {}).get("python_bytecode_identity") or interpreter
    for path in changes:
        if changes[path] == "delete":
            continue
        parts = PurePosixPath(path).parts
        if ".pytest_cache" in parts and pytest:
            tail = "/".join(parts[parts.index(".pytest_cache") + 1:])
            entry = after.get(path) or before.get(path) or {}
            # 缓存目录不是源码豁免区，只放行 pytest 已知的运行记录。
            if entry.get("kind") == "file" and (
                tail in {"v/cache/nodeids", "v/cache/lastfailed", "v/cache/stepwise"}
                or (changes[path] == "create" and tail in {"README.md", "CACHEDIR.TAG", ".gitignore"})
            ):
                artifacts.add(path)
                continue
            if (entry.get("kind") == "directory" and changes[path] == "create"
                    and tail in {"", "v", "v/cache"}):
                cache_directories.add(path)
                continue
            continue
        if parts[-1] == "__pycache__" and changes[path] == "create":
            if after.get(path, {}).get("kind") == "directory":
                cache_directories.add(path)
            continue
        if _bytecode_for_unchanged_source(path, before, after):
            reason = _unconfirmed_bytecode_reason(path, after, store, interpreter)
            if not reason:
                artifacts.add(path)
            elif unconfirmed is not None and len(unconfirmed) < 4:
                unconfirmed[path] = reason
    return _cache_directories(artifacts, cache_directories, after)
