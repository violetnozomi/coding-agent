"""Non-production compatibility facade over the Session-owned JSON adapter."""
from __future__ import annotations

import copy

from nz_coder.runtime.core.request import RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.core.state import RunState
from nz_coder.protocol.message_schema import ensure_message_identities
from nz_coder.runtime.session.model import Session, SessionIdentity, SessionStatus
from nz_coder.runtime.session.store import LegacyJsonSessionStore, SessionStore
from nz_coder.state.sessions import save_session


class FileSessionRepository:
    """Retain the old public RunState API outside the production service graph."""

    def __init__(self, store: SessionStore | None = None) -> None:
        self.store = store or LegacyJsonSessionStore()

    async def load(self, request: RunRequest, state: RunState) -> None:
        """Hydrate a matching session without leaking workspace ContextVars."""
        session = await self.store.load(
            SessionIdentity(
                request.session_id,
                _parent_session_id(request.metadata),
            ),
            request.workspace,
        )
        if session is None:
            return
        state.transcript[:] = copy.deepcopy(session.transcript)
        payload = session.metadata.get("legacy_payload")
        if isinstance(payload, dict):
            state.metadata["session_payload"] = copy.deepcopy(payload)

    async def save(self, request: RunRequest, state: RunState) -> None:
        """Atomically persist one settled Runner snapshot by exact session ID."""
        session = Session.create(
            request.session_id,
            state.transcript,
            workspace=request.workspace,
            parent_session_id=_parent_session_id(request.metadata),
            metadata={
                key: copy.deepcopy(value)
                for key, value in request.metadata.items()
                if key != "parent_session_id"
            },
        )
        session.status = _state_status(state.status)
        await self.store.save(session)

    def checkpoint(self, host, messages: list[dict], run_status: str) -> None:
        """Persist one production-host step boundary in the existing format."""
        ensure_message_identities(messages, host.session_id)
        save_session(
            messages,
            mode=host.permissions.mode,
            session_id=host.session_id,
            activate=False,
            run_status=run_status,
            require_aliases=False,
        )



def _parent_session_id(metadata: dict) -> str | None:
    value = metadata.get("parent_session_id")
    return value if isinstance(value, str) and value else None


def _state_status(status: RunStatus | None) -> SessionStatus:
    if status is None:
        return SessionStatus.RUNNING
    return SessionStatus(status.value)
