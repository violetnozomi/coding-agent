"""Frozen child outcomes and one manager-owned settlement recovery worker."""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import logging
import threading
import time
from typing import Callable

from nz_coder.protocol.public_error import PublicError, public_error_from_wire
from nz_coder.runtime.agent.child_contracts import presentation_excerpt
from nz_coder.runtime.agent.child_process_result import ChildProcessResultFile, discard_child_process_result
from nz_coder.runtime.agent.child_result import CHILD_RESULT_KEY, ChildAgentResult, child_result_from_state
from nz_coder.runtime.workflows.workflow_process import WorkflowJournalBlocked, WorkflowProcessStore


_LOGGER = logging.getLogger(__name__)
_DELAYS = (0.05, 0.1, 0.2, 0.5, 1.0)


def freeze_settlement(
    state: dict, result: str, *, settlement_id: str,
    canonical: dict | None = None, canonical_settlement_id: str = "",
) -> dict:
    """Detach one final decision; only this invocation's facts can be adopted."""
    frozen = copy.deepcopy(state)
    metadata = getattr(result, "metadata", {})
    candidates = [(canonical, canonical_settlement_id)]
    if isinstance(metadata, dict):
        candidates.append((metadata.get(CHILD_RESULT_KEY), metadata.get("child_result_settlement_id")))
    candidates.append((state.get(CHILD_RESULT_KEY), state.get("child_result_settlement_id")))
    outcome = None
    for candidate, identity in candidates:
        if isinstance(candidate, dict) and identity == settlement_id:
            outcome = ChildAgentResult.from_dict(candidate)
            if (outcome.task_id != state["session_id"] or outcome.session_id != state["session_id"]
                    or outcome.parent_session_id != str(state.get("parent_session_id") or "")):
                raise ValueError("Canonical result belongs to another task")
            break
    status = str(frozen.get("status") or "error")
    if status in {"queued", "running", "cancel_requested"}:
        status = outcome.status if outcome is not None and outcome.status not in {"queued", "running", "cancel_requested"} else "error"
    frozen["status"] = status
    frozen["background_result"] = str(result)
    if outcome is not None:
        error = public_error_from_wire(frozen.get("public_error"))
        changes = {"status": status, "interrupted": status in {"cancelled", "interrupted"}, "limit_reached": status == "max_turns"}
        if status != outcome.status or status in {"cancelled", "timeout"} or (error is not None and error.to_dict() != outcome.public_error):
            digest, kind = presentation_excerpt(str(result))
            changes.update(final_text=str(result), final_text_truncated=False, digest=digest, summary_kind=kind,
                           public_error=error.to_dict() if error is not None else None)
        outcome = ChildAgentResult.from_dict(replace(outcome, **changes).to_dict())
    else:
        frozen.pop(CHILD_RESULT_KEY, None)
        frozen["digest"], frozen["summary_kind"] = presentation_excerpt(str(result))
        outcome = child_result_from_state(frozen, final_text=str(result), status=status)
    frozen[CHILD_RESULT_KEY] = outcome.to_dict()
    if outcome.cost is not None:
        frozen["child_cost_baseline"] = outcome.cost
    frozen["settlement_id"] = settlement_id
    frozen["child_result_settlement_id"] = settlement_id
    frozen["digest"] = outcome.digest
    frozen["summary_kind"] = outcome.summary_kind
    frozen["finished_at"] = time.time()
    return frozen


