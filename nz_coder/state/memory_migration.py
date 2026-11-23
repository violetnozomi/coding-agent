"""显式迁移旧文件；SQLite 就绪后，旧文件只作为历史原件保留。"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import TYPE_CHECKING
import uuid

import yaml

from nz_coder.protocol.public_error import PublicInputError, to_public_error

if TYPE_CHECKING:
    from nz_coder.state.memory_store import SQLiteMemoryStore


_OLD_WRITER_PRECONDITION = "迁移前须停止该项目所有旧版本 CLI、HTTP 和后台 worker；新库事务不能阻止旧版本写入原文件。"
_NON_RECORD_MARKDOWN = {"MEMORY.md", "AUTO_DREAM.md"}


class _LegacyLoader(yaml.SafeLoader):
    pass


# 日期保留原文；不能把旧资料中的日期误变成迁移时的新核查时间。
_LegacyLoader.yaml_implicit_resolvers = {
    key: [(tag, pattern) for tag, pattern in values if tag != "tag:yaml.org,2002:timestamp"]
    for key, values in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_paths(root: Path) -> list[tuple[str, str]]:
    paths = [(path.name, "auxiliary" if path.name in _NON_RECORD_MARKDOWN else "record")
             for path in root.glob("*.md")]
    paths.extend((str(path.relative_to(root).as_posix()), "proposal")
                 for path in (root / "memory-control" / "proposals").glob("*.json"))
    for relative, kind in (("memory-control/ledger.jsonl", "ledger"),
                           ("auto_dream_state.json", "auxiliary_json")):
        if (root / relative).exists():
            paths.append((relative, kind))
    return sorted(paths)


def has_legacy(memory_dir: Path) -> bool:
    """仅探测已知旧资料，不建目录，也不读取或发布旧正文。"""
    return bool(_source_paths(Path(memory_dir)))


def _stat_identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _scan(root: Path) -> list[dict]:
    snapshots = []
    paths = _source_paths(root)
    for relative, kind in paths:
        path = root / relative
        try:
            if (path.is_symlink() or not path.is_file()
                    or not path.resolve().is_relative_to(root.resolve())):
                raise PublicInputError("旧资料必须是普通文件")
            identity = _stat_identity(path)
            data = path.read_bytes()
            if identity != _stat_identity(path):
                raise PublicInputError("扫描期间源文件发生变化")
            snapshots.append({"path": relative, "kind": kind, "data": data,
                              "identity": identity, "sha256": _digest(data), "size": len(data)})
        except PublicInputError as exc:
            raise PublicInputError(f"{relative}: {to_public_error(exc).message}") from exc
        except (OSError, ValueError) as exc:
            raise PublicInputError(f"{relative}: 无法读取旧资料文件。") from exc
    if paths != _source_paths(root):
        raise PublicInputError("扫描期间源文件集合发生变化")
    return snapshots


def _verify_sources(root: Path, snapshots: list[dict]) -> None:
    expected = [(item["path"], item["kind"]) for item in snapshots]
    if expected != _source_paths(root):
        raise PublicInputError("扫描期间源文件集合发生变化")
    for item in snapshots:
        path = root / item["path"]
        try:
            # 读取前后都核对文件属性，才能发现复核读取期间的替换或改写。
            if (
                path.is_symlink()
                or _stat_identity(path) != item["identity"]
                or _digest(path.read_bytes()) != item["sha256"]
                or _stat_identity(path) != item["identity"]
            ):
                raise PublicInputError("扫描期间源文件发生变化")
        except (OSError, ValueError) as exc:
            raise PublicInputError(f"{item['path']}: 扫描期间源文件发生变化，无法完成复核。") from exc


def _legacy_id(scope_id: str, relative: str, kind: str) -> str:
    # 来源文件决定独立身份；重名和正文相似都不能导致覆盖或合并。
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"nz-coder-memory:{scope_id}:{kind}:{relative}"))


def _parse_record(data: bytes, relative: str, scope_id: str) -> dict:
    text = data.decode("utf-8")
    match = re.match(r"\A\ufeff?---[ \t]*\r?\n(.*?)^---[ \t]*(?:\r?\n|\Z)", text, re.MULTILINE | re.DOTALL)
    if match is None:
        raise PublicInputError("缺少完整 YAML frontmatter（元数据头）")
    metadata = yaml.load(match.group(1), Loader=_LegacyLoader)
    if not isinstance(metadata, dict):
        raise PublicInputError("YAML 元数据必须是对象")
    _json_text(metadata)
    name = metadata.get("name", Path(relative).stem)
    mem_type = metadata.get("type", "project")
    description = metadata.get("description", "")
    if not isinstance(name, str) or not name.strip() or not isinstance(mem_type, str):
        raise PublicInputError("name/type 必须是有效字符串")
    if not isinstance(description, str):
        raise PublicInputError("description 必须是字符串")
    sources = metadata.get("source_refs", [])
    if not isinstance(sources, list):
        raise PublicInputError("source_refs 必须是列表")
    return {"id": _legacy_id(scope_id, relative, "record"), "name": name, "type": mem_type,
            "description": description, "content": text[match.end():],
            "applicability": metadata.get("applicability", ""), "source_refs": sources,
            "status": metadata.get("status", "active"), "revision": 1,
            "created_at": metadata.get("created_at"), "updated_at": metadata.get("updated_at"),
            "legacy_metadata": metadata}


def _parse_json_object(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise PublicInputError("旧 JSON 必须是对象")
    _json_text(value)
    return value


def _parse_proposal(data: bytes, relative: str, scope_id: str) -> dict:
    legacy = _parse_json_object(data.decode("utf-8"))
    fields = ("name", "description", "type", "content", "source_refs", "applicability")
    command = {field: legacy[field] for field in fields if field in legacy}
    # 旧提案缺少可复核的目标版本，按新建提案导入，保留原始决定供追溯。
    command["action"] = "create"
    return {
        "id": _legacy_id(scope_id, relative, "proposal"),
        "revision": 1,
        "action": "create",
        "memory_id": None,
        "expected_revision": None,
        "command": command,
        "status": legacy.get("status", "pending_review"),
        "created_at": legacy.get("created_at"),
        "legacy_metadata": legacy,
    }


def _ledger_events(data: bytes, relative: str, scope_id: str) -> Iterator[dict]:
    # 原文件行号参与事件标识，空行也要计数。
    for line_number, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            legacy = _parse_json_object(line)
        except (ValueError, TypeError) as exc:
            raise PublicInputError(f"第 {line_number} 行: 旧流水 JSON 格式无效。") from exc
        yield {
            "id": _legacy_id(scope_id, f"{relative}:{line_number}", "event"),
            "action": legacy.get("action", "legacy"),
            "memory_id": None,
            "revision": None,
            "created_at": legacy.get("timestamp"),
            "legacy_metadata": legacy,
        }


def _prepare_import(snapshots: list[dict], scope_id: str) -> tuple[list, list, list, dict]:
    records, proposals, events, record_entries = [], [], [], []
    for source in snapshots:
        relative, kind, data = source["path"], source["kind"], source["data"]
        try:
            if kind == "record":
                record = _parse_record(data, relative, scope_id)
                records.append(record)
                record_entries.append({
                    "id": record["id"],
                    "path": relative,
                    "content_sha256": _digest(record["content"].encode("utf-8")),
                })
            elif kind == "proposal":
                proposals.append(_parse_proposal(data, relative, scope_id))
            elif kind == "ledger":
                events.extend(_ledger_events(data, relative, scope_id))
            elif kind == "auxiliary_json":
                _parse_json_object(data.decode("utf-8"))
            else:
                data.decode("utf-8")
        except PublicInputError as exc:
            raise PublicInputError(f"{relative}: {to_public_error(exc).message}") from exc
        except (UnicodeError, ValueError, TypeError, yaml.YAMLError) as exc:
            raise PublicInputError(f"{relative}: 旧资料格式无效，无法解析。") from exc
    files = [{key: source[key] for key in ("path", "kind", "size", "sha256")} for source in snapshots]
    manifest = {
        "version": 1,
        "scope_id": scope_id,
        "files": files,
        "records": record_entries,
        "counts": {"records": len(records), "proposals": len(proposals), "events": len(events)},
        "source_digest": _digest(_json_text(files).encode("utf-8")),
    }
    return records, proposals, events, manifest


def _write_backup(root: Path, snapshots: list[dict], manifest: dict) -> Path:
    backup_root = root / "legacy-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".pending-", dir=backup_root))
    try:
        for item in snapshots:
            destination = staging / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item["data"])
            destination.chmod(0o444)
        manifest_path = staging / "manifest.json"
        manifest_path.write_text(_json_text(manifest), encoding="utf-8")
        manifest_path.chmod(0o444)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        backup_path = backup_root / f"{timestamp}-{uuid.uuid4().hex[:8]}"
        staging.rename(backup_path)
        return backup_path
    except OSError:
        shutil.rmtree(staging)
        raise


def migrate_legacy(memory_dir: Path, scope_id: str = "") -> dict:
    """在最终数据库内原子导入；调用方必须先安排旧版本停写窗口。"""
    from nz_coder.state.memory_store import SQLiteMemoryStore

    root = Path(memory_dir)
    try:
        store = SQLiteMemoryStore(root, scope_id=scope_id, ready=False)
        existing_manifest = store.migration_manifest()
        if existing_manifest is not None:
            return {"status": "already_imported", "counts": existing_manifest["counts"],
                    "manifest": existing_manifest, "precondition": _OLD_WRITER_PRECONDITION}
        snapshots = _scan(root)
        records, proposals, events, manifest = _prepare_import(snapshots, scope_id)
        result = store.import_legacy(
            records, proposals, events, manifest,
            verify_sources=lambda: _verify_sources(root, snapshots),
        )
        # 拿到写锁前可能已有进程提交，只有实际导入的进程负责备份。
        if result["status"] == "already_imported":
            return {**result, "manifest": manifest, "precondition": _OLD_WRITER_PRECONDITION}
        if result["status"] not in {"imported", "saved", "migrated"}:
            return {"status": "error", "error": "旧库导入未完成，请确认记忆库就绪状态和迁移来源。",
                    "store_result": {"status": result["status"]}, "precondition": _OLD_WRITER_PRECONDITION}
        response = {"status": "migrated", "counts": manifest["counts"], "manifest": manifest,
                    "precondition": _OLD_WRITER_PRECONDITION}
        # ready 已提交：备份整理失败不能让调用方误以为应再次导入。
        try:
            response["backup_path"] = str(_write_backup(root, snapshots, manifest))
        except OSError as exc:
            response["backup_warning"] = to_public_error(exc).message
        return response
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error) as exc:
        return {"status": "error", "error": to_public_error(exc).message, "precondition": _OLD_WRITER_PRECONDITION}


def _write_export_file(staging: Path, relative: str, data: bytes) -> dict:
    (staging / relative).write_bytes(data)
    return {"path": relative, "size": len(data), "sha256": _digest(data)}


def _write_export_snapshot(staging: Path, snapshot: dict) -> dict:
    record_entries, files = [], []
    for record in snapshot["records"]:
        memory_id = str(record["id"])
        # 编码后标识只占一个文件名，标识中的斜杠也不会变成目录分隔符。
        relative = f"records/{memory_id.encode('utf-8').hex()}.md"
        metadata = {key: value for key, value in record.items() if key != "content"}
        body = record["content"]
        frontmatter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=True)
        markdown = "---\n" + frontmatter + "---\n" + body
        (staging / relative).parent.mkdir(parents=True, exist_ok=True)
        file_entry = _write_export_file(staging, relative, markdown.encode("utf-8"))
        files.append(file_entry)
        record_entries.append({
            "id": memory_id,
            "revision": record["revision"],
            "path": relative,
            "sha256": file_entry["sha256"],
            "content_sha256": _digest(body.encode("utf-8")),
        })
    for key in ("proposals", "events"):
        data = _json_text(snapshot[key]).encode("utf-8")
        files.append(_write_export_file(staging, f"{key}.json", data))

    manifest = {
        "version": 1,
        "snapshot": True,
        "scope_id": snapshot["scope_id"],
        "generation": snapshot["generation"],
        "records": record_entries,
        "files": files,
        "counts": {key: len(snapshot[key]) for key in ("records", "proposals", "events")},
        "import_manifest": snapshot.get("import_manifest"),
    }
    (staging / "manifest.json").write_text(_json_text(manifest), encoding="utf-8")
    return manifest


def export_memories(store: SQLiteMemoryStore, target: Path) -> dict:
    """从同一读事务导出快照，清单记录每条记忆的标识、版本和正文摘要。"""
    target = Path(target)
    staging = None
    try:
        if target.exists():
            raise PublicInputError("导出目标已存在，请选择新的目录。")
        snapshot = store.export_snapshot()
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".memory-export-", dir=target.parent))
        manifest = _write_export_snapshot(staging, snapshot)
        staging.rename(target)
        staging = None
        return {"status": "exported", "target": str(target), "counts": manifest["counts"],
                "generation": manifest["generation"], "snapshot": True}
    except (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error, yaml.YAMLError) as exc:
        return {"status": "error", "error": to_public_error(exc).message}
    finally:
        if staging is not None:
            shutil.rmtree(staging)
