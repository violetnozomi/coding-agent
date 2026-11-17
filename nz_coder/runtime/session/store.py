"""Storage-neutral SessionStore and current JSON-format production adapter."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Awaitable, Callable, Protocol, runtime_checkable
import hashlib
import json
import uuid
import threading

from nz_coder.foundation.file_lock import FileLockBusy, exclusive_file_lock
from nz_coder.runtime.session.ownership import SessionRunLease, session_busy
from nz_coder.foundation.async_utils import to_thread_settled
from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.runtime.core.result import TokenUsage
from nz_coder.state.sessions import SessionReadError, _read_saved_session, _session_path, _valid_persisted_parts, save_session, scoped_session
from nz_coder.state.workdir import scoped_workdir
from nz_coder.state import sessions as json_sessions
from nz_coder.protocol.public_error import PublicError, PublicRuntimeError


class SessionCommitConflict(PublicRuntimeError):
    def __init__(self):
        super().__init__(PublicError("session_commit_conflict",
            "The frozen result conflicts with the current Session snapshot.", False))


def session_fingerprint(payload: dict | None) -> str:
    if payload is None:
        return "missing"
    normalized = json_sessions._canonical_session_payload(payload)
    normalized.pop("timestamp", None)
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256()
    for part in encoder.iterencode(normalized):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def terminal_commit_marker(interaction_run_id: str) -> str:
    identity = hashlib.sha256(interaction_run_id.encode("utf-8")).hexdigest()
    return f"terminal:{identity}:{uuid.uuid4().hex}"


def confirms_terminal_commit(payload: dict, workspace: Path, session_id: str, interaction_run_id: str) -> bool:
    """显示层只采信本次交互的提交，不能认领继承自旧运行的marker。"""
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return False
    commits = metadata.get("_nz_checkpoint_commits")
    if not isinstance(commits, list):
        return False
    try:
        terminal = SessionStatus(payload.get("run_status")).terminal
    except ValueError:
        return False
    if (not terminal or payload.get("session_id") != session_id
            or payload.get("workspace") != str(Path(workspace).resolve())
            or metadata.get("active_interaction_run_id") != interaction_run_id):
        return False
    identity = hashlib.sha256(interaction_run_id.encode("utf-8")).hexdigest()
    for marker in commits:
        fields = marker.split(":") if isinstance(marker, str) else []
        if (len(fields) == 3 and fields[0] == "terminal" and fields[1] == identity
                and len(fields[2]) == 32 and all(c in "0123456789abcdef" for c in fields[2])):
            return True
    return False


class SessionHistoryUnconfirmed(PublicRuntimeError):
    def __init__(self, transition):
        self.transition = transition
        super().__init__(PublicError("session_commit_pending",
            f"History transition for {transition.candidate.session_id} is waiting for Session storage confirmation.", True))


# 仅保存本进程尚未确认的操作凭据；未知写盘结果绝不能被下一次运行覆盖。
_pending_history: dict[object, "HistoryTransition"] = {}


@dataclass
class HistoryTransition:
    key: object
    baseline_digest: str
    project: Callable[[Session], dict]
    read: Callable[[], dict | None]
    save: Callable[[Session], None]
    original_history: list[dict]
    candidate: Session | None = None
    resolution: str | None = None
    candidate_digest: str = ""
    rollback: Callable[[], None] | None = None

    def commit(self, candidate: Session) -> None:
        self.candidate = _clone_session(candidate)
        self.candidate_digest = session_fingerprint(self.project(candidate))
        try:
            self.save(candidate)
        except BaseException:
            try:
                outcome = self.outcome()
            except BaseException as confirmation_error:
                _pending_history[self.key] = self
                raise SessionHistoryUnconfirmed(self) from confirmation_error
            if outcome == "baseline":
                raise
        candidate.mark_persisted()

    def outcome(self) -> str:
        digest = session_fingerprint(self.read())
        if digest == self.candidate_digest:
            return "candidate"
        if digest == self.baseline_digest:
            return "baseline"
        raise SessionCommitConflict()


def _resolve_history_transition(key: object) -> None:
    pending = _pending_history.get(key)
    if pending is None:
        return
    try:
        outcome = pending.outcome()
        if outcome == "baseline" and pending.rollback is not None:
            pending.rollback()
        pending.resolution = outcome
    except BaseException as error:
        raise SessionHistoryUnconfirmed(pending) from error
    del _pending_history[key]


def require_history_confirmed(store, identity: SessionIdentity, workspace: Path) -> None:
    pending = _pending_history.get(store.ownership_key(identity, workspace))
    if pending is not None:
        raise SessionHistoryUnconfirmed(pending)


def recover_history_transition(store, identity: SessionIdentity, workspace: Path) -> HistoryTransition | None:
    """构造下一次请求前先确认上次历史操作，避免复制尚未刷新的UI历史。"""
    pending = _pending_history.get(store.ownership_key(identity, workspace))
    if pending is not None:
        lease = store.acquire(identity, workspace)
        lease.close_hold()
    return pending


def adopt_history_transition(history: list[dict], original: list[dict], candidate: list[dict]) -> None:
    if history[:len(original)] != original:
        raise SessionCommitConflict()
    history[:] = copy.deepcopy(candidate) + history[len(original):]


@dataclass(frozen=True)
class FrozenSessionWrite:
    """只持基线摘要与原存储端口，不另存一份历史。"""
    baseline_digest: str
    project: Callable[[Session], dict]
    read: Callable[[], Awaitable[dict | None]]
    save: Callable[[Session], Awaitable[None]]


@runtime_checkable
class SessionStore(Protocol):
    """Durable storage boundary for complete Session snapshots."""

    def ownership_key(self, identity: SessionIdentity, workspace: Path) -> object:
        """Identify the data being protected, independently of adapter instance."""
        ...

    def acquire(self, identity: SessionIdentity, workspace: Path, *, outer_hold: bool = False) -> SessionRunLease:
        """Acquire nonblocking single-run ownership before reading history."""
        ...

    async def load(
        self,
        identity: SessionIdentity,
        workspace: Path,
    ) -> Session | None:
        """Load one Session by exact identity or return None."""
        raise NotImplementedError

    async def save(self, session: Session) -> None:
        """Atomically persist the current Session snapshot."""
        raise NotImplementedError

    def load_session(self, identity: SessionIdentity, workspace: Path) -> Session | None:
        ...

    def prepare_history_transition(self, session: Session) -> HistoryTransition:
        ...

    async def prepare_terminal(self, session: Session) -> FrozenSessionWrite:
        """Capture canonical baseline and stable persistence options."""
        raise NotImplementedError


class LegacyJsonSessionStore:
    """Adapt Session objects to NZ-Coder's existing atomic JSON persistence."""

    def ownership_key(self, identity: SessionIdentity, workspace: Path) -> object:
        with scoped_workdir(workspace), scoped_session(identity.session_id):
            return str(_session_path(identity.session_id).absolute())

    def acquire(self, identity: SessionIdentity, workspace: Path, *, outer_hold: bool = False) -> SessionRunLease:
        key = self.ownership_key(identity, workspace)
        manager = exclusive_file_lock(Path(key).with_suffix(".run.lock"), blocking=False)
        try:
            manager.__enter__()
        except FileLockBusy as exc:
            raise session_busy() from exc
        try:
            _resolve_history_transition(key)
        except BaseException:
            manager.__exit__(None, None, None)
            raise
        return SessionRunLease(key, lambda: manager.__exit__(None, None, None), outer_hold=outer_hold)

    async def load(
        self,
        identity: SessionIdentity,
        workspace: Path,
    ) -> Session | None:
        """Load and normalize one exact JSON Session without leaking scopes."""
        return await asyncio.to_thread(self.load_session, identity, workspace)

    def load_session(self, identity: SessionIdentity, workspace: Path) -> Session | None:
        """同步消费者复用规范 Session 解析，不在已有事件循环中启动新循环。"""
        payload = self._load_sync(identity, workspace)
        if payload is None:
            return None
        raw_messages = payload.get("messages")
        candidates = raw_messages if isinstance(raw_messages, list) else []
        if any(isinstance(message, dict) and not _valid_persisted_parts(message) for message in candidates):
            with scoped_workdir(Path(workspace)):
                path = _session_path(identity.session_id)
            raise SessionReadError("corrupt", path, detail="invalid persisted message parts")
        messages = [message for message in candidates if _valid_message(message)]
        invalid_messages = (
            len(candidates) - len(messages)
            if isinstance(raw_messages, list)
            else int(raw_messages is not None)
        )
        parent_session_id = identity.parent_session_id
        invalid_parent = False
        raw_parent = payload.get("parent_session_id")
        if raw_parent is not None and raw_parent != "":
            try:
                candidate_identity = SessionIdentity(
                    identity.session_id,
                    raw_parent,
                )
            except (TypeError, ValueError):
                invalid_parent = True
            else:
                parent_session_id = candidate_identity.parent_session_id
        metadata = (
            dict(payload.get("metadata"))
            if isinstance(payload.get("metadata"), dict)
            else {}
        )
        saved_snapshot = metadata.pop("_nz_session_snapshot", None)
        usage = TokenUsage()
        closed = False
        if saved_snapshot is not None:
            if not isinstance(saved_snapshot, dict) or not isinstance(saved_snapshot.get("closed"), bool):
                raise ValueError("Invalid persisted Session snapshot")
            usage = TokenUsage(**saved_snapshot["usage"])
            closed = saved_snapshot["closed"]
        recovery = {}
        if invalid_messages:
            recovery["invalid_messages_dropped"] = invalid_messages
        if invalid_parent:
            recovery["invalid_parent_session_id_ignored"] = True
        if recovery:
            metadata["session_recovery"] = recovery
        mode = payload.get("mode")
        if isinstance(mode, str) and mode:
            metadata.setdefault("permission_mode", mode)
        title = payload.get("title")
        if isinstance(title, str) and title:
            metadata.setdefault("title", title)
        metadata["legacy_payload"] = {
            key: value for key, value in payload.items() if key != "messages"
        }
        if isinstance(metadata["legacy_payload"].get("metadata"), dict):
            metadata["legacy_payload"]["metadata"] = dict(metadata["legacy_payload"]["metadata"])
            metadata["legacy_payload"]["metadata"].pop("_nz_session_snapshot", None)
        session = Session(
            identity=SessionIdentity(identity.session_id, parent_session_id),
            workspace=Path(workspace),
            transcript=messages,
            status=_session_status(payload.get("run_status")),
            metadata=metadata,
            usage=usage,
            closed=closed,
        )
        session.mark_persisted()
        return session

    async def save(self, session: Session) -> None:
        """Persist through the existing writer and clear the dirty marker."""
        if not isinstance(session, Session):
            raise TypeError("LegacyJsonSessionStore.save requires Session")
        # 已开始的写入必须结清，随后执行的取消终态才不会被旧快照覆盖。
        await to_thread_settled(self._save_sync, session)
        session.mark_persisted()

    def save_session(self, session: Session) -> None:
        """Persist a prepared fork through the same atomic JSON writer."""
        if not isinstance(session, Session):
            raise TypeError("LegacyJsonSessionStore.save_session requires Session")
        self._save_sync(session)
        session.mark_persisted()

    async def prepare_terminal(self, session: Session) -> FrozenSessionWrite:
        return await to_thread_settled(self._prepare_terminal_sync, session)

    def prepare_history_transition(self, session: Session) -> HistoryTransition:
        baseline, project, read, save = self._prepare_write_sync(session)
        return HistoryTransition(self.ownership_key(session.identity, session.workspace),
                                 session_fingerprint(baseline), project, read, save,
                                 copy.deepcopy(session.transcript))

    def _prepare_terminal_sync(self, session: Session) -> FrozenSessionWrite:
        baseline, projection, read_sync, write = self._prepare_write_sync(session)
        async def read():
            return await to_thread_settled(read_sync)
        async def save(current):
            await to_thread_settled(write, current)
            current.mark_persisted()
        return FrozenSessionWrite(session_fingerprint(baseline), projection, read, save)

    def _prepare_write_sync(self, session: Session):
        identity, workspace = session.identity, session.workspace
        with scoped_workdir(workspace), scoped_session(identity.session_id):
            path = _session_path(identity.session_id)
            baseline = _read_saved_session(path, expected_id=identity.session_id)
            initial, _ = json_sessions._session_write_payload(**_session_arguments(session), existing=baseline)
        # 配置恢复或切换后，原候选仍使用第一次准备时选定的显示和运行参数。
        defaults = {name: initial.get(name) for name in ("model", "mode", "title")}

        def projection(current):
            if current.identity != identity or current.workspace != workspace:
                raise SessionCommitConflict()
            with scoped_workdir(workspace), scoped_session(identity.session_id):
                if _session_path(identity.session_id) != path:
                    raise SessionCommitConflict()
                args = _session_arguments(current)
                args.update(defaults)
                return json_sessions._session_write_payload(**args)[0]

        def read_sync():
            with scoped_workdir(workspace), scoped_session(identity.session_id):
                if _session_path(identity.session_id) != path:
                    raise SessionCommitConflict()
                return json_sessions._read_session_payload(path)

        def write(current):
            with scoped_workdir(workspace), scoped_session(identity.session_id):
                payload = projection(current)
                json_sessions._save_session_payload(path, payload,
                    json_sessions.session_diffs(current.transcript), strict_diffs=True)

        return baseline, projection, read_sync, write

    @staticmethod
    def _load_sync(identity: SessionIdentity, workspace: Path) -> dict | None:
        with scoped_workdir(Path(workspace)), scoped_session(identity.session_id):
            return _read_saved_session(_session_path(identity.session_id), expected_id=identity.session_id)

    @staticmethod
    def _save_sync(session: Session) -> None:
        with scoped_workdir(session.workspace), scoped_session(session.session_id):
            save_session(**_session_arguments(session), activate=False, require_aliases=False)