def project_checkpoint(raw: dict, item: dict | None, identity: str | None, *, committed: bool, parent_session_id: str) -> dict:
    """Derive a read-only view; raw files cannot supersede confirmed admission."""
    state = copy.deepcopy(raw)
    if item is None:
        if not state.get("background"):
            return state
        item = {"task_id": state["session_id"], "title": state.get("display_name", "child")}
    task = str(item["task_id"])
    state.update(session_id=task, parent_session_id=parent_session_id, background=True)
    state.setdefault("display_name", item.get("title", "child"))
    current = str(identity or "")
    canonical = state.get(CHILD_RESULT_KEY)
    matching = str(raw.get("settlement_id") or "") == current
    eligible = isinstance(canonical, dict) and matching and (not current or raw.get("child_result_settlement_id") == current)
    outcome = None
    if eligible:
        outcome = ChildAgentResult.from_dict(canonical)
        eligible = (outcome.task_id == task and outcome.session_id == task
                    and outcome.parent_session_id == parent_session_id)
    if committed:
        state["status"] = str(item["raw_status"])
        valid = False
        if eligible:
            valid = outcome.status == item["raw_status"] and outcome.usage == item.get("usage", {})
            if item.get("summary") and outcome.digest:
                valid = valid and outcome.digest[:800] == item["summary"]
        if not valid:
            state.pop(CHILD_RESULT_KEY, None)
            state.pop("background_result", None)
            state["_checkpoint_unavailable"] = True
        return state
    # 没有本轮 terminal marker 的历史执行只可视为中断，不恢复为成功。
    state["status"] = "interrupted"
    state["_uncommitted_invocation"] = True
    state["settlement_id"] = current
    message = "Parent session ended before this invocation was committed."
    state["background_result"] = message
    if eligible:
        digest, kind = presentation_excerpt(message)
        state[CHILD_RESULT_KEY] = replace(outcome, status="interrupted", final_text=message, digest=digest,
                                         summary_kind=kind, interrupted=True, limit_reached=False,
                                         final_text_truncated=False, public_error=None).to_dict()
    else:
        for key in (CHILD_RESULT_KEY, "tokens", "cost", "cost_known", "structured_output"):
            state.pop(key, None)
        if not raw or isinstance(canonical, dict):
            state["_checkpoint_unavailable"] = True
    return state


@dataclass(frozen=True)
class FrozenSettlement:
    settlement_id: str
    event_id: str
    state: dict
    result_file: ChildProcessResultFile | None
    on_committed: Callable[[], None]
    reconcile_only: bool = False


@dataclass
class _Pending:
    record: FrozenSettlement
    state_saved: bool = False
    attempts: int = 0
    retry_at: float = 0.0
    blocked: bool = False
    error: PublicError | None = None


