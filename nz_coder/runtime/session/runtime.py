"""Session-centric open, checkpoint, and finalization orchestration."""
from __future__ import annotations

import copy
import uuid

from nz_coder.protocol.message_schema import (
    INTERACTION_RUN_ID_KEY,
    MESSAGE_ID_KEY,
    cleanup_incomplete_tool_history,
    ensure_message_identities,
)
from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.runtime.session.store import SessionStore


class SessionRuntime:
    """Create one RunContext around a durable Session and persist its boundaries."""

    def __init__(self, store: SessionStore) -> None:
        if not isinstance(store, SessionStore):
            raise TypeError("SessionRuntime store must implement SessionStore")
        self.store = store

    async def open(self, request: RunRequest) -> RunContext:
        """Load or create a Session, begin a new run, and reconcile request input."""
        if not isinstance(request, RunRequest):
            raise TypeError("SessionRuntime.open requires RunRequest")
        parent_session_id = _parent_session_id(request.metadata)
        identity = SessionIdentity(request.session_id, parent_session_id)
        session = await self.store.load(identity, request.workspace)
        open_state = "resumed" if session is not None else "created"
        if session is None:
            session = Session.create(
                request.session_id,
                request.messages,
                workspace=request.workspace,
                parent_session_id=parent_session_id,
                metadata=_session_metadata(request),
            )
        else:
            _merge_session_metadata(session, request)
            _reconcile_transcript(session, list(request.messages))
        session.begin_run()
        metadata = copy.deepcopy(request.metadata)
        interaction_run_id = (
            request.interaction_run_id
            or _interaction_id(metadata.get("interaction_run_id"))
            or f"interaction-{uuid.uuid4().hex}"
        )
        metadata["interaction_run_id"] = interaction_run_id
        metadata["session_open"] = open_state
        _migrate_active_interaction_identity(session, interaction_run_id)
        return RunContext(
            request=request,
            session=session,
            active_agent=request.agent.name,
            interaction_run_id=interaction_run_id,
            metadata=metadata,
        )

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
        context.session.record_status(normalized)
        await self.store.save(context.session)

    async def finalize(self, context: RunContext, status: RunStatus) -> None:
        """Persist one terminal run state exactly once."""
        self._validate_context(context)
        if context.finalized or context.terminal_status is not None:
            raise RuntimeError("RunContext is already terminal")
        self._clean_tool_history(context)
        previous_status = context.session.status
        previous_usage = context.session.usage
        context.session.usage = previous_usage.add(context.usage)
        context.session.finish(SessionStatus(status.value))
        try:
            await self.store.save(context.session)
        except BaseException:
            # The durable boundary did not commit. Restore the in-memory
            # terminal/usage fields so the caller may retry without double
            # charging this run. Tool-history cleanup remains intentionally
            # applied because it is a validity repair, not terminal state.
            context.session.status = previous_status
            context.session.usage = previous_usage
            context.session.mark_dirty()
            raise
        context.finish(status)

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
        if key != "parent_session_id"
    }


def _merge_session_metadata(session: Session, request: RunRequest) -> None:
    for key, value in _session_metadata(request).items():
        session.metadata[key] = value


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
    if common == len(durable):
        for message in requested[common:]:
            session.append(message)
        return
    if _is_new_activation(requested):
        for message in requested:
            session.append(message)
        return
    if requested:
        raise ValueError(
            "RunRequest transcript conflicts with the durable Session history"
        )


def _is_new_activation(messages: list[dict]) -> bool:
    return bool(
        messages
        and messages[0].get("role") == "user"
        and all(not message.get(MESSAGE_ID_KEY) for message in messages)
    )


def _session_status(value: SessionStatus | str) -> SessionStatus:
    try:
        status = value if isinstance(value, SessionStatus) else SessionStatus(str(value))
    except ValueError as error:
        raise ValueError(f"Unknown Session checkpoint status: {value}") from error
    return status
