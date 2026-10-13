"""Regression for R1's completed-tool/failed-HTTP settlement and lost history."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import threading
import time

from nz_coder.http_service.client import NZCoderClient
from nz_coder.http_service.server import SessionHTTPService
from nz_coder.protocol.session_events import SessionEventBus
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
from nz_coder.runtime.core.profiles import MAIN_PROFILE
from nz_coder.runtime.core.request import AgentDefinition, RunRequest
from nz_coder.runtime.core.result import RunStatus
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.sdk import AgentClient


def offline_transport(monkeypatch):
    path = Path(__file__).parent / "provider/r1_scripted.py"
    spec = importlib.util.spec_from_file_location("r1_scripted", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("API_KEY", "r1-dummy-not-a-secret")
    monkeypatch.setenv("MODEL_PROVIDER", "openai-compatible")
    monkeypatch.setenv("MODEL_ID", "deepseek-v4-flash")
    monkeypatch.setenv("MEMORY_AUTO_EXTRACT", "0")
    monkeypatch.setenv("MEMORY_AUTO_DREAM", "0")
    monkeypatch.setattr(OpenAICompatibleProvider, "create_client", lambda self: object())
    monkeypatch.setattr(OpenAICompatibleProvider, "create_completion", module.ScriptedProvider.create_completion)


def test_sdk_live_event_bus_is_not_copied_into_result_metadata(tmp_path, monkeypatch):
    offline_transport(monkeypatch)
    bus = SessionEventBus(session_id="r1-bus")
    request = RunRequest(agent=AgentDefinition(name="worker", instructions="Handle the offline interaction."),
        profile=MAIN_PROFILE, messages=[{"role":"user", "content":"R1:F05 reuse"}],
        workspace=tmp_path, session_id="r1-bus", stream=True)
    try:
        result = asyncio.run(AgentClient().run(request, event_bus=bus))
        assert result.status is RunStatus.COMPLETED
        assert "session usable" in result.final_text
        json.dumps(result.metadata)
        assert "event_publisher" not in result.metadata
    finally:
        bus.close()


def test_real_http_native_runner_preserves_completed_history_and_reuse(tmp_path, monkeypatch):
    offline_transport(monkeypatch)
    with scoped_workdir(tmp_path):
        service = SessionHTTPService(port=0, restore_saved_sessions=False)
    thread = threading.Thread(target=service.serve_forever)
    thread.start()
    try:
        client = NZCoderClient(service.base_url, service.token, timeout=5)
        sid = client.create_session("acceptEdits")["id"]
        for prompt in ("R1:F01 write a harmless file", "R1:F05 reuse"):
            client.run(sid, prompt)
            deadline = time.monotonic()+10
            while time.monotonic() < deadline:
                state = client.attach_snapshot(sid)
                if state["settled"]:
                    break
                time.sleep(0.02)
            assert state["settled"]
            assert state["session"]["status"] == "completed", state["session"]["last_error"]
        assert (tmp_path/"permission-note.txt").read_text() == "R1 approved write\n"
        messages = client.messages(sid)
        users = [m for m in messages if m.get("role")=="user"]
        assistants = [m for m in messages if m.get("role")=="assistant" and m.get("content")]
        assert len(users) == 2
        assert len(assistants) == 2
        assert "session usable" in assistants[-1]["content"]
    finally:
        service.shutdown()
        thread.join(timeout=5)
