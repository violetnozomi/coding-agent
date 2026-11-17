"""旧记忆的显式迁移与可核对快照导出。"""
from __future__ import annotations

import hashlib
import importlib
import json
import multiprocessing
from pathlib import Path
import sqlite3

import pytest
import yaml


def _migration():
    assert importlib.util.find_spec("nz_coder.state.memory_migration") is not None
    return importlib.import_module("nz_coder.state.memory_migration")


def _store(root):
    from nz_coder.state.memory_store import SQLiteMemoryStore
    return SQLiteMemoryStore(root, scope_id="test-project")


def _write_memory(root, filename="first.md", **metadata):
    root.mkdir(parents=True, exist_ok=True)
    fields = {"name": "同名记忆", "description": "第一行\n第二行\n", "type": "project", **metadata}
    body = "需要原样保留的中文正文。\n\n第二段。\n"
    path = root / filename
    path.write_text("---\n" + yaml.safe_dump(fields, allow_unicode=True) + "---\n" + body, encoding="utf-8")
    return path, body


def test_has_legacy_does_not_create_missing_directory(tmp_path):
    root = tmp_path / "missing"
    assert _migration().has_legacy(root) is False
    assert not root.exists()
    _write_memory(root)
    assert _migration().has_legacy(root) is True


def test_migrate_preserves_yaml_body_duplicate_names_and_unknown_dates(tmp_path):
    root = tmp_path / "memory"
    first, body = _write_memory(root, created_at=12.5, last_accessed=999, access_count=7,
                               custom={"tags": ["一", "二"], "note": "多行\n值"})
    second, _ = _write_memory(root, "second.md", type="user")
    (root / "MEMORY.md").write_text("派生索引，无需frontmatter", encoding="utf-8")
    (root / "AUTO_DREAM.md").write_text("自动整理旧报告", encoding="utf-8")
    originals = {path.name: path.read_bytes() for path in root.iterdir()}

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "migrated", result
    records = _store(root).list_records(status=None, limit=100)["items"]
    assert len(records) == 2
    assert len({item["id"] for item in records}) == 2
    assert {item["name"] for item in records} == {"同名记忆"}
    by_type = {item["type"]: _store(root).read(item["id"]) for item in records}
    assert by_type["project"]["content"] == body
    assert by_type["project"]["description"] == "第一行\n第二行\n"
    assert by_type["project"]["legacy_metadata"]["custom"] == {"tags": ["一", "二"], "note": "多行\n值"}
    assert by_type["project"]["legacy_metadata"]["last_accessed"] == 999
    assert float(by_type["project"]["created_at"]) == 12.5
    assert by_type["project"]["legacy_metadata"]["created_at"] == 12.5
    assert by_type["project"]["updated_at"] is None
    assert by_type["user"]["created_at"] is None
    assert by_type["user"]["source_refs"] == []
    assert not by_type["user"].get("last_checked")
    assert {name: (root / name).read_bytes() for name in originals} == originals
    backup = Path(result["backup_path"])
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"] == {"records": 2, "proposals": 0, "events": 0}
    for entry in manifest["files"]:
        assert hashlib.sha256((backup / entry["path"]).read_bytes()).hexdigest() == entry["sha256"]
        assert (backup / entry["path"]).stat().st_mode & 0o222 == 0
    assert {entry["path"] for entry in manifest["records"]} == {first.name, second.name}


