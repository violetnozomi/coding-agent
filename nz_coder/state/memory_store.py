"""项目长期记忆的 SQLite 权威存储。

每次调用独立连接；正文、当前指针、索引、提案和收据在同一短事务提交。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import time
from typing import Any
from uuid import uuid4

from nz_coder.protocol.public_error import (
    PublicError, PublicInputError, PublicRuntimeError, to_public_error,
)


_TEXT_FIELDS = ("name", "type", "description", "content", "applicability")
_FIELDS = (*_TEXT_FIELDS, "source_refs")
_TARGET_ACTIONS = {"update", "archive", "restore", "delete", "assess"}
_PROPOSABLE_ACTIONS = {"create", "update", "assess"}
_REVIEW_ACTIONS = {"apply_proposal", "reject_proposal"}
_ACTIONS = {"create", *_TARGET_ACTIONS, *_REVIEW_ACTIONS}
_IDENTITY_FIELDS = ("actor_id", "session_id", "parent_session_id", "child_session_id", "task_id")


class MemoryStoreUnavailable(PublicRuntimeError):
    """数据库尚未就绪或无法提供所需存储能力。"""

    def __init__(self, message: str):
        super().__init__(PublicError("memory_unavailable", message))


class _MutationRejected(Exception):
    """目标不存在或版本冲突时中止本次修改，调用方仍需保存操作回执。"""

    def __init__(self, result: dict):
        self.result = result


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tokens(text: str) -> list[str]:
    """索引和查询共用词元；不靠业务别名猜测用户意图。"""
    result = []
    for word in re.findall(r"[A-Za-z0-9_./:-]+|[\u3400-\u9fff]+", text):
        if re.fullmatch(r"[\u3400-\u9fff]+", word):
            result.extend(word)
            result.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            result.append(word.lower())
            result.extend(part.lower() for part in re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|[0-9]+", word))
            result.extend(part.lower() for part in re.split(r"[_./:\-]+", word) if part)
    return list(dict.fromkeys(result))


def _content_hash(record: dict) -> str:
    return hashlib.sha256(_json({key: record[key] for key in _FIELDS}).encode()).hexdigest()


def _embedding_is_current(record: dict | None, revision: int, content_hash: str) -> bool:
    """向量的版本与内容指纹必须同时匹配当前可检索的记忆。"""
    return (
        record is not None
        and record["status"] == "active"
        and record["revision"] == revision
        and _content_hash(record) == content_hash
    )


class SQLiteMemoryStore:
    def __init__(self, memory_dir: Path, *, scope_id: str = "", ready: bool = True):
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.memory_dir / "memory.sqlite3"
        self.scope_id = scope_id
        with self._connect(check_scope=False) as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_meta'").fetchone()
            if existing:
                self._check_scope(conn)
            schema = """
                    CREATE TABLE IF NOT EXISTS memory_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS memory_records(id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS memory_versions(
                        memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
                        revision INTEGER NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
                        description TEXT NOT NULL, content TEXT NOT NULL, applicability TEXT NOT NULL,
                        source_refs TEXT NOT NULL, status TEXT NOT NULL,
                        created_at TEXT, updated_at TEXT, legacy_metadata TEXT NOT NULL,
                        PRIMARY KEY(memory_id,revision));
                    CREATE TABLE IF NOT EXISTS memory_proposals(
                        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
                        action TEXT NOT NULL, memory_id TEXT, data TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS memory_events(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
                        action TEXT NOT NULL, memory_id TEXT, revision INTEGER,
                        created_at TEXT, data TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS memory_operations(
                        namespace TEXT NOT NULL, operation_id TEXT NOT NULL, digest TEXT NOT NULL,
                        receipt TEXT NOT NULL, PRIMARY KEY(namespace,operation_id));
                    CREATE TABLE IF NOT EXISTS memory_embeddings(
                        memory_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE CASCADE,
                        revision INTEGER NOT NULL, content_hash TEXT NOT NULL, encoder_id TEXT NOT NULL,
                        dimensions INTEGER NOT NULL, vector TEXT NOT NULL,
                        PRIMARY KEY(memory_id,encoder_id));
                    CREATE TABLE IF NOT EXISTS memory_imports(
                        id INTEGER PRIMARY KEY CHECK(id=1), manifest TEXT NOT NULL, digest TEXT NOT NULL,
                        counts TEXT NOT NULL, completed_at TEXT NOT NULL);
                    CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(memory_id UNINDEXED,tokens);
                """
            try:
                for statement in schema.split(";"):
                    if statement.strip():
                        conn.execute(statement)
            except sqlite3.OperationalError as exc:
                # 原始异常仅用于内部能力诊断，不进入公开错误消息。
                if "fts5" in str(exc).lower():
                    raise MemoryStoreUnavailable("SQLite 缺少 FTS5，长期记忆不可用") from exc
                raise
            defaults = {"schema_version": "1", "scope_id": scope_id, "generation": "0",
                        "index_revision": "0", "ready": "1" if ready else "0"}
            for key, value in defaults.items():
                conn.execute("INSERT OR IGNORE INTO memory_meta VALUES (?,?)", (key, value))
            conn.commit()
            conn.execute("PRAGMA journal_mode=DELETE")

    @contextmanager
    def _connect(self, timeout: float = 3.0, *, check_scope=True):
        if self.db_path.is_symlink():
            raise MemoryStoreUnavailable("记忆数据库路径不得是符号链接 symlink")
        conn = sqlite3.connect(self.db_path, timeout=max(0.0, timeout), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            if check_scope:
                self._check_scope(conn)
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def _check_scope(self, conn):
        row = conn.execute("SELECT value FROM memory_meta WHERE key='scope_id'").fetchone()
        if row is None or row[0] != self.scope_id:
            raise MemoryStoreUnavailable("长期记忆 scope 与数据库不一致")

    @staticmethod
    def _ready(conn):
        if conn.execute("SELECT value FROM memory_meta WHERE key='ready'").fetchone()[0] != "1":
            raise MemoryStoreUnavailable("长期记忆需要先完成显式迁移")

    @contextmanager
    def _snapshot(self):
        with self._connect() as conn:
            conn.execute("BEGIN")
            self._ready(conn)
            yield conn
            conn.commit()

    @staticmethod
    def _generation(conn) -> tuple[int, int]:
        values = dict(conn.execute("SELECT key,value FROM memory_meta WHERE key IN ('generation','index_revision')"))
        return int(values["generation"]), int(values["index_revision"])

    def generation(self) -> tuple[int, int]:
        with self._snapshot() as conn:
            return self._generation(conn)

    @staticmethod
    def _bump(conn, key="generation"):
        conn.execute("UPDATE memory_meta SET value=CAST(value AS INTEGER)+1 WHERE key=?", (key,))

    @staticmethod
    def _record(row) -> dict | None:
        if row is None:
            return None
        record = dict(row)
        record["id"] = record.pop("memory_id")
        record["source_refs"] = json.loads(record["source_refs"])
        legacy = json.loads(record.pop("legacy_metadata"))
        if legacy:
            record["legacy_metadata"] = legacy
        return record

    def _read(self, conn, memory_id, revision=None):
        if revision is None:
            row = conn.execute("""SELECT v.* FROM memory_versions v JOIN memory_records r
                ON r.id=v.memory_id AND r.revision=v.revision WHERE r.id=?""", (memory_id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM memory_versions WHERE memory_id=? AND revision=?",
                               (memory_id, revision)).fetchone()
        return self._record(row)

    @staticmethod
    def _event(row) -> dict:
        event = json.loads(row["data"])
        event.update({key: row[key] for key in ("seq", "event_id", "action", "memory_id", "revision", "created_at")})
        event["recorded_at"] = row["created_at"]
        if row["action"] == "assess":
            event.setdefault("observed_at", None)
        return event

    def _attach_assessments(self, conn, record):
        """把核查意见和关联记忆附到读取的版本上，包括后来已解决的冲突。"""
        if record is None:
            return None
        rows = conn.execute("SELECT * FROM memory_events WHERE action='assess' ORDER BY seq")
        events = [self._event(row) for row in rows]

        # 解决冲突的声明可能比被解决的事件晚写入，先按事件编号整理这些声明。
        resolutions: dict[str, list[str]] = {}
        for event in events:
            for resolved_event_id in event.get("resolves_event_ids", []):
                resolutions.setdefault(resolved_event_id, []).append(event["event_id"])
        assessments = []
        relations = []
        for event in events:
            resolving_events = resolutions.get(event["event_id"], [])
            event["resolved"] = bool(resolving_events)
            event["resolved_by"] = resolving_events
            if event["memory_id"] == record["id"]:
                event["record_revision_matches"] = event["revision"] == record["revision"]
                assessments.append(event)
            relations.extend(self._related_records_for_event(conn, record, event))
        record["assessments"] = assessments
        record["related_records"] = relations
        record["content_hash"] = _content_hash(record)
        return record

    @staticmethod
    def _related_records_for_event(conn, record, event) -> list[dict]:
        related = event.get("related_records", [])
        endpoints = [
            {"memory_id": event["memory_id"], "expected_revision": event["revision"]},
            *related,
        ]
        record_endpoint = next((item for item in endpoints if item["memory_id"] == record["id"]), None)
        # 参与冲突的每条记忆都能查到关联记录。解决声明只更新原事件的状态。
        if record_endpoint is None or not related or event.get("resolves_event_ids"):
            return []

        record_revision_changed = record_endpoint["expected_revision"] != record["revision"]
        relations = []
        for endpoint in endpoints:
            other_id = endpoint["memory_id"]
            if other_id == record["id"]:
                continue
            head = conn.execute("SELECT revision FROM memory_records WHERE id=?", (other_id,)).fetchone()
            current_revision = head[0] if head is not None else None
            expected_revision = endpoint["expected_revision"]
            relations.append({
                "memory_id": other_id,
                "revision": expected_revision,
                "current_revision": current_revision,
                "event_id": event["event_id"],
                "seq": event["seq"],
                "resolved": event["resolved"],
                "resolved_by": event["resolved_by"],
                "historical": record_revision_changed or head is None or expected_revision != current_revision,
                "availability": "available" if head is not None else "deleted",
            })
        return relations

    def read(self, memory_id, revision=None) -> dict | None:
        with self._snapshot() as conn:
            return self._attach_assessments(conn, self._read(conn, memory_id, revision))

    def list_records(self, type=None, status="active", limit=50, cursor=None) -> dict:
        limit = max(1, min(int(limit), 1000))
        with self._snapshot() as conn:
            clauses, args = ["r.id>?"], [cursor or ""]
            if type is not None:
                clauses.append("v.type=?")
                args.append(type)
            if status is not None:
                clauses.append("v.status=?")
                args.append(status)
            rows = conn.execute("SELECT v.* FROM memory_records r JOIN memory_versions v ON "
                                "r.id=v.memory_id AND r.revision=v.revision WHERE " + " AND ".join(clauses)
                                + " ORDER BY r.id LIMIT ?", [*args, limit + 1]).fetchall()
            items = [self._attach_assessments(conn, self._record(row)) for row in rows[:limit]]
            return {"items": items, "next_cursor": items[-1]["id"] if len(rows) > limit else None}

    def search(self, query, limit=5) -> list[dict]:
        return self.search_snapshot(query, limit)["items"]

    def search_snapshot(self, query=None, limit=5) -> dict:
        limit = max(0, min(int(limit), 1000))
        with self._snapshot() as conn:
            if query is None:
                rows = conn.execute("""SELECT v.* FROM memory_records r JOIN memory_versions v
                    ON r.id=v.memory_id AND r.revision=v.revision
                    WHERE v.status='active' ORDER BY r.id LIMIT ?""", (limit,))
                items = [self._attach_assessments(conn, self._record(row)) for row in rows]
            else:
                items = self._search(conn, query, limit)
            return {"items": items, "generation": self._generation(conn)}

    def _search(self, conn, query, limit):
        limit = max(0, min(int(limit), 1000))
        if not limit:
            return []
        query_text = str(query)
        phrase = query_text.strip().casefold()
        if not phrase:
            return []
        terms = _tokens(query_text)
        exact_rows = conn.execute("""SELECT v.* FROM memory_records r JOIN memory_versions v ON
            r.id=v.memory_id AND r.revision=v.revision WHERE v.status='active' AND
            (r.id=? OR v.name=?) ORDER BY r.id""", (query, query)).fetchall()
        ranked_rows = []
        if terms:
            # 用户输入只作为转义词元，不能注入 MATCH 运算符。
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in terms)
            ranked_rows = conn.execute("""SELECT v.*,bm25(memory_fts) AS score FROM memory_fts
                JOIN memory_records r ON r.id=memory_fts.memory_id
                JOIN memory_versions v ON v.memory_id=r.id AND v.revision=r.revision
                WHERE memory_fts MATCH ? AND v.status='active' ORDER BY score,r.id LIMIT ?""",
                                  (match, max(limit * 4, 20))).fetchall()
        records_by_id = {}
        for row in [*exact_rows, *ranked_rows]:
            record = self._record(row)
            record.pop("score", None)
            records_by_id.setdefault(record["id"], record)
        exact_ids = {row["memory_id"] for row in exact_rows}

        def match_priority(record):
            exact_match = record["id"] in exact_ids
            phrase_match = any(phrase in str(record[field]).casefold() for field in _TEXT_FIELDS)
            return not exact_match, not phrase_match

        # 精确命中优先，其次是完整短语。同一优先级沿用数据库返回的顺序。
        ordered = sorted(records_by_id.values(), key=match_priority)
        return [self._attach_assessments(conn, record) for record in ordered[:limit]]

    @staticmethod
    def _context(context) -> dict:
        return {key: context[key] for key in _IDENTITY_FIELDS if key in context}

    def _emit(self, conn, action, memory_id, revision, context, **data):
        event_id = "evt_" + uuid4().hex
        data["context"] = self._context(context)
        data["operation_namespace"] = context.get("operation_namespace")
        data["operation_id"] = context.get("operation_id")
        conn.execute("INSERT INTO memory_events(event_id,action,memory_id,revision,created_at,data) VALUES (?,?,?,?,?,?)",
                     (event_id, action, memory_id, revision, _now(), _json(data)))
        return event_id

    def _read_expected_revision(self, conn, memory_id, expected_revision):
        record = self._read(conn, memory_id)
        if record is None:
            raise _MutationRejected({"status": "not_found", "memory_id": memory_id})
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
            raise PublicInputError("必须提供整数 expected_revision")
        if record["revision"] != expected_revision:
            raise _MutationRejected({"status": "conflict", "memory_id": memory_id, "revision": record["revision"]})
        return record

    @staticmethod
    def _fields(command, previous=None):
        fields = {"name": "", "type": "project", "description": "", "content": "",
                  "applicability": "", "source_refs": []}
        if previous is not None:
            fields.update({key: previous[key] for key in _FIELDS})
        fields.update({key: command[key] for key in _FIELDS if key in command})
        for key in _TEXT_FIELDS:
            if not isinstance(fields[key], str):
                raise PublicInputError(f"{key} 必须是字符串")
        if not fields["name"].strip() or not fields["content"].strip():
            raise PublicInputError("name 和 content 不能为空")
        if fields["type"] not in {"user", "project", "feedback", "reference"}:
            raise PublicInputError("未知的记忆类型")
        if not isinstance(fields["source_refs"], list) or any(not isinstance(ref, dict) for ref in fields["source_refs"]):
            raise PublicInputError("source_refs 必须是对象列表")
        return fields

    def _validate_assessment(self, conn, command, record):
        judgment = command.get("judgment")
        related = command.get("related_records", [])
        resolves = command.get("resolves_event_ids", [])
        if not judgment and not related and not resolves:
            raise PublicInputError("assess 至少包含判断、冲突引用或明确解决声明")
        if judgment is not None:
            if not isinstance(judgment, dict):
                raise PublicInputError("judgment 必须是对象")
            if judgment.get("coverage") not in {"whole", "partial"}:
                raise PublicInputError("coverage 必须为 whole 或 partial")
            if judgment.get("result") not in {"supported", "contradicted", "inconclusive"}:
                raise PublicInputError("未知的核查判断")
            if not isinstance(judgment.get("assessment"), str) or not judgment["assessment"].strip():
                raise PublicInputError("必须说明本次判断理由")
            if judgment["coverage"] == "partial" and not judgment.get("covered_claims"):
                raise PublicInputError("partial 必须说明 covered_claims")
        if not isinstance(related, list) or not isinstance(resolves, list):
            raise PublicInputError("related_records 和 resolves_event_ids 必须为列表")
        related_ids = set()
        for endpoint in related:
            if not isinstance(endpoint, dict) or not isinstance(endpoint.get("memory_id"), str):
                raise PublicInputError("关联端点必须包含 memory_id")
            endpoint_id = endpoint["memory_id"]
            if endpoint_id == record["id"] or endpoint_id in related_ids:
                raise PublicInputError("关联端点不得重复或指向自身")
            related_ids.add(endpoint_id)
            self._read_expected_revision(conn, endpoint_id, endpoint.get("expected_revision"))
        if resolves:
            self._validate_resolution(conn, record["id"], related_ids, resolves, command.get("resolution_reason"))
        evidence = command.get("evidence_refs", [])
        if not isinstance(evidence, list) or any(not isinstance(ref, dict) for ref in evidence):
            raise PublicInputError("evidence_refs 必须是对象列表")

    def _validate_resolution(self, conn, memory_id, checked_related_ids, event_ids, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise PublicInputError("明确解决声明必须说明 resolution_reason")
        if any(not isinstance(event_id, str) for event_id in event_ids) or len(event_ids) != len(set(event_ids)):
            raise PublicInputError("解决引用必须是唯一的 event_id")
        for event_id in event_ids:
            row = conn.execute(
                "SELECT * FROM memory_events WHERE event_id=? AND action='assess'", (event_id,),
            ).fetchone()
            if row is None:
                raise PublicInputError("被解决事件不存在")
            event = self._event(row)
            affected_ids = {event["memory_id"], *(ref["memory_id"] for ref in event.get("related_records", []))}
            if memory_id not in affected_ids:
                raise PublicInputError("被解决事件与目标记录无关")
            # 当前记录已核对版本，同一冲突涉及的其余记录也须逐一核对才能结清。
            other_affected_ids = affected_ids - {memory_id}
            if not other_affected_ids.issubset(checked_related_ids):
                raise PublicInputError("解决冲突时必须提供所有关联端点的当前版本")

    def _prepare_record(self, conn, command) -> dict:
        """新建时返回正文字段，其余操作返回已核对版本的记录副本。审批时会再次读取当前版本。"""
        action = command["action"]
        if action == "create":
            return self._fields(command)

        record = self._read_expected_revision(conn, command.get("memory_id"), command.get("expected_revision"))
        if action == "update":
            record.update(self._fields(command, record))
        elif action == "assess":
            self._validate_assessment(conn, command, record)
        return record

    def _put_version(self, conn, record):
        conn.execute("INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            record["id"], record["revision"], record["name"], record["type"], record["description"],
            record["content"], record["applicability"], _json(record["source_refs"]), record["status"],
            record["created_at"], record["updated_at"], _json(record.get("legacy_metadata", {}))))
        conn.execute("UPDATE memory_records SET revision=? WHERE id=?", (record["revision"], record["id"]))
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (record["id"],))
        if record["status"] == "active":
            text = " ".join(str(record[key]) for key in _TEXT_FIELDS)
            conn.execute("INSERT INTO memory_fts(memory_id,tokens) VALUES (?,?)", (record["id"], " ".join(_tokens(text))))

    def _delete(self, conn, memory_id):
        affected_proposals = []
        for row in conn.execute("SELECT * FROM memory_proposals"):
            proposal = self._proposal(row)
            command = proposal.get("command", {})
            if (proposal["memory_id"] == memory_id
                    or any(item.get("memory_id") == memory_id for item in command.get("related_records", []))):
                affected_proposals.append(proposal["id"])

        # 核查和审阅事件也可能保存正文副本。保留版本和事件编号，以便另一端仍能显示历史关联。
        for row in conn.execute("SELECT * FROM memory_events").fetchall():
            event = self._event(row)
            related = event.get("related_records", [])
            references_memory = (
                event["memory_id"] == memory_id
                or event.get("proposal_id") in affected_proposals
                or any(item.get("memory_id") == memory_id for item in related)
            )
            if not references_memory:
                continue
            retained = {"redacted": True}
            if related:
                retained["related_records"] = [
                    {"memory_id": item["memory_id"], "expected_revision": item["expected_revision"]}
                    for item in related
                ]
            if event.get("resolves_event_ids"):
                retained["resolves_event_ids"] = event["resolves_event_ids"]
            conn.execute("UPDATE memory_events SET data=? WHERE event_id=?", (_json(retained), event["event_id"]))

        for proposal_id in affected_proposals:
            conn.execute("DELETE FROM memory_proposals WHERE id=?", (proposal_id,))
        conn.execute("DELETE FROM memory_fts WHERE memory_id=?", (memory_id,))
        conn.execute("DELETE FROM memory_records WHERE id=?", (memory_id,))

    def _apply_record_command(self, conn, command, context):
        action = command["action"]
        record = self._prepare_record(conn, command)
        if action == "create":
            now = _now()
            record.update(id="mem_" + uuid4().hex, revision=1, status="active", created_at=now, updated_at=now)
            conn.execute("INSERT INTO memory_records VALUES (?,?)", (record["id"], 1))
            self._put_version(conn, record)
        elif action in {"update", "archive", "restore"}:
            record["revision"] += 1
            record["updated_at"] = _now()
            if action == "archive":
                record["status"] = "archived"
            elif action == "restore":
                record["status"] = "active"
            self._put_version(conn, record)
        elif action == "delete":
            self._delete(conn, record["id"])

        event_data = {}
        if action == "assess":
            # 核查意见附在被核查的版本上。只有一条已绑定且带时间的观察时，才给事件设置观察时间。
            assessment_fields = ("judgment", "evidence_refs", "related_records", "resolves_event_ids", "resolution_reason")
            event_data = {key: command[key] for key in assessment_fields if key in command}
            observation_times = [
                ref["observed_at"] for ref in command.get("evidence_refs", [])
                if ref.get("binding") == "bound" and ref.get("observed_at") is not None
            ]
            event_data["observed_at"] = observation_times[0] if len(observation_times) == 1 else None
        elif action != "delete" and "reason" in command:
            event_data["reason"] = command["reason"]
        event_id = self._emit(conn, action, record["id"], record["revision"], context, **event_data)
        return {"status": "saved", "memory_id": record["id"], "revision": record["revision"], "event_ids": [event_id]}

    @staticmethod
    def _proposal(row):
        if row is None:
            return None
        proposal = json.loads(row["data"])
        proposal["source_session"] = proposal.get("context", {}).get("session_id")
        proposal.update({"id": row["id"], "proposal_id": row["id"], "revision": row["revision"],
                         "proposal_revision": row["revision"], "status": row["status"],
                         "action": row["action"], "memory_id": row["memory_id"]})
        return proposal

    def _create_proposal(self, conn, command, context):
        # 只保存提议修改的字段。批准时再与当前记录合并，提案等待期间的其他修改才能保留。
        self._prepare_record(conn, command)
        proposal_id = "prop_" + uuid4().hex
        now = _now()
        memory_id = command.get("memory_id")
        expected_revision = command.get("expected_revision")
        data = {
            "command": command,
            "context": self._context(context),
            "created_at": now,
            "updated_at": now,
            "expected_revision": expected_revision,
            "operation_namespace": context["operation_namespace"],
            "operation_id": context["operation_id"],
        }
        conn.execute(
            "INSERT INTO memory_proposals VALUES (?,?,?,?,?,?)",
            (proposal_id, 1, "pending", command["action"], memory_id, _json(data)),
        )
        event_id = self._emit(
            conn, "propose", memory_id, expected_revision, context,
            proposal_id=proposal_id, proposal_revision=1, proposed_action=command["action"],
        )
        return {"status": "pending_review", "proposal_id": proposal_id, "proposal_revision": 1, "event_ids": [event_id]}

    @staticmethod
    def _command_for_approval(proposal: dict, decision: dict) -> dict:
        """沿用提案的修改内容，按审阅者看到的目标版本执行。"""
        proposed = dict(proposal.get("command", {}))
        if proposed.get("action") not in _PROPOSABLE_ACTIONS:
            raise PublicInputError("该旧提案没有可接受的明确动作")
        if proposed["action"] in _TARGET_ACTIONS:
            proposed["expected_revision"] = decision.get("expected_revision")
        if "related_records" in decision:
            # 审阅者可以更新关联记忆的版本，但提案指向的记忆必须与提交时一致。
            proposed_ids = {item["memory_id"] for item in proposed.get("related_records", [])}
            reviewed_ids = {item["memory_id"] for item in decision["related_records"]}
            if proposed_ids != reviewed_ids:
                raise PublicInputError("接受提案只能核对关联端点版本，不能更换端点")
            proposed["related_records"] = decision["related_records"]
        return proposed

    def _review_proposal(self, conn, command, context):
        proposal_id = command.get("proposal_id")
        row = conn.execute("SELECT * FROM memory_proposals WHERE id=?", (proposal_id,)).fetchone()
        proposal = self._proposal(row)
        if proposal is None:
            raise _MutationRejected({"status": "not_found", "proposal_id": proposal_id})
        expected_proposal_revision = command.get("expected_proposal_revision")
        if not isinstance(expected_proposal_revision, int) or isinstance(expected_proposal_revision, bool):
            raise PublicInputError("必须提供整数 expected_proposal_revision")
        is_pending = proposal["status"] in {"pending", "pending_review"}
        if proposal["revision"] != expected_proposal_revision or not is_pending:
            raise _MutationRejected({
                "status": "conflict", "proposal_id": proposal_id, "proposal_revision": proposal["revision"],
            })

        # 提案版本保护审阅决定。批准涉及已有记忆的提案时，还要核对审阅者看到的正文版本。
        if command["action"] == "apply_proposal":
            proposed = self._command_for_approval(proposal, command)
            result = self._apply_record_command(conn, proposed, context)
            proposal_status = "applied"
        else:
            result = {"status": "saved", "event_ids": []}
            proposal_status = "rejected"

        memory_id = result.get("memory_id", proposal["memory_id"])
        next_proposal_revision = proposal["revision"] + 1
        reason = command.get("reason", "")
        column_fields = {"id", "proposal_id", "revision", "proposal_revision", "status", "action", "memory_id"}
        data = {key: value for key, value in proposal.items() if key not in column_fields}
        data.update(updated_at=_now(), decision_context=self._context(context), decision_reason=reason)
        conn.execute(
            "UPDATE memory_proposals SET status=?,revision=?,memory_id=?,data=? WHERE id=?",
            (proposal_status, next_proposal_revision, memory_id, _json(data), proposal_id),
        )
        event_id = self._emit(
            conn, command["action"], memory_id, result.get("revision"), context,
            proposal_id=proposal_id, proposal_revision=next_proposal_revision, reason=reason,
        )
        result.update(proposal_id=proposal_id, proposal_revision=next_proposal_revision)
        result["event_ids"].append(event_id)
        return result

    def mutate(self, command: dict, context: dict) -> dict:
        """用同一笔事务提交记忆变更、审阅事件和回执，重复操作可按编号取回原结果。"""
        capability = context.get("capability")
        action = command.get("action")
        if context.get("scope_id") != self.scope_id or capability not in {"propose", "manage"}:
            return {"status": "denied", "reason": "记忆权限或 scope 不允许写入"}
        if capability == "propose" and action not in _PROPOSABLE_ACTIONS:
            return {"status": "denied", "reason": "子任务仅能创建 create/update/assess 提案"}
        if action not in _ACTIONS:
            return {"status": "error", "reason": "未知记忆动作"}

        namespace = context.get("operation_namespace")
        operation_id = context.get("operation_id")
        if not isinstance(namespace, str) or not namespace or not isinstance(operation_id, str) or not operation_id:
            return {"status": "error", "reason": "缺少可信 operation 身份"}
        try:
            command_digest = hashlib.sha256(_json(command).encode()).hexdigest()
            cancel_event = context.get("cancel_event")
            deadline = context.get("deadline")
            if cancel_event is not None and cancel_event.is_set():
                return {"status": "unavailable", "reason": "记忆操作已取消"}
            lock_timeout = 3.0
            if deadline is not None:
                lock_timeout = min(lock_timeout, max(0.0, deadline - time.monotonic()))
                if lock_timeout <= 0:
                    return {"status": "unavailable", "reason": "记忆操作已超时"}

            with self._connect(lock_timeout) as conn:
                # 先拿写锁再查回执，两个进程同时重试同一操作时只会写一次。
                conn.execute("BEGIN IMMEDIATE")
                self._ready(conn)
                previous_operation = conn.execute(
                    "SELECT digest,receipt FROM memory_operations WHERE namespace=? AND operation_id=?",
                    (namespace, operation_id),
                ).fetchone()
                if previous_operation is not None:
                    if previous_operation["digest"] != command_digest:
                        return {"status": "operation_conflict", "operation_id": operation_id}
                    return json.loads(previous_operation["receipt"])

                # 等锁期间也可能收到取消或到达期限。已有回执可以直接返回，新操作要重新检查。
                if cancel_event is not None and cancel_event.is_set():
                    return {"status": "unavailable", "reason": "记忆操作已取消"}
                if deadline is not None and time.monotonic() >= deadline:
                    return {"status": "unavailable", "reason": "记忆操作已超时"}
                try:
                    if capability == "propose":
                        result = self._create_proposal(conn, command, context)
                    elif action in _REVIEW_ACTIONS:
                        result = self._review_proposal(conn, command, context)
                    else:
                        result = self._apply_record_command(conn, command, context)
                except _MutationRejected as exc:
                    result = exc.result

                # 版本冲突和目标缺失也保存回执。只有实际变更才推进记忆库的版本。
                if result["status"] in {"saved", "pending_review"}:
                    self._bump(conn)
                conn.execute(
                    "INSERT INTO memory_operations VALUES (?,?,?,?)",
                    (namespace, operation_id, command_digest, _json(result)),
                )
                conn.commit()
                return result
        except (ValueError, TypeError, KeyError) as exc:
            return {"status": "error", "reason": to_public_error(exc).message}
        except (sqlite3.Error, MemoryStoreUnavailable) as exc:
            return {"status": "unavailable", "reason": to_public_error(exc).message}

    def proposals(self, filters=None, limit=50, cursor=None) -> dict:
        filters = filters or {}
        limit = max(1, min(int(limit), 1000))
        with self._snapshot() as conn:
            items = []
            for row in conn.execute("SELECT * FROM memory_proposals WHERE id>? ORDER BY id", (cursor or "",)):
                proposal = self._proposal(row)
                if all(proposal.get(key, proposal.get("context", {}).get(key)) == value for key, value in filters.items()):
                    items.append(proposal)
                    if len(items) > limit:
                        break
            return {"items": items[:limit], "next_cursor": items[limit - 1]["id"] if len(items) > limit else None}

    def read_proposal(self, id) -> dict | None:
        with self._snapshot() as conn:
            return self._proposal(conn.execute("SELECT * FROM memory_proposals WHERE id=?", (id,)).fetchone())

    def history(self, id) -> list:
        with self._snapshot() as conn:
            return [self._attach_assessments(conn, self._record(row)) for row in conn.execute(
                "SELECT * FROM memory_versions WHERE memory_id=? ORDER BY revision", (id,))]

    def ledger(self, limit=100) -> list:
        with self._snapshot() as conn:
            return [self._event(row) for row in conn.execute("SELECT * FROM memory_events ORDER BY seq DESC LIMIT ?", (max(0, int(limit)),))]

    @staticmethod
    def _stored_import(conn):
        row = conn.execute("SELECT * FROM memory_imports WHERE id=1").fetchone()
        if row is None:
            return None
        try:
            manifest = json.loads(row["manifest"])
            counts = json.loads(row["counts"])
            valid = (isinstance(manifest, dict) and isinstance(counts, dict)
                     and hashlib.sha256(_json(manifest).encode()).hexdigest() == row["digest"]
                     and ("counts" not in manifest or manifest["counts"] == counts)
                     and conn.execute("SELECT value FROM memory_meta WHERE key='ready'").fetchone()[0] == "1")
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise MemoryStoreUnavailable("迁移清单 manifest 损坏或提交未完成")
        return {"manifest": manifest, "counts": counts, "digest": row["digest"]}

    def migration_manifest(self) -> dict | None:
        with self._connect() as conn:
            conn.execute("BEGIN")
            stored = self._stored_import(conn)
            return stored["manifest"] if stored else None

    def import_legacy(self, records, proposals, events, manifest, *, verify_sources=None) -> dict:
        digest = hashlib.sha256(_json(manifest).encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = self._stored_import(conn)
            if old is not None:
                if old["digest"] != digest:
                    return {"status": "conflict", "reason": "库已由其他来源清单完成迁移"}
                return {"status": "already_imported", "counts": old["counts"]}
            if conn.execute("SELECT value FROM memory_meta WHERE key='ready'").fetchone()[0] == "1":
                return {"status": "conflict", "reason": "已经就绪的库不能重新导入旧状态"}
            for imported in records:
                record = {"revision": 1, "status": "active", "created_at": "", "updated_at": "",
                          **imported, **self._fields(imported)}
                if record["status"] not in {"active", "archived"}:
                    raise PublicInputError("迁移记录包含未知 status")
                if not isinstance(record["revision"], int) or record["revision"] < 1:
                    raise PublicInputError("迁移记录 revision 无效")
                conn.execute("INSERT INTO memory_records VALUES (?,?)", (record["id"], record["revision"]))
                self._put_version(conn, record)
            for imported in proposals:
                proposal = dict(imported)
                proposal_id = proposal.get("id", proposal.get("proposal_id")) or "prop_" + uuid4().hex
                revision = proposal.get("revision", proposal.get("proposal_revision", 1))
                action = proposal.get("action", proposal.get("command", {}).get("action", "create"))
                memory_id = proposal.get("memory_id", proposal.get("command", {}).get("memory_id"))
                conn.execute("INSERT INTO memory_proposals VALUES (?,?,?,?,?,?)", (
                    proposal_id, revision, proposal.get("status", "pending"), action, memory_id, _json(proposal)))
            for imported in events:
                event = dict(imported)
                event_id = event.get("event_id", event.get("id")) or "evt_" + uuid4().hex
                action = event.get("action", event.get("kind", "legacy"))
                conn.execute("INSERT INTO memory_events(event_id,action,memory_id,revision,created_at,data) VALUES (?,?,?,?,?,?)", (
                    event_id, action, event.get("memory_id"), event.get("revision"),
                    event.get("created_at", event.get("recorded_at", "")), _json(event)))
            counts = {"records": len(records), "proposals": len(proposals), "events": len(events)}
            if "counts" in manifest and manifest["counts"] != counts:
                raise PublicInputError("迁移清单数量 count 与导入数量不一致")
            actual_counts = {
                "records": conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0],
                "proposals": conn.execute("SELECT COUNT(*) FROM memory_proposals").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0],
            }
            if actual_counts != counts:
                raise PublicInputError("迁移数据库数量 count 与输入数量不一致")
            if "records" in manifest:
                expected_hashes = {item["id"]: item["content_sha256"] for item in manifest["records"]}
                actual_hashes = {
                    row["memory_id"]: hashlib.sha256(row["content"].encode()).hexdigest()
                    for row in conn.execute("SELECT memory_id,content FROM memory_versions")
                }
                if len(expected_hashes) != len(manifest["records"]) or expected_hashes != actual_hashes:
                    raise PublicInputError("迁移正文 hash 摘要与来源清单不一致")
            if verify_sources is not None:
                verify_sources()
            conn.execute("INSERT INTO memory_imports VALUES (1,?,?,?,?)", (_json(manifest), digest, _json(counts), _now()))
            conn.execute("UPDATE memory_meta SET value='1' WHERE key='ready'")
            self._bump(conn)
            conn.commit()
            return {"status": "saved", "counts": counts}

    def export_snapshot(self) -> dict:
        with self._snapshot() as conn:
            rows = conn.execute("""SELECT v.* FROM memory_records r JOIN memory_versions v
                ON r.id=v.memory_id AND r.revision=v.revision ORDER BY r.id""")
            records = [self._attach_assessments(conn, self._record(row)) for row in rows]
            proposals = [self._proposal(row) for row in conn.execute("SELECT * FROM memory_proposals ORDER BY id")]
            events = [self._event(row) for row in conn.execute("SELECT * FROM memory_events ORDER BY seq")]
            manifest = self._stored_import(conn)
            return {"records": records, "proposals": proposals, "events": events,
                    "generation": self._generation(conn), "scope_id": self.scope_id,
                    "import_manifest": manifest["manifest"] if manifest else None}

    def embedding_candidates(self, encoder_id, limit=50) -> list[dict]:
        limit = int(limit)
        if limit <= 0:
            return []
        with self._snapshot() as conn:
            rows = conn.execute("""SELECT v.* FROM memory_records r JOIN memory_versions v ON
                r.id=v.memory_id AND r.revision=v.revision WHERE v.status='active' ORDER BY r.id""")
            result = []
            for row in rows:
                record = self._record(row)
                record["content_hash"] = _content_hash(record)
                embedding = conn.execute("SELECT revision,content_hash FROM memory_embeddings WHERE memory_id=? AND encoder_id=?",
                                         (record["id"], encoder_id)).fetchone()
                if embedding is None or embedding["revision"] != record["revision"] or embedding["content_hash"] != record["content_hash"]:
                    result.append(record)
                    if len(result) >= limit:
                        break
            return result

    def save_embedding(self, memory_id, revision, content_hash, encoder_id, vector) -> bool:
        values = [float(value) for value in vector]
        if not values or any(not math.isfinite(value) for value in values):
            raise PublicInputError("向量必须为有限的非空数值列表")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ready(conn)
            record = self._read(conn, memory_id)
            if not _embedding_is_current(record, revision, content_hash):
                return False
            conn.execute("INSERT OR REPLACE INTO memory_embeddings VALUES (?,?,?,?,?,?)", (
                memory_id, revision, content_hash, encoder_id, len(values), _json(values)))
            self._bump(conn, "index_revision")
            conn.commit()
            return True

    def embeddings(self, encoder_id, limit=1000) -> list[dict]:
        limit = int(limit)
        if limit <= 0:
            return []
        with self._snapshot() as conn:
            result = []
            for row in conn.execute("""SELECT e.* FROM memory_embeddings e
                JOIN memory_records r ON r.id=e.memory_id AND r.revision=e.revision
                JOIN memory_versions v ON v.memory_id=r.id AND v.revision=r.revision
                WHERE e.encoder_id=? AND v.status='active' ORDER BY e.memory_id LIMIT ?""", (encoder_id, limit)):
                record = self._read(conn, row["memory_id"])
                if not _embedding_is_current(record, row["revision"], row["content_hash"]):
                    continue
                result.append({
                    **self._attach_assessments(conn, record),
                    "encoder_id": encoder_id,
                    "content_hash": row["content_hash"],
                    "dimensions": row["dimensions"],
                    "vector": json.loads(row["vector"]),
                })
            return result
