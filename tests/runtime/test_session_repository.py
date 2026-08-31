"""Contract tests for the shared Runner file-session adapter."""
from __future__ import annotations

import asyncio

from nz_coder.foundation import config
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.state import RunState
from nz_coder.runtime.session.session_repository import FileSessionRepository


def _request(tmp_path, session_id="session-runtime"):
    return RunRequest(
        agent=AgentDefinition(name="coder", instructions="Fix the repository."),
        profile=MAIN_PROFILE,
        messages=[{"role": "user", "content": "initial"}],
        workspace=tmp_path,
        session_id=session_id,
    )


def test_file_repository_round_trips_exact_runner_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / ".nz-coder" / "sessions")
    request = _request(tmp_path)
    state = RunState.from_request(request)
    state.append_message({"role": "assistant", "content": "settled"})
    repository = FileSessionRepository()

    asyncio.run(repository.save(request, state))
    restored = RunState.from_request(_request(tmp_path, request.session_id))
    asyncio.run(repository.load(request, restored))

    assert restored.transcript == state.transcript
    assert restored.metadata["session_payload"]["session_id"] == request.session_id


def test_missing_session_preserves_request_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SESSION_DIR", tmp_path / ".nz-coder" / "sessions")
    request = _request(tmp_path, "session-missing")
    state = RunState.from_request(request)

    asyncio.run(FileSessionRepository().load(request, state))

    assert state.transcript == [{"role": "user", "content": "initial"}]
