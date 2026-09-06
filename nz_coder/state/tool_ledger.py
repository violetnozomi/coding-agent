"""Workspace-private authority for tool executions and owned file mutations.

JSON sessions remain the transcript authority. This database owns execution
order, file ownership, and recovery progress; ToolPart fields are projections.
Connections belong to a single operation/thread, never an Agent invocation.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

from nz_coder.foundation.private_paths import harden_private_path
from nz_coder.foundation.user_paths import prepare_user_storage, _secure_directory
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess, WorkspaceFileIdentity
from nz_coder.foundation.content_objects import ContentObjectStore
from nz_coder.protocol.recovery import RecoveryError


_SCHEMA = """
CREATE TABLE IF NOT EXISTS executions (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 execution_id TEXT NOT NULL UNIQUE, attempt_id TEXT NOT NULL UNIQUE,
 session_id TEXT NOT NULL, interaction_id TEXT NOT NULL,
 assistant_step_id TEXT NOT NULL, agent_id TEXT NOT NULL,
 call_id TEXT NOT NULL, tool TEXT NOT NULL, tool_input TEXT NOT NULL,
 execution_state TEXT NOT NULL DEFAULT 'registered',
 terminal_cause TEXT NOT NULL DEFAULT 'none', result_ref TEXT NOT NULL DEFAULT '',
 result_preview TEXT NOT NULL DEFAULT '',
 preview_admitted INTEGER NOT NULL DEFAULT 0,
 UNIQUE(session_id, assistant_step_id, call_id)
);
CREATE TABLE IF NOT EXISTS file_states (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('absent','present')),
 blob TEXT, mode INTEGER NOT NULL, size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS mutations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 operation_id TEXT NOT NULL UNIQUE,
 execution_id TEXT NOT NULL REFERENCES executions(execution_id),
 path TEXT NOT NULL, ordinal INTEGER NOT NULL,
 before_ref TEXT NOT NULL REFERENCES file_states(id),
 planned_ref TEXT NOT NULL REFERENCES file_states(id),
 after_ref TEXT REFERENCES file_states(id),
 status TEXT NOT NULL DEFAULT 'prepared',
 disposition TEXT NOT NULL DEFAULT 'unknown',
 UNIQUE(execution_id, ordinal)
);
CREATE TABLE IF NOT EXISTS session_bases (
 session_id TEXT NOT NULL, path TEXT NOT NULL,
 before_ref TEXT NOT NULL REFERENCES file_states(id),
 PRIMARY KEY(session_id,path)
);
CREATE INDEX IF NOT EXISTS executions_session ON executions(session_id,sequence);
CREATE INDEX IF NOT EXISTS mutations_path ON mutations(path,sequence);
CREATE TABLE IF NOT EXISTS recovery_archives (
 session_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, artifact_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_operations (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
 session_id TEXT NOT NULL, direction TEXT NOT NULL,
 status TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_files (
 operation_id TEXT NOT NULL REFERENCES recovery_operations(operation_id),
 ordinal INTEGER NOT NULL, path TEXT NOT NULL,
 source_ref TEXT NOT NULL REFERENCES file_states(id),
 target_ref TEXT NOT NULL REFERENCES file_states(id),
 status TEXT NOT NULL DEFAULT 'prepared',
 PRIMARY KEY(operation_id,path)
);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ToolLedger:
    """Persist scoped execution identities and byte-exact checkpoint edges."""

    def __init__(self, workspace: Path, *, max_file_bytes: int = 8 * 1024 * 1024):
        self.workspace = Path(workspace).resolve(strict=True)
        layout = prepare_user_storage(self.workspace)
        self.workspace_id = layout.workspace_key
        self.root = layout.workspace_state / "tool-recovery"
        _secure_directory(self.root)
        self.path = self.root / "ledger.sqlite3"
        self.max_file_bytes = max_file_bytes
        self.blobs = ContentObjectStore(self.root / "content")
        self.access = WorkspaceFileAccess(self.workspace)
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError("unsupported tool ledger schema version")
            db.executescript(_SCHEMA)
            if "result_preview" not in {row[1] for row in db.execute("PRAGMA table_info(executions)")}:
                db.execute("ALTER TABLE executions ADD COLUMN result_preview TEXT NOT NULL DEFAULT ''")
            if "preview_admitted" not in {row[1] for row in db.execute("PRAGMA table_info(executions)")}:
                db.execute("ALTER TABLE executions ADD COLUMN preview_admitted INTEGER NOT NULL DEFAULT 0")
            # ALTER may survive interruption before its following DML. Repair
            # on every open, not only while first adding the admission column.
            db.execute("UPDATE executions SET result_preview='' WHERE preview_admitted=0 AND result_preview<>''")
            db.execute("PRAGMA user_version=4")
        if not harden_private_path(self.path).hardened:
            raise ValueError("tool ledger is not owner-private")

    @contextmanager
    def connection(self):
        """One short SQLite transaction; no model calls or file writes inside."""
        _secure_directory(self.root)
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode) or int(getattr(info, "st_file_attributes", 0)) & 0x400:
                raise ValueError("unsafe tool ledger storage alias")
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("PRAGMA synchronous=FULL")
            # DELETE journal avoids orphan WAL sidecars; commits are short and
            # workspace mutation ownership is enforced separately by OS locks.
            with db:
                yield db
        finally:
            db.close()

    def register(self, *, session_id: str, interaction_id: str,
                 assistant_step_id: str, agent_id: str, call_id: str,
                 tool: str, tool_input: dict) -> dict:
        """Idempotently register a fully admitted call, without executing it."""
        identities = (session_id, interaction_id, assistant_step_id, agent_id, call_id, tool)
        if any(not isinstance(item, str) or not item or len(item) > 512 for item in identities):
            raise ValueError("invalid tool execution identity")
        arguments = _json(tool_input)
        if not isinstance(tool_input, dict) or len(arguments) > 1024 * 1024:
            raise ValueError("tool input exceeds ledger limit")
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO executions "
                "(execution_id,attempt_id,session_id,interaction_id,assistant_step_id,agent_id,call_id,tool,tool_input) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("exec-" + uuid.uuid4().hex, "attempt-" + uuid.uuid4().hex, *identities, arguments),
            )
            row = db.execute(
                "SELECT * FROM executions WHERE session_id=? AND assistant_step_id=? AND call_id=?",
                (session_id, assistant_step_id, call_id),
            ).fetchone()
            result = dict(row)
            if (result["tool"], result["tool_input"], result["interaction_id"]) != (tool, arguments, interaction_id):
                raise ValueError("tool identity reused with different input or interaction")
            return result

    def executions(self, session_id: str) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM executions WHERE session_id=? ORDER BY sequence", (session_id,),
            )]

    def assert_writable(self) -> None:
        with self.connection() as db:
            pending = db.execute("SELECT operation_id FROM recovery_operations WHERE status NOT IN ('completed','invalidated') LIMIT 1").fetchone()
        if pending:
            raise RecoveryError("unfinished recovery blocks workspace writes", operation_id=pending[0])

    def capture_file(self, access, path: str):
        try:
            data, identity = access.read_bytes_with_identity(path, maximum=self.max_file_bytes)
        except FileNotFoundError:
            data, identity = None, WorkspaceFileIdentity.missing()
        return self.save_state(data, mode=identity.mode), identity

    def record_compensation(self, operations: list[str]) -> None:
        with self.connection() as db:
            db.executemany("UPDATE mutations SET disposition='compensated' WHERE operation_id=?", [(op,) for op in operations])

    def save_state(self, data: bytes | None, *, mode: int = 0o600) -> str:
        """Durably store raw bytes BEFORE a referencing metadata transaction."""
        if data is not None and len(data) > self.max_file_bytes:
            raise ValueError("checkpoint file byte limit exceeded")
        if data is not None and not isinstance(data, bytes):
            raise TypeError("checkpoint data must be bytes or absent")
        if os.name == "nt":
            # Windows chmod/stat expose readonly vs writable, not POSIX bits.
            mode = 0o666 if mode & stat.S_IWUSR else 0o444
        blob = hashlib.sha256(data).hexdigest() if data is not None else None
        record = {"kind": "absent" if data is None else "present", "blob": blob,
                  "mode": stat.S_IMODE(mode) if data is not None else 0,
                  "size": len(data) if data is not None else 0}
        reference = "state-" + hashlib.sha256(_json(record).encode()).hexdigest()
        if data is not None:
            target = self.blobs._blob_path(blob)
            _secure_directory(target.parent)
            # Unlike best-effort whole-workspace scans, recovery references
            # require the blob and its directory entries to be durable first.
            self.blobs._atomic_bytes(target, data, 0o600, sync=True)
            if os.name != "nt":
                for directory in (target.parent, target.parent.parent, self.blobs.root, self.root):
                    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        os.fsync(fd)
                    finally:
                        os.close(fd)
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO file_states VALUES (?,?,?,?,?)",
                       (reference, record["kind"], blob, record["mode"], record["size"]))
        return reference

    def state(self, reference: str) -> dict:
        with self.connection() as db:
            row = db.execute("SELECT * FROM file_states WHERE id=?", (reference,)).fetchone()
        if row is None:
            raise ValueError("checkpoint state is missing or unknown")
        record = dict(row)
        identity = {key: record[key] for key in ("kind", "blob", "mode", "size")}
        if reference != "state-" + hashlib.sha256(_json(identity).encode()).hexdigest():
            raise ValueError("corrupt checkpoint state")
        return record

    def state_bytes(self, reference: str) -> bytes | None:
        record = self.state(reference)
        if record["kind"] == "absent":
            return None
        if record["size"] > self.max_file_bytes:
            raise ValueError("checkpoint file byte limit exceeded")
        path = self.blobs._blob_path(record["blob"])
        # Protected directory policy rejects aliases; use bounded no-follow read.
        _secure_directory(path.parent)
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("unsafe checkpoint blob")
            data = stream.read(self.max_file_bytes + 1)
        if len(data) != record["size"] or hashlib.sha256(data).hexdigest() != record["blob"]:
            raise ValueError("corrupt checkpoint blob")
        return data

    def prepare_mutation(self, execution_id: str, path: str, before_ref: str,
                         planned_ref: str, *, ordinal: int) -> dict:
        """Persist one intent; after_ref stays NULL until observed and confirmed."""
        relative = self.access._relative(path, write=True).as_posix()
        self.state_bytes(before_ref)
        self.state_bytes(planned_ref)
        if not isinstance(ordinal, int) or ordinal < 0:
            raise ValueError("invalid mutation ordinal")
        with self.connection() as db:
            execution = db.execute("SELECT session_id FROM executions WHERE execution_id=?", (execution_id,)).fetchone()
            if execution is None:
                raise ValueError("unknown tool execution")
            db.execute(
                "INSERT OR IGNORE INTO mutations(operation_id,execution_id,path,ordinal,before_ref,planned_ref) "
                "VALUES (?,?,?,?,?,?)", ("mutation-" + uuid.uuid4().hex, execution_id, relative, ordinal, before_ref, planned_ref),
            )
            row = dict(db.execute("SELECT * FROM mutations WHERE execution_id=? AND ordinal=?", (execution_id, ordinal)).fetchone())
            if (row["path"], row["before_ref"], row["planned_ref"]) != (relative, before_ref, planned_ref):
                raise ValueError("mutation identity reused with different file state")
            db.execute("INSERT OR IGNORE INTO session_bases VALUES (?,?,?)", (execution[0], relative, before_ref))
            return row

    def confirm_mutation(self, operation_id: str, after_ref: str) -> None:
        """Idempotently settle observed bytes, never infer an after from intent."""
        self.state_bytes(after_ref)
        with self.connection() as db:
            row = db.execute("SELECT * FROM mutations WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise ValueError("unknown mutation")
            if row["planned_ref"] != after_ref:
                raise ValueError("checkpoint after differs from intended mutation")
            if row["after_ref"] not in (None, after_ref):
                raise ValueError("mutation already has a different after state")
            db.execute("UPDATE mutations SET after_ref=?,status='confirmed',disposition=CASE WHEN disposition='unknown' THEN 'committed' ELSE disposition END WHERE operation_id=?",
                       (after_ref, operation_id))

    def mutations(self, session_id: str) -> list[dict]:
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT m.*,e.session_id,e.interaction_id,e.assistant_step_id,e.agent_id,e.call_id,e.attempt_id "
                "FROM mutations m JOIN executions e USING(execution_id) WHERE e.session_id=? ORDER BY m.sequence",
                (session_id,),
            )]

    def base(self, session_id: str, path: str) -> str | None:
        with self.connection() as db:
            row = db.execute("SELECT before_ref FROM session_bases WHERE session_id=? AND path=?", (session_id, path)).fetchone()
        return row[0] if row else None
