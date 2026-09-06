"""Bind tool ownership to the existing controlled file mutation boundary."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import threading
import uuid

from nz_coder.foundation.file_lock import exclusive_file_lock
from nz_coder.foundation.mutation_hooks import scoped_mutation_recorder
from nz_coder.state.tool_ledger import ToolLedger


@dataclass
class CheckpointExecution:
    """One execution attempt, copied by reference into its settled workers."""

    ledger: ToolLedger
    execution_id: str
    ordinal: int = 0
    lock: object = field(default_factory=threading.Lock)

    def reserve(self) -> int:
        with self.lock:
            result = self.ordinal
            self.ordinal += 1
            return result


_EXECUTION: ContextVar[CheckpointExecution | None] = ContextVar("checkpoint_execution", default=None)
_RUN: ContextVar[RecoveryRun | None] = ContextVar("tool_recovery_run", default=None)
_RECOVERY_WRITES: ContextVar[bool] = ContextVar("recovery_file_writes", default=False)


@contextmanager
def recovery_file_writes():
    """Journal-owned replay keeps safe file access but does not invent tools."""
    token = _RECOVERY_WRITES.set(True)
    try:
        with scoped_mutation_recorder(recorded_mutation):
            yield
    finally:
        _RECOVERY_WRITES.reset(token)


def assert_workspace_writable(ledger: ToolLedger) -> None:
    ledger.assert_writable()


@contextmanager
def checkpoint_execution(ledger: ToolLedger, execution_id: str):
    """Scope reliable ownership; arbitrary shell writes never receive receipts."""
    token = _EXECUTION.set(CheckpointExecution(ledger, execution_id))
    try:
        with scoped_mutation_recorder(recorded_mutation):
            yield
    finally:
        _EXECUTION.reset(token)


def capture_file(ledger: ToolLedger, access, path: str):
    """Capture raw bytes and identity from one held file descriptor."""
    return ledger.capture_file(access, path)


@contextmanager
def recorded_mutation(access, path: str, data: bytes | None, transaction, expected, mode):
    """Write-ahead intent, existing safe mutation, then confirmed after state.

    A failed after capture/commit leaves a prepared intent, which is UNKNOWN,
    not evidence that the tool did not write. The transaction layer remains
    responsible for immediate compensation and reports it into this ledger.
    """
    execution = _EXECUTION.get()
    if _RECOVERY_WRITES.get():
        yield expected
        return
    if execution is None or execution.ledger.workspace != access.root:
        from nz_coder.foundation.user_paths import user_storage_layout

        path = user_storage_layout(access.root).workspace_state / "tool-recovery" / "ledger.sqlite3"
        if path.exists():
            ledger = ToolLedger(access.root)
            with exclusive_file_lock(ledger.root / "workspace-write.lock"):
                assert_workspace_writable(ledger)
                yield expected
            return
        yield expected
        return
    ledger = execution.ledger
    with exclusive_file_lock(ledger.root / "workspace-write.lock"):
        assert_workspace_writable(ledger)
        before, current = capture_file(ledger, access, path)
        access._validate_expected(current, expected)
        intended = ledger.save_state(data, mode=current.mode if mode is None else mode)
        mutation = ledger.prepare_mutation(
            execution.execution_id, path, before, intended, ordinal=execution.reserve(),
        )
        if transaction is not None and getattr(transaction, "active", False):
            transaction.record_checkpoint(ledger, mutation)
        yield current
        after, _identity = capture_file(ledger, access, path)
        ledger.confirm_mutation(mutation["operation_id"], after)


def record_compensation(ledger: ToolLedger, operations: list[str]) -> None:
    """Called only after TransactionManager actually restores its owned target."""
    ledger.record_compensation(operations)


@dataclass
class RecoveryRun:
    """An OS-owned Session invocation, not a frontend connection or PID guess."""

    ledger: ToolLedger
    session_id: str
    agent_id: str = field(default_factory=lambda: "invocation-" + uuid.uuid4().hex)
    interaction_id: str = ""
    calls: dict = field(default_factory=dict)

    def attach(self, interaction_id: str, messages: list[dict]) -> None:
        self.interaction_id = interaction_id
        self.overlay(messages, rebuild=True)
        self._archive(messages)

    def _archive(self, messages: list[dict]) -> None:
        """Reuse quota-bounded artifact reads when the recovery block omits facts."""
        from nz_coder.protocol.message_schema import project_public_protocol_value
        from nz_coder.tool_platform.artifacts import ArtifactError, ArtifactStore

        owner = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if owner is None:
            return
        mutations = self.ledger.mutations(self.session_id)
        facts = [{**execution_facts(self.ledger, row, [m for m in mutations if m["execution_id"] == row["execution_id"]]),
                  "call_id": row["call_id"], "tool": row["tool"],
                  "input": project_public_protocol_value(json.loads(row["tool_input"])),
                  "result_summary": row["result_preview"] if row["preview_admitted"] == 1 else ""}
                 for row in self.ledger.executions(self.session_id)]
        rendered = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        if len(rendered) < 4000:
            return
        fingerprint = hashlib.sha256(rendered.encode()).hexdigest()
        store = ArtifactStore(self.ledger.workspace, self.session_id)
        with self.ledger.connection() as db:
            previous = db.execute("SELECT fingerprint,artifact_id FROM recovery_archives WHERE session_id=?", (self.session_id,)).fetchone()
        try:
            if previous and previous[0] == fingerprint:
                try:
                    store.read(previous[1])
                    owner["_nz_tool_recovery_archive"] = previous[1]
                    return
                except ArtifactError:
                    pass
            reference = store.put(rendered, kind="tool-result")
        except ArtifactError:
            owner["_nz_tool_recovery_archive"] = "unavailable: artifact quota or storage failure; inspect session history"
            return
        with self.ledger.connection() as db:
            db.execute("INSERT OR REPLACE INTO recovery_archives VALUES (?,?,?)", (self.session_id, fingerprint, reference))
        owner["_nz_tool_recovery_archive"] = reference

    def overlay(self, messages: list[dict], *, rebuild: bool = False) -> None:
        """Refresh non-authoritative ToolPart projections using stable IDs."""
        records = self.ledger.executions(self.session_id)
        mutations = self.ledger.mutations(self.session_id)
        seen = set()
        for message in messages:
            for part in message.get("_nz_parts", []) if isinstance(message, dict) else []:
                if not isinstance(part, dict) or part.get("type") != "tool":
                    continue
                source_step = (part.get("metadata") or {}).get("recovery_source_message_id") or part.get("message_id") or message.get("_nz_message_id")
                row = next((r for r in records if r["call_id"] == part.get("call_id") and r["assistant_step_id"] == source_step), None)
                if row is None:
                    continue
                seen.add(row["execution_id"])
                from nz_coder.runtime.conversation.tool_recovery import _artifact_reference

                artifact = _artifact_reference(part.get("state") or {})
                if artifact and not row["result_ref"]:
                    with self.ledger.connection() as db:
                        db.execute("UPDATE executions SET result_ref=? WHERE execution_id=? AND result_ref=''", (artifact, row["execution_id"]))
                    row["result_ref"] = artifact
                owned = [item for item in mutations if item["execution_id"] == row["execution_id"]]
                part.setdefault("state", {})["recovery"] = execution_facts(self.ledger, row, owned)
        # The admitted envelope may outlive the JSON checkpoint that contained
        # its ToolPart. Rebuild only a non-authoritative data projection, never
        # an assistant call or fabricated provider tool-result message.
        if not rebuild:
            return
        owner = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if owner is None:
            return
        for row in records:
            if row["execution_id"] in seen:
                continue
            owned = [item for item in mutations if item["execution_id"] == row["execution_id"]]
            if owned and all(m["disposition"] == "reverted" for m in owned):
                continue
            state = {"status": "completed" if row["execution_state"] == "succeeded" else "error",
                     "input": json.loads(row["tool_input"]), "recovery": execution_facts(self.ledger, row, owned)}
            if row["execution_state"] == "succeeded":
                if row["preview_admitted"] == 1:
                    state["output"] = row["result_preview"]
            else:
                state["error"] = {"message": "Runtime recovery: inspect recorded execution and file-effect facts."}
            owner.setdefault("_nz_parts", []).append({
                "id": "part-" + row["execution_id"], "message_id": owner.get("_nz_message_id", ""),
                "type": "tool", "tool": row["tool"], "call_id": row["call_id"],
                "internal": True, "authoritative": False, "state": state,
                "metadata": {"recovery_source_message_id": row["assistant_step_id"],
                             "recovery_source_session_id": self.session_id},
            })


def execution_facts(ledger: ToolLedger, row: dict, mutations: list[dict]) -> dict:
    """Closed fact projection: no raw input, output, exception or private fields."""
    from nz_coder.tools import get_tool_side_effect

    if mutations:
        states = {m["disposition"] for m in mutations}
        effect = next(iter(states)) if len(states) == 1 else "unknown"
    elif row["execution_state"] in {"registered", "not_executed"}:
        effect = "none"
    elif get_tool_side_effect(row["tool"]) in {"readonly", "reads-network"}:
        effect = "none"
    else:
        effect = "unknown"
    return {
        "version": 1, "workspace_id": ledger.workspace_id,
        **{key: row[key] for key in ("execution_id", "attempt_id", "session_id", "interaction_id",
                                     "assistant_step_id", "agent_id", "sequence", "execution_state", "terminal_cause")},
        "side_effect_state": effect, "result_ref": row["result_ref"],
        "files": [{"path": m["path"], "operation_id": m["operation_id"],
                   "before_ref": m["before_ref"], "after_ref": m["after_ref"] or "",
                   "state": m["disposition"] if m["disposition"] != "unknown" else "unknown"}
                  for m in mutations],
    }


@contextmanager
def recovery_run(workspace, session_id: str):
    """Own the Session before reconciling unfinished execution records."""
    ledger = ToolLedger(workspace)
    lock_key = hashlib.sha256(session_id.encode()).hexdigest()
    with exclusive_file_lock(ledger.root / (lock_key + ".owner.lock"), blocking=False):
        with exclusive_file_lock(ledger.root / "workspace-write.lock"):
            assert_workspace_writable(ledger)
            with ledger.connection() as db:
                db.execute("UPDATE recovery_operations SET status='invalidated' WHERE session_id=? AND status='completed'", (session_id,))
        # Only an OS-released lock grants permission to call old attempts lost.
        # Pure attach/snapshot readers never enter this function.
        with ledger.connection() as db:
            db.execute("UPDATE executions SET execution_state='uncertain',terminal_cause='process_lost' "
                       "WHERE session_id=? AND execution_state IN ('registered','running')", (session_id,))
        run = RecoveryRun(ledger, session_id)
        token = _RUN.set(run)
        try:
            yield run
        except BaseException as exc:
            abort_registered(error=exc)
            raise
        else:
            abort_registered()
        finally:
            _RUN.reset(token)


def register_batch(messages: list[dict], calls: list[dict]) -> None:
    """Persist admitted envelopes before either permission prompts or dispatch."""
    run = _RUN.get()
    if run is None:
        return
    owner = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
    step = owner.get("_nz_message_id")
    if not step:
        raise ValueError("admitted tool batch requires a stable assistant step identity")
    for call in calls:
        function = call["function"]
        raw = function.get("arguments", {})
        arguments = json.loads(raw) if isinstance(raw, str) else raw
        row = run.ledger.register(session_id=run.session_id, interaction_id=run.interaction_id,
                                  assistant_step_id=step, agent_id=run.agent_id,
                                  call_id=call["id"], tool=function["name"], tool_input=arguments)
        run.calls[call["id"]] = row
    run.overlay(messages)


def attach_recovery_context(context) -> None:
    run = _RUN.get()
    if run is not None:
        run.attach(context.interaction_run_id, context.transcript)


def overlay_recovery(messages: list[dict]) -> None:
    run = _RUN.get()
    if run is not None:
        run.overlay(messages)


@contextmanager
def dispatch_checkpoint(call_id: str):
    """Called AFTER authorization; CAS prevents replaying a settled attempt."""
    run = _RUN.get()
    record = run.calls.get(call_id) if run is not None else None
    if record is None:
        yield True
        return
    from nz_coder.tools import get_tool_side_effect

    if get_tool_side_effect(record["tool"]) not in {"readonly", "reads-network"}:
        assert_workspace_writable(run.ledger)
    with run.ledger.connection() as db:
        changed = db.execute("UPDATE executions SET execution_state='running' WHERE execution_id=? AND execution_state='registered'",
                             (record["execution_id"],)).rowcount
    if not changed:
        yield False
        return
    with checkpoint_execution(run.ledger, record["execution_id"]):
        yield True


def settle_execution(call_id: str, result=None, *, cancelled: bool = False, failed: bool = False) -> None:
    """Record execution completion separately from the file-effect disposition."""
    run = _RUN.get()
    record = run.calls.get(call_id) if run is not None else None
    if record is None:
        return
    with run.ledger.connection() as db:
        row = db.execute("SELECT execution_state FROM executions WHERE execution_id=?", (record["execution_id"],)).fetchone()
        if row[0] not in {"registered", "running"}:
            return
        if cancelled or failed:
            state = "not_executed" if row[0] == "registered" else "uncertain"
            cause = "user_cancelled" if cancelled else "exception"
        else:
            state = "not_executed" if not result.executed else "failed" if result.dispatch_failed or result.command_failed else "succeeded"
            metadata = result.metadata or {}
            cause = ("permission_denied" if result.permission_denied else
                     "user_cancelled" if metadata.get("cancelled") or metadata.get("termination") == "cancelled" else
                     "timeout" if metadata.get("termination") == "timeout" else
                     "exception" if state == "failed" else "none")
        # Execution facts may settle before output guardrails. No raw result
        # preview crosses that boundary; publication is a separate admission.
        db.execute("UPDATE executions SET execution_state=?,terminal_cause=? WHERE execution_id=? AND execution_state=?",
                   (state, cause, record["execution_id"], row[0]))


def settle_batch(dispatched: list, messages: list[dict]) -> None:
    for _index, call, result in dispatched:
        settle_execution(call["id"], result)
        run = _RUN.get()
        record = run.calls.get(call["id"]) if run is not None else None
        if record is not None:
            preview = str(result.output)[:500] if result.executed and not result.dispatch_failed and not result.permission_denied else ""
            with run.ledger.connection() as db:
                db.execute("UPDATE executions SET result_preview=?,preview_admitted=1 WHERE execution_id=? AND preview_admitted=0",
                           (preview, record["execution_id"]))
    overlay_recovery(messages)


def abort_registered(calls: list[dict] | None = None, *, error: BaseException | None = None, cause: str | None = None) -> None:
    """A controlled exit proves registered-but-unstarted calls did not execute."""
    import asyncio

    run = _RUN.get()
    if run is None:
        return
    records = list(run.calls.values()) if calls is None else [run.calls[c["id"]] for c in calls if c["id"] in run.calls]
    reason = cause or ("user_cancelled" if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt)) else "exception" if error is not None else "unknown")
    with run.ledger.connection() as db:
        db.executemany("UPDATE executions SET execution_state='not_executed',terminal_cause=? WHERE execution_id=? AND execution_state='registered'",
                       [(reason, row["execution_id"]) for row in records])