def test_legacy_proposals_and_ledger_preserve_decisions_without_guessing_target(tmp_path):
    root = tmp_path / "memory"
    _write_memory(root)
    proposals = root / "memory-control" / "proposals"
    proposals.mkdir(parents=True)
    original = {"name": "同名记忆", "description": "候选", "type": "project", "content": "旧候选正文",
                "fingerprint": "legacy-fingerprint", "status": "pending_review", "created_at": 3.0,
                "source_session": "old-session", "source_message_ids": ["m1"], "risk": "high"}
    (proposals / "pending.json").write_text(json.dumps(original), encoding="utf-8")
    applied = {**original, "fingerprint": "applied-fingerprint", "status": "applied"}
    (proposals / "applied.json").write_text(json.dumps(applied), encoding="utf-8")
    events = [{"action": "queued_for_review", "fingerprint": "legacy-fingerprint", "timestamp": 4.0},
              {"action": "applied", "fingerprint": "applied-fingerprint", "reviewer": "human", "timestamp": 5.0}]
    (root / "memory-control" / "ledger.jsonl").write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "migrated", result
    snapshot = _store(root).export_snapshot()
    assert len(snapshot["proposals"]) == 2
    pending = next(item for item in snapshot["proposals"] if item["legacy_metadata"]["fingerprint"] == "legacy-fingerprint")
    assert pending["memory_id"] is None
    assert pending["action"] == "create"
    assert pending["legacy_metadata"] == original
    assert any(item["legacy_metadata"] == applied for item in snapshot["proposals"])
    historical = [item["legacy_metadata"] for item in snapshot["events"] if item.get("legacy_metadata") in events]
    assert historical == events
    decision = _store(root).mutate(
        {"action": "apply_proposal", "proposal_id": pending["id"], "expected_proposal_revision": pending["revision"]},
        {"capability": "manage", "scope_id": "test-project", "actor": "test-user",
         "operation_namespace": "migration-test", "operation_id": "accept-old-pending"},
    )
    assert decision["status"] == "saved", decision
    assert len(_store(root).list_records(status=None)["items"]) == 2
    assert _store(root).read(decision["memory_id"])["content"] == original["content"]


@pytest.mark.parametrize("relative,data", [
    ("broken.md", b"---\nname: [unclosed\n---\nbody"),
    ("broken.md", b"plain markdown without frontmatter"),
    ("broken.md", b"---\nname: bad\n---\n\xff"),
    ("memory-control/proposals/broken.json", b"{broken"),
    ("memory-control/proposals/broken.json", b"[]"),
    ("memory-control/ledger.jsonl", b'{"action":"ok"}\n{broken\n'),
])
def test_broken_source_prevents_partial_ready_database(tmp_path, relative, data):
    root = tmp_path / "memory"
    good, _ = _write_memory(root)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    original = good.read_bytes()

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "error", result
    assert relative in result["error"]
    assert good.read_bytes() == original
    assert path.read_bytes() == data
    if (root / "memory.sqlite3").exists():
        from nz_coder.state.memory_store import SQLiteMemoryStore
        store = SQLiteMemoryStore(root, scope_id="test-project", ready=False)
        assert store.migration_manifest() is None


