"""Durable, conflict-checked file/history recovery without filesystem ACID.

Each operation is prepared before disk mutation. Reconciliation always checks
source/target/neither, never infers ownership from a workspace snapshot diff.
Only this journal coordinates file disposition and transcript commit markers.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os

from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.protocol.recovery import RecoveryError, SessionRevertResult
from nz_coder.runtime.process.checkpoint_runtime import capture_file
from nz_coder.state.sessions import load_session, save_session
from nz_coder.state.workdir import scoped_workdir
from nz_coder.state.tool_ledger import ToolLedger, _json


def history_hash(messages: list[dict]) -> str:
    """Hash canonical transcript facts, excluding refreshable ledger projections."""
    value = copy.deepcopy(messages)
    for message in value:
        for part in message.get("_nz_parts", []):
            if isinstance(part, dict) and isinstance(part.get("state"), dict):
                part["state"].pop("recovery", None)
    return hashlib.sha256(_json(value).encode()).hexdigest()


class RecoveryJournal:
    """Coordinate one Session's owned recovery under the workspace write lock."""

    def __init__(self, ledger: ToolLedger, session_id: str):
        self.ledger = ledger
        self.session_id = session_id

    def pending(self) -> dict | None:
        with self.ledger.connection() as db:
            row = db.execute("SELECT * FROM recovery_operations WHERE status NOT IN ('completed','invalidated') ORDER BY sequence LIMIT 1").fetchone()
        return self._decode(row)

    def latest(self) -> dict | None:
        with self.ledger.connection() as db:
            row = db.execute("SELECT * FROM recovery_operations WHERE session_id=? ORDER BY sequence DESC LIMIT 1", (self.session_id,)).fetchone()
        return self._decode(row)

    @staticmethod
    def _decode(row) -> dict | None:
        if row is None:
            return None
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def undo(self, messages: list[dict], target: int) -> SessionRevertResult:
        pending = self.pending()
        if pending:
            raise RecoveryError("unfinished recovery must be reconciled first", operation_id=pending["operation_id"])
        tail = messages[target:]
        steps = {m.get("_nz_message_id") for m in tail}
        records = self.ledger.executions(self.session_id)
        selected = [m for m in self.ledger.mutations(self.session_id)
                    if m["assistant_step_id"] in steps and m["disposition"] != "compensated"]
        if not selected:
            raise RecoveryError("no owned checkpoint evidence; legacy/unknown ownership is not automatically reverted", recovery_required=False)
        if any(m["status"] != "confirmed" or m["disposition"] != "committed" for m in selected):
            raise RecoveryError("unconfirmed file effects require inspection before recovery", conflicts=[m["path"] for m in selected if m["status"] != "confirmed"])
        by_path: dict[str, list[dict]] = {}
        for mutation in selected:
            by_path.setdefault(mutation["path"], []).append(mutation)
        conflicts = []
        files = []
        for path, chain in sorted(by_path.items()):
            if any(a["after_ref"] != b["before_ref"] for a, b in zip(chain, chain[1:])):
                conflicts.append(path)
            # Later foreign owned changes also break attribution even if their
            # bytes happen to match an earlier checkpoint.
            with self.ledger.connection() as db:
                ownership = {m["operation_id"] for m in chain}
                foreign = any(row[0] not in ownership for row in db.execute(
                    "SELECT operation_id FROM mutations WHERE path=? AND sequence>=? AND disposition NOT IN ('reverted','compensated')",
                    (path, chain[0]["sequence"])))
            if foreign:
                conflicts.append(path)
            files.append({"path": path, "source_ref": chain[-1]["after_ref"], "target_ref": chain[0]["before_ref"]})
        if conflicts:
            raise RecoveryError("owned file chain conflict; user/other Agent changes were not overwritten", conflicts=sorted(set(conflicts)), recovery_required=False)
        from nz_coder.tools import get_tool_side_effect

        owned_executions = {m["execution_id"] for m in selected}
        unsupported = [r["call_id"] for r in records if r["assistant_step_id"] in steps
                       and r["execution_state"] not in {"registered", "not_executed"}
                       and (get_tool_side_effect(r["tool"]) in {"mutates-shell", "mutates-network"}
                            or (get_tool_side_effect(r["tool"]) == "mutates-fs" and r["execution_id"] not in owned_executions))]
        payload = {"message_id": str(tail[0].get("_nz_message_id") or ""),
                   "source_history": copy.deepcopy(messages), "target_history": copy.deepcopy(messages[:target]),
                   "source_hash": history_hash(messages), "target_hash": history_hash(messages[:target]),
                   "mutation_ids": [m["operation_id"] for m in selected], "unsupported_tools": unsupported,
                   "removed_messages": len(tail)}
        operation = self._prepare("undo", payload, files)
        return self.apply(operation, messages)

    def redo(self, messages: list[dict]) -> SessionRevertResult:
        if self.pending():
            raise RecoveryError("unfinished recovery must be reconciled first")
        previous = self.latest()
        if not previous or previous["direction"] != "undo" or previous["status"] != "completed":
            raise RecoveryError("redo unavailable: no completed Undo or new work invalidated Redo", recovery_required=False)
        old = previous["payload"]
        if history_hash(messages) != old["target_hash"]:
            raise RecoveryError("conversation advanced after revert; Redo is invalid", recovery_required=False)
        payload = {**old, "source_history": old["target_history"], "target_history": old["source_history"],
                   "source_hash": old["target_hash"], "target_hash": old["source_hash"]}
        files = [{"path": item["path"], "source_ref": item["target_ref"], "target_ref": item["source_ref"]}
                 for item in self.files(previous["operation_id"])]
        return self.apply(self._prepare("redo", payload, files), messages)

    def _prepare(self, direction: str, payload: dict, files: list[dict]) -> dict:
        import uuid

        with scoped_workdir(self.ledger.workspace):
            durable = load_session(self.session_id)
        durable_hash = history_hash(durable.get("messages") or []) if durable else None
        if durable_hash is not None and durable_hash != payload["source_hash"]:
            raise RecoveryError("durable Session history changed before recovery", recovery_required=False)
        payload["source_durable_hash"] = durable_hash
        if len(_json(payload).encode()) > 32 * 1024 * 1024:
            raise RecoveryError("recovery history exceeds journal limit", recovery_required=False)
        # Validate all objects and source files before the first mutation.
        conflicts = self._ownership_conflicts(payload, files)
        for item in files:
            self.ledger.state_bytes(item["source_ref"])
            self.ledger.state_bytes(item["target_ref"])
            if self._current(item["path"])[0] != item["source_ref"]:
                conflicts.append(item["path"])
        if conflicts:
            raise RecoveryError("workspace recovery conflict: " + ", ".join(conflicts), conflicts=conflicts, recovery_required=False)
        operation_id = "recovery-" + uuid.uuid4().hex
        with self.ledger.connection() as db:
            if db.execute("SELECT 1 FROM recovery_operations WHERE status NOT IN ('completed','invalidated')").fetchone():
                raise RecoveryError("unfinished workspace recovery")
            db.execute("INSERT INTO recovery_operations(operation_id,session_id,direction,status,payload) VALUES (?,?,?,'prepared',?)",
                       (operation_id, self.session_id, direction, _json(payload)))
            db.executemany("INSERT INTO recovery_files(operation_id,ordinal,path,source_ref,target_ref) VALUES (?,?,?,?,?)",
                           [(operation_id, i, item["path"], item["source_ref"], item["target_ref"]) for i, item in enumerate(files)])
        return {"operation_id": operation_id, "session_id": self.session_id, "direction": direction, "status": "prepared", "payload": payload}

    def _ownership_conflicts(self, payload: dict, files: list[dict]) -> list[str]:
        """Redo and restart use the same sequence-based foreign-owner check."""
        selected = set(payload["mutation_ids"])
        conflicts = []
        with self.ledger.connection() as db:
            for item in files:
                rows = [dict(row) for row in db.execute("SELECT operation_id,sequence,disposition FROM mutations WHERE path=? ORDER BY sequence", (item["path"],))]
                own = [row for row in rows if row["operation_id"] in selected]
                if not own or any(row["sequence"] >= own[0]["sequence"] and row["operation_id"] not in selected
                                  and row["disposition"] not in {"compensated", "reverted"} for row in rows):
                    conflicts.append(item["path"])
        return conflicts

    def files(self, operation_id: str) -> list[dict]:
        with self.ledger.connection() as db:
            return [dict(row) for row in db.execute("SELECT * FROM recovery_files WHERE operation_id=? ORDER BY ordinal", (operation_id,))]

    def _status(self, operation_id: str, status: str) -> None:
        with self.ledger.connection() as db:
            db.execute("UPDATE recovery_operations SET status=? WHERE operation_id=?", (status, operation_id))

    def _current(self, path: str):
        return capture_file(self.ledger, self.ledger.access, path)

    def _apply_file(self, item: dict) -> None:
        from nz_coder.runtime.process.checkpoint_runtime import recovery_file_writes

        current, identity = self._current(item["path"])
        if current == item["target_ref"]:
            return  # Already applied, including process death before receipt.
        if current != item["source_ref"]:
            raise RecoveryError("workspace recovery conflict: " + item["path"], conflicts=[item["path"]])
        data = self.ledger.state_bytes(item["target_ref"])
        with recovery_file_writes():
            if data is None:
                self.ledger.access.delete(item["path"], expected=identity)
            else:
                self.ledger.access.write_bytes(item["path"], data, expected=identity,
                                               overwrite=identity.expected_exists,
                                               mode=self.ledger.state(item["target_ref"])["mode"])
        if self._current(item["path"])[0] != item["target_ref"]:
            raise RecoveryError("workspace changed during recovery", conflicts=[item["path"]])

    def _history_committed(self, operation: dict) -> bool:
        with scoped_workdir(self.ledger.workspace):
            payload = load_session(self.session_id)
        return ((payload.get("metadata") or {}).get("recovery_operation") == operation["operation_id"]
                and history_hash(payload.get("messages") or []) == operation["payload"]["target_hash"])

    def _commit_history(self, operation: dict) -> None:
        self._assert_durable_history(operation)
        with scoped_workdir(self.ledger.workspace):
            existing = load_session(self.session_id)
            metadata = dict(existing.get("metadata") or {})
            metadata.update(recovery_operation=operation["operation_id"], verification_invalidated=operation["operation_id"])
            save_session(copy.deepcopy(operation["payload"]["target_history"]), session_id=self.session_id,
                         activate=False, require_aliases=False, mode=existing.get("mode"), model=existing.get("model"),
                         run_status="interrupted", session_metadata=metadata)

    def _assert_durable_history(self, operation: dict) -> None:
        if self._history_committed(operation):
            return
        with scoped_workdir(self.ledger.workspace):
            durable = load_session(self.session_id)
        current = history_hash(durable.get("messages") or []) if durable else None
        if current != operation["payload"].get("source_durable_hash"):
            raise RecoveryError("durable Session history changed during recovery", operation_id=operation["operation_id"])

    def apply(self, operation: dict, messages: list[dict]) -> SessionRevertResult:
        if operation["session_id"] != self.session_id:
            raise RecoveryError("another Session owns the unfinished workspace recovery", operation_id=operation["operation_id"])
        payload = operation["payload"]
        operation_id = operation["operation_id"]
        files = self.files(operation_id)
        current_history = history_hash(messages)
        if current_history not in {payload["source_hash"], payload["target_hash"]}:
            self._status(operation_id, "conflicted")
            raise RecoveryError("history conflict during recovery", operation_id=operation_id)
        try:
            self._assert_durable_history(operation)
            # Restart preflight validates ALL paths/objects before continuing.
            conflicts = self._ownership_conflicts(payload, files)
            for item in files:
                self.ledger.state_bytes(item["source_ref"])
                self.ledger.state_bytes(item["target_ref"])
                if self._current(item["path"])[0] not in {item["source_ref"], item["target_ref"]}:
                    conflicts.append(item["path"])
            if conflicts:
                raise RecoveryError("workspace recovery conflict", conflicts=conflicts)
            self._status(operation_id, "applying")
            for item in files:
                self._apply_file(item)
                with self.ledger.connection() as db:
                    db.execute("UPDATE recovery_files SET status='applied' WHERE operation_id=? AND path=?", (operation_id, item["path"]))
            with self.ledger.connection() as db:
                disposition = "reverted" if operation["direction"] == "undo" else "committed"
                db.executemany("UPDATE mutations SET disposition=? WHERE operation_id=?",
                               [(disposition, mutation) for mutation in payload["mutation_ids"]])
            self._status(operation_id, "files_applied")
            self._invalidate(operation_id, [item["path"] for item in files])
            if not self._history_committed(operation):
                try:
                    self._commit_history(operation)
                except Exception:
                    if not self._history_committed(operation):
                        raise
            self._sync_history()
            messages[:] = copy.deepcopy(payload["target_history"])
            self._status(operation_id, "completed")
        except Exception as exc:
            conflicts = exc.result.conflicts if isinstance(exc, RecoveryError) else ()
            self._status(operation_id, "conflicted" if conflicts else "recovery_required")
            raise RecoveryError("recovery incomplete; recorded progress retained", operation_id=operation_id, conflicts=conflicts) from exc
        return SessionRevertResult(payload["message_id"], tuple(item["path"] for item in files), payload["removed_messages"],
                                   operation_id=operation_id, unsupported_tools=tuple(payload.get("unsupported_tools") or ()))

    def _sync_history(self) -> None:
        """Persist the authoritative JSON directory entry before completion."""
        from nz_coder.state.sessions import session_dir

        if os.name != "nt":
            with scoped_workdir(self.ledger.workspace):
                directory = session_dir()
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def recover(self, messages: list[dict]) -> SessionRevertResult | None:
        operation = self.pending()
        return self.apply(operation, messages) if operation else None

    def invalidate_redo(self) -> None:
        pending = self.pending()
        if pending:
            raise RecoveryError("unfinished workspace recovery prevents new work", operation_id=pending["operation_id"])
        with self.ledger.connection() as db:
            db.execute("UPDATE recovery_operations SET status='invalidated' WHERE session_id=? AND status='completed'", (self.session_id,))

    def _invalidate(self, operation_id: str, paths: list[str]) -> None:
        """Invalidate old validation evidence before committing changed history."""
        from nz_coder.state.sessions import session_runtime_state_path, write_session_runtime_json

        with scoped_workdir(self.ledger.workspace):
            state_path = session_runtime_state_path(self.session_id)
            if state_path.exists():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                state["verification_invalidated"] = operation_id
                state["active"] = False
                write_session_runtime_json(state_path, state)
        # Existing live caches must not wait for a polling watcher. No new
        # intelligence service or background indexing job is started here.
        from nz_coder.intelligence.service import workspace_repo_intelligence

        service = workspace_repo_intelligence(self.ledger.workspace, create=False)
        if service is not None:
            service._apply_incremental(tuple(paths), 5000)

    def locked(self):
        return exclusive_file_lock(self.ledger.root / "workspace-write.lock")
