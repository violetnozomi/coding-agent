"""Session-centric open, checkpoint, and finalization orchestration."""
from __future__ import annotations

import copy
import asyncio
import concurrent.futures
from dataclasses import dataclass
import uuid

from nz_coder.protocol.message_schema import (
    INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY,
    attach_message_identity,
    cleanup_incomplete_tool_history,
    ensure_message_identities,
    is_user_request_message,
)
from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.result import RunStatus, TokenUsage
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.runtime.session.ownership import SessionRunLease
from nz_coder.runtime.session.store import SessionStore, FrozenSessionWrite, SessionCommitConflict, session_fingerprint, terminal_commit_marker


@dataclass(frozen=True)
class _TerminalCommit:
    write: FrozenSessionWrite
    marker: str
    candidate_digest: str
    previous_status: SessionStatus
    previous_usage: TokenUsage
    run_usage: TokenUsage


async def _await_settlement(future) -> None:
    wrapped = asyncio.wrap_future(future)
    try:
        await asyncio.shield(wrapped)
    except asyncio.CancelledError:
        # 等待者取消不撤销原提交；已进入的I/O仍须结清。
        while not wrapped.done():
            try:
                await asyncio.shield(wrapped)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        if wrapped.done() and not wrapped.cancelled():
            wrapped.exception()
        raise