def test_source_change_during_scan_rolls_back_import(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    path, _ = _write_memory(root)
    from nz_coder.state.memory_store import SQLiteMemoryStore
    real_import = SQLiteMemoryStore.import_legacy

    def changed_import(self, records, proposals, events, manifest, **kwargs):
        path.write_text(path.read_text(encoding="utf-8") + "扫描期间新增\n", encoding="utf-8")
        return real_import(self, records, proposals, events, manifest, **kwargs)

    monkeypatch.setattr(SQLiteMemoryStore, "import_legacy", changed_import)
    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "error", result
    assert "变" in result["error"]
    assert SQLiteMemoryStore(root, scope_id="test-project", ready=False).migration_manifest() is None
    monkeypatch.undo()
    retry = _migration().migrate_legacy(root, "test-project")
    assert retry["status"] == "migrated", retry
    records = _store(root).list_records(status=None)["items"]
    assert len(records) == 1
    assert _store(root).read(records[0]["id"])["content"].endswith("扫描期间新增\n")


def test_ready_database_repeat_does_not_read_or_reimport_old_files(tmp_path):
    root = tmp_path / "memory"
    path, _ = _write_memory(root)
    migration = _migration()
    first = migration.migrate_legacy(root, "test-project")
    assert first["status"] == "migrated", first
    generation = _store(root).generation()
    path.write_bytes(b"broken original after explicit migration")
    second = migration.migrate_legacy(root, "test-project")
    assert second["status"] == "already_imported", second
    assert _store(root).generation() == generation
    assert len(_store(root).list_records(status=None)["items"]) == 1


def _migration_process(root, queue):
    from nz_coder.state.memory_migration import migrate_legacy
    queue.put(migrate_legacy(Path(root), "test-project"))


def test_two_process_migrations_import_exactly_once(tmp_path):
    root = tmp_path / "memory"
    _write_memory(root)
    _migration()
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [context.Process(target=_migration_process, args=(str(root), queue)) for _ in range(2)]
    for process in workers:
        process.start()
    results = [queue.get(timeout=20) for _ in workers]
    for process in workers:
        process.join(timeout=20)
        assert process.exitcode == 0
    assert sorted(item["status"] for item in results) == ["already_imported", "migrated"], results
    assert len(_store(root).list_records(status=None)["items"]) == 1


def test_explicit_export_has_id_revision_body_hash_and_preserves_existing_target(tmp_path):
    root = tmp_path / "memory"
    _, body = _write_memory(root)
    migration = _migration()
    assert migration.migrate_legacy(root, "test-project")["status"] == "migrated"
    target = tmp_path / "export"

    result = migration.export_memories(_store(root), target)

    assert result["status"] == "exported", result
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot"] is True
    assert manifest["scope_id"] == "test-project"
    assert {entry["path"] for entry in manifest["files"]} >= {"proposals.json", "events.json"}
    for entry in manifest["files"]:
        data = (target / entry["path"]).read_bytes()
        assert len(data) == entry["size"]
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    record = manifest["records"][0]
    text = (target / record["path"]).read_text(encoding="utf-8")
    _, frontmatter, exported_body = text.split("---\n", 2)
    metadata = yaml.safe_load(frontmatter)
    assert metadata["id"] == record["id"]
    assert metadata["revision"] == record["revision"] == 1
    assert exported_body == body
    assert hashlib.sha256(exported_body.encode()).hexdigest() == record["content_sha256"]
    before = (target / "manifest.json").read_bytes()
    assert migration.export_memories(_store(root), target)["status"] == "error"
    assert (target / "manifest.json").read_bytes() == before


def test_export_database_failure_is_reported_without_partial_directory(tmp_path):
    class BrokenStore:
        def export_snapshot(self):
            raise sqlite3.OperationalError("database is locked")

    target = tmp_path / "export"
    result = _migration().export_memories(BrokenStore(), target)
    assert result["status"] == "error"
    assert result["error"] == "An internal error occurred."
    assert not target.exists()


def test_migration_source_read_error_keeps_path_without_private_exception(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    path, _ = _write_memory(root)
    marker = "PRIVATE_SOURCE_ERROR_42581"
    real_read = Path.read_bytes

    def read_bytes(self):
        if self == path:
            raise OSError(marker)
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    result = _migration().migrate_legacy(root, "test-project")
    assert result["status"] == "error"
    assert path.name in result["error"]
    assert marker not in json.dumps(result)
    assert real_read(path)


def test_migration_sqlite_error_is_not_exposed(tmp_path, monkeypatch):
    from nz_coder.state.memory_store import SQLiteMemoryStore
    root = tmp_path / "memory"
    path, _ = _write_memory(root)
    original = path.read_bytes()
    marker = "PRIVATE_SQLITE_ERROR_42581"

    def import_legacy(self, *args, **kwargs):
        raise sqlite3.OperationalError(marker)

    monkeypatch.setattr(SQLiteMemoryStore, "import_legacy", import_legacy)
    result = _migration().migrate_legacy(root, "test-project")
    assert result["status"] == "error"
    assert marker not in json.dumps(result)
    assert path.read_bytes() == original


def test_migration_failed_receipt_does_not_expose_private_reason(tmp_path, monkeypatch):
    from nz_coder.state.memory_store import SQLiteMemoryStore
    root = tmp_path / "memory"
    _write_memory(root)
    marker = "PRIVATE_IMPORT_REASON_42581"
    monkeypatch.setattr(SQLiteMemoryStore, "import_legacy", lambda *args, **kwargs:
                        {"status": "error", "reason": marker})
    result = _migration().migrate_legacy(root, "test-project")
    assert result["status"] == "error"
    assert marker not in json.dumps(result)


def test_backup_warning_is_safe_after_committed_migration(tmp_path, monkeypatch):
    root = tmp_path / "memory"
    _write_memory(root)
    migration = _migration()
    marker = "PRIVATE_BACKUP_ERROR_42581"

    def fail_backup(*args):
        raise OSError(marker)

    monkeypatch.setattr(migration, "_write_backup", fail_backup)
    result = migration.migrate_legacy(root, "test-project")
    assert result["status"] == "migrated"
    assert result["backup_warning"]
    assert marker not in result["backup_warning"]
    assert len(_store(root).list_records()["items"]) == 1


def test_yaml_error_locates_file_without_echoing_private_source(tmp_path):
    root = tmp_path / "memory"
    root.mkdir()
    marker = "PRIVATE_YAML_VALUE_42581"
    path = root / "broken.md"
    original = f"---\nname: [{marker}\n---\nbody\n".encode()
    path.write_bytes(original)

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "error"
    assert path.name in result["error"]
    assert marker not in json.dumps(result)
    assert path.read_bytes() == original


def test_bad_ledger_reports_relative_file_and_line(tmp_path):
    root = tmp_path / "memory"
    _write_memory(root)
    ledger = root / "memory-control" / "ledger.jsonl"
    ledger.parent.mkdir()
    ledger.write_text('{"action":"ok"}\n{broken\n', encoding="utf-8")
    result = _migration().migrate_legacy(root, "test-project")
    assert result["status"] == "error"
    assert "memory-control/ledger.jsonl" in result["error"]
    assert "2" in result["error"]


@pytest.mark.parametrize("failure", [OSError, sqlite3.OperationalError, yaml.YAMLError, ValueError])
def test_export_does_not_expose_private_exception(tmp_path, failure):
    marker = "PRIVATE_EXPORT_ERROR_42581"

    class BrokenStore:
        def export_snapshot(self):
            raise failure(marker)

    result = _migration().export_memories(BrokenStore(), tmp_path / "export")
    assert result["status"] == "error"
    assert marker not in json.dumps(result)


def test_frontmatter_yaml_preserves_iso_dates_and_nested_source_refs(tmp_path):
    path, body = _write_memory(tmp_path / "memory", created_at="2025-01-02", updated_at="2025-02-03",
                               source_refs=[{"path": "src/main.py", "observed_hash": "old-hash"}])
    migration = _migration()
    record = migration._parse_record(path.read_bytes(), path.name, "scope")
    assert record["created_at"] == "2025-01-02"
    assert record["updated_at"] == "2025-02-03"
    assert record["content"] == body
    assert record["source_refs"] == [{"path": "src/main.py", "observed_hash": "old-hash"}]


def test_legacy_ledger_missing_timestamp_stays_unknown(tmp_path):
    root = tmp_path / "memory"
    _write_memory(root)
    control = root / "memory-control"
    control.mkdir()
    original = {"action": "rejected", "fingerprint": "unbound-old-proposal"}
    (control / "ledger.jsonl").write_text(json.dumps(original) + "\n", encoding="utf-8")

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "migrated", result
    event = _store(root).export_snapshot()["events"][0]
    assert event["created_at"] is None
    assert event["memory_id"] is None
    assert event["legacy_metadata"] == original


@pytest.mark.parametrize("mem_type", ["user", "project", "feedback", "reference"])
def test_migration_preserves_all_legacy_types(tmp_path, mem_type):
    root = tmp_path / "memory"
    _write_memory(root, type=mem_type)
    result = _migration().migrate_legacy(root, "test-project")
    assert result["status"] == "migrated", result
    assert _store(root).list_records(status=None)["items"][0]["type"] == mem_type


def test_legacy_proposal_symlink_cannot_import_another_workspace(tmp_path):
    root = tmp_path / "memory"
    _write_memory(root)
    external = tmp_path / "other-workspace"
    external.mkdir()
    original = b'{"content":"another project secret"}'
    (external / "proposal.json").write_bytes(original)
    control = root / "memory-control"
    control.mkdir()
    (control / "proposals").symlink_to(external, target_is_directory=True)

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "error", result
    assert "memory-control/proposals/proposal.json" in result["error"]
    assert (external / "proposal.json").read_bytes() == original


def test_existing_ready_database_rejects_late_legacy_files(tmp_path):
    root = tmp_path / "memory"
    store = _store(root)
    original_generation = store.generation()
    path, _ = _write_memory(root)
    before = path.read_bytes()

    result = _migration().migrate_legacy(root, "test-project")

    assert result["status"] == "error", result
    assert "就绪" in result["error"]
    assert store.generation() == original_generation
    assert store.list_records(status=None)["items"] == []
    assert path.read_bytes() == before
