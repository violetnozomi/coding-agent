"""Storage-neutral SessionStore and current JSON-format production adapter."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from typing import Protocol, runtime_checkable

from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.state.sessions import load_session, save_session, scoped_session
from nz_coder.state.workdir import scoped_workdir


@runtime_checkable
class SessionStore(Protocol):
    """Durable storage boundary for complete Session snapshots."""

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


class LegacyJsonSessionStore:
    """Adapt Session objects to NZ-Coder's existing atomic JSON persistence."""

    async def load(
        self,
        identity: SessionIdentity,
        workspace: Path,
    ) -> Session | None:
        """Load and normalize one exact JSON Session without leaking scopes."""
        payload = await asyncio.to_thread(self._load_sync, identity, workspace)
        if not payload:
            return None
        raw_messages = payload.get("messages")
        candidates = raw_messages if isinstance(raw_messages, list) else []
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
        session = Session(
            identity=SessionIdentity(identity.session_id, parent_session_id),
            workspace=Path(workspace),
            transcript=messages,
            status=_session_status(payload.get("run_status")),
            metadata=metadata,
        )
        session.mark_persisted()
        return session

    async def save(self, session: Session) -> None:
        """Persist through the existing writer and clear the dirty marker."""
        if not isinstance(session, Session):
            raise TypeError("LegacyJsonSessionStore.save requires Session")
        await asyncio.to_thread(self._save_sync, session)
        session.mark_persisted()

    @staticmethod
    def _load_sync(identity: SessionIdentity, workspace: Path) -> dict:
        with scoped_workdir(Path(workspace)), scoped_session(identity.session_id):
            return load_session(identity.session_id)

    @staticmethod
    def _save_sync(session: Session) -> None:
        snapshot = session.snapshot()
        metadata = dict(snapshot.metadata)
        metadata.pop("legacy_payload", None)
        mode = metadata.pop("permission_mode", None)
        title = metadata.pop("title", None)
        with (
            scoped_workdir(snapshot.workspace),
            scoped_session(snapshot.identity.session_id),
        ):
            save_session(
                list(snapshot.transcript),
                mode=str(mode) if mode else None,
                session_id=snapshot.identity.session_id,
                activate=False,
                run_status=snapshot.status.value,
                require_aliases=False,
                title=str(title) if title else None,
                parent_session_id=snapshot.identity.parent_session_id,
                session_metadata=metadata,
            )


class EphemeralSessionStore:
    """Process-local SessionStore used by explicit ``--no-session`` runs."""

    def __init__(self) -> None:
        self._sessions: dict[SessionIdentity, Session] = {}

    async def load(
        self,
        identity: SessionIdentity,
        workspace: Path,
    ) -> Session | None:
        session = self._sessions.get(identity)
        if session is None or session.workspace != Path(workspace).resolve():
            return None
        return _clone_session(session)

    async def save(self, session: Session) -> None:
        if not isinstance(session, Session):
            raise TypeError("EphemeralSessionStore.save requires Session")
        self._sessions[session.identity] = _clone_session(session)
        session.mark_persisted()


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