class SessionRuntime:
    """Create one RunContext around a durable Session and persist its boundaries."""

    def __init__(self, store: SessionStore) -> None:
        if not isinstance(store, SessionStore):
            raise TypeError("SessionRuntime store must implement SessionStore")
        self.store = store

    async def replace_transcript(self, identity, workspace, history, candidate, *, session_lease):
        """显式历史转换不创建运行，也不放宽运行输入的前缀校验。"""
        from nz_coder.foundation.async_utils import to_thread_settled
        def commit():
            key = self.store.ownership_key(identity, workspace)
            if not session_lease.active or session_lease.key != key:
                raise RuntimeError("Session has no active history owner")
            session = self.store.load_session(identity, workspace)
            if session is None:
                session = Session.create(identity.session_id, history, workspace=workspace,
                                         parent_session_id=identity.parent_session_id)
            elif session.transcript != history:
                raise ValueError("Session transcript conflicts with history transition")
            write = self.store.prepare_history_transition(session)
            session.replace_transcript(candidate, allow_terminal=True)
            from nz_coder.protocol.message_schema import ensure_message_identities
            ensure_message_identities(session.transcript, identity.session_id)
            write.commit(session)
            return copy.deepcopy(session.transcript)
        # 候选一旦进入提交边界，取消只能等待原提交结清，不能丢掉已提交的UI替换。
        operation = asyncio.create_task(to_thread_settled(commit))
        while True:
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                if operation.done():
                    return operation.result()

    async def open(self, request: RunRequest, *, session_lease: SessionRunLease | None = None) -> RunContext:
        """Load or create a Session, begin a new run, and reconcile request input."""
        if not isinstance(request, RunRequest):
            raise TypeError("SessionRuntime.open requires RunRequest")
        parent_session_id = _parent_session_id(request.metadata)
        identity = SessionIdentity(request.session_id, parent_session_id)
        # 同步非阻塞获取到进入清理作用域之间没有可取消的 await 空窗。
        key = self.store.ownership_key(identity, request.workspace)
        lease = session_lease if session_lease is not None else self.store.acquire(identity, request.workspace)
        lease.claim(key)
        try:
            session = await self.store.load(identity, request.workspace)
            open_state = "resumed" if session is not None else "created"
            if session is None:
                messages = [_new_input(message, request.session_id) for message in request.messages]
                session = Session.create(
                    request.session_id,
                    messages,
                    workspace=request.workspace,
                    parent_session_id=parent_session_id,
                    metadata=_session_metadata(request),
                )
            else:
                _merge_session_metadata(session, request)
                _reconcile_transcript(session, list(request.messages))
            session.metadata.pop("verification", None)
            session.metadata.pop("_nz_terminal_result", None)
            session.metadata.pop("_nz_http_terminal_result", None)
            # 这两项属于本次激活；早期异常也不能把上一轮原稿作为当前审计结果落盘。
            session.metadata.pop("authored_content", None)
            session.begin_run()
            metadata = copy.deepcopy(request.metadata)
            metadata.pop("authored_content", None)
            interaction_run_id = (
                request.interaction_run_id
                or _interaction_id(metadata.get("interaction_run_id"))
                or f"interaction-{uuid.uuid4().hex}"
            )
            metadata["interaction_run_id"] = interaction_run_id
            metadata["session_open"] = open_state
            _migrate_active_interaction_identity(session, interaction_run_id)
            context = RunContext(
                request=request,
                session=session,
                active_agent=request.agent.name,
                interaction_run_id=interaction_run_id,
                metadata=metadata,
            )
            context._session_lease = lease
            return context
        except BaseException:
            lease.finish()
            raise

    def abandon(self, context: RunContext) -> None:
        """仅在准备 writer 已退休后调用；不持久化失败的新历史。"""
        self._validate_context(context)
        if context.terminal_status is not None:
            raise RuntimeError("Cannot abandon a frozen Session result")
        lease = context._session_lease
        if lease is not None:
            lease.finish()


    async def checkpoint(
        self,
        context: RunContext,
        status: SessionStatus | str = SessionStatus.RUNNING,
    ) -> None:
        """Persist one settled non-terminal state from the live RunContext."""
        self._validate_context(context)
        if context.finalized or context.terminal_status is not None:
            return
        normalized = _session_status(status)
        if normalized.terminal:
            self._clean_tool_history(context)
        marker = context.metadata.get("_nz_active_checkpoint_marker")
        if isinstance(marker, str) and marker:
            commits = context.session.metadata.setdefault(
                "_nz_checkpoint_commits", []
            )
            if not isinstance(commits, list):
                commits = []
                context.session.metadata["_nz_checkpoint_commits"] = commits
            if marker not in commits:
                commits.append(marker)
                del commits[:-32]
        self._require_owner(context)
        context.session.record_status(normalized)
        await self.store.save(context.session)

    async def checkpoint_committed(
        self,
        context: RunContext,
        marker: str,
    ) -> bool:
        """Reconstruct whether a checkpoint survived an after-write failure."""
        self._validate_context(context)
        if not isinstance(marker, str) or not marker:
            return False
        durable = await self.store.load(
            context.session.identity,
            context.session.workspace,
        )
        if durable is None:
            return False
        commits = durable.metadata.get("_nz_checkpoint_commits")
        return isinstance(commits, list) and marker in commits

    async def finalize(self, context: RunContext, status: RunStatus) -> None:
        """同一原owner的提交串行，锁仅登记future，不包住磁盘或await。"""
        self._validate_context(context)
        with context._state_lock:
            if context.finalized:
                raise RuntimeError("RunContext is already terminal")
            if context.terminal_status is not None and context.terminal_status != status:
                raise RuntimeError("Cannot change frozen run status")
            future = context._settlement_future
            follower = future is not None
            if not follower:
                future = concurrent.futures.Future()
                context._settlement_future = future
        if follower:
            await _await_settlement(future)
            return
        try:
            context.freeze_execution(status)
            if context._retain_terminal_commit is not None:
                context._retain_terminal_commit()
            await self._commit_terminal(context, status)
        except BaseException as error:
            future.set_exception(error)
            raise
        else:
            if context._session_lease is not None:
                context._session_lease.finish()
            future.set_result(None)
        finally:
            with context._state_lock:
                if context._settlement_future is future:
                    context._settlement_future = None

    async def _commit_terminal(self, context, status):
        self._require_owner(context)
        context.freeze_execution(status)
        pending = context._terminal_commit
        if pending is None:
            if context.verification is not None:
                verification = context.verification.snapshot().to_dict()
                context.metadata["verification"] = verification
                context.session.metadata["verification"] = copy.deepcopy(verification)
            self._clean_tool_history(context)
            write = await self.store.prepare_terminal(context.session)
            marker = terminal_commit_marker(context.interaction_run_id)
            commits = context.session.metadata.setdefault("_nz_checkpoint_commits", [])
            if not isinstance(commits, list):
                raise ValueError("Invalid checkpoint commit markers")
            commits.append(marker)
            del commits[:-32]
            previous_status, previous_usage = context.session.status, context.session.usage
            try:
                context.session.usage = previous_usage.add(context.usage)
                context.session.finish(SessionStatus(status.value))
                digest = session_fingerprint(write.project(context.session))
            finally:
                context.session.status, context.session.usage = previous_status, previous_usage
            pending = _TerminalCommit(write, marker, digest, previous_status, previous_usage, context.usage)
            context._terminal_commit = pending
        try:
            if context.usage != pending.run_usage:
                raise SessionCommitConflict()
            context.session.usage = pending.previous_usage.add(pending.run_usage)
            context.session.finish(SessionStatus(status.value))
            if session_fingerprint(pending.write.project(context.session)) != pending.candidate_digest:
                raise SessionCommitConflict()
            durable = await pending.write.read()
            digest = session_fingerprint(durable)
            commits = (durable or {}).get("metadata", {}).get("_nz_checkpoint_commits", [])
            if digest == pending.candidate_digest and pending.marker in commits:
                context.session.mark_persisted()
            elif digest == pending.write.baseline_digest:
                await pending.write.save(context.session)
            else:
                raise SessionCommitConflict()
        except BaseException:
            context.session.status = pending.previous_status
            context.session.usage = pending.previous_usage
            context.session.mark_dirty()
            raise
        context.finish(status)
        context._terminal_commit = None

    def _require_owner(self, context: RunContext) -> None:
        lease = context._session_lease
        key = self.store.ownership_key(context.session.identity, context.session.workspace)
        if lease is None or not lease.active or lease.key != key:
            raise RuntimeError("Session has no active write owner")

    @staticmethod
    def _clean_tool_history(context: RunContext) -> None:
        """Repair interrupted Provider protocol envelopes before persistence."""
        cleaned = cleanup_incomplete_tool_history(context.transcript)
        if cleaned != context.transcript:
            context.session.replace_transcript(cleaned)

    @staticmethod
    def _validate_context(context: RunContext) -> None:
        if not isinstance(context, RunContext):
            raise TypeError("SessionRuntime requires RunContext")


