"""One shared owned-file Undo/Redo coordinator for product entry points."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path

from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.protocol.message_schema import MESSAGE_ID_KEY, is_synthetic_user_message
from nz_coder.runtime.process.workspace_snapshot import SnapshotError, WorkspaceSnapshotStore
from nz_coder.runtime.session.recovery_journal import RecoveryError, RecoveryJournal, SessionRevertResult
from nz_coder.state.sessions import active_session_id, write_session_runtime_json
from nz_coder.state.tool_ledger import ToolLedger
from nz_coder.state.workdir import scoped_workdir

__all__ = ["SessionReverter", "SessionRevertResult", "RecoveryError"]


class SessionReverter:
    """Select transcript boundaries; the durable journal owns every transition."""

    def __init__(self, store: WorkspaceSnapshotStore, state_path: Path, *, session_id: str | None = None):
        self.store = store
        self.state_path = Path(state_path)  # Compatibility/UI hint, not authority.
        self.session_id = session_id

    def _session(self, messages: list[dict]) -> str:
        identity = self.session_id or next((m.get("_nz_session_id") for m in messages if m.get("_nz_session_id")), None) or active_session_id()
        if not identity:
            raise SnapshotError("Session identity is required for owned recovery")
        self.session_id = str(identity)
        return self.session_id

    @contextmanager
    def _journal(self, messages: list[dict]):
        session_id = self._session(messages)
        ledger = ToolLedger(self.store.workspace)
        key = hashlib.sha256(session_id.encode()).hexdigest()
        try:
            with exclusive_file_lock(ledger.root / (key + ".owner.lock"), blocking=False):
                journal = RecoveryJournal(ledger, session_id)
                with journal.locked():
                    yield journal
        except BlockingIOError as exc:
            raise RecoveryError("Session execution is still owned; cannot recover live work", recovery_required=False) from exc

    def revert(self, messages: list[dict], *, message_id: str | None = None) -> SessionRevertResult:
        with self._journal(messages) as journal:
            pending = journal.pending()
            if pending and pending["direction"] == "undo" and (message_id is None or message_id == pending["payload"]["message_id"]):
                result = journal.apply(pending, messages)
            else:
                target = self._target_index(messages, message_id)
                if target is None:
                    raise RecoveryError("no revertible message", recovery_required=False)
                result = journal.undo(messages, target)
            self._hint(result)
        self.store._file_cache.clear()
        return result

    def unrevert(self, messages: list[dict]) -> SessionRevertResult:
        with self._journal(messages) as journal:
            pending = journal.pending()
            result = journal.apply(pending, messages) if pending and pending["direction"] == "redo" else journal.redo(messages)
            self._hint(result)
        self.store._file_cache.clear()
        return result

    def recover(self, messages: list[dict]) -> SessionRevertResult | None:
        """Reconcile a previously interrupted operation without replaying writes."""
        with self._journal(messages) as journal:
            result = journal.recover(messages)
            if result is not None:
                self._hint(result)
        self.store._file_cache.clear()
        return result

    def clear(self) -> None:
        """New work invalidates completed Redo, never an unfinished operation."""
        if not self.session_id:
            return
        journal = RecoveryJournal(ToolLedger(self.store.workspace), self.session_id)
        with journal.locked():
            journal.invalidate_redo()

    def _hint(self, result: SessionRevertResult) -> None:
        # Optional UI hint is not a second recovery authority.
        try:
            with scoped_workdir(self.store.workspace):
                write_session_runtime_json(self.state_path, {"version": 2, "operation_id": result.operation_id,
                                                            "session_id": self.session_id, "status": result.status})
        except OSError:
            pass

    @staticmethod
    def _target_index(messages: list[dict], message_id: str | None) -> int | None:
        if message_id:
            return next((i for i, message in enumerate(messages) if message.get(MESSAGE_ID_KEY) == message_id), None)
        return next((i for i in range(len(messages) - 1, -1, -1)
                     if messages[i].get("role") == "user" and not is_synthetic_user_message(messages[i])), None)