class SettlementCoordinator:
    """Own retries and their lifetime, independently of execution workers."""

    def __init__(self, store: WorkflowProcessStore, save_state: Callable[[dict], None], handoff_lock) -> None:
        self._store = store
        self._save_state = save_state
        self._handoff_lock = handoff_lock
        self._condition = threading.Condition(threading.Lock())
        self._pending: dict[str, _Pending] = {}
        self._completed: set[str] = set()
        self._thread: threading.Thread | None = None
        self._close_requested = False
        self._closed = False
        self._recover_requested = False
        self._store_retry_at = 0.0
        self._store_attempts = 0
        self._store_blocked = False
        store.set_recovery_wakeup(self.wake)

    def _start_locked(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="nz-settlement-recovery", daemon=True)
            self._thread.start()

    def submit(self, record: FrozenSettlement) -> None:
        with self._condition:
            if self._closed or self._close_requested:
                raise RuntimeError("Settlement coordinator is closing")
            if record.settlement_id in self._pending or record.settlement_id in self._completed:
                return
            detached = replace(record, state=copy.deepcopy(record.state))
            self._pending[record.settlement_id] = _Pending(detached)
            self._start_locked()
            self._condition.notify_all()

    def wake(self, recheck: bool = False) -> None:
        with self._condition:
            if self._closed:
                return
            self._recover_requested = True
            if recheck:
                self._store_blocked = False
                self._store_retry_at = 0.0
                for pending in self._pending.values():
                    pending.blocked = False
                    pending.retry_at = 0.0
            self._start_locked()
            self._condition.notify_all()

    def is_pending(self, identity: str) -> bool:
        with self._condition:
            return identity in self._pending

    def has_pending_task(self, task_id: str) -> bool:
        with self._condition:
            return any(p.record.state["session_id"] == task_id for p in self._pending.values())

    @property
    def closing(self) -> bool:
        with self._condition:
            return self._close_requested or self._closed

    def error_for(self, identity: str) -> PublicError | None:
        with self._condition:
            pending = self._pending.get(identity)
            return pending.error if pending is not None else None

    def _attempt(self, pending: _Pending) -> None:
        record = pending.record
        try:
            with self._handoff_lock:
                # 旧启动修复不能先写 checkpoint、再依赖 reconcile 拒绝旧身份。
                obsolete = record.reconcile_only and (
                    str(self._store.current_invocation(record.state["session_id"]) or "")
                    != str(record.state.get("settlement_id") or "")
                )
                if not obsolete:
                    if not pending.state_saved:
                        self._save_state(copy.deepcopy(record.state))
                        pending.state_saved = True
                    if record.reconcile_only:
                        self._store.reconcile([record.state])
                    else:
                        self._store.commit_terminal(record.state, event_id=record.event_id)
                # 提交以后，通知及传输件清理失败都不能再改业务状态或重做保存。
                try:
                    record.on_committed()
                except Exception:
                    _LOGGER.warning("Child settlement presentation callback failed")
                if record.result_file is not None:
                    try:
                        discard_child_process_result(record.result_file)
                    except (OSError, ValueError):
                        _LOGGER.warning("Committed child transport retained for inspection")
                with self._condition:
                    self._pending.pop(record.settlement_id, None)
                    self._completed.add(record.settlement_id)
                    self._condition.notify_all()
        except (OSError, ValueError) as exc:
            blocked = isinstance(exc, ValueError)
            capacity = isinstance(exc, WorkflowJournalBlocked) and ("capacity" in str(exc) or "exceeds" in str(exc))
            error = PublicError(
                "workflow_journal_capacity" if capacity else "child_settlement_pending",
                "Workflow journal capacity reached; task settlement remains pending." if capacity else "Task settlement is waiting for workflow storage recovery.",
                retryable=not blocked, metadata={"task_id": record.state["session_id"]},
            )
            with self._condition:
                pending.error = error
                pending.blocked = blocked
                pending.retry_at = time.monotonic() + _DELAYS[min(pending.attempts, len(_DELAYS) - 1)]
                pending.attempts += 1
            return
    def _repair_store(self) -> None:
        blocked = False
        try:
            with self._handoff_lock:
                self._store.recover()
        except ValueError:
            blocked = True
        except OSError:
            pass
        with self._condition:
            self._store_blocked = blocked
            self._store_retry_at = time.monotonic() + _DELAYS[min(self._store_attempts, len(_DELAYS) - 1)]
            self._store_attempts += 1

    def _run(self) -> None:
        while True:
            # 不在 condition 内取 store 锁，避免 store 的唤醒回调形成锁环。
            recovery = self._store.needs_recovery
            with self._condition:
                if self._close_requested and not self._pending:
                    return
                now = time.monotonic()
                ready = [pending for pending in self._pending.values() if not pending.blocked and pending.retry_at <= now]
                pending = ready[0] if ready else None
                repair = (recovery or self._recover_requested) and not self._store_blocked and self._store_retry_at <= now
                if pending is None and not repair:
                    deadlines = [p.retry_at for p in self._pending.values() if not p.blocked]
                    if (recovery or self._recover_requested) and not self._store_blocked:
                        deadlines.append(self._store_retry_at)
                    self._condition.wait(max(0.001, min(deadlines) - now) if deadlines else None)
                    continue
                if repair and pending is None:
                    self._recover_requested = False
            if pending is not None:
                self._attempt(pending)
            else:
                self._repair_store()

    def close(self, *, deadline: float) -> bool:
        self.wake(recheck=True)
        with self._condition:
            if self._closed:
                return True
            self._close_requested = True
            self._condition.notify_all()
            thread = self._thread
        assert thread is not None
        thread.join(max(0.0, deadline - time.monotonic()))
        with self._condition:
            # 关闭意图不可撤销；超时以后仍由本 owner 排空，不能接收新执行。
            if thread.is_alive() or self._pending:
                return False
            self._closed = True
            return True