def _parent_session_id(metadata: dict) -> str | None:
    value = metadata.get("parent_session_id")
    return value if isinstance(value, str) and value else None


def _interaction_id(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _migrate_active_interaction_identity(
    session: Session,
    interaction_run_id: str,
) -> None:
    """Upgrade one resumed legacy activation to a durable live identity."""
    ensure_message_identities(session.transcript, session.session_id)
    start = next(
        (
            index
            for index in range(len(session.transcript) - 1, -1, -1)
            if isinstance(session.transcript[index], dict)
            and session.transcript[index].get("role") == "user"
            and not session.transcript[index].get("_nz_synthetic")
        ),
        max(0, len(session.transcript) - 1),
    )
    for message in session.transcript[start:]:
        if not isinstance(message, dict):
            continue
        message[INTERACTION_RUN_ID_KEY] = interaction_run_id
        for part in message.get("_nz_parts", []) or []:
            if not isinstance(part, dict):
                continue
            part["interaction_run_id"] = interaction_run_id
    session.metadata["active_interaction_run_id"] = interaction_run_id
    session.mark_dirty()


def _session_metadata(request: RunRequest) -> dict:
    return {
        key: copy.deepcopy(value)
        for key, value in request.metadata.items()
        if key not in {"parent_session_id", "_nz_terminal_result", "_nz_http_terminal_result"}
    }


def _merge_session_metadata(session: Session, request: RunRequest) -> None:
    prior_scope = session.metadata.get("memory_scope")
    incoming_scope = request.metadata.get("memory_scope")
    if prior_scope is not None and incoming_scope is not None and prior_scope != incoming_scope:
        raise ValueError("Restored session memory scope differs from its trusted workspace scope")
    for key, value in _session_metadata(request).items():
        session.metadata[key] = value


def preview_activation_transcript(session: Session | None, requested: list[dict]) -> list[dict]:
    """只在副本上复用真实接续算法，不开启或持久化第二次运行。"""
    if session is None:
        return copy.deepcopy(requested)
    preview = copy.deepcopy(session)
    _reconcile_transcript(preview, copy.deepcopy(requested))
    return preview.transcript


def _reconcile_transcript(session: Session, requested: list[dict]) -> None:
    durable = session.transcript
    if requested and len(requested) <= len(durable):
        if durable[-len(requested):] == requested:
            return
    common = 0
    for stored, incoming in zip(durable, requested):
        if stored != incoming:
            break
        common += 1
    if common == len(requested):
        return
    durable_ids = {m.get(MESSAGE_ID_KEY) for m in durable if m.get(MESSAGE_ID_KEY)}
    if any(m.get(MESSAGE_ID_KEY) in durable_ids for m in requested[common:] if m.get(MESSAGE_ID_KEY)):
        raise ValueError("RunRequest transcript conflicts with the durable Session history")
    if common == len(durable):
        for message in requested[common:]:
            session.append(_new_input(message, session.session_id))
        return
    if _is_new_activation(requested, durable):
        for message in requested:
            session.append(_new_input(message, session.session_id))
        return
    if requested:
        raise ValueError(
            "RunRequest transcript conflicts with the durable Session history"
        )


def _new_input(message: dict, session_id: str) -> dict:
    message = copy.deepcopy(message)
    if is_user_request_message(message) and not message.get(MESSAGE_ID_KEY):
        # 新提交只赋号一次；历史补号不能区分两次内容相同的提交。
        attach_message_identity(message, session_id=session_id)
    return message


def _is_new_activation(messages: list[dict], durable: list[dict]) -> bool:
    if len(messages) != 1 or not is_user_request_message(messages[0]):
        return False
    message = messages[0]
    message_id = message.get(MESSAGE_ID_KEY)
    if message_id and any(item.get(MESSAGE_ID_KEY) == message_id for item in durable):
        return False
    for part in message.get("_nz_parts", []):
        if not isinstance(part, dict) or part.get("message_id") != message_id:
            return False
        if "source_message_id" in part and part["source_message_id"] != message_id:
            return False
    return True


def _session_status(value: SessionStatus | str) -> SessionStatus:
    try:
        status = value if isinstance(value, SessionStatus) else SessionStatus(str(value))
    except ValueError as error:
        raise ValueError(f"Unknown Session checkpoint status: {value}") from error
    return status
