"""Mutable state owned by exactly one AgentRunner invocation."""
from __future__ import annotations

import copy
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import uuid
from pathlib import Path
from dataclasses import dataclass, field

from nz_coder.foundation.cancellation import CancellationView
from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.result import RunStatus, TokenUsage
from nz_coder.runtime.session.model import Session


_current_run: ContextVar[RunContext | None] = ContextVar("native_run_context", default=None)


def current_run_context():
    return _current_run.get()


@contextmanager
def scoped_run_context(context):
    # 只绑定原 owner，不在 ContextVar 中复制取消或生命周期状态。
    token = _current_run.set(context)
    try:
        yield
    finally:
        _current_run.reset(token)


def request_current_run_cancel() -> None:
    context = current_run_context()
    if context is not None:
        context.request_cancel()


@dataclass
class RunContext:
    """Run-scoped counters and policy state around one Session-owned transcript."""

    request: RunRequest
    session: Session
    active_agent: str
    interaction_run_id: str = ""
    turn_count: int = 0
    iteration_count: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    retry_count: int = 0
    compaction_attempts: int = 0
    budget_zones_emitted: list[str] = field(default_factory=list)
    cancellation: object | None = None
    metadata: dict = field(default_factory=dict)
    terminal_status: RunStatus | None = None
    finalized: bool = False
    _verification: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.request, RunRequest):
            raise TypeError("RunContext request must be RunRequest")
        if not isinstance(self.session, Session):
            raise TypeError("RunContext session must be Session")
        if not isinstance(self.active_agent, str) or not self.active_agent.strip():
            raise ValueError("RunContext active_agent must be non-empty")
        self.metadata = copy.deepcopy(dict(self.metadata))
        if not isinstance(self.interaction_run_id, str):
            raise ValueError("RunContext interaction_run_id must be a string")
        if not self.interaction_run_id.strip():
            selected = (
                self.request.interaction_run_id
                or self.metadata.get("interaction_run_id")
                or f"interaction-{uuid.uuid4().hex}"
            )
            self.interaction_run_id = str(selected)
        self.metadata["interaction_run_id"] = self.interaction_run_id
        self._state_lock = threading.RLock()
        self._cancel_event = threading.Event()
        self._terminal_commit = None
        self._settlement_future = None
        self._retain_terminal_commit = None
        self._terminal_error = None
        self._session_lease = None
        self._execution_started = False

    @property
    def cancel_scope(self) -> CancellationView:
        return CancellationView(self._cancel_event, self.cancellation)

    def request_cancel(self) -> None:
        self._cancel_event.set()

    def check_cancelled(self) -> None:
        """冻结前取消优先；冻结后只结算原结论。"""
        with self._state_lock:
            if self.terminal_status is None and self.cancel_scope.is_set():
                raise asyncio.CancelledError()

    @property
    def verification(self):
        return self._verification

    def attach_verification(self, domain) -> None:
        """域与运行共用锁和身份，绑定后不可被另一实例替换。"""
        with self._state_lock:
            if self._verification is not None:
                raise RuntimeError("Verification domain is already attached")
            if domain._lock is not self._state_lock:
                raise ValueError("Verification domain must share the run lock")
            scope = domain.scope
            if (scope.interaction_run_id != self.interaction_run_id
                    or scope.task.session_id != self.session.session_id
                    or Path(scope.task.workspace).resolve() != self.request.workspace.resolve()):
                raise ValueError("Verification domain belongs to a different run")
            self._verification = domain

    @property
    def transcript(self) -> list[dict]:
        """Expose the one live transcript owned by this context's Session."""
        return self.session.transcript

    def begin_turn(self) -> int:
        """Advance and return the one-based model turn counter."""
        with self._state_lock:
            if self.finalized or self.terminal_status is not None:
                raise RuntimeError("Cannot begin a turn after RunContext finalization")
            self.turn_count += 1
            return self.turn_count

    def add_usage(self, usage: TokenUsage) -> None:
        """Accumulate provider-independent usage for this run."""
        with self._state_lock:
            if self.finalized:
                raise RuntimeError("Cannot add usage after RunContext finalization")
            self.usage = self.usage.add(usage)

    def finish(self, status: RunStatus) -> None:
        """Commit the terminal run state exactly once in memory."""
        with self._state_lock:
            if self.finalized:
                raise RuntimeError("RunContext is already terminal")
            if not isinstance(status, RunStatus):
                raise TypeError("RunContext status must be RunStatus")
            if self.terminal_status is not None and self.terminal_status != status:
                raise RuntimeError("Cannot change frozen run status")
            self.freeze_execution(status)
            self.finalized = True

    def freeze_execution(self, status: RunStatus) -> None:
        """执行结论先冻结；计费和持久化结算完成后再 finish。"""
        with self._state_lock:
            if not isinstance(status, RunStatus):
                raise TypeError("RunContext status must be RunStatus")
            if self.terminal_status is not None and self.terminal_status != status:
                raise RuntimeError("Cannot change frozen run status")
            if status not in {RunStatus.CANCELLED, RunStatus.INTERRUPTED}:
                self.check_cancelled()
            if self._verification is not None:
                snapshot = self._verification.snapshot()
                self._verification.seal(expected_revision=snapshot.revision, terminal_reason=status.value)
            self.terminal_status = status