class EphemeralSessionStore:
    """Process-local SessionStore used by explicit ``--no-session`` runs."""

    def __init__(self) -> None:
        self._sessions: dict[SessionIdentity, Session] = {}
        self._owners: set[object] = set()
        self._owner_lock = threading.RLock()

    def ownership_key(self, identity: SessionIdentity, workspace: Path) -> object:
        return id(self), str(Path(workspace).resolve()), identity.session_id

    def acquire(self, identity: SessionIdentity, workspace: Path, *, outer_hold: bool = False) -> SessionRunLease:
        key = self.ownership_key(identity, workspace)
        with self._owner_lock:
            if key in self._owners:
                raise session_busy()
            self._owners.add(key)
        try:
            _resolve_history_transition(key)
        except BaseException:
            with self._owner_lock:
                self._owners.remove(key)
            raise
        def close():
            with self._owner_lock:
                self._owners.remove(key)
        return SessionRunLease(key, close, outer_hold=outer_hold)

    async def load(
        self,
        identity: SessionIdentity,
        workspace: Path,
    ) -> Session | None:
        return self.load_session(identity, workspace)

    def load_session(self, identity: SessionIdentity, workspace: Path) -> Session | None:
        session = self._sessions.get(identity)
        if session is None or session.workspace != Path(workspace).resolve():
            return None
        return _clone_session(session)

    async def save(self, session: Session) -> None:
        self.save_session(session)

    def save_session(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("EphemeralSessionStore.save requires Session")
        self._sessions[session.identity] = _clone_session(session)
        session.mark_persisted()

    def prepare_history_transition(self, session: Session) -> HistoryTransition:
        identity, workspace = session.identity, session.workspace
        def read():
            current = self.load_session(identity, workspace)
            return self._projection(current) if current is not None else None
        return HistoryTransition(self.ownership_key(identity, workspace),
                                 session_fingerprint(read()), self._projection, read, self.save_session,
                                 copy.deepcopy(session.transcript))

    @staticmethod
    def _projection(current):
        snapshot = current.snapshot()
        return {"session_id": snapshot.identity.session_id,
                "parent_session_id": snapshot.identity.parent_session_id,
                "workspace": str(snapshot.workspace),
                "messages": list(snapshot.transcript), "run_status": snapshot.status.value,
                "metadata": snapshot.metadata, "usage": asdict(snapshot.usage), "closed": snapshot.closed}

    async def prepare_terminal(self, session: Session) -> FrozenSessionWrite:
        transition = self.prepare_history_transition(session)
        async def read():
            return transition.read()
        return FrozenSessionWrite(transition.baseline_digest, transition.project, read, self.save)


def _session_arguments(session):
    snapshot = session.snapshot()
    metadata = dict(snapshot.metadata)
    metadata.pop("legacy_payload", None)
    metadata["_nz_session_snapshot"] = {"usage": asdict(snapshot.usage), "closed": snapshot.closed}
    return {"messages": list(snapshot.transcript),
            "mode": metadata.pop("permission_mode", None),
            "title": metadata.pop("title", None),
            "session_id": snapshot.identity.session_id,
            "run_status": snapshot.status.value,
            "parent_session_id": snapshot.identity.parent_session_id,
            "session_metadata": metadata}


def _clone_session(session: Session) -> Session:
    snapshot = session.snapshot()
    clone = Session(
        identity=snapshot.identity,
        workspace=snapshot.workspace,
        transcript=copy.deepcopy(list(snapshot.transcript)),
        status=snapshot.status,
        metadata=copy.deepcopy(snapshot.metadata),
        usage=snapshot.usage,
        closed=snapshot.closed,
    )
    clone.mark_persisted()
    return clone


def _session_status(value: object) -> SessionStatus:
    try:
        return SessionStatus(str(value))
    except ValueError:
        return SessionStatus.IDLE


def _valid_message(value: object) -> bool:
    """Return whether a legacy transcript entry satisfies Session's minimum."""
    return bool(
        isinstance(value, dict)
        and isinstance(value.get("role"), str)
        and "content" in value
    )
